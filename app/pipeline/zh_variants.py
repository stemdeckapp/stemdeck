"""Traditional and simplified Chinese as one, for comparing names.

The same song or singer is written two ways: 鄧麗君 in Taiwan and Hong Kong,
邓丽君 on the mainland; 紅豆 and 红豆. LRCLIB holds both, and a track's tags
or MusicBrainz credit can be either, so names are compared with every
traditional character folded to its simplified form. Folding both sides only
makes variants of one character equal: no two names that differ in anything
else become one.

The table is app/_vendor/opencc/zh_t2s.txt, derived from OpenCC (Apache-2.0:
the file carries the attribution, and the license is beside it).
static/js/zhVariants.js holds the same pairs for the page, and a test keeps
the two equal.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_TABLE_PATH = Path(__file__).resolve().parent.parent / "_vendor" / "opencc" / "zh_t2s.txt"


@lru_cache(maxsize=1)
def _table() -> dict[int, str]:
    """{ord(traditional): simplified}, for str.translate. Empty when the data
    file cannot be read: names then compare as written, as they did before."""
    table: dict[int, str] = {}
    try:
        text = _TABLE_PATH.read_text(encoding="utf-8")
    except OSError:
        return table
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        pair = line.split(" ")
        if len(pair) == 2 and len(pair[0]) == 1 and len(pair[1]) == 1:
            table[ord(pair[0])] = pair[1]
    return table


def pairs() -> dict[str, str]:
    """The table as {traditional: simplified}."""
    return {chr(k): v for k, v in _table().items()}


def to_simplified(text: str) -> str:
    """``text`` with every traditional Chinese character folded to its
    simplified form. Anything else is left as it is."""
    return text.translate(_table())
