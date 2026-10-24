"""Click sounds and the isolated click stem (#805).

- The preset table: parsed from static/js/clickSounds.js, validated, Classic is
  the default and still says what the old constants said.
- Classic is unchanged: its renders are byte-identical to the renderer before
  presets existed (fingerprints taken on big-release).
- Every preset: non-silent, finite, the right length, a downbeat that differs
  from a plain beat, never clipping, deterministic, and level-matched to
  Classic.
- Drift: tests/fixtures/click_sounds.json is what Python measures now; the JS
  half (tests/js/click-sounds.test.mjs) holds the browser to the same file.
- The API: an unknown sound is a 422 everywhere a click is rendered, the mix
  export uses the chosen sound, and the stems zip carries `click.<ext>` only
  when asked, the stems' exact length, with its onsets on the grid's beats.
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import wave
import zipfile
from pathlib import Path

import httpx
import numpy as np
import pytest
import soundfile as sf
from fastapi import HTTPException

from app.api import stems as stems_mod
from app.core.config import CLICK_STEM_NAME, EXTRA_STEM_NAMES, STEM_NAMES, ffmpeg_executable
from app.core.models import Job
from app.core.registry import _jobs
from app.pipeline import click_render
from app.pipeline import click_sounds as cs
from app.pipeline.click_render import (
    LEVEL_DOWNBEAT,
    LEVEL_GROUP,
    LEVEL_WEAK,
    beat_level,
    render_click_wav,
    render_count_in_wav,
)
from tests.ffmpeg_probe import skip_without_ffmpeg, skip_without_vorbis

sys.path.insert(0, str(Path(__file__).parent))
from _click_sounds_fixture import build as build_fixture  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "click_sounds.json"
SR = 44100
LEVELS = (LEVEL_WEAK, LEVEL_GROUP, LEVEL_DOWNBEAT)

# ── The table ──


def test_the_table_parses_and_lists_the_five_presets_classic_first():
    assert cs.CLICK_SOUND_IDS == ("classic", "woodblock", "rimshot", "soft", "cowbell")
    assert cs.DEFAULT_CLICK_SOUND == "classic"


def test_classic_in_the_table_is_the_old_click():
    """The default's numbers are the constants the click always had. A change
    here is a change to every existing user's click."""
    classic = cs._BY_ID["classic"]
    assert classic["gain"] == 1.0
    for level, freq, peak in (
        ("beat", click_render.CLICK_FREQ, click_render.CLICK_PEAK),
        ("group", click_render.GROUP_FREQ, click_render.GROUP_PEAK),
        ("accent", click_render.ACCENT_FREQ, click_render.ACCENT_PEAK),
    ):
        (comp,) = classic["voices"][level]
        assert comp == {
            "type": "sine",
            "freq": freq,
            "attack": click_render.CLICK_ATTACK,
            "decay": click_render.CLICK_DECAY,
            "peak": peak,
        }
    assert cs.FLOOR == click_render.RAMP_FLOOR


@pytest.mark.parametrize(
    "text",
    [
        "export const X = {};",
        "/* CLICK_SOUNDS_JSON_BEGIN */ {'presets': []} /* CLICK_SOUNDS_JSON_END */",
        '/* CLICK_SOUNDS_JSON_BEGIN */ {"default": "a", "floor": 0.0001, "presets": []}'
        " /* CLICK_SOUNDS_JSON_END */",
    ],
)
def test_a_malformed_table_is_refused_not_defaulted(text):
    with pytest.raises(cs.ClickSoundTableError):
        cs.parse_table(text)


def test_a_sample_preset_is_refused_until_it_is_implemented():
    table = json.loads(cs._JSON_BLOCK.search(cs.CLICK_SOUNDS_TABLE.read_text()).group(1))
    table["presets"][1]["kind"] = "sample"
    text = f"/* CLICK_SOUNDS_JSON_BEGIN */ {json.dumps(table)} /* CLICK_SOUNDS_JSON_END */"
    with pytest.raises(cs.ClickSoundTableError):
        cs.parse_table(text)


def test_an_unknown_sound_is_a_key_error_not_the_default():
    with pytest.raises(KeyError):
        cs.render_voice("../../etc/passwd", LEVEL_WEAK, SR)


# ── Classic is unchanged ──

