"""Polite HTTP layer: browser UA, per-domain rate limit, robots.txt, SQLite cache.

Recon note (2026-08-18): destateparks.com and Simpleview endpoints 403 generic
clients but accept a desktop-browser User-Agent, so every request sends one.
"""

from __future__ import annotations

import threading
import time
from typing import Optional, Tuple
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests

from . import db

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 SceneScout/0.1 (+DDOA pilot)"
)
# robots.txt matching uses the crawler's product token, not the full UA:
# some sites carry legacy "User-agent: Mozilla" blocks meant for fake browsers,
# which would otherwise match the Chrome prefix we need for hosts that 403
# non-browser clients.
ROBOT_NAME = "SceneScout"
MIN_INTERVAL = 1.0  # seconds between requests to the same domain
TIMEOUT = 20

_lock = threading.Lock()
_next_allowed: dict = {}
_robots: dict = {}


def _wait_turn(domain: str) -> None:
    while True:
        with _lock:
            t = _next_allowed.get(domain, 0.0)
            nw = time.monotonic()
            if nw >= t:
                _next_allowed[domain] = nw + MIN_INTERVAL
                return
            wait = t - nw
        time.sleep(wait)


def robots_allows(url: str) -> bool:
    domain = urlparse(url).netloc
    with _lock:
        rp = _robots.get(domain)
    if rp is None:
        rp = RobotFileParser()
        robots_url = f"{urlparse(url).scheme}://{domain}/robots.txt"
        try:
            _wait_turn(domain)
            resp = requests.get(
                robots_url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT
            )
            if resp.status_code == 200:
                rp.parse(resp.text.splitlines())
            else:
                # No robots.txt (e.g. delawarescene.com 404s it): allow all.
                rp.allow_all = True
        except requests.RequestException:
            rp.allow_all = True
        with _lock:
            _robots[domain] = rp
    return _robots_verdict(rp, url)


def _robots_verdict(rp: RobotFileParser, url: str) -> bool:
    """RFC 9309-style matching: exact product-token match, else the '*' group.

    Python's RobotFileParser does substring matching, so a legacy entry like
    'User-agent: es' would swallow 'SceneScout' and wrongly deny everything.
    """
    if getattr(rp, "allow_all", False):
        return True
    if getattr(rp, "disallow_all", False):
        return False
    from urllib.parse import quote, unquote, urlparse as up, urlunparse

    parsed = up(unquote(url))
    path = urlunparse(("", "", parsed.path, parsed.params, parsed.query, parsed.fragment))
    path = quote(path) or "/"
    name = ROBOT_NAME.lower()
    for entry in rp.entries:
        if any(a.lower() == name for a in entry.useragents):
            return entry.allowance(path)
    if rp.default_entry:
        return rp.default_entry.allowance(path)
    return True


def fetch(
    url: str,
    *,
    conn=None,
    max_age_hours: float = 12.0,
    respect_robots: bool = True,
    extra_headers: Optional[dict] = None,
) -> Tuple[int, bytes, bool]:
    """GET a URL. Returns (status, content, from_cache).

    Status 0 means a transport error; -1 means blocked by robots.txt.
    Caches 200 responses in fetch_cache when a connection is supplied.
    """
    own_conn = False
    if conn is None:
        conn = db.connect()
        own_conn = True
    try:
        if max_age_hours > 0:
            row = conn.execute(
                "SELECT fetched_at, status, content FROM fetch_cache WHERE url = ?",
                (url,),
            ).fetchone()
            if row and row["status"] == 200:
                age_ok = row["fetched_at"] >= _cutoff(max_age_hours)
                if age_ok:
                    return 200, row["content"], True

        if respect_robots and not robots_allows(url):
            return -1, b"", False

        domain = urlparse(url).netloc
        _wait_turn(domain)
        headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
        if extra_headers:
            headers.update(extra_headers)
        try:
            resp = requests.get(
                url, headers=headers, timeout=TIMEOUT, allow_redirects=True
            )
            status_code, body = resp.status_code, resp.content
        except requests.RequestException:
            # This interpreter's LibreSSL is too old for some hosts' TLS
            # (e.g. desu.edu); system curl handles them fine.
            status_code, body = _curl_fetch(url, headers)
            if status_code == 0:
                return 0, b"", False

        if status_code == 200:
            conn.execute(
                "INSERT OR REPLACE INTO fetch_cache (url, fetched_at, status, content) "
                "VALUES (?, ?, ?, ?)",
                (url, db.now(), status_code, body),
            )
            conn.commit()
        return status_code, body, False
    finally:
        if own_conn:
            conn.close()


def _curl_fetch(url: str, headers: dict) -> Tuple[int, bytes]:
    import subprocess

    cmd = ["curl", "-sL", "--max-time", str(TIMEOUT), "-w", "\\n%{http_code}", url]
    for k, v in headers.items():
        cmd[1:1] = ["-H", f"{k}: {v}"]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT + 5).stdout
        body, _, code = out.rpartition(b"\n")
        return int(code or 0), body
    except (subprocess.SubprocessError, ValueError):
        return 0, b""


def _cutoff(hours: float) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
