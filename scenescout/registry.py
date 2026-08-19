"""Stage A: load websites.csv, health-check each source, auto-detect its
extraction route.

The route cascade (best channel first, cached in sources.extract_route):
  libcal           Springshare LibCal JSON (special-cased by domain)
  ics              iCal feed (CivicPlus iCalendar.aspx, WP ?ical=1, ...)
  tribe-rest       The Events Calendar REST API (full event JSON)
  wpv2:<rest_base> WordPress custom post type via /wp-json/wp/v2/ (titles/links)
  squarespace-json Squarespace events collection via ?format=json
  rss:<url>        RSS feed advertised by the page
  jsonld           schema.org Event JSON-LD on event pages
  html-llm         server-rendered HTML -> LLM extraction (the tail)
  headless         client-rendered or bot-blocked; deferred past the MVP
"""

from __future__ import annotations

import csv
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from bs4 import BeautifulSoup

from . import db, http

ASSETS = Path(__file__).resolve().parent.parent / "assets"
WEBSITES_CSV = ASSETS / "websites.csv"

# Tier boundaries follow the curated ordering of websites.csv.
TIER1_LAST = "Wilmington Drama League"
TIER3_NAME = "delaware libraries"


def load_sources(conn, path: Path = WEBSITES_CSV) -> int:
    tier = 1
    n = 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            name = row["Organization Name"].strip()
            if not name:
                continue
            if tier == 2 and name.lower() == TIER3_NAME:
                tier = 3
            events_raw = (row.get("Events") or "").strip()
            urls = [u.strip() for u in events_raw.split(";") if u.strip()]
            sitemap = (row.get("Site Map") or "").strip()
            conn.execute(
                "INSERT INTO sources (name, tier, home_url, sitemap_url, events_url, extra_urls) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET home_url=excluded.home_url, "
                "sitemap_url=excluded.sitemap_url, events_url=excluded.events_url, "
                "extra_urls=excluded.extra_urls",
                (
                    name,
                    tier,
                    _norm_url(row["Organization URL"]),
                    sitemap if sitemap.lower() not in ("", "nks") else None,
                    _norm_url(urls[0]) if urls else None,
                    db.to_json(urls[1:]) if len(urls) > 1 else None,
                ),
            )
            n += 1
            if name == TIER1_LAST:
                tier = 2
            elif tier == 3:
                tier = 4
    conn.commit()
    return n


def probe_all(conn, workers: int = 8, only_missing: bool = True) -> None:
    q = "SELECT * FROM sources"
    if only_missing:
        q += " WHERE extract_route IS NULL"
    rows = [dict(r) for r in conn.execute(q).fetchall()]

    def safe_probe(src):
        # One malformed source must not abort the whole batch: pool.map
        # re-raises on iteration and every other probe's work would be lost.
        try:
            return probe_source(src)
        except Exception as e:  # noqa: BLE001
            return _res(None, {}, "probe-error", 0, f"{type(e).__name__}: {e}")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(safe_probe, rows))
    for src, res in zip(rows, results):
        conn.execute(
            "UPDATE sources SET extract_route=?, route_detail=?, status=?, "
            "http_status=?, last_checked=?, notes=? WHERE id=?",
            (
                res["route"],
                db.to_json(res["detail"]),
                res["status"],
                res["http_status"],
                db.now(),
                res.get("notes"),
                src["id"],
            ),
        )
    conn.commit()


