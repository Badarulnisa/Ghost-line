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

v2 changes:
- check_many_usernames_detailed builds one flat task pool across every
  (username, site) pair and shares a single semaphore/client/connection
  pool across all of it, so wall time scales with total (username x
  site) work divided by concurrency, not with username count times
  per-username wall time.
- Connect timeout tightened independently of read timeout.
- Failed/timed-out checks are tracked and returned separately from
  "checked and absent".
- Small random jitter before each request to avoid a synchronized burst.

v3 changes:
- IPv4-only DNS resolution, opt-out via WMN_FORCE_IPV4=0.
- Exception logging switched from str(e) to repr(e).

v4 changes:
- asyncio.gather(..., return_exceptions=True) everywhere tasks are
  fanned out. Previously a single unhandled exception anywhere in the
  (username x site) task pool -- e.g. UnicodeDecodeError on a
  malformed response body -- propagated out of gather() and cancelled
  every other in-flight task, silently discarding a partially-complete
  multi-hour run. Now a per-task exception is recorded as a failed
  check for that (username, site) pair and everything else keeps
  going; unexpected exception types are logged once via repr() so
  they're visible without killing the run.
- check_username and check_many_usernames are now thin wrappers around
  check_many_usernames_detailed instead of separately-maintained copies
  of the same fan-out logic -- previously a fix to one had to be
  manually re-applied to the other two, and nothing enforced that.
- Progress callback no longer does order.index(u) on every completed
  check (O(n) per call, O(n^2) total across a run) -- position is now
  looked up once from a prebuilt index map.
- resp.text access (which can raise UnicodeDecodeError on a malformed
  or mislabeled response body) is now inside the same try/except as
  the request itself, so a bad body is treated as a failed check
  instead of an unhandled exception.
- The IPv4-only monkeypatch is no longer applied as a side effect of
  importing this module. It's applied lazily, once, the first time a
  client is actually built (ensure_ipv4_patch()), so importing
  wmn_wrapper for its dataclasses/types (e.g. from reporter.py or a
  test file) can't silently disable IPv6 for the whole process.
