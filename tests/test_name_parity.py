"""Python half of the name parity gate.

Asserts the committed tests/fixtures/name_parity.json is still what the
server's lyrics matching says about each name (fold, script_names,
same_artist, same_song). The JS half, tests/js/name-parity.test.mjs, asserts
the page's lyricsLookup.js says the same, and that zhVariants.js folds every
character the server's table does. Neither side can drift without a visible
diff to the fixture. Regenerate it as tests/_name_parity.py says.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests._name_parity import build

FIXTURE = Path(__file__).parent / "fixtures" / "name_parity.json"


def test_the_committed_fixture_matches_what_the_server_says():
    assert FIXTURE.is_file(), (
        "name parity fixture is missing; regenerate it (tests/_name_parity.py)"
    )
    committed = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert committed == build(), (
        "The server's name matching no longer matches tests/fixtures/name_parity.json. "
        "If the change is intended, regenerate the fixture and check that "
        "static/js/lyricsLookup.js was changed to match."
    )


def test_the_fixture_pins_both_answers():
    """A fixture where every case said the same would pass while pinning half
    the rule."""
    built = build()
    assert {case["same"] for case in built["sameArtist"]} == {True, False}
    assert {case["same"] for case in built["sameSong"]} == {True, False}
    assert any(case["out"] for case in built["scriptNames"])
    assert any(not case["out"] for case in built["scriptNames"])
    assert any(case["in"] != case["out"] for case in built["fold"])
