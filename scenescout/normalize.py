"""Stage C: raw_events -> canonical events rows.

- Dates/times parsed to ISO (dates) + HH:MM 24h (times); export reformats.
- Prices parsed from cost text; phones normalized to digits.
- Relevance + categories via a tiered policy:
    tier 1/2 arts orgs  -> in scope by default; rules-classify categories
    nature-ish tier 2   -> screened like tier 3/4
    tier 3 libraries    -> LibCal category prefilter, then screen
    tier 4 gov/tourism  -> screen everything
  "Screen" = rules classifier; events the rules can't decide go to the LLM
  until llm_budget is exhausted, after that they're marked review.
"""

from __future__ import annotations

import hashlib
import json
import re

from dateutil import parser as dateparser

from . import db, geo, llm

# Sources whose calendars carry a lot of non-arts programming, so an event
# needs its own arts signal rather than inheriting the organization's.
# Tourism aggregators are the important case: they list restaurant happy
# hours, karaoke nights, and ball games alongside real arts events.
SCREENED_SOURCES = {
    # nature / science / historic venues
    "Delaware Nature Society",
    "Delaware Botanic Gardens",
    "Delaware Center for Horticulture",
    "Mt. Cuba Center",
    "Brandywine Zoo",
    "Delaware Agricultural Museum & Village",
    "Delaware Museum of Nature & Science",
    "Delaware State Parks",
    "Nemours Estate",
    # destination-marketing aggregators
    "Visit Delaware",
    "Visit Rehoboth",
    "Visit Wilmington DE",
    "Riverfront Wilmington",
}

# LibCal tags its own events, and those tags are more reliable than guessing
# from a title. Arts-specific tags map straight to DelawareScene categories.
LIBCAL_ARTS = {
    "Arts and Crafts": 11,
    "Book Discussions": 8,
}
# Tags that hold arts events and ordinary library services alike: "History and
# Genealogy" covers both a genealogy talk and passport services, and
# "Community and Culture" covers both a concert and a civic meeting. A hint
# worth an LLM call, not a verdict.
LIBCAL_WEAK = {"Community and Culture", "History and Genealogy",
               "Literacy and Language"}
LIBCAL_DROP = {"Story Time", "Jobs/Careers/Entrepreneurship", "Social Services",
               "Computers and eBooks", "Health and Wellness", "STREAM",
               "Cooking and Gardening"}

PHONE_RE = re.compile(r"(?:\+?1[\s.-]?)?\(?(\d{3})\)?[\s.-]?(\d{3})[\s.-]?(\d{4})")
MONEY_RE = re.compile(r"\$\s*(\d[\d,]*(?:\.\d{1,2})?)")
BARE_MONEY_RE = re.compile(r"^\s*(\d[\d,]*(?:\.\d{1,2})?)(?:\s*[-–—]\s*(\d[\d,]*(?:\.\d{1,2})?))?\s*$")


def clean_text(value):
    """Unescape HTML entities, drop residual tags (double-encoded HTML shows
    up after unescaping), and collapse whitespace (incl. &nbsp;)."""
    if not value:
        return value
    import html

    text = html.unescape(str(value)).replace("\xa0", " ")
    text = re.sub(r"<[^>]{0,200}>", " ", text)
    text = text.replace("\\n", " ").replace("[…]", "…")
    return re.sub(r"\s+", " ", text).strip()


_EASTERN = None


def _eastern():
    global _EASTERN
    if _EASTERN is None:
        try:
            from zoneinfo import ZoneInfo

            _EASTERN = ZoneInfo("America/New_York")
        except Exception:  # noqa: BLE001
            _EASTERN = False
    return _EASTERN


