# Troubleshooting

<sub>[Back to the README](../README.md)</sub>

**`ffmpeg: command not found`:** install ffmpeg and restart with `./run.sh restart`.

**`WARNING: [youtube] No supported JavaScript runtime`:** install deno (`brew install deno` on macOS) and restart. Downloads still work without it but may pick suboptimal formats.

**First separation is very slow:** Demucs downloads `htdemucs_6s` weights (~170 MB) on first run; cached afterwards.

**Demucs runs on CPU only:** check the startup log for `device=mps` or `device=cuda`. If you see `cpu`, your torch install may be CPU-only.

**Transpose is greyed out on another machine:** the pitch stage is built on `AudioWorklet`, which browsers only expose on a *secure context*. `https://` and `localhost` count. A plain `http://192.168.x.x` does not, so a client opening StemDeck over the network is never given the API and transpose cannot work there. Changing the speed still works on such a client, but it resamples instead, so the key moves with it.

Three ways to get a secure context, in order of least effort:

- **Tunnel to localhost.** On the client: `ssh -N -L 8000:localhost:8000 user@host`, then open `http://localhost:8000`. The origin is now localhost, so everything works, including transpose.
- **Tailscale Serve.** `tailscale serve 8000` on the host publishes StemDeck on your tailnet over real HTTPS with a genuine certificate, no warnings and nothing to install on the client beyond Tailscale itself. Note the plain Tailscale IP (`100.x.y.z`) is *not* a secure context; it has to go through `serve`.
- **Any HTTPS reverse proxy** in front of StemDeck: Caddy, nginx, or a tunnel like Cloudflare Tunnel.

**Page reloaded mid-job:** the job keeps running server-side. Wait for it to finish, then resubmit.

**`./run.sh: Permission denied`:** run `chmod +x run.sh`.

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
