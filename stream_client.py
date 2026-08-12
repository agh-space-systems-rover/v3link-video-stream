#!/usr/bin/env python3

import argparse
import colorsys
import os
import socket
import sys
import termios
import threading
import time
import tty

import gi

gi.require_version("Gst", "1.0")
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gst, Gtk  # noqa: E402

Gst.init(None)

N = 16 
GAIN_MAX = 2.0 

# Below NOISE_LO original saturation, curve gains are ignored entirely so chroma noise in near-grey pixels doesn't get amplified; above
# NOISE_HI the full curve gain applies; linear ramp in between.
NOISE_LO = 0.04
NOISE_HI = 0.30


def _chain(prefix: str, n: int) -> str:
    body = "\n".join(
        f"    if (x < {i + 1}.0) return mix({prefix}{i}, {prefix}{i + 1}, x - {i}.0);"
        for i in range(n - 1)
    )
    return body + f"\n    return mix({prefix}{n - 1}, {prefix}0, x - {n - 1}.0);"


def make_fragment(n: int) -> str:
    """Generate the fragment shader for n control points per curve."""
    decls = " ".join(f"uniform float s{i};" for i in range(n))
    decld = " ".join(f"uniform float v{i};" for i in range(n))
    return f"""
#version 100
#ifdef GL_ES
#ifdef GL_FRAGMENT_PRECISION_HIGH
precision highp float;
#else
precision mediump float;
#endif
#endif
varying vec2 v_texcoord;
uniform sampler2D tex;
{decls}
{decld}

vec3 rgb2hsv(vec3 c) {{
    vec4 K = vec4(0.0, -1.0/3.0, 2.0/3.0, -1.0);
    vec4 p = mix(vec4(c.bg, K.wz), vec4(c.gb, K.xy), step(c.b, c.g));
    vec4 q = mix(vec4(p.xyw, c.r), vec4(c.r, p.yzx), step(p.x, c.r));
    float d = q.x - min(q.w, q.y);
    float e = 1.0e-4;
    return vec3(abs(q.z + (q.w - q.y) / (6.0 * d + e)), d / (q.x + e), q.x);
}}

vec3 hsv2rgb(vec3 c) {{
    vec4 K = vec4(1.0, 2.0/3.0, 1.0/3.0, 3.0);
    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}}

float sat_curve(float h) {{             // piecewise-linear, wraps at 1.0
    float x = h * {n}.0;
{_chain("s", n)}
}}

float val_curve(float h) {{
    float x = h * {n}.0;
{_chain("v", n)}
}}

void main() {{
    vec3 hsv = rgb2hsv(texture2D(tex, v_texcoord).rgb);
    float w = hsv.y;                    // grey pixels have no meaningful hue,
    float gate = smoothstep({NOISE_LO}, {NOISE_HI}, w);  // suppress boost on low-sat noise
    float sgain = mix(1.0, sat_curve(hsv.x), gate);
    hsv.y = clamp(hsv.y * sgain, 0.0, 1.0);
    hsv.z = clamp(hsv.z * mix(1.0, val_curve(hsv.x), w), 0.0, 1.0);
    gl_FragColor = vec4(hsv2rgb(hsv), 1.0);
}}
"""