def parse_dt(value):
    """Return (date_iso, time_hhmm) from a raw start/end value.

    Timezone-aware inputs are converted to Eastern first: a UTC-stamped feed
    time like 2026-09-06T00:30:00Z is 8:30 p.m. on Sept 5 in Delaware, and
    taking the date as written would file it on the wrong day.
    """
    if not value:
        return None, None
    s = str(value).strip()
    try:
        dt = dateparser.parse(s)
    except (ValueError, OverflowError, TypeError):
        return None, None
    if dt is None:
        return None, None
    if dt.tzinfo is not None and _eastern():
        dt = dt.astimezone(_eastern())
    time = None
    if re.search(r"\d:\d\d|T\d\d|\d\s*[ap]\.?m", s, re.I) and (dt.hour, dt.minute) != (0, 0):
        time = f"{dt.hour:02d}:{dt.minute:02d}"
    return dt.date().isoformat(), time


def parse_price(cost_text):
    """-> (low, high, is_free) with whole-dollar ints.

    'Free' only wins when no real price is present: "Free for members, $10
    general" is a ticketed event, and exporting it as FREE misprices it.
    """
    if not cost_text:
        return None, None, None
    t = str(cost_text).strip()
    says_free = bool(re.search(r"\bfree\b", t, re.I))

    amounts = [float(m.replace(",", "")) for m in MONEY_RE.findall(t)]
    if not amounts:
        bare = BARE_MONEY_RE.match(t)
        if bare:
            amounts = [float(g.replace(",", "")) for g in bare.groups() if g]
    amounts = [a for a in amounts if 0 <= a < 10000]

    if not amounts:
        return None, None, 1 if says_free else None
    low, high = int(round(min(amounts))), int(round(max(amounts)))
    # Free only when nothing costs anything. A "$0 to $44" range is a ticketed
    # event with a free tier; calling it FREE would drop the $44 ceiling.
    return low, high, 1 if high == 0 else 0


def parse_phone(text):
    if not text:
        return None
    m = PHONE_RE.search(str(text))
    return f"{m[1]}-{m[2]}-{m[3]}" if m else None


def _coerce_time(value):
    """Accept '7:30 pm', '19:30', datetime-ish strings -> 'HH:MM'."""
    if not value:
        return None
    s = str(value).strip()
    if re.match(r"^\d{2}:\d{2}$", s):
        return s
    _, hhmm = parse_dt(f"2000-01-01 {s}")
    return hhmm


def norm_title(title):
    """Fuzzy title key for cross-source matching (dedupe stage).

    Deliberately lossy: strips ordinals and years so "53rd Annual Craft Show"
    and "54th Annual Craft Show" compare as the same series. Never use it as
    an identity key — see ident_title.
    """
    t = (title or "").lower()
    t = re.sub(r"\b(\d{1,3})(st|nd|rd|th)\b", "", t)          # ordinals
    t = re.sub(r"\b(annual|20\d\d)\b", "", t)
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def ident_title(title):
    """Identity key: normalize whitespace/punctuation only, keep every word."""
    t = (title or "").lower()
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def content_hash(source_id, title, start_date, start_time, venue):
    """Stable row identity. Includes start_time so a matinee and an evening
    performance of the same show on the same day stay distinct rows, and uses
    ident_title so "3rd Grade Art Show" never collides with "4th Grade ...".
    """
    key = "|".join([
        str(source_id), ident_title(title), start_date or "",
        start_time or "", (venue or "").lower().strip(),
    ])
    return hashlib.sha1(key.encode()).hexdigest()


COMMIT_EVERY = 50