# sha256 of each render, taken on big-release before presets existed.
_CLASSIC_FINGERPRINTS = {
    "44100": "93183f477c35b7259bda9465a462b430f6af687af7a03e93d3e88629b2e30222",
    "48000": "7cd370a6e71a8f588060c7ceb2306a8adbd62cc4c0530021eb4f87cca0c47270",
    "grouped": "9e9f68eb93e78009e3c6a6270aa1f69ec9c50939955424a303046f36637110ef",
    "countin": "925d342ca0162d2df576b67195e42413992365cfba3cd356b4ae8462cbf9bf83",
}
_BEATS = [0.5 + i * 0.5 for i in range(16)]
_BARS = [{"beat": 0, "beats_per_bar": 4}]


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_the_default_sound_renders_byte_for_byte_what_it_did_before_presets(tmp_path):
    for sr in (44100, 48000):
        p = render_click_wav(tmp_path / f"{sr}.wav", _BEATS, _BARS, 9.0, sample_rate=sr)
        assert _sha(p) == _CLASSIC_FINGERPRINTS[str(sr)], f"classic click changed at {sr} Hz"
    p = render_click_wav(tmp_path / "g.wav", _BEATS, [], 9.0, accent_mode=7, groups=[3, 2, 2])
    assert _sha(p) == _CLASSIC_FINGERPRINTS["grouped"]
    p, lead = render_count_in_wav(
        tmp_path / "ci.wav", _BEATS, _BARS, 9.0, count_in_bars=1, start=2.0, end=6.0
    )
    assert lead == 2.0
    assert _sha(p) == _CLASSIC_FINGERPRINTS["countin"]


def test_naming_classic_is_the_same_as_naming_nothing(tmp_path):
    a = render_click_wav(tmp_path / "a.wav", _BEATS, _BARS, 9.0)
    b = render_click_wav(tmp_path / "b.wav", _BEATS, _BARS, 9.0, sound="classic")
    assert _sha(a) == _sha(b)
    assert click_render.cache_key("j", _BEATS, _BARS, 9.0, SR, 1.0, -1) == click_render.cache_key(
        "j", _BEATS, _BARS, 9.0, SR, 1.0, -1, sound="classic"
    ), "a classic export must keep the cache key it always had"


# ── Every preset ──


@pytest.mark.parametrize("sound", cs.CLICK_SOUND_IDS)
def test_every_preset_voice_is_audible_finite_and_short(sound):
    for level in LEVELS:
        v = cs.render_voice(sound, level, SR)
        assert np.all(np.isfinite(v))
        assert 0.02 * SR <= len(v) <= 0.2 * SR, "a click is a transient, not a note"
        assert np.max(np.abs(v)) > 0.2, "silent voice"
        # Starts from (near) silence and ends there: no step at either edge.
        assert abs(v[0]) < 1e-3 and np.max(np.abs(v[-8:])) < 2e-3


@pytest.mark.parametrize("sound", cs.CLICK_SOUND_IDS)
def test_the_downbeat_differs_from_a_plain_beat(sound):
    accent = cs.render_voice(sound, LEVEL_DOWNBEAT, SR)
    beat = cs.render_voice(sound, LEVEL_WEAK, SR)
    group = cs.render_voice(sound, LEVEL_GROUP, SR)

    def centroid(x):
        # Power-weighted: a magnitude-weighted centroid of a 35 ms burst is
        # dominated by the truncation's broadband sidelobes, not its pitch.
        power = np.abs(np.fft.rfft(x, n=16384)) ** 2
        return float(np.sum(np.fft.rfftfreq(16384, 1 / SR) * power) / np.sum(power))

    # Higher and louder, with the group start between the two (#595).
    assert centroid(accent) > centroid(group) > centroid(beat)
    assert np.max(np.abs(accent)) > np.max(np.abs(group)) > np.max(np.abs(beat))


@pytest.mark.parametrize("sound", cs.CLICK_SOUND_IDS)
@pytest.mark.parametrize("sr", [22050, 44100, 48000, 96000])
def test_no_preset_clips_at_full_click_volume(sound, sr):
    for level in LEVELS:
        assert np.max(np.abs(cs.render_voice(sound, level, sr))) < 0.99


