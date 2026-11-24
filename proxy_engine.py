"""
proxy_engine.py — fetch, filter and verify proxies.

Kept dependency-light (requests only) and thread-safe so it can be shared by
the CLI, the bot and the FastAPI mini-app backend.
"""
from __future__ import annotations

import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, wait as wait_futures
from dataclasses import asdict, dataclass
from typing import Any, Optional

import requests

# Upstream data provider (kept as-is — it is the source of the proxy list).
PROXY_API_BASE = "https://hproxy.com/api/proxy-list"
CHECK_URL = "https://api.ipify.org?format=json"
# Endpoints tried in order for the sustained speed sample (first working one wins).
# cloudflare.com serves a heavy page and is reachable through most free proxies.
SPEED_URLS = (
    "https://www.cloudflare.com/",
    "https://www.google.com",
    "https://www.wikipedia.org",
    "http://speedtest.tele2.net/100KB.zip",
    "http://mirrors.aliyun.com/ubuntu/ls-lR.gz",
)
CHECK_TIMEOUT = 4
SPEED_TIMEOUT = 5
# How long we keep downloading through a proxy to measure sustained throughput.
SPEED_BUDGET = 5.0
# Quality tiers: minimum sustained KB/s required for each label.
TIER_TURBO = 512.0   # ~GB/h class
TIER_FAST = 128.0    # ~hundreds of MB/h
TIER_OK = 32.0

PROTOCOLS = ("http", "https", "socks4", "socks5")
ANONYMITY = ("elite", "anonymous", "transparent")


@dataclass
class Proxy:
    ip: str
    port: int
    protocol: str
    anonymity: str = ""
    country_code: str = ""
    city: str = ""
    asn_org: str = ""
    latency_ms: int = 0
    uptime_24h: float = 0.0
    alive: bool = False
    test_latency: float = 0.0
    speed_kbps: float = 0.0
    bytes_transferred: float = 0.0
    transfer_1h_mb: float = 0.0
    tier: str = ""
    provider_status: str = ""
    reliability: str = ""

    @property
    def url(self) -> str:
        return f"{self.protocol}://{self.ip}:{self.port}"

    @property
    def address(self) -> str:
        return f"{self.ip}:{self.port}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["url"] = self.url
        d["address"] = self.address
        return d


def fetch_proxies(
    country: str = "IN",
    protocol: Optional[str] = None,
    anonymity: Optional[str] = None,
    limit: int = 100,
    recent: bool = True,
) -> list[Proxy]:
    """Fetch proxies from the upstream free API. Returns [] on failure."""
    params: dict[str, Any] = {
        "format": "json",
        "country": (country or "IN").lower(),
        "limit": max(1, min(int(limit), 500)),
    }
    if protocol:
        params["protocol"] = protocol.lower()
    if anonymity:
        params["anonymity"] = anonymity.lower()
    if recent:
        params["recent"] = "true"

    try:
        resp = requests.get(PROXY_API_BASE, params=params, timeout=30)
        if resp.status_code == 429:
            retry = int(resp.headers.get("Retry-After", "5") or 5)
            time.sleep(min(retry, 15))
            resp = requests.get(PROXY_API_BASE, params=params, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, list):
            payload = payload.get("data", []) if isinstance(payload, dict) else []
    except Exception:
        return []

    out: list[Proxy] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        protos = item.get("protocols") or item.get("protocol") or []
        if isinstance(protos, str):
            protos = [protos]
        if not protos:
            protos = [protocol or "http"]

        ip = str(item.get("ip", "")).strip()
        try:
            port = int(item.get("port", 0))
        except (TypeError, ValueError):
            port = 0
        if not ip or not port:
            continue

        for proto in protos:
            p = str(proto).lower()
            if p not in PROTOCOLS:
                continue
            try:
                latency = int(item.get("latency_ms", 0) or 0)
            except (TypeError, ValueError):
                latency = 0
            try:
                uptime = float(item.get("uptime_24h", 0) or 0)
            except (TypeError, ValueError):
                uptime = 0.0
            out.append(
                Proxy(
                    ip=ip,
                    port=port,
                    protocol=p,
                    anonymity=str(item.get("anonymity", "") or ""),
                    country_code=str(item.get("country_code", "") or ""),
                    city=str(item.get("city", "") or ""),
                    asn_org=str(item.get("asn_org", "") or ""),
                    latency_ms=latency,
                    uptime_24h=uptime,
                    provider_status=str(item.get("status", "") or ""),
                    reliability=str(item.get("reliability", "") or ""),
                )
            )

    seen: set[tuple[str, int, str]] = set()
    unique: list[Proxy] = []
    for p in out:
        key = (p.ip, p.port, p.protocol)
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


def _speed_sample(cfg: dict) -> tuple[int, float]:
    """Download through `cfg` for a few seconds. Returns (bytes, seconds)."""
    deadline = time.perf_counter() + SPEED_BUDGET
    best_bytes, best_seconds = 0, 1.0

    for url in SPEED_URLS:
        if time.perf_counter() >= deadline:
            break
        total, t0 = 0, time.perf_counter()
        try:
            while time.perf_counter() < deadline:
                r = requests.get(url, proxies=cfg, timeout=SPEED_TIMEOUT)
                if r.status_code != 200:
                    break
                total += len(r.content)
                if time.perf_counter() - t0 >= 2.0:
                    break  # enough of a sample from this endpoint
        except Exception:
            pass
        seconds = max(0.001, time.perf_counter() - t0)
        if total > best_bytes and total >= 1024:
            best_bytes, best_seconds = total, seconds
        if best_bytes and time.perf_counter() - t0 >= 1.0:
            break  # got a usable measurement already

    return best_bytes, best_seconds


