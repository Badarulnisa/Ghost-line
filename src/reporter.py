"""
reporter.py
Formats scan results (dict[username -> UsernameResult]) into JSON, Markdown,
and CSV output for downstream use (documentation, sharing, further tooling).

v2 change: results are now UsernameResult objects (hits + failed_sites +
checked_count + elapsed) instead of plain list[SiteHit]. This lets reports
distinguish "checked and confirmed absent" from "site never responded,
outcome unknown" -- previously both collapsed to the same "0 hits" with
no way to tell which. See wmn_wrapper.check_many_usernames_detailed.
"""

from __future__ import annotations
import csv
import json
from pathlib import Path

from wmn_wrapper import UsernameResult


def write_json_report(results: dict[str, UsernameResult], path: str) -> None:
    serializable = {
        username: {
            "hits": [hit.to_dict() for hit in r.hits],
            "failed_sites": r.failed_sites,
            "checked_count": r.checked_count,
            "hit_count": r.hit_count,
            "failure_count": r.failure_count,
            "elapsed_seconds": round(r.elapsed, 2),
        }
        for username, r in results.items()
    }
    Path(path).write_text(json.dumps(serializable, indent=2))


def write_csv_report(results: dict[str, UsernameResult], path: str) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["username", "site_name", "category", "url", "status"])
        for username, r in results.items():
            for hit in r.hits:
                writer.writerow([hit.username, hit.site_name, hit.category, hit.url, "hit"])
            for site_name in r.failed_sites:
                writer.writerow([username, site_name, "", "", "unknown (no response)"])


def write_markdown_report(results: dict[str, UsernameResult], path: str) -> None:
    total_hits = sum(r.hit_count for r in results.values())
    total_failed = sum(r.failure_count for r in results.values())
    lines = [
        "# Username Variant Recon Report",
        "",
        f"**Usernames checked:** {len(results)}  ",
        f"**Total hits found:** {total_hits}  ",
        f"**Total unresolved (no response) checks:** {total_failed}",
        "",
        "> ⚠️ Hits below are candidates only. A matching username on a site "
        "does not confirm the same person owns it — verify manually before "
        "drawing conclusions (compare avatar, bio, activity, cross-links).",
        "",
        "> ℹ️ \"Unresolved\" sites did not respond (timeout/connection error) "
        "during this run and were **not** confirmed absent — they're unknown, "
        "not negative. Re-run with a longer `--timeout` or lower "
        "`--concurrency` if a username shows a high unresolved count.",
        "",
    ]

    for username, r in results.items():
        lines.append(f"## `{username}` — {r.hit_count} hit(s), {r.failure_count} unresolved")
        if not r.hits:
            lines.append("_No confirmed matches found._")
            if r.failed_sites:
                lines.append(
                    f"_Note: {r.failure_count} of {r.checked_count} sites did not "
                    f"respond — treat this as an incomplete check, not a clean result._"
                )
            lines.append("")
            continue

        by_category: dict[str, list] = {}
        for hit in r.hits:
            by_category.setdefault(hit.category, []).append(hit)

        for category, cat_hits in sorted(by_category.items()):
            lines.append(f"\n**{category}**")
            for hit in cat_hits:
                lines.append(f"- [{hit.site_name}]({hit.url})")
        lines.append("")

        if r.failed_sites:
            lines.append(
                f"<details><summary>{r.failure_count} unresolved site(s) "
                f"(no response — not checked as absent)</summary>\n"
            )
            for site_name in r.failed_sites:
                lines.append(f"- {site_name}")
            lines.append("\n</details>\n")

    Path(path).write_text("\n".join(lines))