class CurveEditor(Gtk.DrawingArea):
    """Hue on x, gain on y, N draggable wrapping control points."""

    PAD, R = 16, 7  # padding px, point radius px

    def __init__(self, gains, on_change):
        super().__init__()
        self.gains = gains
        self.defaults = list(gains)   # snapshot for reset
        self.on_change = on_change
        self.drag = None
        self.set_size_request(480, 160)
        self.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        self.connect("draw", self.on_draw)
        self.connect("button-press-event", self.on_press)
        self.connect("button-release-event", lambda *a: setattr(self, "drag", None))
        self.connect("motion-notify-event", self.on_motion)

    def rect(self):
        a = self.get_allocation()
        return self.PAD, self.PAD, a.width - 2 * self.PAD, a.height - 2 * self.PAD

    def to_px(self, i):
        x0, y0, w, h = self.rect()
        return x0 + w * i / N, y0 + h * (1.0 - self.gains[i] / GAIN_MAX)

    # --- drawing ------------------------------------------------------------
    def on_draw(self, _w, cr):
        x0, y0, w, h = self.rect()
        for px in range(int(w)): 
            r, g, b = colorsys.hsv_to_rgb(px / w, 1.0, 1.0)
            cr.set_source_rgba(r, g, b, 0.35)
            cr.rectangle(x0 + px, y0, 1, h)
            cr.fill()

        y_unity = y0 + h * (1.0 - 1.0 / GAIN_MAX) 
        cr.set_source_rgba(0, 0, 0, 0.3)
        cr.set_line_width(1)
        cr.set_dash([4, 4])
        cr.move_to(x0, y_unity)
        cr.line_to(x0 + w, y_unity)
        cr.stroke()
        cr.set_dash([])

        cr.set_source_rgb(0.1, 0.1, 0.1)
        cr.set_line_width(2)
        cr.move_to(*self.to_px(0))
        for i in range(1, N):
            cr.line_to(*self.to_px(i))
        cr.line_to(x0 + w, y0 + h * (1.0 - self.gains[0] / GAIN_MAX))
        cr.stroke()

        for i in range(N): 
            px, py = self.to_px(i)
            cr.arc(px, py, self.R, 0, 6.2832)
            cr.set_source_rgb(1, 1, 1)
            cr.fill_preserve()
            cr.set_source_rgb(0, 0, 0)
            cr.stroke()

    # --- interaction --------------------------------------------------------
    def on_press(self, _w, ev):
        for i in range(N):
            px, py = self.to_px(i)
            if (ev.x - px) ** 2 + (ev.y - py) ** 2 <= (self.R + 4) ** 2:
                self.drag = i
                return

    def on_motion(self, _w, ev):
        if self.drag is None:
            return
        _x0, y0, _w_, h = self.rect()
        self.gains[self.drag] = max(
            0.0, min(GAIN_MAX, (1.0 - (ev.y - y0) / h) * GAIN_MAX)
        )
        self.queue_draw()
        self.on_change()

    def reset(self):
        """Restore the curve this editor was constructed with."""
        self.gains[:] = self.defaults
        self.queue_draw()
        self.on_change()


class ControlWindow(Gtk.Window):
    def __init__(self, sat, val, on_change):
        super().__init__(title="Hue curves")
        self.on_change = on_change
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_border_width(8)
        self.add(box)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.toggle = Gtk.CheckButton(label="Filter enabled")
        self.toggle.set_active(True)
        self.toggle.connect("toggled", lambda *a: on_change())
        bar.pack_start(self.toggle, False, False, 0)
        reset = Gtk.Button(label="Reset to default")
        bar.pack_end(reset, False, False, 0)
        box.pack_start(bar, False, False, 0)

        box.pack_start(Gtk.Label(label="Saturation gain", xalign=0), False, False, 0)
        self.sat_ed = CurveEditor(sat, on_change)
        box.pack_start(self.sat_ed, True, True, 0)

        box.pack_start(Gtk.Label(label="Brightness gain", xalign=0), False, False, 0)
        self.val_ed = CurveEditor(val, on_change)
        box.pack_start(self.val_ed, True, True, 0)

        reset.connect("clicked", lambda *a: (self.sat_ed.reset(), self.val_ed.reset()))
        self.show_all()

    @property
    def enabled(self):
        return self.toggle.get_active()


def push_uniforms(shader, sat, val, enabled):
    if enabled:
        s, v = sat, val
    else:
        s = v = [1.0] * N 
    fields = ",".join(
        [f"s{i}=(float){s[i]:.4f}" for i in range(N)]
        + [f"v{i}=(float){v[i]:.4f}" for i in range(N)]
    )
    shader.set_property("uniforms", Gst.Structure.from_string("uniforms," + fields)[0])


def default_curves():
    """Identity everywhere except green-yellow (hue ~0.15-0.35), which gets a
    saturation/brightness bump."""
    boost = [0.15 <= i / N <= 0.35 for i in range(N)]
    sat = [1.4 if b else 0.1 for b in boost]
    val = [2.0 if b else 1.0 for b in boost]
    return sat, val


