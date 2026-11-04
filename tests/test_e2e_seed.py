"""The browser tests' fixture songs are songs of the current loudness method
(#923). tests/e2e/seed.py writes the number rather than importing it, so a
change of LOUDNESS_VERSION has to change the seed too: an older one makes the
first open measure the fixture again, and every later spec in the run then
shows the "measured again" note under the cards."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from app.core.models import LOUDNESS_VERSION

SEED = Path(__file__).parent / "e2e" / "seed.py"


def test_the_fixture_songs_use_the_current_loudness_method(tmp_path):
    spec = importlib.util.spec_from_file_location("e2e_seed", SEED)
    assert spec is not None and spec.loader is not None
    seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed)

    record = seed._build_job(tmp_path, seed.JOB_ID, seed.TITLE, 1)
    assert record["loudness_version"] == LOUDNESS_VERSION
