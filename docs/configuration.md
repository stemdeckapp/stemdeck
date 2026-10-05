# Configuration

<sub>[Back to the README](../README.md)</sub>

| Variable | Default | Purpose |
|---|---|---|
| `STEMDECK_DEMUCS_DEVICE` | auto | Force Torch device: `cuda`, `mps`, or `cpu`. |
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
| `STEMDECK_TIMEOUT_FFMPEG` | `300` | ffmpeg subprocess timeout (seconds). |
| `STEMDECK_TIMEOUT_ANALYZE` | `120` | Audio analysis timeout (seconds). |
| `STEMDECK_TIMEOUT_DEMUCS_STALL` | `1800` | Kill Demucs if no output for this many seconds. |
| `STEMDECK_SSL_CERT` | (none) | PEM certificate; set with the key below to serve https directly. |
| `STEMDECK_SSL_KEY` | (none) | PEM private key for the certificate above. |
| `STEMDECK_HTTPS_PORT` | (none) | Serve https on this port *in addition* to the main listener. Set by the desktop app; see below. |

`run.sh` also reads: `HOST` (default `127.0.0.1`), `PORT` (default `8765`), `RELOAD=1` (enable uvicorn auto-reload for development), `FOREGROUND=1` (run in foreground instead of backgrounding).

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

- **Plain http on `127.0.0.1`** for its own window. Loopback is already a
  secure context, so nothing is lost, and it is the only scheme that works: a
  self-signed certificate would raise a warning page the app window has no way
  to click through.
- **https on the LAN**, port 8443 by default, for phones and other computers.
  This is the address Settings shows and the QR code points at.

Both listeners serve the same process, so there is one library, one queue and
one Demucs worker either way.

The certificate is generated on your own machine the first time you enable
network access, and lives in `<data>/certs/` beside `jobs/` and
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