class CameraError(Exception):
    ...

class CameraControl:
    def __init__(self, host: str, port: int, timeout: float = 5.0, retry_interval: float = 3.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.retry_interval = retry_interval
        self.sock: socket.socket | None = None
        self._rfile = None

    def connect(self) -> "CameraControl":
        attempt = 0
        while True:
            attempt += 1
            try:
                self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
                self.sock.settimeout(self.timeout)
                self._rfile = self.sock.makefile("rb")
                return self
            except OSError as e:
                print(f"control: connect to {self.host}:{self.port} failed "
                      f"(attempt {attempt}): {e}; "
                      f"retrying in {self.retry_interval:.0f}s", file=sys.stderr)
                time.sleep(self.retry_interval)

    def _command(self, text: str) -> str:
        if self.sock is None or self._rfile is None:
            raise CameraError("not connected")
        try:
            self.sock.sendall(text.encode() + b"\n")
            line = self._rfile.readline()
        except OSError as exc:
            raise CameraError(f"i/o error: {exc}") from exc
        if not line:
            raise CameraError("server closed connection")
        return line.decode(errors="replace").strip()

    def switch(self, channel: int) -> int:
        reply = self._command(f"CAM {channel}")
        parts = reply.split()
        if len(parts) == 2 and parts[0] == "OK" and parts[1].isdigit():
            return int(parts[1])
        raise CameraError(reply)

    def status(self) -> tuple[int, bool]:
        reply = self._command("STATUS")
        parts = reply.split()

        if len(parts) == 3 and parts[0] == "CAM" and parts[1].isdigit():
            return int(parts[1]), parts[2] == "RUNNING"
        raise CameraError(reply)

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.sendall(b"QUIT\n")
            except OSError:
                pass
        if self._rfile is not None:
            try:
                self._rfile.close()
            except OSError:
                pass
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self._rfile = None

def receiver_pipeline(args) -> str:
    return (
        f"udpsrc port={args.video_port} buffer-size=4194304 ! "
        "application/x-rtp,media=video,clock-rate=90000,"
        "encoding-name=H264,payload=96 ! "
        "rtpjitterbuffer latency=20 drop-on-latency=true ! "
        "rtph264depay ! h264parse ! avdec_h264 ! videoconvert ! videoflip method=rotate-180 ! "
        "glupload ! glcolorconvert ! glshader name=shader ! glcolorconvert ! gldownload ! "
        "videoconvert ! xvimagesink name=videosink sync=false"
    )


class Viewer:
    def __init__(self, args):
        self.args = args
        self.loop = GLib.MainLoop()
        self.pipeline = None
        self.shader = None
        self.color_win: ControlWindow | None = None
        self.sat, self.val = default_curves()
        self.cam: CameraControl | None = None
        self.cam_lock = threading.Lock()
        self.current_cam: int | None = None   # last known active camera
        self.target_cam: int | None = None    # switch currently in flight
        self.req_lock = threading.Lock()

    # --- video ---
    def start_video(self) -> None:
        desc = receiver_pipeline(self.args)
        print(f"video: {desc}")
        self.pipeline = Gst.parse_launch(desc)
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_error)
        bus.connect("message::eos", lambda *_: self.loop.quit())
        self._watch_video_keys()
        self._init_color_filter()
        self.pipeline.set_state(Gst.State.PLAYING)

    def _init_color_filter(self) -> None:
        """Wire up the per-hue saturation/brightness shader and its GTK control window."""
        self.shader = self.pipeline.get_by_name("shader")
        self.shader.set_property("fragment", make_fragment(N))
        self.color_win = ControlWindow(
            self.sat, self.val,
            lambda: push_uniforms(self.shader, self.sat, self.val, self.color_win.enabled),
        )
        push_uniforms(self.shader, self.sat, self.val, self.color_win.enabled)

    def _watch_video_keys(self) -> None:
        """Catch key presses made while the video window has focus."""
        sink = self.pipeline.get_by_name("videosink")
        pad = sink and sink.get_static_pad("sink")
        peer = pad and pad.get_peer()
        if peer is None:
            print("note: no key control from the video window (no sink peer pad)",
                  file=sys.stderr)
            return
        peer.add_probe(Gst.PadProbeType.EVENT_UPSTREAM, self._on_nav_event)

    def _on_nav_event(self, _pad, info) -> "Gst.PadProbeReturn":
        event = info.get_event()
        if event and event.type == Gst.EventType.NAVIGATION:
            s = event.get_structure()
            if s and s.get_string("event") == "key-press":
                key = s.get_string("key")
                if key:
                    GLib.idle_add(lambda k=key: (self._handle_key(k),
                                                 GLib.SOURCE_REMOVE)[1])
        return Gst.PadProbeReturn.OK

    def _on_error(self, _bus, msg) -> None:
        err, dbg = msg.parse_error()
        print(f"video error: {err.message} ({dbg})", file=sys.stderr)
        self.loop.quit()

    # --- control ---
    def _control(self) -> CameraControl:
        """Lazy connect / reconnect to the control server."""
        if self.cam is None:
            self.cam = CameraControl(self.args.host, self.args.control_port).connect()
        return self.cam

    def _drop_control(self) -> None:
        if self.cam:
            self.cam.close()
            self.cam = None

    def request_cam(self, channel: int) -> None:
        """Ask for a camera, ignoring key autorepeat."""
        with self.req_lock:
            if channel == self.current_cam or channel == self.target_cam:
                return
            self.target_cam = channel
        print(f"switching to camera {channel}...")
        threading.Thread(target=self._switch, args=(channel,), daemon=True).start()

    def _switch(self, channel: int) -> None:
        with self.cam_lock:
            try:
                ch = self._control().switch(channel)
                self.current_cam = ch
                print(f"active: camera {ch}")
            except CameraError as exc:
                print(f"control error: {exc}", file=sys.stderr)
                self._drop_control()  # reconnect on next command
            finally:
                with self.req_lock:
                    if self.target_cam == channel:
                        self.target_cam = None

    def do_status(self) -> None:
        with self.cam_lock:
            try:
                ch, running = self._control().status()
                self.current_cam = ch
                print(f"camera {ch}, {'running' if running else 'DOWN'}")
            except CameraError as exc:
                print(f"control error: {exc}", file=sys.stderr)
                self._drop_control()

    # --- keys ---
    def _handle_key(self, key: str) -> None:
        if key in ("q", "Q", "Escape", "\x03", "\x04"):
            self.loop.quit()
        elif key in ("1", "2"):
            self.request_cam(int(key))
        elif key in ("s", "S"):
            threading.Thread(target=self.do_status, daemon=True).start()

    def _on_stdin(self, _source, _cond) -> bool:
        try:
            ch = os.read(sys.stdin.fileno(), 1).decode(errors="ignore")
        except OSError:
            return True
        if not ch:          # EOF (stdin closed / not a tty)
            self.loop.quit()
            return False
        self._handle_key(ch)
        return True         # SOURCE_CONTINUE: keep watching stdin

    # --- run ---
    def run(self) -> None:
        self.start_video()
        GLib.io_add_watch(sys.stdin.fileno(), GLib.IO_IN, self._on_stdin)
        print(f"control: {self.args.host}:{self.args.control_port}  "
              "[1 | 2 | s = status | q = quit]")

        threading.Thread(target=self.do_status, daemon=True).start()
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd) if os.isatty(fd) else None
        if saved is not None:
            tty.setcbreak(fd)
        try:
            self.loop.run()
        except KeyboardInterrupt:
            pass
        finally:
            if saved is not None:
                termios.tcsetattr(fd, termios.TCSADRAIN, saved)
            if self.pipeline:
                self.pipeline.set_state(Gst.State.NULL)
            self._drop_control()
            print("stopped")


def main():
    parser = argparse.ArgumentParser(description="Camera Stream Client")
    parser.add_argument("--host", help="Video stream server host")
    parser.add_argument("--video-port", type=int, default=5000)
    parser.add_argument("--control-port", type=int, default=9000)

    args = parser.parse_args()
    Viewer(args).run()

if __name__ == "__main__":
    main()