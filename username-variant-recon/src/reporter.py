"""
reporter.py
Formats scan results (dict[username -> list[SiteHit]]) into JSON, Markdown,
and CSV output for downstream use (documentation, sharing, further tooling).
"""

from __future__ import annotations
import csv
import json
from pathlib import Path

from wmn_wrapper import SiteHit


def write_json_report(results: dict[str, list[SiteHit]], path: str) -> None:
    serializable = {
        username: [hit.to_dict() for hit in hits]
        for username, hits in results.items()
    }
    Path(path).write_text(json.dumps(serializable, indent=2))


def write_csv_report(results: dict[str, list[SiteHit]], path: str) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["username", "site_name", "category", "url"])
        for username, hits in results.items():
            for hit in hits:
                writer.writerow([hit.username, hit.site_name, hit.category, hit.url])


def write_markdown_report(results: dict[str, list[SiteHit]], path: str) -> None:
    total_hits = sum(len(hits) for hits in results.values())
    lines = [
        "# Username Variant Recon Report",
        "",
        f"**Usernames checked:** {len(results)}  ",
        f"**Total hits found:** {total_hits}",
        "",
        "> ⚠️ Hits below are candidates only. A matching username on a site "
        "does not confirm the same person owns it — verify manually before "
        "drawing conclusions (compare avatar, bio, activity, cross-links).",
        "",
    ]

    for username, hits in results.items():
        lines.append(f"## `{username}` — {len(hits)} hit(s)")
        if not hits:
            lines.append("_No matches found._\n")
            continue

        by_category: dict[str, list[SiteHit]] = {}
        for hit in hits:
            by_category.setdefault(hit.category, []).append(hit)

        for category, cat_hits in sorted(by_category.items()):
            lines.append(f"\n**{category}**")
            for hit in cat_hits:
                lines.append(f"- [{hit.site_name}]({hit.url})")
        lines.append("")

    Path(path).write_text("\n".join(lines))
