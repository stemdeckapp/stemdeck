# Troubleshooting

<sub>[Back to the README](../README.md)</sub>

**`ffmpeg: command not found`:** install ffmpeg and restart with `./run.sh restart`.

**`WARNING: [youtube] No supported JavaScript runtime`:** install deno (`brew install deno` on macOS) and restart. Downloads still work without it but may pick suboptimal formats.

**First separation is very slow:** Demucs downloads `htdemucs_6s` weights (~170 MB) on first run; cached afterwards. The stage reads "Downloading the separation model" while it does.

**"Could not download the separation model":** the network blocked or stalled the connection to both the StemDeck mirror (`files.stemdeck.app`, tried first for `htdemucs_6s`, see `STEMDECK_MODEL_MIRROR` in [Configuration](configuration.md)) and `dl.fbaipublicfiles.com`. Try another network, a VPN, or a proxy (`HTTPS_PROXY` is honoured). Or download the file the error names (`https://dl.fbaipublicfiles.com/demucs/hybrid_transformer/5c90dfd2-34c22ccb.th` for `htdemucs_6s`, also at `https://files.stemdeck.app/demucs/hybrid_transformer/5c90dfd2-34c22ccb.th`) in a browser and put it, keeping its file name, in the folder the error names. The error writes StemDeck's models folder as `<models>`, so a bug report never carries your user name: it is `%LOCALAPPDATA%\StemDeck\models` on Windows (`data\models` beside `StemDeck.exe` for the portable zip), `~/Library/Application Support/StemDeck/models` on macOS and `~/.local/share/stemdeck/models` on Linux. The Docker image uses `/cache/torch/hub/checkpoints`. A file already in that folder is used as it is, without a download. The timeouts are in [Configuration](configuration.md).

**Demucs runs on CPU only:** check the startup log for `device=mps` or `device=cuda`. If you see `cpu`, your torch install may be CPU-only. On an AMD GPU, `device=cuda gpu=rocm` means the ROCm build is in use.

**AMD GPU not used (Linux):**

- `cannot open /dev/kfd` in `logs/setup.log`, or `No HIP GPUs are available`: your user cannot reach the GPU. Run `sudo usermod -aG render,video $USER`, then log out and in.
- `no ROCm build StemDeck installs has kernels for gfxNNNN`: the official ROCm build of torch does not support that card (the RX 6600/6700 family, gfx1031/gfx1032, for example). StemDeck separates on the CPU there.
- `KFD GPU` lines in `logs/setup.log` list every AMD GPU found, and `AMD GPU chosen` the one StemDeck uses: with an integrated GPU as well, the card ROCm supports is chosen.
- The first separation on a new AMD card pauses for a while before it moves: MIOpen, ROCm's cuDNN, is compiling and caching kernels for that card. Later separations start straight away.

**Transpose is greyed out on another machine:** the pitch stage is built on `AudioWorklet`, which browsers only expose on a *secure context*. `https://` and `localhost` count. A plain `http://192.168.x.x` does not, so a client opening StemDeck over the network is never given the API and transpose cannot work there. Changing the speed still works on such a client, but it resamples instead, so the key moves with it.

Three ways to get a secure context, in order of least effort:

- **Tunnel to localhost.** On the client: `ssh -N -L 8000:localhost:8000 user@host`, then open `http://localhost:8000`. The origin is now localhost, so everything works, including transpose.
- **Tailscale Serve.** `tailscale serve 8000` on the host publishes StemDeck on your tailnet over real HTTPS with a genuine certificate, no warnings and nothing to install on the client beyond Tailscale itself. Note the plain Tailscale IP (`100.x.y.z`) is *not* a secure context; it has to go through `serve`.
- **Any HTTPS reverse proxy** in front of StemDeck: Caddy, nginx, or a tunnel like Cloudflare Tunnel.

**Page reloaded mid-job:** the job keeps running server-side. Wait for it to finish, then resubmit.

**`./run.sh: Permission denied`:** run `chmod +x run.sh`.

## Linux desktop app

**StemDeck closes as soon as it opens on Wayland:** the terminal shows `Error 71 (Protocol error) dispatching to Wayland display`, often after `Missing acquire timeline`. This is WebKitGTK's DMA-BUF renderer, seen mostly with the NVIDIA driver. StemDeck turns that renderer off by itself in two cases: a Wayland session with the NVIDIA driver loaded, and after two launches in a row that closed before their window finished loading with the renderer on. One early close is not enough, because a kill, a logout or running out of memory looks the same. The second switch is saved, and it is not for good: after 10 launches that load cleanly, or when WebKitGTK or the NVIDIA driver changes, StemDeck tries the renderer again, and one more early close turns it back off. Each retry that ends that way doubles the wait: 20, then 40, then at most 80 clean launches. Quitting normally within the first 5 seconds is not counted as an early close, and it does not reset the count either. A logout or a `kill` (SIGTERM) in those seconds does count as an early close; the two-in-a-row rule is there so that one of them changes nothing. If StemDeck still closes, start it with the renderer off:

```
WEBKIT_DISABLE_DMABUF_RENDERER=1 ./StemDeck
```

To control it yourself, set `STEMDECK_WEBKIT_DMABUF`:

- `auto` (the default): the two automatic cases above.
- `off`: the renderer is always off.
- `on`: the renderer stays on, nothing is switched for you, and a saved switch is forgotten.

A `WEBKIT_DISABLE_DMABUF_RENDERER` you set yourself always wins over all of this. Each launch writes the choice it made to `logs/setup.log` in the data directory.

## Layout on disk

```
jobs/<job_id>/
└── stems/
    ├── vocals.wav      # the 6 Demucs stems (always present)
    ├── drums.wav
    ├── bass.wav
    ├── guitar.wav
    ├── piano.wav
    ├── other.wav
    ├── original.wav    # sum of un-selected stems (subset only)
    └── mix.wav         # ffmpeg amix of selected stems (subset only)
```

The library itself is one file, `jobs/registry.json`, so it survives a restart. A song's folder is only removed when you empty the Trash, or by the opt-in **Automatically delete finished tracks** setting, which is off unless you turn it on.
