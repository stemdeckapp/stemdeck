"""Child process output must never be able to fail a job by being unreadable.

`text=True` on its own decodes with the locale encoding. On Windows that is
cp1252, so a single byte outside it in Demucs' or ffprobe's output raised

    'charmap' codec can't decode byte 0x8f in position 20

and killed the whole separation. The output in question is a progress bar and
some echoed metadata: diagnostic text that is never worth failing a job over.

Both halves have to agree. The parent reads utf-8 with errors="replace", and
the child is told to write utf-8, or the mismatch just moves.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

SITES = [
    ("app/pipeline/separate.py", "the Demucs worker"),
    ("app/pipeline/vocal_split.py", "the vocal-split worker"),
    ("app/api/jobs.py", "ffprobe on upload"),
]


@pytest.mark.parametrize(("path", "what"), SITES)
def test_decoding_is_pinned_not_left_to_the_locale(path: str, what: str):
    src = Path(path).read_text(encoding="utf-8")
    assert "text=True" in src, f"{path} no longer spawns a text-mode child; update this test"
    assert 'encoding="utf-8"' in src, f"{what} decodes with the locale encoding, not utf-8"
    assert 'errors="replace"' in src, f"{what} would still raise on an undecodable byte"


@pytest.mark.parametrize("path", ["app/pipeline/separate.py", "app/pipeline/vocal_split.py"])
def test_children_are_told_to_write_utf8(path: str):
    """The parent reading utf-8 is only half of it."""
    src = Path(path).read_text(encoding="utf-8")
    assert "PYTHONIOENCODING" in src, f"{path} lets the child pick its own stdio encoding"


def test_a_byte_outside_cp1252_survives_a_round_trip():
    """The actual failure, reproduced. 0x8f is undefined in cp1252, so the old
    configuration raised here instead of returning a string."""
    payload = b"progress: \x8f\x9d\x81 50%\n"

    with pytest.raises(UnicodeDecodeError):
        payload.decode("cp1252")

    # What the code does now.
    assert payload.decode("utf-8", errors="replace")


def test_ffprobe_duration_survives_undecodable_output():
    """A stray byte on stderr must not stop a valid duration being read."""
    from app.api import jobs as jobs_mod

    def fake_run(cmd, **kwargs):
        assert kwargs.get("encoding") == "utf-8"
        assert kwargs.get("errors") == "replace"
        # Exactly what a text-mode pipe hands back once errors="replace" is on.
        return subprocess.CompletedProcess(cmd, 0, "212.5\n", "warn ��\n")

    with patch.object(jobs_mod.subprocess, "run", fake_run):
        assert jobs_mod._probe_duration(Path("whatever.mp3")) == 212.5
