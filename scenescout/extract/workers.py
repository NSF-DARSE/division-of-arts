"""Stage B route workers. Each worker takes (source_row, conn) and returns a
list of raw event dicts in the common shape:

    {title, description, start, end, all_day, start_time, end_time,
     venue_name, venue_address, venue_city, presenter, url, ticket_url,
     phone, cost_text, source_categories}

Only fields the channel actually provides are set; Stage C normalizes.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .. import http, llm

HORIZON_MONTHS = 18


def _origin(url):
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _page_urls(src):
    """URLs to read for a source, best first.

    route_detail.probed_from records what the probe actually succeeded on,
    which matters when events_url was dead and the probe fell back to the
    homepage — the workers must read the same page the route was detected on.
    """
    detail = json.loads(src["route_detail"] or "{}")
    urls = []
    for candidate in (detail.get("probed_from"), src["events_url"], src["home_url"]):
        if candidate and candidate not in urls:
            urls.append(candidate)
    for extra in json.loads(src["extra_urls"] or "[]"):
        if extra and extra not in urls:
            urls.append(extra)
    return urls


def _soup(markup):
    """lxml is tolerant of real-world markup (bad marked sections crash
    html.parser); fall back if lxml is unavailable."""
    try:
        return BeautifulSoup(markup, "lxml")
    except Exception:  # noqa: BLE001
        return BeautifulSoup(markup, "html.parser")


def _strip_html(html_text: str) -> str:
    if not html_text:
        return ""
    return _soup(html_text).get_text(" ", strip=True)


def _sitemap_locs(body: bytes):
    """Tolerant <loc> extraction: lxml first, regex fallback for sitemaps with
    markup the parser rejects (e.g. City Theater's marked sections)."""
    try:
        soup = BeautifulSoup(body, "xml")
        locs = [loc.text.strip() for loc in soup.find_all("loc") if loc.text]
        if locs:
            return locs
    except Exception:  # noqa: BLE001
        pass
    return [m.decode("utf-8", "replace").strip()
            for m in re.findall(rb"<loc>\s*([^<\s][^<]*)</loc>", body)]


DATEISH_RE = re.compile(
    r"\b(january|february|march|april|may|june|july|august|september|october|"
    r"november|december|\d{1,2}/\d{1,2}/\d{2,4})\b", re.I)


def _within_horizon(iso: str, end_iso: str = None) -> bool:
    """Keep events that have not finished yet and start inside the horizon.

    An exhibit that opened months ago but runs for another year is still a
    live listing, so the *end* date decides whether it is past.
    """
    if not iso:
        return True
    try:
        start = datetime.fromisoformat(iso[:10]).date()
    except ValueError:
        return True
    end = start
    if end_iso:
        try:
            end = datetime.fromisoformat(str(end_iso)[:10]).date()
        except ValueError:
            end = start
    today = date.today()
    if end < today - timedelta(days=1):
        return False
    return start <= today + timedelta(days=30 * HORIZON_MONTHS)


EASTERN = None


def _to_eastern(dt):
    """Delaware events are Eastern; a UTC-stamped feed time can otherwise land
    on the wrong calendar date."""
    global EASTERN
    if not isinstance(dt, datetime) or dt.tzinfo is None:
        return dt
    if EASTERN is None:
        try:
            from zoneinfo import ZoneInfo

            EASTERN = ZoneInfo("America/New_York")
        except Exception:  # noqa: BLE001
            EASTERN = False
    return dt.astimezone(EASTERN) if EASTERN else dt


# ---------------------------------------------------------------- tribe REST
def pull_tribe(src, conn):
    detail = json.loads(src["route_detail"] or "{}")
    endpoint = detail.get("endpoint")
    events, page = [], 1
    while page <= 20:
        status, body, _ = http.fetch(f"{endpoint}?per_page=50&page={page}", conn=conn)
        if status != 200:
            break
        try:
            data = json.loads(body)
        except ValueError:
            break
        for ev in data.get("events") or []:
            venue = ev.get("venue") or {}
            if isinstance(venue, list):
                venue = venue[0] if venue else {}
            if not isinstance(venue, dict):
                venue = {}
            organizer = ev.get("organizer") or []
            if isinstance(organizer, dict):
                organizer = [organizer]
            elif isinstance(organizer, str):
                organizer = [{"organizer": organizer}]
            organizer = [o for o in organizer if isinstance(o, dict)]
            start = ev.get("start_date") or ""
            end = ev.get("end_date") or ""
            if not _within_horizon(start, end):
                continue
            cost = ev.get("cost") or ""
            events.append({
                "title": _strip_html(ev.get("title", "")),
                "description": _strip_html(ev.get("description", ""))[:2000],
                "start": start,
                "end": end,
                "all_day": bool(ev.get("all_day")),
                "venue_name": _strip_html(str(venue.get("venue") or "")) or None,
                "venue_address": venue.get("address") or None,
                "venue_city": venue.get("city") or None,
                "phone": venue.get("phone") or None,
                "presenter": _strip_html(str(organizer[0].get("organizer"))) if organizer else None,
                "url": ev.get("url"),
                "ticket_url": ev.get("website") or None,
                "cost_text": _strip_html(str(cost)) if cost else None,
                "source_categories": [c.get("name") for c in (ev.get("categories") or [])
                                      if isinstance(c, dict) and c.get("name")],
            })
        if page >= int(data.get("total_pages") or 1):
            break
        page += 1
    return events


# ------------------------------------------------------------------- LibCal
def pull_libcal(src, conn):
    detail = json.loads(src["route_detail"] or "{}")
    endpoint = detail.get("endpoint")
    events, page = [], 1
    while page <= 10:
        status, body, _ = http.fetch(endpoint.format(page=page), conn=conn)
        if status != 200:
            break
        try:
            data = json.loads(body)
        except ValueError:
            break
        results = data.get("results") or []
        for ev in results:
            startdt = (ev.get("startdt") or "").replace(" ", "T")
            enddt = (ev.get("enddt") or "").replace(" ", "T")
            if not _within_horizon(startdt, enddt):
                continue
            cats = [c.get("name") for c in (ev.get("categories_arr") or [])
                    if isinstance(c, dict) and c.get("name")]
            audiences = [a.get("name") for a in (ev.get("audiences") or [])
                         if isinstance(a, dict) and a.get("name")]
            cost = ev.get("registration_cost") or ev.get("cost")
            events.append({
                "title": ev.get("title"),
                "description": _strip_html(ev.get("description", ""))[:2000],
                "start": startdt,
                "end": (ev.get("enddt") or "").replace(" ", "T"),
                "all_day": bool(ev.get("all_day")),
                "venue_name": ev.get("campus") or ev.get("calendar"),
                "presenter": ev.get("presenter") or ev.get("campus"),
                "url": ev.get("url"),
                "cost_text": str(cost) if cost else None,
                "source_categories": cats + audiences,
            })
        total = int(data.get("total_results") or 0)
        if page * int(data.get("perpage") or 100) >= total or not results:
            break
        page += 1
    return events


# -------------------------------------------------------------- Squarespace
def pull_squarespace(src, conn):
    detail = json.loads(src["route_detail"] or "{}")
    endpoint = detail.get("endpoint")
    origin = _origin(endpoint)
    status, body, _ = http.fetch(endpoint, conn=conn)
    if status != 200:
        return []
    try:
        data = json.loads(body)
    except ValueError:
        return []
    items = data.get("upcoming") or data.get("items") or []
    events = []
    for it in items:
        if not isinstance(it, dict):
            continue
        start_ms = it.get("startDate")
        end_ms = it.get("endDate")
        start = datetime.fromtimestamp(start_ms / 1000).isoformat() if start_ms else None
        end = datetime.fromtimestamp(end_ms / 1000).isoformat() if end_ms else None
        if start and not _within_horizon(start, end):
            continue
        loc = it.get("location") or {}
        if not isinstance(loc, dict):
            loc = {}
        addr = ", ".join(filter(None, [loc.get("addressLine1"), loc.get("addressLine2")])) or None
        full = it.get("fullUrl")
        events.append({
            "title": it.get("title"),
            "description": _strip_html(it.get("excerpt", "") or it.get("body", ""))[:2000],
            "start": start,
            "end": end,
            "venue_address": addr,
            "url": origin + full if full and full.startswith("/") else full,
            "source_categories": it.get("categories") or [],
        })
    return events


# --------------------------------------------------------------------- ICS
def pull_ics(src, conn):
    from icalendar import Calendar

    detail = json.loads(src["route_detail"] or "{}")
    endpoint = detail.get("endpoint")
    status, body, _ = http.fetch(endpoint, conn=conn)
    if status != 200 or not body.lstrip().startswith(b"BEGIN:VCALENDAR"):
        return []
    try:
        cal = Calendar.from_ical(body)
    except ValueError:
        return []
    events = []
    for comp in cal.walk("VEVENT"):
        dtstart = comp.get("dtstart")
        if dtstart is None:
            continue
        sd = _to_eastern(dtstart.dt)
        if isinstance(sd, str):
            continue
        all_day = not isinstance(sd, datetime)
        start = sd.isoformat()

        end = None
        dtend = comp.get("dtend")
        if dtend is not None and not isinstance(dtend.dt, str):
            ed = _to_eastern(dtend.dt)
            # RFC 5545: DTEND is exclusive. For date-valued (all-day) events
            # that makes the stored end one day too late.
            if all_day and not isinstance(ed, datetime):
                ed = ed - timedelta(days=1)
            end = ed.isoformat()

        if not _within_horizon(start, end):
            continue
        status_prop = str(comp.get("status", "")).upper()
        if status_prop == "CANCELLED":
            continue
        events.append({
            "title": str(comp.get("summary", "")),
            "description": _strip_html(str(comp.get("description", "")))[:2000],
            "start": start,
            "end": end,
            "all_day": all_day,
            "venue_name": str(comp.get("location", "")) or None,
            "url": str(comp.get("url", "")) or None,
        })
    return events


# --------------------------------------------------------------------- RSS
def pull_rss(src, conn):
    """Blog/news feeds: entries are announcements, not structured events.
    Concatenate the feed and run one LLM extraction over it."""
    import feedparser

    detail = json.loads(src["route_detail"] or "{}")
    endpoint = detail.get("endpoint") or src["extract_route"].split(":", 1)[1]
    status, body, _ = http.fetch(endpoint, conn=conn)
    if status != 200:
        return []
    feed = feedparser.parse(body)
    chunks = []
    for e in feed.entries[:25]:
        text = _strip_html(getattr(e, "summary", "") or "")
        chunks.append(f"### {getattr(e, 'title', '')}\nlink: {getattr(e, 'link', '')}\n{text[:1500]}")
    if not chunks:
        return []
    return _llm_extract_chunks(chunks, src, conn)


# ------------------------------------------------- WP custom post type (v2)
def pull_wpv2(src, conn):
    """CPT posts carry title/link/content but usually no date meta; the LLM
    reads the rendered content."""
    detail = json.loads(src["route_detail"] or "{}")
    endpoint = detail.get("endpoint")
    status, body, _ = http.fetch(f"{endpoint}?per_page=50", conn=conn)
    if status != 200:
        return []
    try:
        posts = json.loads(body)
    except ValueError:
        return []
    if not isinstance(posts, list):
        return []
    chunks = []
    for p in posts[:50]:
        title = _strip_html((p.get("title") or {}).get("rendered", ""))
        content = _strip_html((p.get("content") or {}).get("rendered", ""))
        if not content or len(content) < 40 or not DATEISH_RE.search(content):
            # Content-less or date-less CPT (dates often live in ACF fields
            # that only render on the page): fetch the actual page.
            s2, page_body, _ = http.fetch(p.get("link", ""), conn=conn, max_age_hours=48)
            if s2 == 200:
                content = _strip_html(page_body.decode("utf-8", "replace"))
        chunks.append(f"### {title}\nlink: {p.get('link', '')}\n{content[:4000]}")
    return _llm_extract_chunks(chunks, src, conn)


# ------------------------------------------------------------------ JSON-LD
def pull_jsonld(src, conn):
    detail = json.loads(src["route_detail"] or "{}")
    sitemap = detail.get("sitemap")
    page_urls = []
    if sitemap:
        status, body, _ = http.fetch(sitemap, conn=conn)
        if status == 200:
            page_urls = [
                u for u in _sitemap_locs(body)
                if not u.endswith(".xml") and "/form" not in u
            ][:80]
    else:
        page_urls = _page_urls(src)[:3]
    events = []
    for u in page_urls:
        status, body, _ = http.fetch(u, conn=conn, max_age_hours=48)
        if status != 200:
            continue
        events.extend(_parse_jsonld_events(body, u))
    return events


def _parse_jsonld_events(body: bytes, url: str):
    try:
        soup = _soup(body)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text())
        except (ValueError, TypeError):
            continue
        nodes = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for node in nodes:
            if not isinstance(node, dict) or "Event" not in str(node.get("@type", "")):
                continue
            start = node.get("startDate")
            if not start or not _within_horizon(start, node.get("endDate")):
                continue
            if "cancel" in str(node.get("eventStatus", "")).lower():
                continue
            loc = node.get("location") or {}
            if isinstance(loc, list):
                loc = loc[0] if loc else {}
            if not isinstance(loc, dict):
                loc = {"name": str(loc)}
            addr = loc.get("address") or {}
            if isinstance(addr, str):
                addr = {"streetAddress": addr}
            if not isinstance(addr, dict):
                addr = {}
            offers = node.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            if not isinstance(offers, dict):
                offers = {}
            org = node.get("organizer") or {}
            if isinstance(org, list):
                org = org[0] if org else {}
            if isinstance(org, str):
                org = {"name": org}
            if not isinstance(org, dict):
                org = {}
            out.append({
                "title": node.get("name"),
                "description": _strip_html(node.get("description", ""))[:2000],
                "start": start,
                "end": node.get("endDate"),
                "venue_name": loc.get("name"),
                "venue_address": addr.get("streetAddress"),
                "venue_city": addr.get("addressLocality"),
                "presenter": org.get("name"),
                "url": node.get("url") or url,
                "ticket_url": offers.get("url"),
                "cost_text": str(offers.get("price")) if offers.get("price") not in (None, "") else None,
            })
    return out