def run(conn, llm_budget: int = 60) -> dict:
    stats = {"inserted": 0, "updated": 0, "skipped_no_date": 0, "out_of_scope": 0,
             "outside_delaware": 0, "unclassified_no_budget": 0,
             "llm_calls": 0, "review": 0}
    processed = 0
    sources = {s["id"]: dict(s) for s in conn.execute("SELECT * FROM sources")}
    rows = conn.execute(
        "SELECT r.* FROM raw_events r ORDER BY r.source_id"
    ).fetchall()
    for row in rows:
        raw = json.loads(row["payload"])
        for field in ("title", "description", "venue_name", "presenter", "venue_address"):
            raw[field] = clean_text(raw.get(field))
        src = sources.get(row["source_id"], {})
        title = (raw.get("title") or "").strip()
        start_date, t1 = parse_dt(raw.get("start"))
        if not title or not start_date:
            stats["skipped_no_date"] += 1
            continue
        end_date, t2 = parse_dt(raw.get("end"))
        if end_date == start_date:
            end_date = None
        start_time = _coerce_time(raw.get("start_time")) or t1
        end_time = _coerce_time(raw.get("end_time")) or t2
        low, high, is_free = parse_price(raw.get("cost_text"))

        relevance, reason, cats, used_llm = _classify(raw, src, stats, llm_budget)
        if used_llm:
            stats["llm_calls"] += 1
        chash = content_hash(row["source_id"], title, start_date, start_time,
                             raw.get("venue_name"))
        if relevance == "out":
            stats["out_of_scope"] += 1
            if reason.startswith("outside Delaware"):
                stats["outside_delaware"] += 1
            elif "budget exhausted" in reason:
                # Filtered without ever being classified — raising the LLM
                # budget would give these a real verdict.
                stats["unclassified_no_budget"] += 1
            # A re-run can reclassify a previously in-scope event; drop the
            # stale row so it cannot keep exporting.
            conn.execute("DELETE FROM events WHERE content_hash = ?", (chash,))
            processed += 1
            if processed % COMMIT_EVERY == 0:
                conn.commit()
            continue
        if relevance == "review":
            stats["review"] += 1
        cats = llm.sanitize_categories(cats)
        if is_free and 5 not in cats:
            # Category 5 (Free) is required for free events; make room for it
            # rather than dropping it when three content categories exist.
            cats = llm.sanitize_categories(cats[:2] + [5])
        existing = conn.execute(
            "SELECT id FROM events WHERE content_hash = ?", (chash,)
        ).fetchone()
        values = (
            row["source_id"], row["id"], title,
            raw.get("presenter") or src.get("name"),
            raw.get("venue_name") or (src.get("name") if src.get("tier") in (1, 2) else None),
            raw.get("venue_address"), raw.get("venue_city"),
            start_date, start_time, end_date, end_time,
            ",".join(str(c) for c in cats) if cats else None,
            raw.get("url"), raw.get("ticket_url"),
            parse_phone(raw.get("phone")),
            (raw.get("description") or "").strip() or None,
            low, high, is_free,
            relevance, reason,
        )
        if existing:
            conn.execute(
                "UPDATE events SET source_id=?, raw_id=?, title=?, presenter=?, venue_name=?, "
                "venue_address=?, venue_city=?, start_date=?, start_time=?, end_date=?, end_time=?, "
                "category_ids=?, url=?, ticket_url=?, phone=?, description=?, price_low=?, "
                "price_high=?, is_free=?, relevance=?, relevance_reason=?, last_seen=? "
                "WHERE content_hash=?",
                values + (db.now(), chash),
            )
            stats["updated"] += 1
        else:
            conn.execute(
                "INSERT INTO events (source_id, raw_id, title, presenter, venue_name, "
                "venue_address, venue_city, start_date, start_time, end_date, end_time, "
                "category_ids, url, ticket_url, phone, description, price_low, price_high, "
                "is_free, relevance, relevance_reason, content_hash, first_seen, last_seen) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                values + (chash, db.now(), db.now()),
            )
            stats["inserted"] += 1

        # Classification can spend minutes in LLM calls; committing in batches
        # keeps the write lock from being held for the whole stage, which
        # would block every other connection to the database.
        processed += 1
        if processed % COMMIT_EVERY == 0:
            conn.commit()
    conn.commit()
    # Rejected events are deleted, not stored, so the export's report cannot
    # recount them; persist the tallies it needs.
    db.save_stats(conn, "normalize", stats)
    return stats


