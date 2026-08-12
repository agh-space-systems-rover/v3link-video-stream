#!/usr/bin/env python3

import os
import threading
import time
import sys
import argparse
import socket
import subprocess
import signal

I2C_BUS = "10"
DES_ADDR = "0x0c"

I2C_TIMEOUT = 5
KILL_TIMEOUT = 3
STALL_TIMEOUT = 2
STARTUP_GRACE = 5
FLOW_LOG_SECS = 10

def log(msg: str):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


class StreamManager:
    def __init__(self, args):
        self.args = args
        self.channel = 1
        self.cam = None
        self.gst = None
        self.lock = threading.RLock()
        self.stopping = False
        self.generation = 0
        self.last_data = 0
        self.bytes_total = 0
        self.started_at = 0
        self.stall_timeout = STALL_TIMEOUT # no data for STALL_TIMEOUT seconds is considered a stall
        self.stall_restarts = 0

    # low-level functions

    def _select_channel(self, channel: int):
        try:
            subprocess.run(
                ["i2ctransfer", "-f", "-y", I2C_BUS,
                 f"w3@{DES_ADDR}", "0xff", "0x55", f"0x{channel:02x}"],
                check=True, timeout=I2C_TIMEOUT, capture_output=True
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("I2C command timed out")
        except subprocess.CalledProcessError as e:
            detail = e.stderr.decode(errors="replace").strip() or f"exit {e.returncode}"
            raise RuntimeError(f"I2C command failed: {detail}")
        log(f"i2c: selected channel {channel}")
        time.sleep(0.3)

    def _pump(self, cam, gst, generation: int):
        """Copy rpicam-vid output to gstreamer input by hand so watchdog can see if the camera is still alive."""
        src, dst = cam.stdout.fileno(), gst.stdin.fileno()
        try:
            while True:
                data = os.read(src, 65536)
                if not data:
                    log(f"pump[{generation}]: rpicam-vid closed stdout (EOF)")
                    break
                self.last_data = time.monotonic()
                self.bytes_total += len(data)
                while data:
                    data = data[os.write(dst, data):]
        except (BrokenPipeError, ValueError, OSError) as e:
            log(f"pump[{generation}]: stopped: {e}")
        finally:
            try:
                gst.stdin.close()
            except Exception:
                pass

    def _log_stderr(self, cam, generation: int) -> None:
        for raw in iter(cam.stderr.readline, b""):
            line = raw.decode(errors="replace").rstrip()
            if line:
                log(f"rpicam[{generation}]: {line}")

    def _start(self):
        a = self.args
        self.generation += 1
        generation = self.generation
        rpicam_cmd = [
            "rpicam-vid", "-t", "0",
            "--mode", "1640:1232:8",
            "--width", str(a.width), "--height", str(a.height),
            "--framerate", str(a.fps),
            "--codec", "h264", "--profile", "baseline",
            "--intra", str(a.fps),
            "--inline", "--flush", "--denoise", "cdn_off",
            "--bitrate", str(a.bitrate),
            "-n", "-o", "-",
        ]
        gst_cmd = [
            "gst-launch-1.0", "-q",
            "fdsrc", "fd=0", "!", "h264parse", "!",
            "rtph264pay", "config-interval=1", "pt=96",
            "aggregate-mode=zero-latency", "!",
            "udpsink", f"host={a.host}", f"port={a.port}",
            "sync=false", "async=false",
        ]

        self.cam = subprocess.Popen(rpicam_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)

        try:
            self.gst = subprocess.Popen(gst_cmd, stdin=subprocess.PIPE, start_new_session=True)
        except Exception as e:
            self._kill(self.cam, "rpicam-vid")
            self.cam = None
            raise

        self.started_at = self.last_data = time.monotonic()
        self.bytes_total = 0
        for target in (
            lambda: self._pump(self.cam, self.gst, generation),
            lambda: self._log_stderr(self.cam, generation),
        ):
            threading.Thread(target=target, daemon=True).start()

        log(f"stream[{generation}]: started on camera {self.channel} "
            f"(rpicam pid {self.cam.pid}, gst pid {self.gst.pid})")

    def _kill(self, proc, name: str, graceful: bool = True):
        signals = (signal.SIGINT, signal.SIGTERM, signal.SIGKILL) if graceful \
            else (signal.SIGKILL,)
        for sig in signals:
            if proc.poll() is not None:
                return
            try:
                os.killpg(os.getpgid(proc.pid), sig)
            except (ProcessLookupError, PermissionError):
                return
            deadline = time.monotonic() + KILL_TIMEOUT
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    return
                time.sleep(0.05)
            log(f"{name} (pid {proc.pid}) ignored {sig.name}, escalating")

    def _stop(self, graceful: bool = True) -> None:
        for proc, name in ((self.gst, "gst-launch"), (self.cam, "rpicam-vid")):
            if proc is not None:
                self._kill(proc, name, graceful)
        self.cam = self.gst = None

    
    def _restart(self, graceful: bool = True) -> None:
        self._stop(graceful)
        self._select_channel(self.channel)
        self._start()

    # public interface

    def start(self):
        with self.lock:
            self._select_channel(self.channel)
            self._start()

    def shutdown(self):
        with self.lock:
            self.stopping = True
            self._stop()

    def running(self) -> bool:
        cam, gst = self.cam, self.gst
        return (cam is not None and cam.poll() is None
                and gst is not None and gst.poll() is None)

    def switch(self, channel: int) -> str:
        if channel not in (1, 2):
            return "ERR: invalid channel"
        with self.lock:
            if channel == self.channel and self.running():
                return f"OK {channel}"
            try:
                previous, self.channel = self.channel, channel
                self._restart()
                return f"OK {channel}"
            except Exception as e:
                self.channel = previous
                log(f"switch: failed to switch to channel {channel}: {e}")
                return f"ERR: {e}"

    def status(self) -> str:
        with self.lock:
            state = "RUNNING" if self.running() else "STOPPED"
            return f"CAM {self.channel} {state}"

    def watchdog(self):
        """Restart the stream when it dies or when it silently stops producing data."""
        next_flow_log = time.monotonic() + FLOW_LOG_SECS
        last_bytes = 0
        while not self.stopping:
            time.sleep(1)
            with self.lock:
                if self.stopping:
                    break
                if not self.running():
                    log(f"watchdog: stream is not running, restarting")
                    try:
                        self._restart()
                    except Exception as e:
                        log(f"watchdog: failed to restart stream: {e}")
                    continue

                idle = time.monotonic() - self.last_data
                warming_up = time.monotonic() - self.started_at < STARTUP_GRACE
                if idle > self.stall_timeout and not warming_up:
                    self.stall_restarts += 1
                    log(f"watchdog: STALLED - no bytes from rpicam-vid for "
                        f"{idle:.1f}s (processes still alive, {self.bytes_total} "
                        f"bytes this run); restarting "
                        f"[stall restart #{self.stall_restarts}]")
                    try:
                        self._restart(graceful=False)
                    except Exception as e:
                        log(f"watchdog: failed to restart stream: {e}")
                    continue

                now = time.monotonic()
                if now >= next_flow_log:
                    rate = (self.bytes_total - last_bytes) / FLOW_LOG_SECS
                    log(f"flow: {rate / 1000:.0f} kB/s, {self.bytes_total} bytes total")
                    last_bytes = self.bytes_total
                    next_flow_log = now + FLOW_LOG_SECS

def sweep_orphans():
    """Kill leftover rpicam-vid and gst-launch-1.0 processes from previous runs."""
    for name in ("rpicam-vid", "gst-launch-1.0"):
        try:
            out = subprocess.run(["pgrep", "-x", name], capture_output=True, timeout=5).stdout.decode().split()
        except (subprocess.SubprocessError, FileNotFoundError):
            continue
        for pid in (int(p) for p in out if p.isdigit()):
            if pid == os.getpid():
                continue
            log(f"sweep: killing stale {name} (pid {pid}) from a previous run")
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    break
                for _ in range(20):
                    time.sleep(0.05)
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                else:
                    continue
                break


def handle_client(conn: socket.socket, addr, mgr: StreamManager) -> None:
    log(f"control: client {addr[0]}:{addr[1]} connected")
    with conn:
        buf = b""
        while True:
            try:
                data = conn.recv(1024)
            except ConnectionError:
                break
            if not data:
                break
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                reply = process_command(line.decode("utf-8").strip(), mgr)
                if reply is None:
                    conn.sendall(b"BYE\n")
                    return
                conn.sendall(reply.encode() + b"\n")
    log(f"control: client {addr[0]}:{addr[1]} disconnected")


def process_command(cmd: str, mgr: StreamManager) -> str | None:
    parts = cmd.upper().split()
    if not parts:
        return "ERR: empty command"
    if parts[0] == "CAM" and len(parts) == 2 and parts[1].isdigit():
        return mgr.switch(int(parts[1]))
    elif parts[0] == "STATUS":
        return mgr.status()
    elif parts[0] == "QUIT":
        return None
    return "ERR: unknown command (CAM 1|CAM 2|STATUS|QUIT)"

def main():
    parser = argparse.ArgumentParser(description="V3Link RTP Stream Server + TCP control")
    parser.add_argument("--host", required=True, help="video receiver IP address")
    parser.add_argument("--port", type=int, default=5000, help="video receiver port")
    parser.add_argument("--control-port", type=int, default=9000, help="control port for TCP commands")
    parser.add_argument("--width", type=int, default=1024)
    parser.add_argument("--height", type=int, default=768)
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--bitrate", type=int, default=4_000_000)
    args = parser.parse_args()


    mgr = StreamManager(args)
    mgr.start()    
    threading.Thread(target=mgr.watchdog, daemon=True).start()


    # Initialize the control server
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", args.control_port))
    server.listen(5)
    server.settimeout(0.5)

    log(f"video -> {args.host}:{args.port}")
    log(f"control -> 0.0.0.0:{args.control_port}")

    quit_evt = threading.Event()

    def _on_signal(signum, _frame):
        if not quit_evt.is_set():
            log(f"got {signal.Signals(signum).name}, shutting down "
                "(children will be killed; please wait)")
            quit_evt.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    try:
        while not quit_evt.is_set():
            try:
                conn, addr = server.accept()
            except socket.timeout:
                continue
            threading.Thread(target=handle_client, args=(conn, addr, mgr), daemon=True).start()
    finally:
        server.close()
        mgr.shutdown()
        log("stopped")

if __name__ == "__main__":
    main()