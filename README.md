# v3link-video-stream

Low-latency H.264 video streaming from a Raspberry Pi with an **Arducam V3Link** (FPD-Link III) camera extension to an operator station, with remote switching between two cameras over a simple TCP control channel.

## Overview

```
 Raspberry Pi (rover)                                   Operator PC
┌───────────────────────────────────────┐            ┌──────────────────────────────┐
│ V3Link deserializer.                  │            │                              │
│        │ channel select               │            │                              │
│        ▼                              │  RTP/H.264 │                              │
│ rpicam-vid ─► pump ─► gst rtph264pay ─┼──── UDP ───┼─► rtpjitterbuffer ─► decode  │
│                 ▲                     │   :5000    │        ─► xvimagesink        │
│              watchdog                 │            │                              │
│                                       │    TCP     │                              │
│ control server  ◄─────────────────────┼─── :9000 ──┼── keyboard: 1 / 2 / s / q    │
└───────────────────────────────────────┘            └──────────────────────────────┘
```

- **`stream_server.py`** (Pi) - selects the active V3Link channel over I²C, encodes with `rpicam-vid` (H.264 baseline), and sends RTP over UDP via GStreamer. Accepts camera-switch commands on a TCP control port.
- **`stream_client.py`** (operator) — receives and decodes the RTP stream with GStreamer, displays it, and sends control commands from the keyboard.
- **`run_server.sh` / `run_client.sh`** — thin wrappers that pass defaults and environment overrides to the Python scripts.

## Requirements

### Raspberry Pi (server)

- Raspberry Pi OS with `libcamera` / `rpicam-apps` (`rpicam-vid`)
- Arducam V3Link kit with an IMX219-based camera
- `i2c-tools`
- GStreamer 1.x: `gstreamer1.0-tools`, `gstreamer1.0-plugins-good`, `gstreamer1.0-plugins-bad`
- Python ≥ 3.10

```bash
sudo apt install rpicam-apps i2c-tools gstreamer1.0-tools \
    gstreamer1.0-plugins-good gstreamer1.0-plugins-bad
```

### Operator station (client)

- Linux with an X11 session (output uses `xvimagesink`)
- GStreamer 1.x with Python bindings and the libav decoder
- Python ≥ 3.10

```bash
sudo apt install python3-gi gir1.2-gstreamer-1.0 gstreamer1.0-tools \
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad gstreamer1.0-libav gstreamer1.0-x
```

## Usage

### 1. Start the server on the Pi

The server streams to a fixed receiver address, so pass the **operator's** IP:

```bash
sudo ./run_server.sh <operator-ip>
```

Root is required for raw I²C access to the deserializer.

Environment overrides:

| Variable       | Default   | Description          |
|----------------|-----------|----------------------|
| `PORT`         | `5000`    | Video UDP port       |
| `CONTROL_PORT` | `9000`    | Control TCP port     |
| `WIDTH`        | `1024`    | Output width         |
| `HEIGHT`       | `768`     | Output height        |
| `FPS`          | `60`      | Frame rate           |
| `BITRATE`      | `4000000` | H.264 bitrate (bps)  |

Example:

```bash
sudo WIDTH=1280 HEIGHT=720 FPS=30 BITRATE=3000000 ./run_server.sh 192.168.1.50
```

### 2. Start the client on the operator station

Pass the **Pi's** IP (used for the control connection):

```bash
./run_client.sh <pi-ip>
```

| Variable       | Default | Description      |
|----------------|---------|------------------|
| `VIDEO_PORT`   | `5000`  | Video UDP port   |
| `CONTROL_PORT` | `9000`  | Control TCP port |

### Keyboard controls

Keys work both in the terminal and with the video window focused.

| Key          | Action                    |
|--------------|---------------------------|
| `1` / `2`    | Switch to camera 1 / 2    |
| `s`          | Query server status       |
| `q` / `Esc`  | Quit                      |

The displayed image is rotated 180° (`videoflip method=rotate-180`) to match the camera mounting.

## Control protocol

Line-based ASCII over TCP (default port `9000`), one command per line, case-insensitive. Usable directly with `nc`:

```bash
nc <pi-ip> 9000
```

| Command  | Reply                                | Notes                              |
|----------|--------------------------------------|------------------------------------|
| `CAM 1`  | `OK 1` or `ERR: <reason>`            | Select camera 1 (restarts stream)  |
| `CAM 2`  | `OK 2` or `ERR: <reason>`            | Select camera 2 (restarts stream)  |
| `STATUS` | `CAM <n> RUNNING` / `CAM <n> STOPPED`| Active channel and pipeline state  |
| `QUIT`   | `BYE`                                | Closes the connection              |

Switching cameras stops the encoder, writes the channel-select command to the V3Link deserializer over I²C, and restarts the pipeline. If the switch fails, the previous channel is kept.
