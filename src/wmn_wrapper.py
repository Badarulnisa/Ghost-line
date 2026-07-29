"""
wmn_wrapper.py
Async engine that loads WhatsMyName's wmn-data.json directly and checks
candidate usernames against every site concurrently, instead of shelling
out to the original sequential checker script.

Why rewrite instead of subprocess-wrapping the original script:
- The original checker is sequential (one request at a time) -> ~400 sites
  takes minutes per username. With N variants that's N * minutes.
- httpx.AsyncClient + asyncio.Semaphore gets this down to seconds per
  username while still respecting a concurrency cap (politeness/OPSEC).

v2 changes (see project notes for rationale):
- check_many_usernames no longer drains one username fully before
  starting the next. It builds one flat task pool across every
  (username, site) pair and shares a single semaphore/client/connection
  pool across all of it. This is the actual fix for multi-variant runs
  taking O(usernames) x O(single-username wall time) -- previously a
  200-variant run could take ~2 hours; the global pool caps total
  in-flight requests regardless of how many usernames are queued, so
  wall time scales with total (username x site) work divided by
  concurrency, not with username count times per-username wall time.
- Connect timeout tightened independently of read timeout. Legitimate
  sites open a TCP+TLS handshake in well under a second; there is no
  reason to wait as long to connect as to read a body. Waiting 5-6s to
  fail a connect on a dead site was the dominant cost in slow runs.
- Failed/timed-out checks are now tracked and returned separately from
  "checked and absent" -- a 0-hit report should distinguish "confirmed
  absent everywhere" from "N sites never responded, treat as unknown."
- Small random jitter before each request to avoid a synchronized burst
  hitting the same host pattern (opsec + avoids self-inflicted
  throttling from sites that rate-limit bursty traffic).

v3 changes:
- IPv4-only DNS resolution, opt-out via WMN_FORCE_IPV4=0 (see block
  below). Fixes VMs/networks with dual-stack DNS but no IPv6 route
  (e.g. default VMware/VirtualBox NAT setups), where connections were
  wasting connect-timeout budget on a dead IPv6 path -- observed on a
  Kali VM as ~90%+ of sites reporting "unresolved" regardless of
  concurrency/timeout tuning, while curl (which falls back to IPv4
  automatically) reached the same hosts in under a second. The patch
  is now gated behind an env var and logs once when active, rather
  than silently and permanently disabling IPv6 for the whole process
  the moment this module is imported -- a global, silent monkeypatch
  would otherwise break anything else in the same process that
  legitimately needs IPv6, with no visible signal as to why.
- Exception logging switched from str(e) to repr(e). str() on some
  httpx/socket exceptions returns an empty string even when the
  exception *type* is informative (ConnectError vs. ConnectTimeout vs.
  DNS failure) -- this was directly responsible for a confusing
  "Live fetch failed ()" log line with no actual information in it
  during debugging of the IPv6 issue above.
"""

from __future__ import annotations
import asyncio
import json
import os
import random
import socket
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import httpx

# --- IPv4-only DNS resolution (opt-out via WMN_FORCE_IPV4=0) -------------
# Some environments (notably NAT-mode VMs like the default VMware/VirtualBox
# Kali setup) resolve dual-stack DNS (both A and AAAA records) but only have
# an IPv4 default route -- there is no IPv6 route at all. asyncio/httpx will
# still attempt the IPv6 address first for every connection, and under high
# concurrency many of those attempts eat a meaningful chunk of the
# connect-timeout budget before falling back to IPv4, rather than failing
# instantly the way curl does. Across hundreds of concurrent per-site
# connections this manifests as most requests going "unresolved" regardless
# of concurrency/timeout tuning.
#
# Monkeypatching socket.getaddrinfo to strip AF_INET6 results forces every
# resolution done by asyncio's default resolver to return IPv4 addresses
# only, avoiding the dead route entirely. Safe no-op on networks that do
# have real IPv6 connectivity -- IPv4 still works everywhere IPv6 would.
# Gated behind an env var (default ON) so it's discoverable and reversible
# instead of a silent, permanent, process-wide side effect of importing
# this module -- set WMN_FORCE_IPV4=0 if you're on a network that actually
# needs IPv6 (e.g. an IPv6-only target).
_FORCE_IPV4 = os.environ.get("WMN_FORCE_IPV4", "1") != "0"

if _FORCE_IPV4:
    _orig_getaddrinfo = socket.getaddrinfo

    def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = _ipv4_only_getaddrinfo
    print("[*] IPv4-only DNS resolution forced (set WMN_FORCE_IPV4=0 to disable)")
# -------------------------------------------------------------------------