# ------------------------------------------------------------- HTML -> LLM
def pull_html_llm(src, conn, max_pages: int = 8):
    detail = json.loads(src["route_detail"] or "{}")
    sitemap = detail.get("sitemap")
    texts = []
    if sitemap:
        status, body, _ = http.fetch(sitemap, conn=conn)
        urls = []
        if status == 200:
            try:
                soup = BeautifulSoup(body, "xml")
                entries = []
                for u in soup.find_all("url"):
                    loc = u.find("loc")
                    lastmod = u.find("lastmod")
                    if loc and loc.text and not loc.text.strip().endswith(".xml"):
                        entries.append((lastmod.text if lastmod else "", loc.text.strip()))
                entries.sort(reverse=True)  # most recently modified first
                urls = [u for _, u in entries[:max_pages]]
            except Exception:  # noqa: BLE001
                urls = []
            if not urls:
                urls = [u for u in _sitemap_locs(body) if not u.endswith(".xml")][:max_pages]
        for u in urls:
            s, b, _ = http.fetch(u, conn=conn, max_age_hours=48)
            if s == 200:
                texts.append(f"PAGE: {u}\n" + _strip_html(b.decode("utf-8", "replace"))[:6000])
    else:
        for u in _page_urls(src)[:3]:
            s, b, _ = http.fetch(u, conn=conn)
            if s == 200:
                texts.append(f"PAGE: {u}\n" + _strip_html(b.decode("utf-8", "replace"))[:30000])
    return _llm_extract_chunks(texts, src, conn)