def _apply_speed(proxy: Proxy, total: int, seconds: float) -> None:
    """Store measured throughput and derive capacity + quality tier."""
    if total > 0:
        proxy.bytes_transferred = float(total)
        proxy.speed_kbps = round((total / 1024) / max(0.001, seconds), 1)
    kbps = proxy.speed_kbps
    proxy.transfer_1h_mb = round(kbps * 3600 / 1024, 1)  # KB/h -> MB/h
    proxy.tier = (
        "TURBO" if kbps >= TIER_TURBO
        else "FAST" if kbps >= TIER_FAST
        else "OK" if kbps >= TIER_OK
        else "SLOW"
    )


def test_proxy(proxy: Proxy) -> bool:
    """Verify a proxy and measure its throughput. Mutates the proxy in place."""
    cfg = {"http": proxy.url, "https": proxy.url}
    try:
        start = time.perf_counter()
        resp = requests.get(CHECK_URL, proxies=cfg, timeout=CHECK_TIMEOUT)
        if resp.status_code == 200:
            proxy.alive = True
            proxy.test_latency = round(time.perf_counter() - start, 2)
            # Body size kept for diagnostics in server logs.
            proxy.bytes_transferred = float(len(resp.content))
            # Sustained download through the proxy to measure real throughput.
            # If nothing can be downloaded, speed stays 0 and the top-up pass
            # in test_proxies() gets a second chance before marking it DEAD.
            total, seconds = _speed_sample(cfg)
            _apply_speed(proxy, total, seconds)
            return True
    except Exception:
        pass
    return False


def test_proxies(
    proxies: list[Proxy],
    workers: int = 80,
    timeout_per: int = 6,
    overall_timeout: Optional[float] = None,
) -> list[Proxy]:
    """Test in parallel. Returns every tested proxy with its alive flag."""
    tested: list[Proxy] = []
    if not proxies:
        return tested
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 120))) as pool:
        futures = {pool.submit(test_proxy, p): p for p in proxies}
        try:
            for fut in as_completed(futures, timeout=overall_timeout):
                p = futures[fut]
                try:
                    fut.result()
                except Exception:
                    pass
                tested.append(p)
        except Exception:
            pass  # deadline reached — keep whatever finished
        # Grace period + salvage: never throw away a proxy that already proved
        # itself alive just because its speed sample was still running.
        pending = [f for f in futures if not f.done()]
        if pending:
            wait_futures(pending, timeout=2.0)
        for fut, p in futures.items():
            if p in tested or fut.cancelled():
                continue
            try:
                fut.result(timeout=0.2)
            except Exception:
                pass
            if fut.done() or p.alive:
                tested.append(p)
            fut.cancel()

    # Quality guarantee: a WORKING proxy must have a measured throughput.
    # Anything that cannot move bytes is demoted to DEAD instead of lying.
    missing = [p for p in tested if p.alive and p.speed_kbps <= 0]
    if missing:
        with ThreadPoolExecutor(max_workers=min(24, len(missing))) as pool:
            samples = pool.map(
                lambda p: _speed_sample({"http": p.url, "https": p.url}), missing
            )
            for p, (total, seconds) in zip(missing, samples):
                _apply_speed(p, total, seconds)
                if p.speed_kbps <= 0:
                    p.alive = False
                    p.speed_kbps = 0.0
                    p.transfer_1h_mb = 0.0
                    if not p.provider_status:
                        p.provider_status = "no throughput"

    tested.sort(key=lambda p: (not p.alive, -p.speed_kbps if p.alive else 999))
    return tested


def get_tested_proxies(
    count: int,
    protocol: Optional[str] = None,
    anonymity: Optional[str] = None,
    country: str = "IN",
    workers: int = 120,
    max_rounds: int = 6,
    time_budget: float = 70.0,
) -> list[Proxy]:
    """Verify proxies and return ONLY working ones, fastest first.

    Keeps fetching and testing in rounds until `count` working proxies are
    collected or the time budget runs out, so the caller gets as close to the
    requested amount as the free pool allows.
    """
    want = max(1, min(int(count), 50))
    deadline = time.monotonic() + time_budget
    results: list[Proxy] = []
    seen: set[str] = set()

    for _ in range(max(1, max_rounds)):
        alive_count = sum(1 for p in results if p.alive)
        if alive_count >= want or deadline - time.monotonic() <= 4:
            break

        fetched = fetch_proxies(
            country=country,
            protocol=protocol,
            anonymity=anonymity,
            limit=300,
        )
        if not fetched:
            continue

        fetched.sort(key=lambda p: p.uptime_24h, reverse=True)
        pool = [p for p in fetched if p.address not in seen][: max(want * 6, 120)]
        if not pool:
            break
        random.shuffle(pool)

        remaining = max(6.0, deadline - time.monotonic() - 10)
        tested = test_proxies(
            pool, workers=workers, timeout_per=CHECK_TIMEOUT, overall_timeout=remaining
        )
        for p in tested:
            if p.address in seen:
                continue
            seen.add(p.address)
            if p.alive:
                results.append(p)

    results.sort(key=lambda p: -p.speed_kbps)
    return results[:want]


def get_working_proxies(
    count: int,
    protocol: Optional[str] = None,
    anonymity: Optional[str] = None,
    country: str = "IN",
    workers: int = 120,
    max_rounds: int = 6,
    time_budget: float = 70.0,
) -> list[Proxy]:
    """Working proxies only (used by the CLI)."""
    return get_tested_proxies(
        count, protocol, anonymity, country, workers, max_rounds, time_budget
    )
