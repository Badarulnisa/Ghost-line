# username-variant-recon

Async username-permutation OSINT tool built on top of the
[WhatsMyName](https://github.com/WebBreacher/WhatsMyName) dataset.

Standard username checkers (including the original WMN checker script) only
check the exact string you give them. In practice, real people rarely use
one consistent handle across every platform — they drop numbers, swap name
order, add their city, or go by a mononym on some sites. This tool generates
realistic candidate variants from seed identity data and checks all of them
concurrently against WMN's ~700-site dataset.

## Why this exists

- WMN's own checker script is sequential — checking ~700 sites for one
  username takes minutes. Checking a dozen variants that way doesn't scale.
- No existing WMN wrapper does variant generation. You either check one
  known handle, or you don't.
- Most username-generation heuristics assume Western `first.last` naming
  conventions. This tool also generates mononym-style and reordered
  candidates common in South Asian / MENA naming conventions, which
  Western-pattern-biased tools under-generate for.

## How it works

1. **`variant_engine.py`** — takes seed identity data (name tokens, a known
   real handle, birth year, location, profession, aliases) and generates a
   deduplicated list of candidate usernames using separator variations,
   name-order permutations, numeric suffixes, and mononym forms.
2. **`wmn_wrapper.py`** — loads WMN's live `wmn-data.json` and checks each
   candidate against every site **concurrently** via `httpx.AsyncClient` +
   `asyncio.Semaphore`, instead of one request at a time.
3. **`reporter.py`** — outputs results as JSON, Markdown, and CSV.

## Installation

```bash
git clone https://github.com/<you>/username-variant-recon.git
cd username-variant-recon
pip install -r requirements.txt
```

## Usage

```bash
# Generate variants from seed data and check all of them
python3 cli.py --name ahmed chaudhary \
    --known-handle chaudharyahmed07 \
    --location lahore \
    --year 1999 \
    --max-variants 100 \
    --output report

# Check exact strings only, no variant generation
python3 cli.py --name someexacthandle --single-only
```

### Options

| Flag | Description |
|---|---|
| `--name` | One or more name tokens (required) |
| `--known-handle` | A confirmed real handle, used as a mutation seed |
| `--location` | City/region to try as prefix/suffix |
| `--profession` | Field/profession token to try as prefix/suffix |
| `--year` | Birth year (also tries 2-digit form) |
| `--extra` | Additional nickname/alias tokens |
| `--max-variants` | Cap on generated variants (default 200) |
| `--single-only` | Skip generation, check exact `--name`/`--known-handle` only |
| `--concurrency` | Max concurrent requests per username (default 30) |
| `--no-refresh` | Use cached dataset instead of fetching latest |

Output: `report.json`, `report.md`, `report.csv`.

## ⚠️ Interpreting results — read this

**A hit means a username exists on that site. It does not mean it's the
same person.** Common usernames get reused/squatted constantly. Before
drawing any conclusion from a hit:

- Compare profile photo, bio text, and posting activity against your
  target's known-confirmed profiles
- Check for cross-links (does the profile link back to other confirmed
  accounts?)
- Treat single-site hits on generic sites with skepticism; treat hits
  clustered across niche/related sites with more confidence

This tool surfaces candidates for manual verification. It does not, and
cannot, confirm identity on its own.

## Ethics & legal use

This tool only checks **public existence** of a username via each site's
normal profile-lookup response — the same request your browser makes when
you visit a profile URL. It does not authenticate, bypass access controls,
or retrieve private data.

Use this only against usernames/identities you have a legitimate reason to
investigate (your own accounts, authorized engagements, or personal
research where you are not violating platform ToS or harassment/stalking
laws in your jurisdiction). Do not use this to enable harassment, doxxing,
or stalking.

## Roadmap

- [x] v1: variant generation + async WMN checking + JSON/MD/CSV reports
- [ ] Cross-platform correlation: perceptual-hash profile photo comparison
      (`imagehash`) + fuzzy bio/display-name matching (`rapidfuzz`) to
      auto-flag which hits are likely the same identity
- [ ] Graph output (NetworkX / pyvis) — visual entity map of
      username → sites → correlated identity clusters; Maltego-compatible
      CSV export
- [ ] Wayback Machine fallback — check CDX API for historical profile
      existence on sites that now return "not found" (deleted/deactivated
      accounts)
- [ ] Site reliability scoring — weight hits using WMN's known
      false-positive-prone site list
- [ ] Stealth mode — randomized delay + UA rotation profile, alongside a
      fast/max-concurrency profile
- [ ] Culturally-aware variant generation beyond South Asian/MENA
      mononym patterns — configurable naming-convention profiles

## License

MIT — see [LICENSE](LICENSE).
