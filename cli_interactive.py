"""
cli_interactive.py — Ghost-line, iterative pivot mode

Each cycle: assess what seeds are known, show a recommended strategy,
let the user confirm or override, run the check, show hits, let the
user pick pivots (new handles/emails/locations found) to fold into
the next cycle. State persists to investigation.json throughout.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from investigation import load_or_create, save
from discovery_loop import assess_strategy, run_cycle, accept_pivot
from wmn_wrapper import fetch_dataset


def _prompt_initial_seeds(inv) -> None:
    if inv.cycle > 0 or inv.names or inv.handles or inv.emails:
        return  # resuming an existing investigation

    print("New investigation — enter what you know (press Enter to skip any field).\n")

    names_raw = input("Name (space-separated tokens, blank if unknown): ").strip()
    if names_raw:
        inv.names = names_raw.split()

    handles_raw = input("Known handle(s), comma-separated (blank if none): ").strip()
    if handles_raw:
        inv.handles.extend(h.strip() for h in handles_raw.split(",") if h.strip())

    email = input("Email address (blank if none): ").strip()
    if email:
        inv.emails.append(email)

    location = input("Location/city (blank if unknown): ").strip()
    if location:
        inv.locations.append(location)

    year = input("Birth year, 4-digit (blank if unknown): ").strip()
    if year:
        inv.birth_year = year

    if not (inv.names or inv.handles or inv.emails):
        print("\n[!] Need at least one of: name, handle, or email. Exiting.")
        sys.exit(1)


def _choose_targets(rec) -> list[str]:
    print(f"\n[Strategy] {rec.reasoning}\n")

    if rec.default_choice == "none":
        return []

    print(f"  direct   -> check {len(rec.direct_targets)} known handle(s) exactly as given")
    if rec.variant_targets:
        print(f"  variants -> generate & check {len(rec.variant_targets)} guessed usernames")
        print(f"  both     -> direct + variants combined")
    print(f"  skip     -> skip this cycle")

    choice = input(f"\nChoice [{rec.default_choice}]: ").strip().lower() or rec.default_choice

    if choice == "direct":
        return rec.direct_targets
    if choice == "variants":
        return rec.variant_targets
    if choice == "both":
        return list(dict.fromkeys(rec.direct_targets + rec.variant_targets))
    return []


def _collect_pivots(inv, flat_hits) -> None:
    if flat_hits:
        print()
        for i, (u, h) in enumerate(flat_hits):
            print(f"[{i}] {u} -> {h.site_name} ({h.url})")

        picks = input("\nLegit hit index(es) to pivot on, comma-separated (blank for none): ").strip()
        if picks:
            for p in picks.split(","):
                p = p.strip()
                if not p.isdigit() or int(p) >= len(flat_hits):
                    continue
                u, h = flat_hits[int(p)]
                accept_pivot(inv, source=f"hit:{u}:{h.site_name}", value=u, kind="handle")

    extra_handle = input("Any other handle you found manually (blank if none): ").strip()
    if extra_handle:
        accept_pivot(inv, source="manual", value=extra_handle, kind="handle")

    extra_email = input("Any email address found (blank if none): ").strip()
    if extra_email:
        accept_pivot(inv, source="manual", value=extra_email, kind="email")

    extra_location = input("Any new location found (blank if none): ").strip()
    if extra_location:
        accept_pivot(inv, source="manual", value=extra_location, kind="location")


async def main():
    inv = load_or_create("investigation.json", case_name="case_001")
    _prompt_initial_seeds(inv)
    save(inv, "investigation.json")

    dataset = await fetch_dataset(refresh=True)

    while True:
        print(f"\n=== Cycle {inv.cycle + 1} ===")
        rec = assess_strategy(inv)
        targets = _choose_targets(rec)

        if not targets:
            print("Nothing to check this cycle.")
        else:
            results = await run_cycle(inv, dataset, targets, concurrency=60, timeout=10.0)
            save(inv, "investigation.json")
            flat_hits = [(u, h) for u, r in results.items() for h in r.hits]
            if not flat_hits:
                print("No hits this cycle.")
            _collect_pivots(inv, flat_hits)
            save(inv, "investigation.json")

        cont = input("\nRun another cycle? [y/N]: ").strip().lower()
        if cont != "y":
            break

    print(f"\n[*] Saved to investigation.json ({inv.cycle} cycle(s) run, {len(inv.leads)} lead(s) recorded).")


if __name__ == "__main__":
    asyncio.run(main())
