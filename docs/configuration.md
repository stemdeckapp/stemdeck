# Configuration

<sub>[Back to the README](../README.md)</sub>

| Variable | Default | Purpose |
|---|---|---|
| `STEMDECK_DEMUCS_DEVICE` | auto | Force Torch device: `cuda` (NVIDIA, or AMD with a ROCm build of torch), `mps`, or `cpu`. |
| `STEMDECK_DEMUCS_MODEL` | `htdemucs_6s` | Demucs model name. |
| `STEMDECK_JOBS_DIR` | `./jobs` | Where job directories land. |
| `STEMDECK_DATA_DIR` | (none) | Portable mode root; sets all sub-dirs below to live inside it. |
| `STEMDECK_CACHE_DIR` | `<data>/cache` | Torch model cache directory. |
| `STEMDECK_DOWNLOADS_DIR` | `<data>/downloads` | yt-dlp download scratch space. |
| `STEMDECK_MODELS_DIR` | `<data>/models` | Demucs model weights directory. |
| `STEMDECK_LOGS_DIR` | `<data>/logs` | Log file output directory. |
| `STEMDECK_FFMPEG_DIR` | (none) | Directory containing a bundled ffmpeg binary. |
| `STEMDECK_FFMPEG` | `ffmpeg` | Path to the ffmpeg executable. |
| `STEMDECK_FFPROBE` | `ffprobe` | Path to the ffprobe executable. |
| `STEMDECK_FPCALC` | `fpcalc` | Path to Chromaprint's fpcalc, used for song fingerprinting when FFmpeg has no chromaprint muxer. |
| `STEMDECK_MAX_DURATION_SEC` | `1200` | Reject audio longer than this (seconds). |
| `STEMDECK_JOB_TTL_SECONDS` | `86400` | How long to keep job dirs on disk. |
| `STEMDECK_MAX_PENDING_JOBS` | `3` | Max queued jobs before returning 503. |
| `STEMDECK_STEM_FORMAT` | `mp3` | How new songs' stems are stored: `mp3` or `wav`. Songs already in the library keep their format. A choice saved in Settings > Library wins over this. |
| `STEMDECK_STEM_MP3_KBPS` | `128` | MP3 stem bitrate: `128`, `192` or `256` (constant bitrate). Used only with `mp3`. |
| `STEMDECK_TIMEOUT_FFMPEG` | `300` | ffmpeg subprocess timeout (seconds). |
| `STEMDECK_TIMEOUT_ANALYZE` | `120` | Audio analysis timeout (seconds). |
| `STEMDECK_TIMEOUT_DEMUCS_STALL` | `1800` | Kill Demucs if no output for this many seconds. |
| `STEMDECK_TIMEOUT_MODEL_DOWNLOAD_READ` | `30` | Give up on one try of the separation model's download after this many seconds without data. |
| `STEMDECK_MODEL_DOWNLOAD_ATTEMPTS` | `3` | Tries for the separation model's download, 2 s then 5 s apart, before the Split fails and names the file and the folder to put it in by hand. |
| `STEMDECK_TIMEOUT_MODEL_STALL` | `120` | Stop the separation worker if it is silent this many seconds while it downloads its model. Everything else, separating included, uses the Demucs stall limit above. |
| `STEMDECK_IDLE_UNLOAD_MINUTES` | `10` | Minutes with no separation or analysis running before the separation worker and the beat model are unloaded from memory; `0` keeps them loaded. The next track loads them again and starts a few seconds later. Unloading frees the graphics card's memory, and another program may take it meanwhile, so the next separation on the GPU can fall back to the CPU (its progress line says so). A value saved in Settings > General wins over this. |
| `STEMDECK_SSL_CERT` | (none) | PEM certificate; set with the key below to serve https directly. |
| `STEMDECK_SSL_KEY` | (none) | PEM private key for the certificate above. |
| `STEMDECK_HTTPS_PORT` | (none) | Serve https on this port *in addition* to the main listener. Set by the desktop app; see below. |

`run.sh` also reads: `HOST` (default `0.0.0.0`, every network interface), `PORT` (default `8000`), `RELOAD=1` (enable uvicorn auto-reload for development), `FOREGROUND=1` (run in foreground instead of backgrounding).

### Who can reach StemDeck

