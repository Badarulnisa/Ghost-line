"""
cli_interactive.py — Ghost-line, iterative pivot mode

Design: one linear story, not a menu tree.

  1. What do you know? (name / handle / email / location / birth year)
  2. Search.
  3. Here's what came up -- what's actually real?
  4. What you confirmed becomes next cycle's seed. Repeat or stop.

Everything else (report export, resuming a saved case) hangs off that
spine instead of branching the main flow.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from investigation import load_or_create, save
from discovery_loop import assess_strategy, run_cycle, accept_pivot
from wmn_wrapper import fetch_dataset, UsernameResult, SiteHit
from reporter import write_json_report, write_markdown_report, write_csv_report

VERIFY_REMINDER = (
    "Remember: these are candidates, not confirmed matches. A hit only means an\n"
    "account with that exact username exists on that site -- open the link and\n"
    "check the photo/bio/activity before trusting it's the same person.\n"
)


def ask(prompt: str) -> str:
    return input(prompt).strip()


def yes(prompt: str, default_no: bool = True) -> bool:
    suffix = " [y/N]: " if default_no else " [Y/n]: "
    ans = ask(prompt + suffix).lower()
    return ans == "y" if default_no else ans != "n"


# ---------------------------------------------------------------------------
# Step 1: seed collection (only asked once per fresh case)
# ---------------------------------------------------------------------------

def collect_seeds(inv) -> None:
    if inv.cycle > 0 or inv.names or inv.handles or inv.emails:
        print(f"Resuming '{inv.case_name}' -- {inv.cycle} cycle(s) so far, "
              f"{len(inv.leads)} confirmed lead(s).\n")
        return

    print(
        "Ghost-line\n"
        "Tell me whatever you already know about the person -- a name, a\n"
        "known username, an email, a city, a birth year. Nothing is required\n"
        "except at least one of these. Press Enter to skip anything.\n"
    )

    names_raw = ask("Full name (space-separated, e.g. 'ahmed chaudhary'): ")
    if names_raw:
        inv.names = names_raw.split()

    handles_raw = ask("Known username(s), comma-separated if more than one: ")
    if handles_raw:
        for h in handles_raw.split(","):
            h = h.strip().lstrip("@")
            if h:
                inv.handles.append(h)

    email = ask("Email address: ")
    if email:
        inv.emails.append(email)

    location = ask("City or region: ")
    if location:
        inv.locations.append(location)

    year = ask("Birth year (4-digit): ")
    if year:
        inv.birth_year = year

    if not (inv.names or inv.handles or inv.emails):
        print("\nNeed at least a name, handle, or email to start. Exiting.")
        sys.exit(1)

    print()


# ---------------------------------------------------------------------------
# Step 2: decide what to search this cycle
# ---------------------------------------------------------------------------

def plan_search(inv) -> list[str]:
    if not (inv.names or inv.handles):
        return []

    rec = assess_strategy(inv, max_variants=100)

    if rec.default_choice == "none":
        print(rec.reasoning)
        return []

    print(rec.reasoning)

    options = {"direct": rec.direct_targets, "variants": rec.variant_targets}
    options["both"] = list(dict.fromkeys(rec.direct_targets + rec.variant_targets))
    options["skip"] = []

    available = [k for k in ("direct", "variants", "both") if options[k]]
    available.append("skip")

    if len(available) == 1:  # only one real option besides skip
        choice = available[0]
    else:
        print(f"\nOptions: {' / '.join(available)}")
        choice = ask(f"Choice [{rec.default_choice}]: ").strip().lower() or rec.default_choice
        if choice not in available:
            print(f"Unrecognized choice, defaulting to '{rec.default_choice}'.")
            choice = rec.default_choice

    targets = options[choice]
    if not targets:
        return []

    print(f"Checking {len(targets)} username(s) across ~700 sites.")
    if not yes("Proceed", default_no=False):
        return []
    return targets


# ---------------------------------------------------------------------------
# Step 3: show hits, collect what's confirmed real
# ---------------------------------------------------------------------------

def review_hits(inv, results) -> None:
    flat_hits = [(u, h) for u, r in results.items() for h in r.hits]

    if not flat_hits:
        print("\nNo hits this cycle.\n")
        return

    print(f"\n{len(flat_hits)} hit(s) found:\n")
    for i, (u, h) in enumerate(flat_hits):
        print(f"  [{i}] {u}  ->  {h.site_name}  ({h.url})")
    print(f"\n{VERIFY_REMINDER}")

    picks = ask("Which index(es) did you verify are real? (comma-separated, blank for none): ")
    if not picks:
        return
    for p in picks.split(","):
        p = p.strip()
        if p.isdigit() and int(p) < len(flat_hits):
            u, h = flat_hits[int(p)]
            accept_pivot(inv, source=f"hit:{u}:{h.site_name}", value=u, kind="handle")
            print(f"  Added '{u}' to seeds for next cycle.")


def add_manual_findings(inv) -> None:
    if not yes("\nAdd anything found outside this tool (handle/email/location)?"):
        return
    handle = ask("  Handle (blank to skip): ").lstrip("@")
    if handle:
        accept_pivot(inv, source="manual", value=handle, kind="handle")
    email = ask("  Email (blank to skip): ")
    if email:
        accept_pivot(inv, source="manual", value=email, kind="email")
    location = ask("  Location (blank to skip): ")
    if location:
        accept_pivot(inv, source="manual", value=location, kind="location")


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_report(inv) -> None:
    results = {}
    for hits_by_username in inv.all_hits.values():
        for username, hit_dicts in hits_by_username.items():
            r = results.setdefault(username, UsernameResult(username=username))
            for hd in hit_dicts:
                r.hits.append(SiteHit(**hd))

    if not results:
        print("Nothing to export yet.")
        return

    name = ask("Filename (no extension) [report]: ") or "report"
    write_json_report(results, f"{name}.json")
    write_markdown_report(results, f"{name}.md")
    write_csv_report(results, f"{name}.csv")
    print(f"Exported {name}.json / {name}.md / {name}.csv")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def main():
    inv = load_or_create("investigation.json", case_name="case_001")
    collect_seeds(inv)
    save(inv, "investigation.json")

    print("Loading site dataset...")
    dataset = await fetch_dataset(refresh=True)

    while True:
        print(f"\n=== Cycle {inv.cycle + 1} ===")
        targets = plan_search(inv)

        if targets:
            results = await run_cycle(inv, dataset, targets, concurrency=60, timeout=10.0)
            save(inv, "investigation.json")
            review_hits(inv, results)
        else:
            print("Nothing searched this cycle.")

        add_manual_findings(inv)
        save(inv, "investigation.json")

        if yes("\nExport a report now?"):
            export_report(inv)

        if not yes("Run another cycle?"):
            break

    print(f"\nDone. {inv.cycle} cycle(s), {len(inv.leads)} confirmed lead(s) saved "
          f"to investigation.json.")
    if yes("Export final report?"):
        export_report(inv)


if __name__ == "__main__":
    asyncio.run(main())
