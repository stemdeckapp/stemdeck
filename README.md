<div align="center">

<img src="imgs/stemdeck-svg-assets/stemdeck-logo-stacked.svg" alt="StemDeck" width="515" />


<div align="center">
  <a href="https://github.com/stemdeckapp/stemdeck/actions/workflows/ci.yml"><img src="https://github.com/stemdeckapp/stemdeck/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/stemdeckapp/stemdeck/stargazers"><img src="https://img.shields.io/github/stars/stemdeckapp/stemdeck?style=flat-square" alt="GitHub Stars"></a>
  <a href="https://github.com/stemdeckapp/stemdeck/releases"><img src="https://img.shields.io/github/downloads/stemdeckapp/stemdeck/total?style=flat-square&color=52c65f" alt="Total Downloads"></a>
  <a href="https://github.com/stemdeckapp/stemdeck/releases/latest"><img src="https://img.shields.io/github/v/release/stemdeckapp/stemdeck?style=flat-square" alt="Latest Release"></a>
  <a href="https://github.com/stemdeckapp/stemdeck/blob/main/LICENSE"><img src="https://img.shields.io/github/license/stemdeckapp/stemdeck?style=flat-square" alt="License"></a>
</div>

<br>

<p align="center"><sub>JOIN THE COMMUNITY</sub></p>
<div align="center">
  <a href="https://github.com/stemdeckapp/stemdeck"><img src="https://img.shields.io/badge/GitHub-stemdeckapp-181717?style=flat-square&logo=github&logoColor=white" alt="GitHub"></a>
  <a href="https://discord.gg/YhCKsjhcwB"><img src="https://img.shields.io/badge/Discord-Join-5865F2?style=flat-square&logo=discord&logoColor=white" alt="Discord"></a>
  <a href="https://www.reddit.com/r/StemDeckApp/"><img src="https://img.shields.io/badge/Reddit-r%2FStemDeckApp-FF4500?style=flat-square&logo=reddit&logoColor=white" alt="Reddit"></a>
  <a href="https://www.instagram.com/stemdeck"><img src="https://img.shields.io/badge/Instagram-stemdeck-E4405F?style=flat-square&logo=instagram&logoColor=white" alt="Instagram"></a>
  <a href="https://x.com/StemDeckApp"><img src="https://img.shields.io/badge/X-StemDeckApp-000000?style=flat-square&logo=x&logoColor=white" alt="X"></a>
  <a href="https://stemdeck.app"><img src="https://img.shields.io/badge/Website-stemdeck.app-000000?style=flat-square&logo=safari&logoColor=white" alt="Website"></a>
</div>

</div>

<br>

<p align="center">
  <b>Drop in a song. Get every instrument on its own track. Practice with them.</b><br>
  <sub>Runs on your own computer: no account, no subscription, and your audio is not uploaded.</sub>
</p>

<p align="center">
  <a href="#download"><img src="https://img.shields.io/badge/Download-free-f4b740?style=for-the-badge" alt="Download, free"></a>
  <a href="https://github.com/stemdeckapp/stemdeck/releases/latest"><img src="https://img.shields.io/github/v/release/stemdeckapp/stemdeck?style=for-the-badge&label=latest&color=2e3942" alt="Latest release"></a>
</p>

<p align="center">
  <a href="#download"><b>Download</b></a> &nbsp;·&nbsp;
  <a href="#what-you-can-do">What you can do</a> &nbsp;·&nbsp;
  <a href="#honest-comparison">Honest comparison</a> &nbsp;·&nbsp;
  <a href="#we-recommend">We Recommend</a> &nbsp;·&nbsp;
  <a href="#for-developers">For developers</a>
</p>

<p align="center">
  <img src="imgs/screenshot/stemdeck.png" alt="The StemDeck studio: a song split into vocals, drums, bass, guitar, piano and other, each on its own lane" width="100%">
</p>

StemDeck splits a song into up to six stems, vocals, drums, bass, guitar, piano and other, and opens them in a studio where you can mute, solo, slow down, loop, change the key and play along with a click. Drop in an audio file or paste a YouTube or SoundCloud link. Separation runs on your own machine, and StemDeck does not upload your audio.

