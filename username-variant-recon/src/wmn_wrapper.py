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
"""

from __future__ import annotations
import asyncio
import json
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import httpx

WMN_DATA_URL = "https://raw.githubusercontent.com/WebBreacher/WhatsMyName/main/wmn-data.json"
DEFAULT_TIMEOUT = 10.0
DEFAULT_CONCURRENCY = 30
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


async def fetch_dataset(cache_path: str | Path = "wmn-data.json", refresh: bool = True) -> dict:
    """
    Downloads the latest WMN dataset. Falls back to local cache if the
    network fetch fails (offline use / rate limiting).
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
                    f"Could not fetch WMN dataset and no local cache exists: {e}"
                ) from e
            print(f"[!] Live fetch failed ({e}), falling back to cached dataset")

    if not cache_path.exists():
        raise RuntimeError("No cached wmn-data.json found and refresh=False")
    return json.loads(cache_path.read_text())


async def _check_one_site(
    client: httpx.AsyncClient,
    site: dict,
    username: str,
    semaphore: asyncio.Semaphore,
) -> SiteHit | None:
    """Checks a single username against a single site entry. Returns a
    SiteHit if the site's existence signature matches, else None."""

    uri_check = site.get("uri_check", "")
    if "{account}" not in uri_check:
        return None

    check_url = uri_check.replace("{account}", username)

    e_code = str(site.get("e_code", ""))
    e_string = site.get("e_string", "")
    m_code = str(site.get("m_code", ""))
    m_string = site.get("m_string", "")

    headers = {"User-Agent": DEFAULT_USER_AGENT}
    headers.update(site.get("headers", {}))  # per-site overrides from dataset

    async with semaphore:
        try:
            resp = await client.get(
                check_url,
                headers=headers,
                follow_redirects=True,
            )
        except httpx.HTTPError:
            return None

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
            site_name=site.get("name", "unknown"),
            url=pretty_url,
            category=site.get("cat", "uncategorized"),
        )
    return None


async def check_username(
    username: str,
    dataset: dict,
    concurrency: int = DEFAULT_CONCURRENCY,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[SiteHit]:
    """Checks one username against every site in the dataset concurrently."""
    sites = dataset.get("sites", [])
    semaphore = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(timeout=timeout) as client:
        tasks = [_check_one_site(client, site, username, semaphore) for site in sites]
        results = await asyncio.gather(*tasks)

    return [r for r in results if r is not None]


async def check_many_usernames(
    usernames: list[str],
    dataset: dict,
    concurrency: int = DEFAULT_CONCURRENCY,
    timeout: float = DEFAULT_TIMEOUT,
    progress_cb=None,
) -> dict[str, list[SiteHit]]:
    """
    Checks multiple usernames sequentially (each internally concurrent
    across sites) to avoid overwhelming the same target sites with an
    N-username x M-site burst all at once.
    """
    results: dict[str, list[SiteHit]] = {}
    for i, username in enumerate(usernames, 1):
        start = time.monotonic()
        hits = await check_username(username, dataset, concurrency, timeout)
        elapsed = time.monotonic() - start
        results[username] = hits
        if progress_cb:
            progress_cb(i, len(usernames), username, len(hits), elapsed)
    return results
