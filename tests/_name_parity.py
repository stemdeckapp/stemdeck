"""Builds tests/fixtures/name_parity.json: what the server says about names,
for the page to be held to (tests/js/name-parity.test.mjs).

The lyrics lookup runs twice, once in the server (lyrics_lookup.py,
name_aliases.py, zh_variants.py) and once in the page when a version is picked
by hand (lyricsLookup.js, zhVariants.js). Each keeps a version only when its
artist and song are the track's; if the two disagree, a version the server
kept is one the page refuses, or the other way round.

Regenerate after an intentional change:

    uv run python -c "import json,sys; from tests._name_parity import build; \
json.dump(build(), open('tests/fixtures/name_parity.json','w',encoding='utf-8'), \
ensure_ascii=False, indent=1, sort_keys=True)"
"""

from __future__ import annotations

from typing import Any

from app.pipeline import lyrics_lookup as ll
from app.pipeline.name_aliases import fold

FOLD = [
    "鄧麗君",
    "周杰倫 Jay Chou",
    "紅豆",
    "ＹＯＡＳＯＢＩ",
    "ｷﾝｸﾞﾇｰ",
    "Dawid Podsiadło",
    "Großstadt",
    "Sigur Rós",
    "Ægir Øresund",
    "Œuvre",
    "宇多田ヒカル",
    "아이유",
    "Кино",
    "晴天（Sunny Day）",
    "①②③",
    "ﬁre",
    "",
]

SCRIPT_NAMES = [
    "周杰倫 Jay Chou",
    "鄧麗君 Teresa Teng テレサ・テン",
    "IU (아이유)",
    "五月天 (Mayday)",
    "BTS (방탄소년단)",
    "Official髭男dism",
    "五月天 阿信",
    "Queen",
    "周杰倫【Jay Chou】",
    "",
]

SAME_ARTIST: list[tuple[str, list[str]]] = [
    ("周杰伦", ["周杰倫"]),
    ("邓丽君", ["鄧麗君"]),
    ("ＹＯＡＳＯＢＩ", ["YOASOBI"]),
    ("ｷﾝｸﾞﾇｰ", ["キングヌー"]),
    ("Dawid Podsiadlo", ["Dawid Podsiadło"]),
    ("鄧麗君 (Teresa Teng)", ["Teresa Teng"]),
    ("五月天 (Mayday)", ["五月天"]),
    ("IU", ["IU (아이유)"]),
    ("周杰倫", ["周杰倫 Jay Chou"]),
    ("Jay Chou", ["周杰倫 Jay Chou"]),
    ("周杰倫 & 費玉清", ["周杰倫"]),
    ("Jay Chou", ["周杰倫"]),
    ("아이유", ["IU"]),
    ("张信哲", ["周杰倫"]),
    ("五月天 阿信", ["五月天"]),
    ("告五人", ["五月天"]),
    ("Official", ["Official髭男dism"]),
    ("林", ["林 & 周杰倫"]),
    ("The Beatles", ["Beatles"]),
    ("Beyonce", ["Beyoncé"]),
    ("Keala Settle", ["Keala Settle & The Greatest Showman Ensemble"]),
    ("The Greatest Showman Cast", ["The Greatest Showman"]),
    ("Pink", ["Pink Floyd"]),
    ("DJ", ["DJ & Someone Else"]),
    ("Kesha", ["Keala Settle"]),
    ("", ["Keala Settle"]),
    ("Keala Settle", [""]),
    ("王菲", ["王菲 & 那英"]),
    ("Sigur Ros", ["Sigur Rós"]),
    ("Grossstadt", ["Großstadt"]),
]

SAME_SONG: list[tuple[str, str]] = [
    ("红豆", "紅豆"),
    ("月亮代表我的心", "月亮代表我的心"),
    ("晴天 (Sunny Day)", "晴天"),
    ("晴天（Sunny Day）", "晴天"),
    ("「白日」", "白日"),
    ("【白日】", "白日"),
    ("밤편지 (Through the Night)", "밤편지"),
    ("月亮代表我的心 - 劇集 “黃金有罪” 插曲", "月亮代表我的心"),
    ("Malomiasteczkowy", "Małomiasteczkowy"),
    ("晴れの日(晴天)", "晴天"),
    ("雨天", "晴天"),
    ("The Moon Represents My Heart - 月亮代表我的心", "月亮代表我的心"),
    ("This Is Me", "this is me"),
    ("This Is Me (feat. Someone) [Live]", "This Is Me"),
    ("This Is Me - From The Greatest Showman", "This Is Me"),
    ("Part I - Dawn", "Part I - Dusk"),
    ("This Is Not Me", "This Is Me"),
    ("", "This Is Me"),
]


def build() -> dict[str, Any]:
    return {
        "fold": [{"in": text, "out": fold(text)} for text in FOLD],
        "scriptNames": [{"in": name, "out": ll.script_names(name)} for name in SCRIPT_NAMES],
        "sameArtist": [
            {"found": found, "names": names, "same": ll.same_artist(found, names)}
            for found, names in SAME_ARTIST
        ],
        "sameSong": [
            {"found": found, "song": song, "same": ll.same_song(found, song)}
            for found, song in SAME_SONG
        ],
    }
