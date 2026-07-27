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
"""

from __future__ import annotations
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from variant_engine import SeedIdentity, generate_variants
from wmn_wrapper import fetch_dataset, check_many_usernames
from reporter import write_json_report, write_markdown_report, write_csv_report


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="username-variant-recon",
        description="Generate username variants from seed identity data and "
                    "check them across ~700 sites via the WhatsMyName dataset.",
    )
    p.add_argument("--name", nargs="+", required=True,
                   help="One or more name tokens, e.g. --name ahmed chaudhary")
    p.add_argument("--known-handle", default=None,
                   help="A confirmed real handle to seed further mutation")
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
                   help="Max concurrent requests per username (default 60)")
    p.add_argument("--timeout", type=float, default=6.0,
                   help="Per-request timeout in seconds (default 6)")
    p.add_argument("--no-refresh", action="store_true",
                   help="Use cached wmn-data.json instead of fetching latest")
    p.add_argument("--output", default="report",
                   help="Output file basename (writes .json/.md/.csv)")
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

    results = await check_many_usernames(
        usernames,
        dataset,
        concurrency=args.concurrency,
        timeout=args.timeout,
        progress_cb=_progress,
    )
    return results


def main():
    args = build_arg_parser().parse_args()
    results = asyncio.run(run(args))

    total_hits = sum(len(hits) for hits in results.values())
    print(f"\n[*] Done. {total_hits} total hit(s) across {len(results)} username(s).")

    write_json_report(results, f"{args.output}.json")
    write_markdown_report(results, f"{args.output}.md")
    write_csv_report(results, f"{args.output}.csv")
    print(f"[*] Reports written: {args.output}.json / .md / .csv")


if __name__ == "__main__":
    main()