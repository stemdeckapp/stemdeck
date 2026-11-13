#!/usr/bin/env python3
"""Record one snapshot of release download counts for the stats page.

Run by .github/workflows/download-stats.yml every 10 minutes. It reads the
existing data file (if any), asks the GitHub API for the newest releases,
appends one snapshot with the download count of the latest release and the
5 before it, thins old snapshots, and writes the file back.

Usage: collect.py DATA_JSON [REPO]

Only the standard library is used, so the workflow needs no install step.
A GITHUB_TOKEN in the environment raises the API rate limit; without one the
script still works for a public repository.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

TRACKED = 6  # latest release + the 5 before it
FULL_RES = timedelta(days=14)  # keep every snapshot this far back
THIN_STEP = timedelta(hours=1)  # older than that: one snapshot per hour

# Checksums and small metadata files are fetched by scripts and the app's
# updater, not by people installing StemDeck, so they do not count.
SKIP_SUFFIXES = (".sha256", ".txt", ".json", ".sig", ".asc")


def counts_as_download(asset_name: str) -> bool:
    return not asset_name.lower().endswith(SKIP_SUFFIXES)


def fetch_releases(repo: str) -> list[dict]:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases?per_page=30",
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "stemdeck-download-stats",
        },
    )
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def thin(snapshots: list[dict], now: datetime) -> list[dict]:
    """Keep recent snapshots whole; keep the last one of each older hour."""
    kept: list[dict] = []
    last_bucket = None
    for snap in snapshots:
        t = parse_time(snap["t"])
        if now - t <= FULL_RES:
            kept.append(snap)
            continue
        bucket = int(t.timestamp() // THIN_STEP.total_seconds())
        if bucket == last_bucket and kept:
            kept[-1] = snap  # later snapshot in the same hour wins
        else:
            kept.append(snap)
        last_bucket = bucket
    return kept


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    path = sys.argv[1]
    repo = sys.argv[2] if len(sys.argv) > 2 else "stemdeckapp/stemdeck"

    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        data = {}
    data.setdefault("releases", {})
    data.setdefault("snapshots", [])

    published = [
        r for r in fetch_releases(repo) if not r.get("draft") and r.get("published_at")
    ]
    published.sort(key=lambda r: r["published_at"], reverse=True)
    tracked = published[:TRACKED]
    if not tracked:
        print("No published releases yet; nothing to record.")
        return 0

    now = datetime.now(timezone.utc)
    counts = {}
    for rel in tracked:
        tag = rel["tag_name"]
        counts[tag] = sum(
            a["download_count"] for a in rel.get("assets", []) if counts_as_download(a["name"])
        )
        data["releases"][tag] = {
            "name": rel.get("name") or tag,
            "published_at": rel["published_at"],
            "prerelease": bool(rel.get("prerelease")),
            "url": rel["html_url"],
        }

    data["repo"] = repo
    data["tracked"] = [r["tag_name"] for r in tracked]
    data["updated"] = iso(now)
    data["snapshots"].append({"t": iso(now), "d": counts})
    data["snapshots"] = thin(data["snapshots"], now)

    # Drop releases that are neither tracked nor present in any snapshot.
    seen = {tag for snap in data["snapshots"] for tag in snap["d"]}
    data["releases"] = {k: v for k, v in data["releases"].items() if k in seen}

    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, separators=(",", ":"))
        fh.write("\n")

    summary = ", ".join(f"{tag}={n}" for tag, n in counts.items())
    print(f"Recorded {iso(now)}: {summary} ({len(data['snapshots'])} snapshots kept)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