WMN_DATA_URL = "https://raw.githubusercontent.com/WebBreacher/WhatsMyName/main/wmn-data.json"
DEFAULT_TIMEOUT = 6.0
DEFAULT_CONCURRENCY = 60
DEFAULT_CONNECT_TIMEOUT = 3.0
DEFAULT_JITTER_MS = (10, 150)
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@dataclass
class SiteHit:
    username: str
    site_name: str
    url: str
    category: str
    confidence_note: str = ""  # populated later by correlator/reliability scoring

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class UsernameResult:
    """Everything learned about one username: confirmed hits plus which
    sites failed to respond (so a 0-hit report can't be misread as
    'confirmed absent everywhere' when it's actually 'partially unknown')."""
    username: str
    hits: list[SiteHit] = field(default_factory=list)
    failed_sites: list[str] = field(default_factory=list)
    checked_count: int = 0
    elapsed: float = 0.0

    @property
    def hit_count(self) -> int:
        return len(self.hits)

    @property
    def failure_count(self) -> int:
        return len(self.failed_sites)


async def fetch_dataset(cache_path: str | Path = "wmn-data.json", refresh: bool = True) -> dict:
    """
    Downloads the latest WMN dataset. Falls back to local cache if the
    network fetch fails (offline use / rate limiting / IPv6-less network).
    """
    cache_path = Path(cache_path)

    if refresh:
        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                resp = await client.get(WMN_DATA_URL)
                resp.raise_for_status()
                data = resp.json()
                cache_path.write_text(json.dumps(data))
                return data
        except (httpx.HTTPError, json.JSONDecodeError) as e:
            if not cache_path.exists():
                raise RuntimeError(
                    f"Could not fetch WMN dataset and no local cache exists: {e!r}"
                ) from e
            print(f"[!] Live fetch failed ({e!r}), falling back to cached dataset")

    if not cache_path.exists():
        raise RuntimeError("No cached wmn-data.json found and refresh=False")
    return json.loads(cache_path.read_text())


async def _check_one_site(
    client: httpx.AsyncClient,
    site: dict,
    username: str,
    semaphore: asyncio.Semaphore,
    jitter_ms: tuple[int, int] = DEFAULT_JITTER_MS,
) -> tuple[SiteHit | None, str | None]:
    """
    Checks a single username against a single site entry.

    Returns (hit, failed_site_name):
      - (SiteHit, None)        -> confirmed existence
      - (None, None)           -> checked cleanly, confirmed absent
      - (None, site_name)      -> request failed/timed out, outcome unknown
    """
    uri_check = site.get("uri_check", "")
    site_name = site.get("name", "unknown")
    if "{account}" not in uri_check:
        return None, None

    check_url = uri_check.replace("{account}", username)

    e_code = str(site.get("e_code", ""))
    e_string = site.get("e_string", "")
    m_code = str(site.get("m_code", ""))
    m_string = site.get("m_string", "")

    headers = {"User-Agent": DEFAULT_USER_AGENT}
    headers.update(site.get("headers", {}))  # per-site overrides from dataset

  async with semaphore:
        await asyncio.sleep(random.uniform(*jitter_ms) / 1000)
        try:
            resp = await client.get(
                check_url,
                headers=headers,
                follow_redirects=True,
            )
        except httpx.HTTPError as e:
            with open("debug_failures.log", "a") as f:
                f.write(f"{site_name}\t{type(e).__name__}\t{e!r}\n")
            return None, site_name

    body = resp.text if resp.text else ""
    status = str(resp.status_code)

    # Existence match: status code matches AND (no string required OR string present)
    exists = (status == e_code) and (not e_string or e_string in body)
    # Missing match: explicit negative signature — used to rule out false positives
    missing = (status == m_code) and (not m_string or m_string in body)

    if exists and not missing:
        pretty_url = site.get("uri_pretty", uri_check).replace("{account}", username)
        return SiteHit(
            username=username,
            site_name=site_name,
            url=pretty_url,
            category=site.get("cat", "uncategorized"),
        ), None

    return None, None


def _build_client_kwargs(concurrency: int, timeout: float) -> dict:
    """Shared client tuning so check_username and check_many_usernames
    can't drift out of sync with each other."""
    limits = httpx.Limits(
        max_connections=concurrency * 2,
        max_keepalive_connections=concurrency,
    )
    # Connect timeout is intentionally much shorter than read timeout.
    # A legitimate site opens a TCP+TLS handshake in well under a second;
    # there's no reason to wait as long to connect as to read a body.
    # Dead/unreachable hosts should fail fast instead of eating the full
    # per-request budget.
    timeout_config = httpx.Timeout(
        connect=min(timeout, DEFAULT_CONNECT_TIMEOUT),
        read=timeout,
        write=5.0,
        pool=5.0,
    )
    return {"timeout": timeout_config, "limits": limits}


