"""
wmn_wrapper.py
Async engine that loads WhatsMyName's wmn-data.json directly and checks
candidate usernames against every site concurrently, instead of shelling
out to the original sequential checker script.

*** TEMPORARY DIAGNOSTIC BUILD ***
_check_one_site logs the exception type/repr for every failed request to
debug_failures.log. This is for one-off diagnosis of why a run reports a
high unresolved count -- strip the logging block back out (see the
"DEBUG:" comments below) once the cause is confirmed and fixed. Everything
else in this file is identical to the non-debug version already in the
repo.
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
    confidence_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class UsernameResult:
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
    headers.update(site.get("headers", {}))

    async with semaphore:
        await asyncio.sleep(random.uniform(*jitter_ms) / 1000)
        try:
            resp = await client.get(
                check_url,
                headers=headers,
                follow_redirects=True,
            )
        except httpx.HTTPError as e:
            # DEBUG: temporary diagnostic logging -- remove this block once
            # the cause of unresolved-site counts is confirmed and fixed.
            with open("debug_failures.log", "a") as f:
                f.write(f"{site_name}\t{type(e).__name__}\t{e!r}\n")
            return None, site_name

    body = resp.text if resp.text else ""
    status = str(resp.status_code)

    exists = (status == e_code) and (not e_string or e_string in body)
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
    limits = httpx.Limits(
        max_connections=concurrency * 2,
        max_keepalive_connections=concurrency,
    )
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
    sites = dataset.get("sites", [])
    semaphore = asyncio.Semaphore(concurrency)

    results: dict[str, UsernameResult] = {u: UsernameResult(username=u) for u in usernames}
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
    sites = dataset.get("sites", [])
    semaphore = asyncio.Semaphore(concurrency)

    results: dict[str, UsernameResult] = {u: UsernameResult(username=u) for u in usernames}
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