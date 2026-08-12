#!/usr/bin/env python3

import argparse
import os
import socket
import sys
import termios
import threading
import time
import tty

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

Gst.init(None)


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
        "xvimagesink name=videosink sync=false"
    )


class Viewer:
    def __init__(self, args):
        self.args = args
        self.loop = GLib.MainLoop()
        self.pipeline = None
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
        self.pipeline.set_state(Gst.State.PLAYING)

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