While **Make StemDeck available on your network** is off, the desktop app's
backend listens on `127.0.0.1` only, and there is no https listener at all.
Other devices cannot connect to it, and the operating system has no reason to
ask about a firewall rule. Turning the setting on restarts the backend on
every interface, with the https listener beside it, which takes a few seconds;
turning it off restarts it back onto `127.0.0.1`. If a separation is running,
StemDeck asks first, because the restart stops it: the song waits in the
queue, paused, and starts again from the beginning when you press Start. See
[the https section](#serving-other-devices-why-https-is-not-optional) for what a
phone needs once it is on.

StemDeck also checks every request itself. While the setting is off it answers
only the machine it runs on, through loopback (`127.0.0.1`, `::1`) or one of
the machine's own addresses, and a request from any other device gets a 403
that says StemDeck is not available on the network, before it reaches any page
or API. On the desktop this covers the moment between saving the choice and
the restart that applies it.

Docker, Unraid and `run.sh` start with network access on, because a headless
server exists to be reached from other devices. They listen on every interface
(`run.sh`: `HOST`, default `0.0.0.0`) whatever the setting says, so for them
the 403 above is the whole control. To start one with it off, set
`STEMDECK_ALLOW_NETWORK=0` (any value other than `1` means off). In server
mode the switch in Settings is shown on and read-only, so the variable is the
way to change it. If you want a server's port unreachable from other devices
at the network level, block it in your firewall.

### Serving other devices: why https is not optional

Transpose is built on `AudioWorklet`, and browsers grant that only to a
**secure context**. `https://` and `localhost` qualify. A plain
`http://192.168.1.20:8000` does not, so a phone reaching StemDeck over plain
http gets working playback and a key control that cannot do anything. There is
no fallback worth shipping: driving the same DSP from a `ScriptProcessorNode`
measured around 5% of the audio missing, because that node type drops buffers
on its own at every size.

So a server that other devices will use terminates TLS, one of three ways:

1. **A reverse proxy** (SWAG, Nginx Proxy Manager, Traefik, Caddy). The usual
   self-hosted shape, and the best one if you already run it. StemDeck reads
   `X-Forwarded-Proto` and the RFC 7239 `Forwarded` header, so an https browser
   over a plain-http upstream hop is recognised as secure and served normally.
2. **StemDeck itself**, by pointing `STEMDECK_SSL_CERT` and `STEMDECK_SSL_KEY`
   at a certificate and key. uvicorn serves them directly; no extra package is
   installed for this.
3. **A private overlay network** such as Tailscale, whose addresses are already
   https.

Reaching a plaintext non-local origin with none of those in place is refused
with a 403 that explains this, rather than served as an app that is quietly
half-broken. Loopback is always served, so turning this on can never lock the
host out of its own server.

### The desktop app runs two listeners

The desktop app does the same thing without being configured, because it has
two audiences that need opposite things.

- **Plain http**, which its own window reaches at `127.0.0.1`. Loopback is already a
  secure context, so nothing is lost, and it is the only scheme that works: a
  self-signed certificate would raise a warning page the app window has no way
  to click through.
- **https on the LAN**, port 8443 by default, for phones and other computers.
  This is the address Settings shows and the QR code points at. It runs only
  while network access is on.

Both listeners serve the same process, so there is one library, one queue and
one Demucs worker either way.

The certificate is generated on your own machine the first time the app
starts with network access on, and lives in `<data>/certs/` beside `jobs/` and
`settings.json`. Nothing is shipped in the download: a certificate in the
release would publish its private key to everyone who downloaded it, which is
worse than plain http because it looks secure. It is regenerated automatically
when your machine's addresses change or the certificate is close to expiring.

Because it is signed by nobody, **your phone will show a "your connection is
not private" warning the first time**. Tap Advanced, then Continue. Once per
device, per computer. Settings says so, in red, next to the toggle.

## Environment variables for the desktop app

These are for development and testing. Release builds only recognize the variables marked "release".

| Variable | Platform | Scope | Description |
|---|---|---|---|
| `STEMDECK_DATA_DIR` | all | release | Override the user data directory (default: platform-standard location) |
| `STEMDECK_ROOT` | all | release | Override the app root directory (default: derived from executable path) |
| `STEMDECK_PYTHON` | all | **debug builds only** | Override the Python executable path |
| `STEMDECK_FFMPEG_URL` | Windows, macOS | release | Override the FFmpeg download URL |
| `STEMDECK_FFPROBE_URL` | macOS | release | Override the ffprobe download URL |
| `STEMDECK_WEBKIT_DMABUF` | Linux | release | WebKitGTK's DMA-BUF renderer: `auto` (default), `on` or `off`. See [Troubleshooting](troubleshooting.md#linux-desktop-app) |