@pytest.mark.parametrize("sound", cs.CLICK_SOUND_IDS)
def test_a_render_is_deterministic(sound, tmp_path):
    a = render_click_wav(tmp_path / "a.wav", _BEATS, _BARS, 9.0, sound=sound)
    cs._voice_cached.cache_clear()
    b = render_click_wav(tmp_path / "b.wav", _BEATS, _BARS, 9.0, sound=sound)
    assert _sha(a) == _sha(b)


def test_presets_render_differently_and_key_their_own_cache(tmp_path):
    shas = {
        s: _sha(render_click_wav(tmp_path / f"{s}.wav", _BEATS, _BARS, 9.0, sound=s))
        for s in cs.CLICK_SOUND_IDS
    }
    assert len(set(shas.values())) == len(shas)
    keys = {
        click_render.cache_key("j", _BEATS, _BARS, 9.0, SR, 1.0, -1, sound=s)
        for s in cs.CLICK_SOUND_IDS
    }
    assert len(keys) == len(cs.CLICK_SOUND_IDS)


def _bar_loop(sound: str, bars: int = 8) -> np.ndarray:
    """A 4/4 bar at 120 BPM, repeated: the downbeat and three beats."""
    buf = np.zeros(int(bars * 2.0 * SR))
    for b in range(bars * 4):
        v = cs.render_voice(sound, LEVEL_DOWNBEAT if b % 4 == 0 else LEVEL_WEAK, SR)
        s = int(b * 0.5 * SR)
        buf[s : s + len(v)] += v
    return buf


@pytest.mark.parametrize("sound", cs.CLICK_SOUND_IDS[1:])
def test_presets_are_level_matched_to_classic_by_rms(sound):
    """RMS of one bar within 1.5 dB of Classic (measured: 0.95 dB at most).
    The presets were matched by loudness (the next test); RMS is the check
    that needs no ffmpeg."""

    def rms_db(x):
        return 20 * np.log10(np.sqrt(np.mean(x[: int(2 * SR)] ** 2)))

    assert abs(rms_db(_bar_loop(sound)) - rms_db(_bar_loop("classic"))) < 1.5


