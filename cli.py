#!/usr/bin/env python3
"""
cli.py — username-variant-recon

Generates candidate username variants from seed identity data, checks
each against ~700 sites via WhatsMyName's dataset (async, concurrent),
and produces a structured report of hits.

Usage:
    python3 cli.py --name ahmed chaudhary --known-handle chaudharyahmed07 \\
        --location lahore --year 1999 --output report

    python3 cli.py --name ahmed chaudhary --single-only   # skip variant gen,
                                                            # just check the
                                                            # exact names given

v2 changes:
- --timeout help text now matches the actual default (10.0, not 6).
- Output path's parent directory is created up front with a clear
  error message if that fails, instead of letting reporter.py's
  unguarded write raise deep in the call stack with no context.
- The whole run is wrapped in a top-level try/except so a single
  unexpected failure prints a clear message and a non-zero exit code
  instead of a raw traceback -- most of the reliability work already
  happened inside wmn_wrapper.py (gather no longer aborts the run on
  one bad response), this closes the remaining gap at the CLI layer.
- --single-only --help text now notes that --known-handle is still
  included in the check list in that mode.
"""

from __future__ import annotations
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from variant_engine import SeedIdentity, generate_variants
from wmn_wrapper import fetch_dataset, check_many_usernames_detailed
from reporter import write_json_report, write_markdown_report, write_csv_report, ReportWriteError


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="username-variant-recon",
        description="Generate username variants from seed identity data and "
                    "check them across ~700 sites via the WhatsMyName dataset.",
    )
    p.add_argument("--name", nargs="+", required=True,
                   help="One or more name tokens, e.g. --name ahmed chaudhary")
    p.add_argument("--known-handle", default=None,
                   help="A confirmed real handle to seed further mutation. "
                        "Also included as-is in the check list under --single-only.")
    p.add_argument("--location", default=None,
                   help="City/region token to try as a suffix/prefix")
    p.add_argument("--profession", default=None,
                   help="Profession/field token to try as a suffix/prefix")
    p.add_argument("--year", default=None,
                   help="Birth year (4-digit), also tries 2-digit form")
    p.add_argument("--extra", nargs="*", default=[],
                   help="Extra nickname/alias tokens to include")
    p.add_argument("--max-variants", type=int, default=200,
                   help="Cap on number of generated variants (default 200)")
    p.add_argument("--single-only", action="store_true",
                   help="Skip variant generation; only check the exact "
                        "--name/--known-handle strings given")
    p.add_argument("--concurrency", type=int, default=60,
                   help="Max concurrent requests across the whole run "
                        "(shared across all usernames, default 60)")
    p.add_argument("--timeout", type=float, default=10.0,
                   help="Per-request read timeout in seconds (default 10). "
                        "Connect timeout is capped separately at 3s.")
    p.add_argument("--no-refresh", action="store_true",
                   help="Use cached wmn-data.json instead of fetching latest")
    p.add_argument("--output", default="report",
                   help="Output file basename (writes .json/.md/.csv); "
                        "parent directories are created if needed")
    return p


def _progress(i: int, total: int, username: str, hit_count: int, elapsed: float):
    print(f"[{i}/{total}] {username:<30} -> {hit_count} hit(s)  ({elapsed:.1f}s)")


async def run(args: argparse.Namespace) -> dict:
    if args.single_only:
        usernames = list(dict.fromkeys(
            args.name + ([args.known_handle] if args.known_handle else [])
        ))
    else:
        seed = SeedIdentity(
            names=args.name,
            known_handle=args.known_handle,
            birth_year=args.year,
            location=args.location,
            profession=args.profession,
            extra_tokens=args.extra,
        )
        usernames = generate_variants(seed, max_variants=args.max_variants)

    print(f"[*] {len(usernames)} candidate username(s) to check")
    print(f"[*] Loading WMN dataset...")
    dataset = await fetch_dataset(refresh=not args.no_refresh)
    print(f"[*] {len(dataset.get('sites', []))} sites loaded")

    # check_many_usernames_detailed runs every (username, site) pair through
    # one shared semaphore/client rather than draining one username's full
    # site sweep before starting the next, and no longer aborts the whole
    # run if one response is malformed -- see wmn_wrapper.py notes.
    results = await check_many_usernames_detailed(
        usernames,
        dataset,
        concurrency=args.concurrency,
        timeout=args.timeout,
        progress_cb=_progress,
    )
    return results


def main() -> int:
    args = build_arg_parser().parse_args()

    try:
        results = asyncio.run(run(args))
    except Exception as e:
        print(f"[!] Run failed: {e!r}", file=sys.stderr)
        return 1

    total_hits = sum(r.hit_count for r in results.values())
    total_failed = sum(r.failure_count for r in results.values())
    print(f"\n[*] Done. {total_hits} total hit(s) across {len(results)} username(s).")
    if total_failed:
        print(f"[*] {total_failed} site check(s) did not respond — see report for details "
              f"(these are unresolved, not confirmed-absent).")

    try:
        write_json_report(results, f"{args.output}.json")
        write_markdown_report(results, f"{args.output}.md")
        write_csv_report(results, f"{args.output}.csv")
    except ReportWriteError as e:
        print(f"[!] {e}", file=sys.stderr)
        return 1

    print(f"[*] Reports written: {args.output}.json / .md / .csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())