def _classify(raw, src, stats, llm_budget):
    """-> (relevance, reason, category_ids, used_llm)"""
    tier = src.get("tier")
    name = src.get("name", "")
    source_cats = set(raw.get("source_categories") or [])

    # DelawareScene lists Delaware events. Several sources legitimately
    # program out of state, so an event confirmed elsewhere is dropped no
    # matter how arts-relevant it is. 'unknown' is kept: every source is a
    # Delaware organization, so a missing address is not evidence of absence.
    where, why = geo.verdict(raw.get("venue_city"), raw.get("venue_address"),
                             raw.get("venue_name"))
    if where == "out":
        return "out", f"outside Delaware ({why})", [], False

    payload = {k: raw.get(k) for k in ("title", "description", "venue_name", "presenter")}
    if source_cats:
        payload["source_categories"] = sorted(source_cats)
    rules = llm._RulesBackend().complete_json(
        "EVENT:" + json.dumps(payload, ensure_ascii=False), llm.CLASSIFY_SCHEMA
    )
    excluded_by_title = (not rules["relevant"]
                         and rules["reason"].startswith("rules: excluded"))

    # A disqualifying word in the title outranks every other signal. Story
    # time is out of scope by name even when the library files it under
    # "Arts and Crafts", so this must be checked before the tag shortcut.
    if excluded_by_title:
        return "out", "excluded keyword in title", [], False

    # LibCal tags its own events; those beat guessing from the title.
    tags = {c.strip() for c in source_cats}
    if src.get("extract_route") == "libcal":
        arts_tags = tags & set(LIBCAL_ARTS)
        if arts_tags:
            return ("in", f"library category {sorted(arts_tags)}",
                    llm.sanitize_categories([LIBCAL_ARTS[t] for t in sorted(arts_tags)]), False)
        if tags & LIBCAL_DROP:
            return "out", f"library category {sorted(tags & LIBCAL_DROP)}", [], False

    trusted_org = tier in (1, 2) and name not in SCREENED_SOURCES
    if trusted_org:
        # The org is an arts presenter, so a category word anywhere is enough.
        if rules["category_ids"]:
            return "in", "trusted source + keyword categories", rules["category_ids"], False
        return _llm_or_review(raw, stats, llm_budget, default=("in", "trusted source, uncategorized", []))

    # Screened sources: libraries, government calendars, nature venues,
    # tourism aggregators.
    # Admit outright only when the *title* carries the arts signal. A category
    # word buried in body text is too weak here: a Mt. Cuba dining event was
    # admitted as Theater & Performance because its description mentioned a
    # "performance", and a garden talk became Rock/Pop via "pop-up".
    if rules.get("title_category_ids"):
        return "in", "title keyword match", rules["title_category_ids"], False

    # Some signal, but not enough to admit outright: worth the LLM's time, and
    # worth a human's if the budget is gone.
    if rules["relevant"] or tags & LIBCAL_WEAK:
        return _llm_or_review(
            raw, stats, llm_budget,
            default=("review", "uncertain — arts hint only, no LLM budget", []),
        )

    # No arts signal anywhere. Ask the LLM if there is budget — titles like
    # "Diane Billas | Superficial" are real gallery shows no keyword can spot —
    # otherwise filter it out rather than queueing "Passport Services" and
    # "Library Closed" for a human to read.
    return _llm_or_review(
        raw, stats, llm_budget,
        default=("out", "no arts signal (unclassified: LLM budget exhausted)", []),
    )


def _llm_or_review(raw, stats, llm_budget, default):
    if stats["llm_calls"] >= llm_budget or llm.get_backend().name == "rules":
        return default + (False,)
    verdict = llm.classify(raw)
    rel = "in" if verdict.get("relevant") else "out"
    return rel, "llm: " + verdict.get("reason", "")[:200], verdict.get("category_ids") or [], True