def _lufs(buf: np.ndarray, tmp_path: Path) -> float:
    import re

    p = tmp_path / "loop.wav"
    sf.write(p, buf, SR, subtype="FLOAT")
    r = subprocess.run(
        [ffmpeg_executable(), "-nostats", "-i", str(p), "-af", "ebur128", "-f", "null", "-"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return float(re.findall(r"I:\s+(-?[\d.]+) LUFS", r.stderr)[-1])


def test_presets_are_level_matched_to_classic_by_loudness(tmp_path):
    """Integrated loudness (EBU R128) of a 16 s 4/4 loop, the measurement the
    preset gains were set by: every preset within 0.5 LU of Classic
    (measured: -27.8 LUFS for all, woodblock -27.7)."""
    skip_without_ffmpeg()
    ref = _lufs(_bar_loop("classic"), tmp_path)
    for sound in cs.CLICK_SOUND_IDS[1:]:
        assert abs(_lufs(_bar_loop(sound), tmp_path) - ref) <= 0.5, sound


# ── Drift between the two renderers ──


def test_the_committed_drift_fixture_matches_what_python_measures():
    assert FIXTURE.is_file(), "drift fixture is missing; regenerate it (see _click_sounds_fixture)"
    committed = json.loads(FIXTURE.read_text(encoding="utf-8"))
    now = build_fixture()
    assert [(v["sound"], v["sampleRate"], v["level"]) for v in committed["voices"]] == [
        (v["sound"], v["sampleRate"], v["level"]) for v in now["voices"]
    ], "the fixture no longer covers every preset; regenerate it"
    for want, got in zip(committed["voices"], now["voices"], strict=True):
        tag = f"{got['sound']} {got['level']} @ {got['sampleRate']}"
        assert got["length"] == want["length"], tag
        assert got["peakIndex"] == want["peakIndex"], tag
        assert got["peak"] == pytest.approx(want["peak"], abs=1e-6), tag
        assert got["envelope"] == pytest.approx(want["envelope"], abs=1e-6), tag
        assert got["centroid"] == pytest.approx(want["centroid"], abs=0.01), tag
        assert got["absSum"] == pytest.approx(want["absSum"], abs=1e-3), tag


# ── The API ──

JOB = "c1c2c3c4c5c6"
# The mono click goes to both channels of an export at unity, as the app plays
# it, so a channel of the zip's click or of the mix is the voice times the
# click volume (#833: it was 1 / sqrt(2) lower, -3 dB, while amix did the
# conversion).
STEM_SECONDS = 3.3  # deliberately not the grid's 3.0: the stems decide the length
GRID = {
    "version": 1,
    "beats": [0.5, 1.0, 1.5, 2.0, 2.5, 3.0],
    "bars": [{"beat": 0, "beats_per_bar": 4}],
    "duration": 3.0,
}


@pytest.fixture(autouse=True)
def _isolate_registry():
    _jobs.clear()
    yield
    _jobs.clear()


@pytest.fixture
def jobs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(stems_mod, "_MIXDOWN_CACHE_DIR", tmp_path / "cache" / "mixdown")
    monkeypatch.setattr(stems_mod, "_CLICK_CACHE_DIR", tmp_path / "cache" / "click")
    from app.core import config

    return config.JOBS_DIR


@pytest.fixture
def client(jobs_dir):
    from app.main import app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def _make_job(jobs_dir: Path, names=("vocals", "drums"), mp3=False, grid=True) -> Job:
    job = Job(id=JOB, status="done", title="Clicky", duration_sec=STEM_SECONDS)
    stems = jobs_dir / JOB / "stems"
    stems.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(3)
    n = int(round(STEM_SECONDS * SR))
    for name in names:
        wav = stems / f"{name}.wav"
        sf.write(wav, (rng.standard_normal((n, 2)) * 0.05).clip(-1, 1), SR, subtype="PCM_16")
        if mp3:
            subprocess.run(
                [
                    ffmpeg_executable(),
                    "-v",
                    "error",
                    "-y",
                    "-i",
                    str(wav),
                    "-c:a",
                    "libmp3lame",
                    "-b:a",
                    "128k",
                    str(stems / f"{name}.mp3"),
                ],
                check=True,
            )
            wav.unlink()
        job.stems.append({"name": name, "url": f"/api/jobs/{JOB}/stems/{name}.wav"})
    if grid:
        (stems / "beats.json").write_text(json.dumps(GRID), encoding="utf-8")
    _jobs[job.id] = job
    return job


def _zip(content: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(content))


def _onsets(x: np.ndarray, threshold: float, gap: float = 0.2) -> list[int]:
    """First sample of each burst above `threshold`, bursts `gap` s apart."""
    loud = np.flatnonzero(np.abs(x) > threshold)
    out: list[int] = []
    for i in loud:
        if not out or i - out[-1] > gap * SR:
            out.append(int(i))
    return out


async def test_the_zip_has_no_click_unless_asked(client, jobs_dir):
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    async with client as c:
        r = await c.get(f"/api/jobs/{JOB}/stems/all.zip")
        assert r.status_code == 200
        assert sorted(_zip(r.content).namelist()) == ["Clicky_drums.wav", "Clicky_vocals.wav"]
        r = await c.get(f"/api/jobs/{JOB}/stems/all.zip?click=0&click_sound=cowbell")
        assert sorted(_zip(r.content).namelist()) == ["Clicky_drums.wav", "Clicky_vocals.wav"]


@pytest.mark.parametrize("sound", ["classic", "rimshot"])
async def test_the_zip_click_is_sample_aligned_with_the_stems(client, jobs_dir, sound):
    """Same length, rate and channels as the stems, onsets on the grid's beats
    within one sample, at the click's export level."""
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    gain = 0.6
    async with client as c:
        r = await c.get(
            f"/api/jobs/{JOB}/stems/all.zip?click=1&click_gain={gain}&click_sound={sound}"
        )
    assert r.status_code == 200
    zf = _zip(r.content)
    assert sorted(zf.namelist()) == ["Clicky_click.wav", "Clicky_drums.wav", "Clicky_vocals.wav"]
    stem, stem_sr = sf.read(io.BytesIO(zf.read("Clicky_drums.wav")))
    click, click_sr = sf.read(io.BytesIO(zf.read("Clicky_click.wav")))
    assert click_sr == stem_sr == SR
    assert click.shape == stem.shape, "the click must be exactly as long as the stems"
    assert np.array_equal(click[:, 0], click[:, 1]), "a mono click, on both channels"

    # Each beat's onset is where that beat's voice first crosses the
    # threshold, counted from the beat's own sample.
    thr = 0.02
    expected = []
    for i, t in enumerate(GRID["beats"]):
        voice = cs.render_voice(sound, beat_level(i, GRID["bars"], -1), SR) * gain
        expected.append(int(round(t * SR)) + int(np.flatnonzero(np.abs(voice) > thr)[0]))
    got = _onsets(click[:, 0], thr)
    assert len(got) == len(expected)
    assert max(abs(g - e) for g, e in zip(got, expected, strict=True)) <= 1, (got, expected)
    # The level is what the click contributes to the mix.
    peak = np.max(np.abs(click[: int(0.9 * SR), 0]))
    want = np.max(np.abs(cs.render_voice(sound, LEVEL_DOWNBEAT, SR))) * gain
    assert peak == pytest.approx(want, abs=3 / 32767)


async def test_the_zip_click_follows_rate_accent_and_user_edits(client, jobs_dir):
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    edited = [0.25, 0.75, 1.25, 1.75]
    (jobs_dir / JOB / "stems" / "beats.user.json").write_text(
        json.dumps({"beats": edited, "bars": []}), encoding="utf-8"
    )
    async with client as c:
        r = await c.get(f"/api/jobs/{JOB}/stems/all.zip?click=1&click_mult=2&click_accent=0")
    click, _ = sf.read(io.BytesIO(_zip(r.content).read("Clicky_click.wav")))
    # x2 inserts midpoints: 0.25, 0.5, 0.75 ... 1.75 = 7 clicks, 0.25 s apart.
    got = _onsets(click[:, 0], 0.02, gap=0.1)
    assert len(got) == 7
    assert abs(got[0] - 0.25 * SR) < 0.002 * SR
    assert abs(got[1] - 0.5 * SR) < 0.002 * SR


@pytest.mark.parametrize("fmt", ["mp3", "flac", "ogg"])
async def test_the_zip_click_is_in_the_zips_format_and_length(client, jobs_dir, fmt):
    skip_without_ffmpeg()
    if fmt == "ogg":
        skip_without_vorbis()
    _make_job(jobs_dir)
    async with client as c:
        r = await c.get(f"/api/jobs/{JOB}/stems/all.zip?format={fmt}&click=1&click_sound=woodblock")
    assert r.status_code == 200
    zf = _zip(r.content)
    assert f"Clicky_click.{fmt}" in zf.namelist()
    with zf.open(f"Clicky_drums.{fmt}") as s, zf.open(f"Clicky_click.{fmt}") as k:
        a, b = sf.info(io.BytesIO(s.read())), sf.info(io.BytesIO(k.read()))
    assert (b.samplerate, b.channels) == (a.samplerate, a.channels)
    if fmt == "flac":
        assert b.frames == a.frames
    else:
        # Decoded lengths of lossy files can differ by the codec's framing;
        # both went through the same encoder from the same frame count.
        assert abs(b.frames - a.frames) <= 1152


async def test_the_zip_click_of_an_mp3_stored_song_matches_the_decoded_stems(client, jobs_dir):
    """Songs are stored as MP3 by default (#822): the click is rendered from
    the grid, but its length must still be the decoded stems' exactly."""
    skip_without_ffmpeg()
    _make_job(jobs_dir, mp3=True)
    assert not (jobs_dir / JOB / "stems" / "drums.wav").exists()
    async with client as c:
        r = await c.get(f"/api/jobs/{JOB}/stems/all.zip?format=wav&click=1")
    zf = _zip(r.content)
    stem, _ = sf.read(io.BytesIO(zf.read("Clicky_drums.wav")))
    click, _ = sf.read(io.BytesIO(zf.read("Clicky_click.wav")))
    assert click.shape == stem.shape
    assert stem.shape[0] == int(round(STEM_SECONDS * SR))


async def test_no_grid_means_no_click_file_not_an_error(client, jobs_dir):
    skip_without_ffmpeg()
    _make_job(jobs_dir, grid=False)
    async with client as c:
        r = await c.get(f"/api/jobs/{JOB}/stems/all.zip?click=1")
    assert r.status_code == 200
    assert "Clicky_click.wav" not in _zip(r.content).namelist()


def test_the_click_entry_cannot_collide_with_a_stem():
    assert CLICK_STEM_NAME == "click"
    assert CLICK_STEM_NAME not in (*STEM_NAMES, *EXTRA_STEM_NAMES, "original", "mix")


@pytest.mark.parametrize(
    "path",
    [
        "stems/all.zip?click=1&click_sound=bogus",
        "stems/all.zip?click_sound=..%2F..%2Fx",
        "mixdown.wav?stems=drums&gains=1&click=1&click_sound=Classic",
        "mixdown.wav?stems=drums&gains=1&click_sound=" + "x" * 33,
        "video.mp4?stems=drums&gains=1&click=1&click_sound=nope",
    ],
)
async def test_an_unknown_click_sound_is_a_422(client, jobs_dir, path):
    _make_job(jobs_dir)
    (jobs_dir / JOB / "video.mp4").write_bytes(b"\x00")
    async with client as c:
        r = await c.get(f"/api/jobs/{JOB}/{path}")
    assert r.status_code == 422


def test_a_take_render_refuses_an_unknown_click_sound():
    from app.api import takes

    body = takes.RenderBody(click=True, click_sound="bogus")
    with pytest.raises(HTTPException) as e:
        takes._backing_lanes(JOB, "t0000000a", body)
    assert e.value.status_code == 422
    assert takes.RenderBody().click_sound == "classic"


def _wav(content: bytes) -> np.ndarray:
    with wave.open(io.BytesIO(content), "rb") as w:
        n = w.getnchannels()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(float) / 32767.0
    return data.reshape(-1, n)


async def test_the_mix_export_uses_the_chosen_sound(client, jobs_dir):
    """With the stem at gain 0 the export is the click alone: it is the chosen
    preset's render at the click level, not Classic's."""
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    out = {}
    async with client as c:
        for sound in ("classic", "cowbell"):
            r = await c.get(
                f"/api/jobs/{JOB}/mixdown.wav?stems=drums&gains=0&click=1&click_gain=0.5"
                f"&click_sound={sound}"
            )
            assert r.status_code == 200
            out[sound] = _wav(r.content)[:, 0]
    assert not np.array_equal(out["classic"], out["cowbell"])
    # The cowbell export's first click is the cowbell downbeat voice.
    voice = cs.render_voice("cowbell", LEVEL_DOWNBEAT, SR) * 0.5
    start = int(round(GRID["beats"][0] * SR))
    seg = out["cowbell"][start : start + len(voice)]
    assert np.max(np.abs(seg - voice)) < 4 / 32767


def test_the_click_lane_renders_the_chosen_sound(jobs_dir):
    _make_job(jobs_dir)
    a = stems_mod._click_lane(JOB, True, 1.0, -1, 0.6)
    b = stems_mod._click_lane(JOB, True, 1.0, -1, 0.6, sound="soft")
    ci = stems_mod._click_lane(JOB, False, 1.0, -1, 0.6, count_in_bars=1, sound="soft")
    assert a.path != b.path
    assert _sha(a.path) != _sha(b.path)
    assert ci is not None and ci.path not in (a.path, b.path)


async def test_the_zip_sums_to_the_mix_export(client, jobs_dir):
    """The gain rule: the zip's files at unity sum to the mix export of the
    same stems at unity with the same click, so dropping the zip into a DAW
    gives the exported mix."""
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    # The mix's click ends at the grid's recorded duration (a beat past it is
    # dropped there, as it always was); on a real song that is the stems'
    # length, so make it so here.
    (jobs_dir / JOB / "stems" / "beats.json").write_text(
        json.dumps({**GRID, "duration": STEM_SECONDS}), encoding="utf-8"
    )
    q = "click=1&click_gain=0.6&click_sound=woodblock"
    async with client as c:
        z = _zip((await c.get(f"/api/jobs/{JOB}/stems/all.zip?{q}")).content)
        mix = _wav(
            (await c.get(f"/api/jobs/{JOB}/mixdown.wav?stems=vocals,drums&gains=1,1&{q}")).content
        )
    total = sum(
        sf.read(io.BytesIO(z.read(f"Clicky_{n}.wav")))[0] for n in ("vocals", "drums", "click")
    )
    assert total.shape == mix.shape
    assert np.max(np.abs(total - mix)) < 4 / 32767


# ── The exported click's level is the live click's (#833) ──
#
# Live, the click is a mono AudioBuffer through a GainNode at the click volume
# into the stereo master, and Web Audio's mono-to-stereo up-mix copies it to
# both channels at full level. So the level the app plays on each channel is
# the synthesised buffer times the volume, and every export must match it.


def _live_click(level: int, gain: float) -> np.ndarray:
    """The live Classic click on one channel: the browser's buffer times the volume."""
    return cs.render_voice("classic", level, SR) * gain


def _db(a: np.ndarray, b: np.ndarray) -> float:
    return float(20 * np.log10(np.sqrt(np.mean(a**2)) / np.sqrt(np.mean(b**2))))


# (query, the first click's start sample in the export, its level).
_LEVEL_CASES = {
    "mix": ("", int(round(GRID["beats"][0] * SR)), beat_level(0, GRID["bars"], -1)),
    # The region starts on the second beat, so its first click is at sample 0.
    "region": ("&start=1.0&end=3.0", 0, beat_level(1, GRID["bars"], -1)),
    # The count-in's first click is its downbeat, at sample 0.
    "count_in": ("&count_in=1", 0, LEVEL_DOWNBEAT),
}


@pytest.mark.parametrize("ext", ["wav", "flac"])
@pytest.mark.parametrize("case", sorted(_LEVEL_CASES))
async def test_the_exported_click_is_at_the_live_level_on_each_channel(client, jobs_dir, case, ext):
    """The same click at the same volume: each channel of the export carries
    the live click's level, within 0.05 dB (the 16-bit render's rounding). It
    was 3.01 dB lower while the mix graph left the mono-to-stereo conversion
    to amix."""
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    gain = 0.6
    query, at, level = _LEVEL_CASES[case]
    async with client as c:
        r = await c.get(
            f"/api/jobs/{JOB}/mixdown.{ext}?stems=drums&gains=0&click=1&click_gain={gain}{query}"
        )
    assert r.status_code == 200, r.text
    out, _ = sf.read(io.BytesIO(r.content))
    live = _live_click(level, gain)
    assert out.ndim == 2 and out.shape[1] == 2
    for ch in (0, 1):
        diff = _db(out[at : at + len(live), ch], live)
        assert diff == pytest.approx(0.0, abs=0.05), (case, ch, diff)


async def test_the_zip_click_is_at_the_live_level_on_each_channel(client, jobs_dir):
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    gain = 0.6
    async with client as c:
        r = await c.get(f"/api/jobs/{JOB}/stems/all.zip?click=1&click_gain={gain}")
    click, _ = sf.read(io.BytesIO(_zip(r.content).read("Clicky_click.wav")))
    live = _live_click(beat_level(0, GRID["bars"], -1), gain)
    at = int(round(GRID["beats"][0] * SR))
    for ch in (0, 1):
        diff = _db(click[at : at + len(live), ch], live)
        assert diff == pytest.approx(0.0, abs=0.05), (ch, diff)


async def test_the_video_export_click_is_at_the_live_level_on_each_channel(
    client, jobs_dir, tmp_path
):
    """The MP4 export: AAC, so compared by the RMS of the whole click track
    (timing-blind, the encoder's priming does not matter), within 0.2 dB."""
    skip_without_ffmpeg()
    from tests.test_export_limiter import _tiny_video

    _make_job(jobs_dir)
    _tiny_video(jobs_dir / JOB / "video.mp4", STEM_SECONDS)
    gain = 0.6
    async with client as c:
        r = await c.get(f"/api/jobs/{JOB}/video.mp4?stems=drums&gains=0&click=1&click_gain={gain}")
    assert r.status_code == 200, r.text
    out = tmp_path / "out.mp4"
    out.write_bytes(r.content)
    pcm = subprocess.run(
        [
            ffmpeg_executable(),
            "-nostdin",
            "-loglevel",
            "error",
            "-i",
            str(out),
            "-vn",
            "-f",
            "f32le",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
        timeout=60,
    ).stdout
    x = np.frombuffer(pcm, dtype="<f4").reshape(-1, 2)
    # The live click track: the same mono render, times the volume.
    mono = render_click_wav(
        tmp_path / "live.wav", GRID["beats"], GRID["bars"], GRID["duration"], sample_rate=SR
    )
    live, _ = sf.read(mono)
    for ch in (0, 1):
        diff = _db(x[:, ch], live * gain) + 10 * np.log10(len(x) / len(live))
        assert diff == pytest.approx(0.0, abs=0.2), (ch, diff)