# ------------------------------------------------------------------ helpers
def _llm_extract_chunks(texts, src, conn, chunk_chars: int = 24000):
    """Batch page texts into few LLM calls; map results to the raw shape."""
    if not texts:
        return []
    batches, current = [], ""
    for t in texts:
        if current and len(current) + len(t) > chunk_chars:
            batches.append(current)
            current = ""
        current += t + "\n\n"
    if current:
        batches.append(current)
    today = date.today().isoformat()
    urls = _page_urls(src)
    events = []
    for batch in batches:
        for ev in llm.extract_events(batch, src["name"], urls[0] if urls else "", today):
            events.append({
                "title": ev.get("title"),
                "description": ev.get("description"),
                "start": ev.get("start_date"),
                "start_time": ev.get("start_time"),
                "end": ev.get("end_date"),
                "end_time": ev.get("end_time"),
                "venue_name": ev.get("venue_name"),
                "venue_address": ev.get("venue_address"),
                "venue_city": ev.get("venue_city"),
                "presenter": ev.get("presenter"),
                "url": ev.get("url"),
                "ticket_url": ev.get("ticket_url"),
                "phone": ev.get("phone"),
                "cost_text": ev.get("cost_text"),
                "channel_note": "llm-extracted",
            })
    return [e for e in events
            if e.get("title") and _within_horizon(e.get("start") or "", e.get("end"))]
