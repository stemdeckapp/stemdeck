"""The upload limit is one number, written in three languages.

The server refuses anything larger (app/api/jobs.py). The page refuses it first
so a file is not uploaded only to be turned away (static/js/main.js). And on
Linux, where a dropped file reaches the page through the shell rather than the
WebView, the shell refuses to read anything larger into memory
(desktop/src-tauri/src/dropin.rs).

Each copy used to be held to the others by a comment saying it must match.
If they drift, the failure is quiet in either direction: a limit lower in the
page than on the server refuses files the server would take, and one higher in
the shell than in the page is a backstop that no longer stops anything.
"""

import re
from pathlib import Path

from app.api.jobs import _MAX_UPLOAD_BYTES

ROOT = Path(__file__).resolve().parents[1]

# "400 * 1024 * 1024" and similar products of integer literals, the way all
# three are written.
_PRODUCT = r"(\d+(?:\s*\*\s*\d+)*)"


def _evaluate(product: str) -> int:
    value = 1
    for factor in product.split("*"):
        value *= int(factor.strip())
    return value


def _constant(path: Path, pattern: str) -> int:
    text = path.read_text(encoding="utf-8")
    match = re.search(pattern, text)
    assert match, f"no upload limit found in {path.relative_to(ROOT)}"
    return _evaluate(match.group(1))


def test_the_page_refuses_what_the_server_would_refuse():
    page = _constant(
        ROOT / "static" / "js" / "main.js",
        rf"const MAX_UPLOAD_BYTES\s*=\s*{_PRODUCT}\s*;",
    )
    assert page == _MAX_UPLOAD_BYTES


def test_the_shell_reads_no_more_than_the_page_would_accept():
    shell = _constant(
        ROOT / "desktop" / "src-tauri" / "src" / "dropin.rs",
        rf"pub const MAX_DROPPED_FILE_BYTES:\s*u64\s*=\s*{_PRODUCT}\s*;",
    )
    assert shell == _MAX_UPLOAD_BYTES
