"""
reporter.py
Formats scan results (dict[username -> UsernameResult]) into JSON, Markdown,
and CSV output for downstream use (documentation, sharing, further tooling).

v2 change: results are now UsernameResult objects (hits + failed_sites +
checked_count + elapsed) instead of plain list[SiteHit]. This lets reports
distinguish "checked and confirmed absent" from "site never responded,
outcome unknown" -- previously both collapsed to the same "0 hits" with
no way to tell which. See wmn_wrapper.check_many_usernames_detailed.

v3 change: all writers now go through _ensure_parent + explicit utf-8
encoding, and share a single _flatten() helper instead of three
near-identical iteration blocks. Previously Path.write_text()/open()
used the platform default encoding, which mangled or raised on
non-ASCII usernames/site names on Windows, and a missing parent
directory raised an unhandled OSError with no context pointing back
at --output.
"""

from __future__ import annotations
import csv
import json
from pathlib import Path
from typing import Iterator

from wmn_wrapper import UsernameResult, SiteHit


class ReportWriteError(RuntimeError):
    """Raised when a report can't be written, with the offending path attached."""


def _ensure_parent(path: str) -> Path:
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ReportWriteError(f"Could not create output directory {p.parent}: {e!r}") from e
    return p


def _flatten(results: dict[str, UsernameResult]) -> Iterator[tuple[str, UsernameResult]]:
    """Single shared iteration order so all three writers stay in sync."""
    for username, r in results.items():
        yield username, r


def write_json_report(results: dict[str, UsernameResult], path: str) -> None:
    p = _ensure_parent(path)
    serializable = {
        username: {
            "hits": [hit.to_dict() for hit in r.hits],
            "failed_sites": r.failed_sites,
            "checked_count": r.checked_count,
            "hit_count": r.hit_count,
            "failure_count": r.failure_count,
            "elapsed_seconds": round(r.elapsed, 2),
        }
        for username, r in _flatten(results)
    }
    try:
        p.write_text(json.dumps(serializable, indent=2), encoding="utf-8")
    except OSError as e:
        raise ReportWriteError(f"Could not write JSON report to {p}: {e!r}") from e


def write_csv_report(results: dict[str, UsernameResult], path: str) -> None:
    p = _ensure_parent(path)
    try:
        with open(p, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["username", "site_name", "category", "url", "status"])
            for username, r in _flatten(results):
                for hit in r.hits:
                    writer.writerow([hit.username, hit.site_name, hit.category, hit.url, "hit"])
                for site_name in r.failed_sites:
                    writer.writerow([username, site_name, "", "", "unknown (no response)"])
    except OSError as e:
        raise ReportWriteError(f"Could not write CSV report to {p}: {e!r}") from e


def write_markdown_report(results: dict[str, UsernameResult], path: str) -> None:
    p = _ensure_parent(path)
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

    for username, r in _flatten(results):
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

        by_category: dict[str, list[SiteHit]] = {}
        for hit in r.hits:
            by_category.setdefault(hit.category, []).append(hit)

        for category, cat_hits in sorted(by_category.items()):
            lines.append(f"\n**{category}**")
            for hit in cat_hits:
                lines.append(f"- [{hit.site_name}]({hit.url})")
        lines.append("")

        if r.failed_sites:
            lines.append(
                f"_{r.failure_count} site(s) did not respond and were not "
                f"checked (see the CSV report for the full list)._\n"
            )

    try:
        p.write_text("\n".join(lines), encoding="utf-8")
    except OSError as e:
        raise ReportWriteError(f"Could not write Markdown report to {p}: {e!r}") from e