async def check_username(
    username: str,
    dataset: dict,
    concurrency: int = DEFAULT_CONCURRENCY,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[SiteHit]:
    """
    Checks one username against every site in the dataset concurrently.

    Kept for backwards compatibility / single-username callers. Prefer
    check_many_usernames_detailed for anything checking more than one
    username -- it shares a single semaphore/client across all usernames
    instead of draining one at a time.
    """
    sites = dataset.get("sites", [])
    semaphore = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(**_build_client_kwargs(concurrency, timeout)) as client:
        tasks = [_check_one_site(client, site, username, semaphore) for site in sites]
        results = await asyncio.gather(*tasks)

    return [hit for hit, _failed in results if hit is not None]


async def check_many_usernames(
    usernames: list[str],
    dataset: dict,
    concurrency: int = DEFAULT_CONCURRENCY,
    timeout: float = DEFAULT_TIMEOUT,
    progress_cb=None,
) -> dict[str, list[SiteHit]]:
    """
    Checks every (username, site) pair through one shared semaphore and
    one shared client, instead of fully draining each username before
    starting the next.

    This is the important behavioral change from v1: previously this
    function looped `for username in usernames: await check_username(...)`,
    which meant the *concurrency* setting only ever bounded requests
    within a single username's 700-site sweep -- usernames themselves
    were still fully serialized. For a 200-variant run that meant
    ~200 x (single-username wall time), i.e. hours. Here, `concurrency`
    bounds total in-flight requests across the entire username x site
    matrix, so wall time scales with total work / concurrency regardless
    of how many usernames are queued.

    Kept for callers that only want the plain {username: [SiteHit]} shape.
    Prefer check_many_usernames_detailed to also get failed_sites tracking.
    """
    sites = dataset.get("sites", [])
    semaphore = asyncio.Semaphore(concurrency)

    results: dict[str, UsernameResult] = {
        u: UsernameResult(username=u) for u in usernames
    }
    start_times = {u: time.monotonic() for u in usernames}
    remaining = {u: len(sites) for u in usernames}
    order = list(usernames)

    async with httpx.AsyncClient(**_build_client_kwargs(concurrency, timeout)) as client:

        async def _run_one(u: str, site: dict):
            hit, failed_name = await _check_one_site(client, site, u, semaphore)
            r = results[u]
            r.checked_count += 1
            if hit is not None:
                r.hits.append(hit)
            elif failed_name is not None:
                r.failed_sites.append(failed_name)

            remaining[u] -= 1
            if remaining[u] == 0:
                r.elapsed = time.monotonic() - start_times[u]
                if progress_cb:
                    progress_cb(order.index(u) + 1, len(usernames), u, r.hit_count, r.elapsed)

        tasks = [
            asyncio.ensure_future(_run_one(u, site))
            for u in usernames
            for site in sites
        ]
        await asyncio.gather(*tasks)

    return {u: r.hits for u, r in results.items()}


async def check_many_usernames_detailed(
    usernames: list[str],
    dataset: dict,
    concurrency: int = DEFAULT_CONCURRENCY,
    timeout: float = DEFAULT_TIMEOUT,
    progress_cb=None,
) -> dict[str, UsernameResult]:
    """
    Same as check_many_usernames but returns full UsernameResult objects
    (hits + failed_sites + checked_count + elapsed) instead of flattening
    to a plain hit list. Use this from cli.py / reporter.py so
    failed/unknown sites can be surfaced in reports instead of being
    indistinguishable from confirmed-absent.
    """
    sites = dataset.get("sites", [])
    semaphore = asyncio.Semaphore(concurrency)

    results: dict[str, UsernameResult] = {
        u: UsernameResult(username=u) for u in usernames
    }
    start_times = {u: time.monotonic() for u in usernames}
    remaining = {u: len(sites) for u in usernames}
    order = list(usernames)

    async with httpx.AsyncClient(**_build_client_kwargs(concurrency, timeout)) as client:

        async def _run_one(u: str, site: dict):
            hit, failed_name = await _check_one_site(client, site, u, semaphore)
            r = results[u]
            r.checked_count += 1
            if hit is not None:
                r.hits.append(hit)
            elif failed_name is not None:
                r.failed_sites.append(failed_name)

            remaining[u] -= 1
            if remaining[u] == 0:
                r.elapsed = time.monotonic() - start_times[u]
                if progress_cb:
                    progress_cb(order.index(u) + 1, len(usernames), u, r.hit_count, r.elapsed)

        tasks = [
            asyncio.ensure_future(_run_one(u, site))
            for u in usernames
            for site in sites
        ]
        await asyncio.gather(*tasks)

    return results