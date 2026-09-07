"""Python half of the click parity gate.

Asserts the committed fixture is still what Python produces. The JS half
(tests/js/parity.test.mjs) asserts the same fixture is what the browser
produces. Neither language can drift from the other without a visible diff to
tests/fixtures/click_levels.json, and regenerating that file is a deliberate
act somebody reviews -- which is the part hand-written duplicate expectations
in two test files could never give us.

Regenerate after an intentional change:

    uv run python -c "import json,sys; sys.path.insert(0,'tests'); \
from _click_parity import build; \
json.dump(build(), open('tests/fixtures/click_levels.json','w'), indent=2, sort_keys=True)"
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._click_parity import build

FIXTURE = Path(__file__).parent / "fixtures" / "click_levels.json"


def test_the_committed_parity_fixture_matches_what_python_produces():
    assert FIXTURE.is_file(), "parity fixture is missing; regenerate it (see module docstring)"
    committed = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert committed == build(), (
        "Python's click levels no longer match tests/fixtures/click_levels.json. "
        "If the change is intended, regenerate the fixture and check that "
        "static/js/metronome.js was changed to match -- tests/js/parity.test.mjs "
        "is pinned to the same file."
    )


def test_the_fixture_actually_covers_the_cases_worth_pinning():
    """A fixture that silently shrank would keep passing while covering nothing."""
    t = build()
    assert len(t["defaultGrouping"]) >= 14
    assert len(t["levels"]) >= 18
    assert len(t["countIn"]) >= 24
    # The whole point: at least one meter is grouped, and one is not.
    assert any(1 in e["levels"] for e in t["levels"]), "no grouped meter is pinned"
    assert any(set(e["levels"]) == {2, 0} for e in t["levels"]), "no flat meter is pinned"


@pytest.mark.parametrize("simple", [2, 3, 4])
def test_simple_meters_are_pinned_flat_so_a_regression_is_loud(simple):
    """2, 3 and 4 must keep sounding exactly as they did. This is the assertion
    that fails first if a future default grouping starts touching them."""
    t = build()
    assert t["defaultGrouping"][str(simple)] == [simple]
