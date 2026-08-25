#!/usr/bin/env python3
"""Build a standalone GitHub release-download report for StemDeck.

GitHub exposes a lifetime download_count for each release asset. It does not
expose downloader IP addresses or countries, so this report deliberately does
not manufacture a geographic breakdown.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import urllib.request
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

REPOSITORY = "stemdeckapp/stemdeck"
PLATFORMS = ("macOS", "Windows", "Linux")
COLORS = {"macOS": "#a78bfa", "Windows": "#38bdf8", "Linux": "#fbbf24"}


def platform_for_asset(name: str) -> str | None:
    lower = name.lower()
    # Count installable application bundles only. Checksums and runtime packs
    # are support artifacts and would inflate the number of app downloads.
    if lower.endswith((".sha256", ".txt", ".sig")) or "runtime" in lower:
        return None
    if "macos" in lower and lower.endswith(".dmg"):
        return "macOS"
    if "windows" in lower and lower.endswith((".zip", ".exe", ".msi")):
        return "Windows"
    if "linux" in lower and lower.endswith((".tar.gz", ".appimage", ".deb", ".rpm")):
        return "Linux"
    return None


def release_rows(releases: list[dict]) -> list[dict]:
    rows = []
    for release in releases:
        counts = defaultdict(int)
        for asset in release.get("assets", []):
            platform = platform_for_asset(asset.get("name", ""))
            if platform:
                counts[platform] += int(asset.get("download_count", 0))
        if counts:
            rows.append(
                {
                    "tag": release.get("tag_name", "untagged"),
                    "date": (release.get("published_at") or release.get("created_at") or "")[:10],
                    **{platform: counts[platform] for platform in PLATFORMS},
                }
            )
    return sorted(rows, key=lambda row: (row["date"], row["tag"]))


def fetch_releases(repository: str) -> list[dict]:
    url = f"https://api.github.com/repos/{repository}/releases?per_page=100"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "release-download-chart"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return json.load(response)


def chart_svg(rows: list[dict]) -> str:
    width, height = 1040, 430
    left, right, top, bottom = 72, 24, 28, 76
    plot_w, plot_h = width - left - right, height - top - bottom
    maximum = max((row[p] for row in rows for p in PLATFORMS), default=1)
    maximum = max(maximum, 1)
    x = lambda i: left + (plot_w / max(len(rows) - 1, 1)) * i
    y = lambda value: top + plot_h - (value / maximum) * plot_h
    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Downloads per release by operating system">'
    ]
    for step in range(5):
        value = round(maximum * step / 4)
        yy = y(value)
        parts.append(
            f'<line x1="{left}" y1="{yy:.1f}" x2="{width - right}" y2="{yy:.1f}" class="grid"/>'
        )
        parts.append(
            f'<text x="{left - 12}" y="{yy + 4:.1f}" text-anchor="end" class="axis">{value:,}</text>'
        )
    for index, row in enumerate(rows):
        xx = x(index)
        parts.append(
            f'<text x="{xx:.1f}" y="{height - bottom + 24}" text-anchor="end" transform="rotate(-35 {xx:.1f} {height - bottom + 24})" class="axis">{html.escape(row["tag"])}</text>'
        )
    for platform in PLATFORMS:
        points = " ".join(f"{x(i):.1f},{y(row[platform]):.1f}" for i, row in enumerate(rows))
        parts.append(
            f'<polyline points="{points}" fill="none" stroke="{COLORS[platform]}" stroke-width="3"/>'
        )
        for i, row in enumerate(rows):
            parts.append(
                f'<circle cx="{x(i):.1f}" cy="{y(row[platform]):.1f}" r="5" fill="{COLORS[platform]}"><title>{html.escape(row["tag"])} · {platform}: {row[platform]:,}</title></circle>'
            )
    parts.append("</svg>")
    return "".join(parts)


def render(releases: list[dict], repository: str) -> str:
    rows = release_rows(releases)
    totals = {platform: sum(row[platform] for row in rows) for platform in PLATFORMS}
    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    legend = "".join(
        f'<span><i style="background:{COLORS[p]}"></i>{p} <b>{totals[p]:,}</b></span>'
        for p in PLATFORMS
    )
    table_rows = "".join(
        f"<tr><td>{html.escape(row['tag'])}</td><td>{row['date']}</td>"
        + "".join(f"<td>{row[p]:,}</td>" for p in PLATFORMS)
        + "</tr>"
        for row in reversed(rows)
    )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>StemDeck release downloads</title><style>
:root{{--bg:#090d16;--panel:#111827;--text:#e5e7eb;--muted:#94a3b8;--line:#263244}}*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:15px Inter,system-ui,sans-serif}}main{{max-width:1160px;margin:auto;padding:48px 28px}}h1{{font-size:34px;margin:0 0 8px}}p{{color:var(--muted);line-height:1.55}}.panel{{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:24px;margin:24px 0}}.legend{{display:flex;gap:24px;flex-wrap:wrap}}.legend span{{color:var(--muted)}}.legend i{{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:8px}}.legend b{{color:var(--text);margin-left:5px}}svg{{width:100%;height:auto;margin-top:14px;overflow:visible}}.grid{{stroke:var(--line);stroke-width:1}}.axis{{fill:var(--muted);font-size:12px}}table{{width:100%;border-collapse:collapse}}th,td{{padding:11px 13px;border-bottom:1px solid var(--line);text-align:right}}th{{color:var(--muted);font-weight:600}}th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){{text-align:left}}.note{{border-left:3px solid #fbbf24;padding-left:16px}}a{{color:#38bdf8}}@media(max-width:700px){{main{{padding:28px 14px}}.panel{{padding:14px;overflow:auto}}table{{min-width:620px}}}}
</style></head><body><main><h1>Release downloads</h1><p>Lifetime GitHub release-asset downloads by operating system · generated {generated}</p>
<section class="panel"><div class="legend">{legend}</div>{chart_svg(rows)}</section>
<section class="panel"><h2>By release</h2><table><thead><tr><th>Release</th><th>Published</th><th>macOS</th><th>Windows</th><th>Linux</th></tr></thead><tbody>{table_rows}</tbody></table></section>
<section class="panel note"><h2>Country data is unavailable</h2><p>The GitHub Releases API publishes a lifetime count for each asset, but no downloader location or country. A country chart requires first-party download telemetry (for example, a redirect endpoint or CDN logs) with an explicit privacy policy. GitHub counts are cumulative and do not provide a daily history; save regular snapshots if you need downloads over time.</p></section>
<p>Source: <a href="https://github.com/{html.escape(repository)}/releases">github.com/{html.escape(repository)}/releases</a>. Counts include app packages only; checksum, signature, and runtime-support assets are excluded.</p></main></body></html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=REPOSITORY)
    parser.add_argument("--input", type=Path, help="Read GitHub Releases API JSON from a file")
    parser.add_argument("--output", type=Path, default=Path(".docs/release-downloads.html"))
    args = parser.parse_args()
    releases = (
        json.loads(args.input.read_text(encoding="utf-8"))
        if args.input
        else fetch_releases(args.repository)
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(releases, args.repository), encoding="utf-8")
    print(f"Wrote {args.output} from {len(releases)} releases")


if __name__ == "__main__":
    main()
