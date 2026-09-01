"""Write the license text of every packaged Python dependency into the bundle.

Listing a dependency's name and the word "MIT" is not what MIT asks for. The
permissive licenses in this stack -- MIT, BSD, Apache-2.0 -- all require the
copyright notice and the license text itself to travel with a binary
distribution, and the hand-written THIRD_PARTY_NOTICES.txt says as much about
itself: "not a substitute for the full license inventory that must be generated
from the final packaged Python runtime".

Generated rather than maintained, because the list is whatever the packaged venv
actually contains. Three hand-edited notices files across three platforms drift
from the lockfile the first time a dependency is added, and nobody notices,
because nothing reads them.

Stdlib only, on purpose: this runs against the bundled interpreter partway
through packaging, before anything has been installed that is not a runtime
dependency.

Usage:  python collect_licenses.py <site-packages-dir> <output-dir>
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# .dist-info/licenses/ is the modern location (PEP 639). Older wheels drop the
# file at the top of .dist-info instead, under any of these names.
LEGACY_NAMES = re.compile(
    r"^(LICEN[CS]E|COPYING|NOTICE|AUTHORS|COPYRIGHT)([._-].*)?$", re.IGNORECASE
)


def metadata_field(dist_info: Path, field: str) -> str:
    meta = dist_info / "METADATA"
    if not meta.is_file():
        return ""
    prefix = f"{field}:".lower()
    for line in meta.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            break  # headers end at the first blank line; the body is the README
        if line.lower().startswith(prefix):
            return line.split(":", 1)[1].strip()
    return ""


def license_files(dist_info: Path) -> list[Path]:
    found = list((dist_info / "licenses").rglob("*")) if (dist_info / "licenses").is_dir() else []
    found += [p for p in dist_info.iterdir() if p.is_file() and LEGACY_NAMES.match(p.name)]
    return sorted(p for p in found if p.is_file())


def main() -> int:
    site_packages = Path(sys.argv[1])
    out = Path(sys.argv[2])
    if not site_packages.is_dir():
        print(f"not a directory: {site_packages}", file=sys.stderr)
        return 1
    out.mkdir(parents=True, exist_ok=True)

    index: list[dict[str, object]] = []
    missing: list[str] = []

    for dist_info in sorted(site_packages.glob("*.dist-info")):
        name = metadata_field(dist_info, "Name") or dist_info.name.split("-")[0]
        version = metadata_field(dist_info, "Version")
        declared = metadata_field(dist_info, "License-Expression") or metadata_field(
            dist_info, "License"
        )
        # A classifier is the only statement of license for a good number of
        # older wheels, and it is more readable than a pasted license body.
        if not declared or len(declared) > 80:
            classifiers = [
                line.split("::")[-1].strip()
                for line in (dist_info / "METADATA")
                .read_text(encoding="utf-8", errors="replace")
                .splitlines()
                if line.startswith("Classifier: License ::")
            ]
            declared = ", ".join(classifiers) or declared[:80] or "see license text"

        texts = license_files(dist_info)
        stem = f"{name}-{version}" if version else name
        written = []
        for src in texts:
            suffix = "" if len(texts) == 1 else f".{src.name}"
            dest = out / f"{stem}{suffix}.txt"
            dest.write_text(src.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
            written.append(dest.name)
        if not written:
            missing.append(stem)
        index.append({"name": name, "version": version, "license": declared, "files": written})

    (out / "index.json").write_text(
        json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    lines = [
        "PACKAGED PYTHON DEPENDENCIES",
        "============================",
        "",
        "Generated from the bundled interpreter at package time. Each entry's",
        "full license text, where the wheel ships one, is in this directory.",
        "",
    ]
    width = max((len(str(e["name"])) for e in index), default=4)
    for e in index:
        note = "" if e["files"] else "   (no license file in wheel)"
        lines.append(f"{str(e['name']).ljust(width)}  {e['version']}  {e['license']}{note}")
    (out / "INDEX.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"licenses: {len(index)} packages, {len(index) - len(missing)} with license text")
    if missing:
        # Not fatal. Some wheels genuinely ship no license file, and failing the
        # release build over one would be worse than recording the fact.
        print(f"licenses: no text bundled for {len(missing)}: {', '.join(sorted(missing)[:8])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
