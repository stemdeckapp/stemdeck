"""Target curves for Suggest EQ (the data is eq_targets.json).

A target is a third-octave spectrum shape: 31 levels in dB at the ISO 266
bands from 20 Hz to 20 kHz, on a scale where pink noise is flat. Only the
shape matters; the fitter (eq_fit.py) removes the overall level. Shape of one
target, as agreed with the mastering report (01-mastering.md 5.5, 09 4.3):
``{id, label_key, bands_db[31], target_lufs}``.

**A target is a taste choice, not a fact.** It says "this is a common
balance", not "this is correct". The UI says so next to every suggestion.

Where the numbers come from (sources in docs/plans/0.21.0/overnight-log.md,
task G):

- **mix, Balanced** (the master and the "original" lane): from 100 Hz up,
  the mean long-term average spectrum of 12,345 popular music tracks fitted
  by Elowsson and Friberg ("Long-term average spectrum in popular music and
  its relation to the level of the percussion", AES 142nd Convention, 2017,
  paper 9762, equation 6), converted from power density to third-octave
  levels (+10 log10 f, i.e. +3 dB per octave). In third octaves it is close
  to pink up to about 1 kHz (+0.7 dB/oct at 200 Hz, -2 dB/oct at 800 Hz)
  and falls faster above (-4.6 dB/oct at 3.2 kHz, -5.9 at 6.4 kHz), in line
  with the "about 5 dB per octave from 100 to 4000 Hz" in power density that
  Pestana, Ma, Reiss, Barbosa and Black found on 772 number-one singles
  ("Spectral characteristics of popular commercial recordings 1950-2010",
  AES 135th Convention, 2013). So it is *not* pink noise: music is darker
  than pink above 1 kHz, and a pink target would brighten every mix.
  Below 100 Hz the paper's mean falls steeply (its set leans on folk:
  Dylan, Neil Young), and its authors note that deviations below the mean in
  the bass are "generally less alarming". Bass levels in popular music rose
  over the decades (Hove, Vuust and Stupacher, JASA 145(4), 2019), and
  Pestana found an extended low end in hip-hop and electronica. So below
  100 Hz the curve is set by hand to a gentler roll-off (flat to 63 Hz,
  -6 dB at 40, -22 at 20). Above 15.7 kHz (the paper's analysis limit) the
  fit is extrapolated. Checked against the three songs available locally
  (Happy, Shining Star, Come As You Are): their 1 kHz to 10 kHz balance lies
  on both sides of it.
- **Warm / Bright**: the variants tonal-balance practice offers (iZotope's
  Tonal Balance Control analyses thousands of commercial masters into style
  targets): Warm is up to 3 dB less above 4 kHz and 1 dB more at 150-300
  Hz (Pestana: folk and jazz decay at the top); Bright is the opposite.
  Elowsson and Friberg found that tracks with more percussion have relatively
  more bass and more highs. **Full low end** (mix only) adds up to 3 dB
  below 120 Hz (Pestana: hip-hop and electronica).
- **Instruments** (bass, drums, guitar, keys, other): set by hand from common
  mixing practice for where each instrument sits (bass: body 60-160 Hz,
  falling about 7 dB per octave above 250 Hz; drums: kick 50-125 Hz, a
  box-and-mud dip at 300-800 Hz, flat attack and cymbals; guitar: no
  rumble under 100 Hz, flat 300 Hz to 2 kHz, the speaker roll-off above
  5 kHz; keys: 125 Hz to 1 kHz body, falling above; other: the mix curve
  without its lowest octave), then compared with the separated stems of the
  three local songs. No published per-instrument average was found; these
  are the weakest of the targets and say so in the log.
- **target_lufs**: the integrated loudness a master target is meant for
  (-14, the level the large streaming services normalise to); null for
  instruments.
- Vocals have no target: the vocal lanes have a placeholder panel, not an EQ.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.pipeline.eq_analysis import N_BANDS, NOMINAL_HZ

_PATH = Path(__file__).with_name("eq_targets.json")

# Which target family a mixer lane uses by default. Recording tracks have no
# fixed instrument: they get "other" and may pick any instrument's targets.
LANE_USE = {
    "bass": "bass",
    "drums": "drums",
    "guitar": "guitar",
    "piano": "keys",
    "other": "other",
    "original": "mix",
}
TRACK_USES = ("other", "guitar", "bass", "keys", "drums")


@dataclass(frozen=True)
class Target:
    id: str
    use: str
    variant: str
    label_key: str
    bands_db: tuple[float, ...]
    target_lufs: float | None

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "label_key": self.label_key,
            "bands_db": list(self.bands_db),
            "target_lufs": self.target_lufs,
        }


@lru_cache(maxsize=1)
def load_targets() -> dict[str, Target]:
    data = json.loads(_PATH.read_text(encoding="utf-8"))
    if tuple(data["bands_hz"]) != tuple(NOMINAL_HZ):
        raise ValueError("eq_targets.json: band frequencies differ from the analysis")
    out: dict[str, Target] = {}
    for t in data["targets"]:
        base = data["bases"][t["use"]]
        offset = data["variants"][t["variant"]]
        if len(base) != N_BANDS or len(offset) != N_BANDS:
            raise ValueError(f"eq_targets.json: {t['id']} does not have {N_BANDS} bands")
        bands = tuple(round(float(b) + float(o), 2) for b, o in zip(base, offset, strict=True))
        out[t["id"]] = Target(
            id=t["id"],
            use=t["use"],
            variant=t["variant"],
            label_key=t["label_key"],
            bands_db=bands,
            target_lufs=t["target_lufs"],
        )
    return out


def get_target(target_id: str) -> Target | None:
    return load_targets().get(target_id)


def uses_for_lane(lane: str, recorded: bool) -> tuple[str, ...]:
    """The target families offered for a lane, its own first."""
    if recorded:
        return TRACK_USES
    use = LANE_USE.get(lane)
    return (use,) if use else ()


def targets_for_uses(uses: tuple[str, ...]) -> list[Target]:
    return [t for u in uses for t in load_targets().values() if t.use == u]


def default_target(lane: str, recorded: bool) -> Target | None:
    uses = uses_for_lane(lane, recorded)
    return get_target(f"{uses[0]}.balanced") if uses else None