> **A separation tool, not a downloader.** StemDeck is for processing audio you have the right to use. What you import, and the stems made from it, are saved in your library folder on the machine that runs StemDeck; StemDeck does not upload or share them. See the [Disclaimer](#disclaimer).


<a name="download"></a>

## <img src="imgs/readme/download.svg" width="26" align="top" alt=""> Download

Each link downloads the latest release. [Release notes](https://github.com/stemdeckapp/stemdeck/releases/latest) · [Every version](https://github.com/stemdeckapp/stemdeck/releases)

| Platform | NVIDIA or Apple Silicon | Everything else |
|---|---|---|
| <img src="imgs/readme/os-windows.svg" width="16" align="top" alt=""> &nbsp;**Windows** | [NVIDIA GPU](https://github.com/stemdeckapp/stemdeck/releases/latest/download/StemDeck-Windows-x64.NVIDIA.zip) | [No NVIDIA GPU](https://github.com/stemdeckapp/stemdeck/releases/latest/download/StemDeck-Windows-x64.zip) |
| <img src="imgs/readme/os-apple.svg" width="16" align="top" alt=""> &nbsp;**macOS** | [Apple Silicon (M1 and later)](https://github.com/stemdeckapp/stemdeck/releases/latest/download/StemDeck-macOS-arm64.dmg) | [Intel Mac](https://github.com/stemdeckapp/stemdeck/releases/latest/download/StemDeck-macOS-x64.dmg) |
| <img src="imgs/readme/os-linux.svg" width="16" align="top" alt=""> &nbsp;**Linux** | [NVIDIA GPU](https://github.com/stemdeckapp/stemdeck/releases/latest/download/StemDeck-Linux-x64.NVIDIA.tar.gz) | [No NVIDIA GPU](https://github.com/stemdeckapp/stemdeck/releases/latest/download/StemDeck-Linux-x64.tar.gz) |

<img src="imgs/readme/os-docker.svg" width="16" align="top" alt=""> &nbsp;**Docker / Unraid:** [`ghcr.io/stemdeckapp/stemdeck:latest`](https://github.com/stemdeckapp/stemdeck/pkgs/container/stemdeck). It runs on the CPU as it is; add `--runtime=nvidia` to use an NVIDIA GPU. On Unraid, search "StemDeck" in Apps.

<details>
<summary><b>What's new in 0.20.0</b></summary>

<br>

- **Guide.** A new button in the left rail turns the studio into its own manual: point at anything to see what it does, and every number in the song facts says what it measures.
- **The Trash.** A count on its icon, hold a song for a second to bring it back, and pick several at once with Ctrl or Shift.
- **Deleting a song while it extracts stops it,** and keeps its files until you empty the Trash.
- **Imports stay in the background:** the song you have open stays open.
- **We Recommend** welcomes Adrianna Claro, and cards can now link to Spotify and Apple Music.
- **Portable Windows installs** in a folder they cannot write to now say so clearly.

All of it, with credits to the people who reported each problem, is in the [release notes](https://github.com/stemdeckapp/stemdeck/releases/latest).

</details>

<details>
<summary><b>Installing, per platform</b></summary>

<br>

**Windows and Linux.** Unzip, then run `StemDeck.exe` (Windows) or `StemDeck` (Linux). No installer and nothing else to install. Put the folder somewhere you can write to, such as `C:\StemDeck`: a portable StemDeck keeps its runtime, models and settings in a `data` folder beside it, so it cannot run from Program Files. The first launch downloads FFmpeg and the separation model, and on an NVIDIA card sets up GPU support; later launches start in seconds.

**macOS.** Open the DMG and drag StemDeck to Applications. StemDeck is not code-signed yet, so clear the quarantine flag once, or macOS will say the app is damaged:

```sh
xattr -dr com.apple.quarantine /Applications/StemDeck.app
```

The first launch downloads the runtime and the model. Apple Silicon separates on its built-in GPU.

**Docker and Unraid.** See [Build from source](docs/build-from-source.md#docker) for the `docker run` line, GPU passthrough and the Unraid volume mapping.

**Updates.** The desktop app tells you when a new version is out and installs it for you.

</details>

<a name="what-you-can-do"></a>

## <img src="imgs/readme/grid.svg" width="26" align="top" alt=""> What you can do

### <img src="imgs/readme/stems.svg" width="22" align="top" alt=""> Split any song

- **Up to six stems** with Meta's open Demucs model: vocals, drums, bass, guitar, piano and other.
- **Lead and backing vocals** as separate lanes, on demand (not on Intel Macs).
- **Files or links:** MP3, WAV, FLAC, OGG, Opus, M4A and MP4, or a YouTube or SoundCloud link.
- **Search without leaving the app:** YouTube songs and playlists, and SoundCloud, with a preview.
- **Pick only what you need** before you extract.
- **Whole YouTube playlists** go into an import queue and run one after another.

### <img src="imgs/readme/sliders.svg" width="22" align="top" alt=""> Practice with it

- **Mute or solo** any part, and set each one's volume.
- **Slow it down** to half speed, or anywhere from 0.50x to 0.99x, without changing the pitch.
- **Loop the hard part**, by dragging across the waveform.
- **Change the key**, of the whole song or of one part.
- **Play with a click** that follows the song, with a count-in, any beats per bar and groupings such as 3+2+2.
- **Fix the beat grid** when the click drifts: move, add or remove beats and mark the bar lines.

### <img src="imgs/readme/gauge.svg" width="22" align="top" alt=""> Know the song

- **Key, tempo and loudness,** found for you.
- **Sections** such as verse, chorus and solo, detected for you (experimental) or marked by hand.
- **Synced lyrics** from LRCLIB, with timing you can fix by tapping along. With an NVIDIA GPU, StemDeck can also write them down from the vocals when none are found.
- **Song and artist details** from MusicBrainz, Wikipedia and, optionally, Discogs.

### <img src="imgs/readme/export.svg" width="22" align="top" alt=""> Keep and share

- **Export** what you hear as one file, every part as a `.zip`, only the looped part, or, when the song came with a video, a video with the original picture. Add the click or a count-in if you like.
- **Drag a stem or a loop** from the desktop app straight into your other music software.
- **A library** with colored folders, tags you can search by (`#tag`), favorites, and a Trash that only deletes when you empty it.

### <img src="imgs/readme/eye.svg" width="22" align="top" alt=""> It explains itself

Click **Guide** in the left rail and point at anything. Every button, lane control and number says what it does, in plain words. While Guide is on, clicks explain instead of acting, so nothing happens by accident.

### <img src="imgs/readme/phone.svg" width="22" align="top" alt=""> On your phone too

Turn on **Make StemDeck available on your network** in Settings and scan the QR code. Your phone opens your library and mixer over your own Wi-Fi, with speed and key controls. No app store, no cloud.

<p align="center">
  <b>Windows, Linux and macOS</b> &nbsp;·&nbsp; <b>Docker and Unraid</b> &nbsp;·&nbsp; <b>11 languages</b><br>
  <sub>English · Deutsch · Español · Français · Bahasa Indonesia · 日本語 · 한국어 · Polski · Português (Brasil) · Português (Portugal) · 简体中文</sub>
</p>



<a name="honest-comparison"></a>

## <img src="imgs/readme/check.svg" width="26" align="top" alt=""> Honest comparison

StemDeck is not trying to beat the commercial stem-separation services. It covers the core well and stops there. This table is here so you can choose knowingly rather than find the gaps later.

| | StemDeck | Moises, LALAL.AI and similar |
|---|---|---|
| **Price** | Free and open source | Usually freemium, with credits or a subscription for regular use |
| **Where it runs** | On your own machine | Usually in the cloud, so your audio is uploaded |
| **Account** | None | Usually required |
| **Internet** | For links, the first model download, update checks, and song details and lyrics | Usually needed to process audio |
| **Privacy** | Your audio is not uploaded; song names are looked up for details and lyrics | Processed on their servers, under their own retention policy |
| **Separation model** | Demucs `htdemucs_6s`, open source from Meta | Their own models, which may give better results |
| **Stems** | 6, plus lead and backing vocals | Varies; some offer more instruments |
| **Speed** | Depends on your hardware: fast on a GPU, slow on a CPU | Fast on any hardware |
| **Many songs at once** | An import queue and whole playlists, run one after another | Varies by service and plan |
| **Phone** | Your phone opens StemDeck over your own Wi-Fi; no app store app | Some offer mobile apps |
| **Practice tools** | Speed, key change, loops, click track with count-in, synced lyrics; no chord detection | Varies by product; often chords and more |
| **Polish** | A one-person open-source project | Polished, production-grade apps |
| **Source code** | Open source, forkable, self-hostable | Closed |

If you need the best quality, speed on any machine, or a store-bought phone app, the commercial products are worth the money. If you want stems for practice and study, keep your audio private, and like software with no strings attached, StemDeck is enough.


<a name="we-recommend"></a>

## <img src="imgs/readme/person.svg" width="26" align="top" alt=""> We Recommend

StemDeck is free and **does not accept any money, sponsorship, or funding**  from anyone listed below. I share these makers and artists and communities purely for the joy of pointing you toward wonderful people doing beautiful work. Go meet them ❤️

| Category | Name | What they do | Link |
|---|---|---|---|
| Artists & Creators | Adrianna Claro | Soul and Contemporary R&B Singer, Songwriter and Composer | [Spotify](https://open.spotify.com/album/10fZ48iJhIe3lWcXSMUlFG) · [Apple Music](https://music.apple.com/us/album/happy/1592026727?i=1592026728) |
| Artists & Creators | Analog4Lyfe | Analog gear specialist | [@analog4lyfe](https://www.instagram.com/analog4lyfe) |
| Artists & Creators | Dead röses | Cork-based punk rock band | [@dead_rosesband](https://www.instagram.com/dead_rosesband) |
| Artists & Creators | Joao Gaspar | Producer, film scorer, touring/session musician | [@jay_glaspar](https://www.instagram.com/jay_glaspar) |
| Artists & Creators | Killah Trakz | Industry Secret Villain and Media Influencer | [@killahtrakz](https://www.instagram.com/killahtrakz/) · [Spotify](https://open.spotify.com/artist/6nePYOqT4E7D4C48twPtKk) · [Apple Music](https://music.apple.com/us/artist/killah-trakz/371296633) |
| Artists & Creators | More Notes Less Talk | Gear-focused creative project with a raw, tape-recorded identity | [@morenoteslesstalk](https://www.youtube.com/@morenoteslesstalk) |
| Artists & Creators | NIHIL | Modern alternative metal from Argentina. Fueled by emotionally charged vocals, anger and emptiness | [@somosnihil](https://www.instagram.com/somosnihil/) · [Spotify](https://open.spotify.com/artist/1OeKplJxFNM6JHrWeo2SaV) |
| Instrument Builders & Repair | Dlima Guitars | Custom guitars and basses | [@dlimaguitars](https://www.instagram.com/dlimaguitars) |
| Instrument Builders & Repair | Kris Luthier | Instrument repair and restoration | [@krisluthier](https://www.instagram.com/krisluthier) |
| Instrument Builders & Repair | Lisbon Guitar Works | Handmade guitars in Lisbon | [dlimaguitars.com](https://dlimaguitars.com) |
| Media & Community | Not Another Audio Podcast | Audio, music and tech industry podcast by working professionals | [Libsyn](https://directory.libsyn.com/shows/view/id/20f5a1e3-6fea-4d41-8cdf-451ce6fc6cda) |
| Media & Community | r/bass | Bass-player community | [r/Bass](https://www.reddit.com/r/Bass) |
| Media & Community | slashCAM | Camera, video, and post-production media | [@slashcam.de](https://www.instagram.com/slashcam.de) |
| Music & Karaoke Technology | Beltr | Local, subscription-free karaoke software | [beltr.app](https://beltr.app/) |
| Music & Karaoke Technology | Seratone | TV-based karaoke system | [seratone.audio](https://seratone.audio/) |
| Music Gear | Empress Effects | Boutique effects pedals | [empresseffects.com](https://empresseffects.com) |
| Music Gear | Thomann | Large music-equipment retailer | [@thomann.music](https://www.instagram.com/thomann.music) |
| Writers & Storytellers | Alexandre Borges | Portuguese writer, screenwriter, and cultural commentator | [Books & author profile](https://www.instagram.com/alexgram_b/) |


## <img src="imgs/readme/zap.svg" width="26" align="top" alt=""> Star history

<a href="https://www.star-history.com/?repos=stemdeckapp%2Fstemdeck&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=stemdeckapp/stemdeck&type=date&theme=dark&legend=top-left&sealed_token=ZCLzbiDb9k7qGcBlGZ4Y7ztZz0mepkiAVraLOc4qeVQjSM_MfPIQD7P9n2wvwBM8mw8EKfkSAQfM2vByzi3mEPklzgSWtYnVzZsfexzb7LxwoATlrgSmWesQiwczpg4xa32nF4_Np1Qpe62s5eFsl0o46JzPuwwd5M6pwShbj1PfDMFD4b0Yuu0jH3rD" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=stemdeckapp/stemdeck&type=date&legend=top-left&sealed_token=ZCLzbiDb9k7qGcBlGZ4Y7ztZz0mepkiAVraLOc4qeVQjSM_MfPIQD7P9n2wvwBM8mw8EKfkSAQfM2vByzi3mEPklzgSWtYnVzZsfexzb7LxwoATlrgSmWesQiwczpg4xa32nF4_Np1Qpe62s5eFsl0o46JzPuwwd5M6pwShbj1PfDMFD4b0Yuu0jH3rD" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=stemdeckapp/stemdeck&type=date&legend=top-left&sealed_token=ZCLzbiDb9k7qGcBlGZ4Y7ztZz0mepkiAVraLOc4qeVQjSM_MfPIQD7P9n2wvwBM8mw8EKfkSAQfM2vByzi3mEPklzgSWtYnVzZsfexzb7LxwoATlrgSmWesQiwczpg4xa32nF4_Np1Qpe62s5eFsl0o46JzPuwwd5M6pwShbj1PfDMFD4b0Yuu0jH3rD" />
 </picture>
</a>


<a name="for-developers"></a>

## <img src="imgs/readme/wrench.svg" width="26" align="top" alt=""> For developers

| Page | What it covers |
|---|---|
| **[Build from source](docs/build-from-source.md)** | The macOS app, a web server on any platform, Docker and Unraid |
| **[Configuration](docs/configuration.md)** | Environment variables, and serving other devices over https |
| **[HTTP API](docs/api.md)** | The endpoints the web app uses |
| **[Troubleshooting](docs/troubleshooting.md)** | Common problems, and what lives where on disk |
| **[Model licenses](docs/models.md)** | Where each machine-learning model's license comes from |

<details>
<summary><b>Built with</b></summary>

<br>

StemDeck is built on **[Python 3.12](https://python.org)** managed via **[uv](https://github.com/astral-sh/uv)**, with a **[FastAPI](https://fastapi.tiangolo.com)** backend serving REST and Server-Sent Events. Stem separation uses **[Demucs](https://github.com/facebookresearch/demucs)** (`htdemucs_6s`), Meta AI's open-source 6-stem neural network. The optional on-demand lead/backing vocal split runs the UVR-MDX-NET Karaoke 2 model via **[audio-separator](https://github.com/nomadkaraoke/python-audio-separator)**, trained as part of the **[Ultimate Vocal Remover](https://github.com/Anjok07/ultimatevocalremovergui)** project by Anjok07. YouTube audio is fetched via **[yt-dlp](https://github.com/yt-dlp/yt-dlp)**; transcoding and mixing use **[FFmpeg](https://ffmpeg.org)**. BPM detection and key analysis run on **[librosa](https://librosa.org)**; loudness measurement uses **[pyloudnorm](https://github.com/csteinmetz1/pyloudnorm)** (ITU-R BS.1770). The macOS and Windows desktop shells are **[Tauri v2](https://tauri.app)** (Rust/WKWebView on macOS, Rust/WebView2 on Windows). The frontend is vanilla JS with the Web Audio API, no framework and no build step; waveforms are rendered on `<canvas>` using min/max sample rendering.

*Thanks to the creators and maintainers of all the open-source libraries that make StemDeck possible.*

</details>


<a name="disclaimer"></a>

## <img src="imgs/readme/flag.svg" width="26" align="top" alt=""> Disclaimer

StemDeck is a local audio stem separation tool intended for personal study, research, and experimentation. It is not a downloading service. Audio you import, and the stems made from it, are saved only in the library folder on the machine that runs StemDeck, and StemDeck does not upload, share or redistribute them. To find song details and lyrics it sends the song's name to the services named in its Settings and, only if you add an AcoustID key, an audio fingerprint; it does not send the audio itself.

YouTube and SoundCloud link support is provided via [yt-dlp](https://github.com/yt-dlp/yt-dlp) as a convenience. Automated downloading may violate YouTube's Terms of Service. You, the user, are solely responsible for ensuring you have the right to process any audio you submit, complying with the terms of service of any site you download from, and respecting the copyright of the material you work with.

You are also responsible for following the licenses of the underlying tools this project depends on (yt-dlp, Demucs, FFmpeg, PyTorch, and others listed in `pyproject.toml`).

The author(s) of StemDeck provide this software "as is", without warranty of any kind, and accept no responsibility or liability for how it is used.


## <img src="imgs/readme/shield.svg" width="26" align="top" alt=""> License

StemDeck is [Apache-2.0](LICENSE).

Every download ships a `THIRD_PARTY_NOTICES.txt` and a `licenses/` folder. `licenses/INDEX.txt` lists each packaged Python dependency with its version and license, and each one's full license text sits beside it. That inventory is generated from the packaged interpreter at build time, so it describes what actually shipped rather than what was expected to.

FFmpeg is a GPL build. StemDeck downloads it and runs it as a separate executable, so it does not change StemDeck's own license, and `THIRD_PARTY_NOTICES.txt` carries the written offer of source for the exact build your platform receives.


## <img src="imgs/readme/globe.svg" width="26" align="top" alt=""> Community

Questions, ideas and show-and-tell are all welcome.

<div align="center">
  <a href="https://github.com/stemdeckapp/stemdeck"><img src="https://img.shields.io/badge/GitHub-stemdeckapp-181717?style=flat-square&logo=github&logoColor=white" alt="GitHub"></a>
  <a href="https://discord.gg/YhCKsjhcwB"><img src="https://img.shields.io/badge/Discord-Join-5865F2?style=flat-square&logo=discord&logoColor=white" alt="Discord"></a>
  <a href="https://www.reddit.com/r/StemDeckApp/"><img src="https://img.shields.io/badge/Reddit-r%2FStemDeckApp-FF4500?style=flat-square&logo=reddit&logoColor=white" alt="Reddit"></a>
  <a href="https://www.instagram.com/stemdeck"><img src="https://img.shields.io/badge/Instagram-stemdeck-E4405F?style=flat-square&logo=instagram&logoColor=white" alt="Instagram"></a>
  <a href="https://x.com/StemDeckApp"><img src="https://img.shields.io/badge/X-StemDeckApp-000000?style=flat-square&logo=x&logoColor=white" alt="X"></a>
  <a href="https://stemdeck.app"><img src="https://img.shields.io/badge/Website-stemdeck.app-000000?style=flat-square&logo=safari&logoColor=white" alt="Website"></a>
</div>

## <img src="imgs/readme/link.svg" width="26" align="top" alt=""> Contributing

Issues, feature suggestions and pull requests are welcome. See the [open issues](https://github.com/stemdeckapp/stemdeck/issues) for what is planned, and [Build from source](docs/build-from-source.md) to run it yourself.