def probe_source(src: dict) -> dict:
    """Run the route cascade for one source. Pure function, no shared state."""
    events_url = src.get("events_url") or src.get("home_url")
    if not events_url:
        return _res("none", {}, "broken", 0, "no URL at all")

    origin = _origin(events_url)
    detail: dict = {"probed_from": events_url}

    # -- special cases -------------------------------------------------------
    if "libcal.com" in origin:
        detail["endpoint"] = (
            f"{origin}/ajax/calendar/list?c=-1&date=0000-00-00&perpage=100&page={{page}}"
        )
        return _res("libcal", detail, "ok", 200)

    if "calendar.aspx" in events_url.lower():
        cid = (parse_qs(urlparse(events_url).query).get("CID") or ["0"])[0]
        ics_url = f"{origin}/common/modules/iCalendar/iCalendar.aspx?catID={cid}&feed=calendar"
        status, content, _ = http.fetch(ics_url, max_age_hours=6)
        if status == 200 and content.lstrip().startswith(b"BEGIN:VCALENDAR"):
            detail["endpoint"] = ics_url
            return _res("ics", detail, "ok", 200)

    # -- reachability of the events page itself -----------------------------
    status, content, _ = http.fetch(events_url, max_age_hours=6)
    if status in (401, 403, 429):
        return _res("headless", detail, "blocked", status, "bot-blocked")
    if status in (0, -1) or status >= 400:
        # Events URL is dead; retry from the homepage before giving up.
        home = src.get("home_url")
        if home and home != events_url:
            status, content, _ = http.fetch(home, max_age_hours=6)
            if status == 200:
                events_url = home
                origin = _origin(home)
                detail["probed_from"] = home
        if status != 200:
            # Leave the route unset on a transport failure so the next probe
            # retries it; only a real HTTP error is a settled verdict.
            return _res(None if status == 0 else "none", detail, "broken", status)

    is_xml = events_url.lower().endswith(".xml") or content.lstrip().startswith(b"<?xml")

    # -- 1. The Events Calendar REST ----------------------------------------
    tribe_url = f"{origin}/wp-json/tribe/events/v1/events?per_page=1"
    tstatus, tbody, _ = http.fetch(tribe_url, max_age_hours=6)
    if tstatus == 200:
        try:
            data = json.loads(tbody)
            if isinstance(data, dict) and "events" in data:
                detail["endpoint"] = f"{origin}/wp-json/tribe/events/v1/events"
                return _res("tribe-rest", detail, "ok", 200)
        except ValueError:
            pass

    # -- 2. other WP event post types via wp/v2 ------------------------------
    types_url = f"{origin}/wp-json/wp/v2/types"
    tstatus, tbody, _ = http.fetch(types_url, max_age_hours=6)
    if tstatus == 200:
        try:
            types = json.loads(tbody)
            if isinstance(types, dict):
                for slug, t in types.items():
                    rb = (t or {}).get("rest_base", "")
                    if "event" in slug.lower() or "event" in str(rb).lower():
                        # Security/analytics plugins expose event-ish CPTs that
                        # aren't calendar events.
                        if slug in ("nav_menu_item",) or "activity-log" in str(rb) or "activity_log" in slug:
                            continue
                        detail["endpoint"] = f"{origin}/wp-json/wp/v2/{rb}"
                        detail["cpt"] = slug
                        return _res(f"wpv2:{rb}", detail, "ok", 200)
        except ValueError:
            pass

    # -- 3. generic ICS on the events page -----------------------------------
    if not is_xml:
        sep = "&" if "?" in events_url else "?"
        ics_url = f"{events_url}{sep}ical=1"
        istatus, ibody, _ = http.fetch(ics_url, max_age_hours=6)
        if istatus == 200 and ibody.lstrip().startswith(b"BEGIN:VCALENDAR"):
            detail["endpoint"] = ics_url
            return _res("ics", detail, "ok", 200)

    # -- 4. Squarespace JSON --------------------------------------------------
    if not is_xml:
        sep = "&" if "?" in events_url else "?"
        sq_url = f"{events_url}{sep}format=json"
        sstatus, sbody, _ = http.fetch(sq_url, max_age_hours=6)
        if sstatus == 200:
            try:
                data = json.loads(sbody)
                coll = data.get("collection") if isinstance(data, dict) else None
                if not isinstance(coll, dict):
                    coll = {}
                if isinstance(data, dict) and (
                    "event" in str(coll.get("typeName", "")).lower()
                    or data.get("upcoming") is not None
                ):
                    detail["endpoint"] = sq_url
                    return _res("squarespace-json", detail, "ok", 200)
            except ValueError:
                pass

    # -- 5. RSS advertised in the page head ----------------------------------
    if not is_xml and content:
        soup = BeautifulSoup(content, "html.parser")
        link = soup.find(
            "link", attrs={"type": re.compile(r"application/(rss|atom)\+xml")}
        )
        if link and link.get("href"):
            href = link["href"]
            if href.startswith("/"):
                href = origin + href
            if _rss_has_items(href):
                detail["endpoint"] = href
                return _res(f"rss:{href}", detail, "ok", 200)

    # -- 6. schema.org Event JSON-LD -----------------------------------------
    sample = None
    if is_xml:
        sample = _first_sitemap_url(content)
        if sample:
            detail["sample_event_url"] = sample
            s2, c2, _ = http.fetch(sample, max_age_hours=6)
            if s2 == 200 and _has_event_jsonld(c2):
                detail["sitemap"] = events_url
                return _res("jsonld", detail, "ok", 200)
    elif content and _has_event_jsonld(content):
        return _res("jsonld", detail, "ok", 200)

    # -- 7. plain HTML for the LLM tail --------------------------------------
    if is_xml and sample:
        # Sitemap whose event pages lack JSON-LD: LLM reads the pages.
        detail["sitemap"] = events_url
        return _res("html-llm", detail, "ok", 200)
    if content and b"<html" in content[:2000].lower():
        text = BeautifulSoup(content, "html.parser").get_text(" ", strip=True)
        if len(text) < 500:
            return _res("headless", detail, "blocked", status, "near-empty page (client-rendered)")
        return _res("html-llm", detail, "ok", 200)

    return _res("none", detail, "broken", status)


def _res(route, detail, status, http_status, notes=None):
    return {
        "route": route,
        "detail": detail,
        "status": status,
        "http_status": http_status,
        "notes": notes,
    }


def _norm_url(u: str):
    u = (u or "").strip()
    if not u:
        return None
    if not u.startswith("http"):
        u = "https://" + u
    return u


def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _first_sitemap_url(content: bytes):
    soup = BeautifulSoup(content, "xml")
    for loc in soup.find_all("loc"):
        u = (loc.text or "").strip()
        if u and not u.endswith(".xml"):
            return u
    return None


def _has_event_jsonld(content: bytes) -> bool:
    soup = BeautifulSoup(content, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        blob = script.string or script.get_text()
        if blob and re.search(r'"@type"\s*:\s*"?\[?[^"\]]*Event', blob):
            return True
    return False


def _rss_has_items(url: str) -> bool:
    status, content, _ = http.fetch(url, max_age_hours=6)
    return status == 200 and (b"<item" in content or b"<entry" in content)
