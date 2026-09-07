"""The shared click-level expectation table, generated from the Python side.

Playback (static/js/metronome.js) and export (app/pipeline/click_render.py) must
agree beat for beat, or a player monitors one thing and exports another. That was
previously enforced by writing the same expected numbers into two test files by
hand, which only catches a mistake in one of them: reason wrongly the same way
twice and both suites pass.

This builds the table once, from Python. tests/test_click_parity.py asserts the
committed fixture still matches what Python produces, and tests/js/parity.test.mjs
asserts the JS produces the same. Neither language can drift without a visible
diff to the fixture, and changing the fixture is a deliberate act in review.
"""

from __future__ import annotations

from app.pipeline.click_render import beat_level, count_in_beats, default_grouping

# Meters worth pinning: the simple ones that must not change, the odd and
# compound ones the grouping exists for, and a prime with no conventional
# reading. 32 is the upper bound the API accepts.
METERS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 15, 16, 32]

# Explicit groupings, including ones that do not fit the bar and must be refused.
GROUPINGS: list[tuple[int, list[int] | None]] = [
    (7, None),
    (7, [3, 2, 2]),
    (7, [2, 2, 3]),
    (7, [2, 3, 2]),
    (7, [3, 3]),  # does not sum: must fall back to the default
    (7, []),  # empty: default
    (5, [2, 3]),
    (6, [2, 2, 2]),
    (6, [4, 2]),
    (12, [3, 3, 3, 3]),
    (12, [4, 4, 4]),
    (4, [2, 2]),  # a user may group 4 even though the default does not
]

_STEADY = [0.5 + i * 0.5 for i in range(32)]


def build() -> dict:
    """Every parity-relevant answer the two implementations must agree on."""
    table: dict = {"defaultGrouping": {}, "levels": [], "countIn": []}

    for n in METERS:
        table["defaultGrouping"][str(n)] = default_grouping(n)

    for n, groups in GROUPINGS:
        # Two full bars, so a wrong modulo shows up on the second one.
        table["levels"].append(
            {
                "beatsPerBar": n,
                "groups": groups,
                "levels": [beat_level(i, [], n, groups) for i in range(n * 2)],
            }
        )

    # Auto: each detected bar is grouped by its own length, and a supplied
    # grouping must not be forced onto a bar it does not fit.
    for per_bar in (4, 5, 6, 7, 9, 12):
        table["levels"].append(
            {
                "beatsPerBar": -1,
                "bars": [{"beat": 0, "beats_per_bar": per_bar}],
                "groups": None,
                "levels": [
                    beat_level(i, [{"beat": 0, "beats_per_bar": per_bar}], -1)
                    for i in range(per_bar * 2)
                ],
            }
        )

    for n, groups in GROUPINGS:
        for bars_count in (1, 2):
            lead_in, clicks = count_in_beats(
                _STEADY, [], count_bars=bars_count, accent_mode=n, groups=groups or None
            )
            table["countIn"].append(
                {
                    "beatsPerBar": n,
                    "groups": groups,
                    "countBars": bars_count,
                    "leadIn": round(lead_in, 6),
                    "offsets": [round(o, 6) for o, _ in clicks],
                    "levels": [lv for _, lv in clicks],
                }
            )

    return table