"""

from __future__ import annotations
import asyncio
import json
import os
import random
import socket
import ssl
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import httpx

WMN_DATA_URL = "https://raw.githubusercontent.com/WebBreacher/WhatsMyName/main/wmn-data.json"
DEFAULT_TIMEOUT = 10.0
DEFAULT_CONCURRENCY = 60
DEFAULT_CONNECT_TIMEOUT = 3.0
DEFAULT_JITTER_MS = (10, 150)
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# --- IPv4-only DNS resolution (opt-out via WMN_FORCE_IPV4=0) -------------
# Some environments (notably NAT-mode VMs like the default VMware/VirtualBox
# Kali setup) resolve dual-stack DNS (both A and AAAA records) but only have
# an IPv4 default route. asyncio/httpx will still attempt the IPv6 address
# first for every connection, and under high concurrency many of those
# attempts eat a meaningful chunk of the connect-timeout budget before
# falling back to IPv4. Across hundreds of concurrent per-site connections
# this manifests as most requests going "unresolved" regardless of
# concurrency/timeout tuning.
#
# Monkeypatching socket.getaddrinfo strips AF_INET6 results so every
# resolution done by asyncio's default resolver returns IPv4 only. Safe
# no-op on networks with real IPv6 connectivity. Applied lazily via
# ensure_ipv4_patch() the first time a client is actually built, rather
# than as an import-time side effect, so merely importing this module
# (e.g. from reporter.py, or a test file that only needs SiteHit) can't
# silently disable IPv6 process-wide.
_ipv4_patch_applied = False


def ensure_ipv4_patch() -> None:
    global _ipv4_patch_applied
    if _ipv4_patch_applied:
        return
    _ipv4_patch_applied = True

    if os.environ.get("WMN_FORCE_IPV4", "1") == "0":
        return

    orig_getaddrinfo = socket.getaddrinfo

    def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        return orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = _ipv4_only_getaddrinfo
    print("[*] IPv4-only DNS resolution forced (set WMN_FORCE_IPV4=0 to disable)")
# -------------------------------------------------------------------------


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
    ensure_ipv4_patch()
    cache_path = Path(cache_path)

    if refresh:
        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                resp = await client.get(WMN_DATA_URL)
                resp.raise_for_status()
                data = resp.json()
                cache_path.write_text(json.dumps(data), encoding="utf-8")
                return data
        except (httpx.HTTPError, json.JSONDecodeError) as e:
            if not cache_path.exists():
                raise RuntimeError(
                    f"Could not fetch WMN dataset and no local cache exists: {e!r}"
                ) from e
            print(f"[!] Live fetch failed ({e!r}), falling back to cached dataset")

    if not cache_path.exists():
        raise RuntimeError("No cached wmn-data.json found and refresh=False")
    return json.loads(cache_path.read_text(encoding="utf-8"))


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
      - (None, site_name)      -> request failed/timed out/unparseable body,
                                   outcome unknown
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
            # .text can raise UnicodeDecodeError on a malformed/mislabeled
            # body -- keep it inside the same try so a bad response is a
            # failed check, not an unhandled exception that would (pre-v4)
            # have cancelled every other in-flight task via gather().
            body = resp.text if resp.text else ""
        except (httpx.HTTPError, ssl.SSLError, OSError, UnicodeDecodeError):
            return None, site_name

    status = str(resp.status_code)

    # Existence match: status code matches AND (no string required OR string present)
    exists = (status == e_code) and (not e_string or e_string in body)
    # Missing match: explicit negative signature -- used to rule out false positives
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
    """Shared client tuning so every check function pulls from one place."""
    limits = httpx.Limits(
        max_connections=concurrency * 2,
        max_keepalive_connections=0,  # force a fresh connection per request --
        # under concurrent async load this VM's virtual NIC / httpx's pool
        # reuse was producing intermittent SSL "bad record MAC" errors that
        # a raw curl test (no connection reuse) never hit at the same
        # concurrency. Costs a handshake per request but eliminates the
        # corrupted-stream class of failure entirely. If you're seeing
        # unexpectedly low throughput, this is the first thing to
        # reconsider -- a per-worker client (rather than one shared client
        # with keepalive disabled) may be the better trade-off.
    )
    timeout_config = httpx.Timeout(
        connect=min(timeout, DEFAULT_CONNECT_TIMEOUT),
        read=timeout,
        write=5.0,
        pool=5.0,
    )
    return {"timeout": timeout_config, "limits": limits}


async def check_many_usernames_detailed(
    usernames: list[str],
    dataset: dict,
    concurrency: int = DEFAULT_CONCURRENCY,
    timeout: float = DEFAULT_TIMEOUT,
    progress_cb=None,
) -> dict[str, UsernameResult]:
    """
    Checks every (username, site) pair through one shared semaphore and
    one shared client, instead of fully draining each username before
    starting the next. `concurrency` bounds total in-flight requests
    across the entire username x site matrix, so wall time scales with
    total work / concurrency regardless of how many usernames are queued.

    Returns full UsernameResult objects (hits + failed_sites +
    checked_count + elapsed) so failed/unknown sites can be surfaced in
    reports instead of being indistinguishable from confirmed-absent.

    A single malformed response or unexpected exception in one
    (username, site) task no longer aborts the rest of the run -- see
    v4 notes at the top of this file.
    """
    ensure_ipv4_patch()
    sites = dataset.get("sites", [])
    semaphore = asyncio.Semaphore(concurrency)

    results: dict[str, UsernameResult] = {
        u: UsernameResult(username=u) for u in usernames
    }
    start_times = {u: time.monotonic() for u in usernames}
    remaining = {u: len(sites) for u in usernames}
    position = {u: i + 1 for i, u in enumerate(usernames)}  # O(1) progress lookup

    async with httpx.AsyncClient(**_build_client_kwargs(concurrency, timeout)) as client:

        async def _run_one(u: str, site: dict) -> None:
            r = results[u]
            try:
                hit, failed_name = await _check_one_site(client, site, u, semaphore)
            except Exception as e:  # noqa: BLE001 -- last-resort safety net so
                # one unexpected bug in a single check can never take down the
                # rest of a multi-hour run; still surfaced via repr() logging.
                print(f"[!] Unexpected error checking {u!r} against "
                      f"{site.get('name', 'unknown')!r}: {e!r}")
                hit, failed_name = None, site.get("name", "unknown")

            r.checked_count += 1
            if hit is not None:
                r.hits.append(hit)
            elif failed_name is not None:
                r.failed_sites.append(failed_name)

            remaining[u] -= 1
            if remaining[u] == 0:
                r.elapsed = time.monotonic() - start_times[u]
                if progress_cb:
                    progress_cb(position[u], len(usernames), u, r.hit_count, r.elapsed)

        tasks = [_run_one(u, site) for u in usernames for site in sites]
        # return_exceptions=True: _run_one already catches everything it
        # reasonably can, but this is the backstop -- without it, gather()
        # cancels every sibling task the instant any one of them raises,
        # which previously meant one bad response could discard an
        # otherwise-complete multi-hour run.
        await asyncio.gather(*tasks, return_exceptions=True)

    return results


async def check_many_usernames(
    usernames: list[str],
    dataset: dict,
    concurrency: int = DEFAULT_CONCURRENCY,
    timeout: float = DEFAULT_TIMEOUT,
    progress_cb=None,
) -> dict[str, list[SiteHit]]:
    """
    Thin wrapper around check_many_usernames_detailed for callers that
    only want the plain {username: [SiteHit]} shape. Kept for backwards
    compatibility -- prefer check_many_usernames_detailed directly so you
    also get failed_sites tracking.
    """
    detailed = await check_many_usernames_detailed(
        usernames, dataset, concurrency=concurrency, timeout=timeout, progress_cb=progress_cb
    )
    return {u: r.hits for u, r in detailed.items()}


async def check_username(
    username: str,
    dataset: dict,
    concurrency: int = DEFAULT_CONCURRENCY,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[SiteHit]:
    """
    Thin wrapper around check_many_usernames_detailed for a single
    username. Kept for backwards compatibility / single-username callers.
    """
    detailed = await check_many_usernames_detailed(
        [username], dataset, concurrency=concurrency, timeout=timeout
    )
    return detailed[username].hits