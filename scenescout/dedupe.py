"""Stage D: deduplication.

1. Self-dedup: the same event often reaches us via several sources (venue site,
   Visit Delaware, a library calendar). Same normalized title + same date +
   compatible venue -> one dedupe_group; the richest record represents it.
2. Scene match: compare each in-scope event against scene_listings (the live
   DelawareScene calendar + the Currently Listed export). Blocking on date
   overlap, then RapidFuzz scoring on title + venue.

Verdicts: 'dupe' (already listed - suppress), 'new' (export), 'review'
(borderline - surfaced to staff, never silently dropped).
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta

from rapidfuzz import fuzz

from .normalize import norm_title

DUPE_SCORE = 87.0
REVIEW_SCORE = 72.0
# A title-only match (no venue on one side) is weaker evidence, so it needs a
# near-identical title before it may suppress an event.
TITLE_ONLY_DUPE = 96.0

_VENUE_NOISE_RE = re.compile(r"\b(inc|ltd|llc|the|co|corp|foundation|association)\b\.?",
                             re.IGNORECASE)


def norm_venue(v):
    """Normalize a venue name for comparison.

    Word-boundary matching matters: a plain string replace of 'inc' would
    turn 'Lincoln' into 'Loln' and quietly break every Lincoln-venue match.
    """
    if not v:
        return ""
    v = _VENUE_NOISE_RE.sub(" ", str(v).lower())
    v = re.sub(r"[^\w\s]", " ", v)
    return re.sub(r"\s+", " ", v).strip()


def _as_date(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def _date_overlaps(e_start, e_end, listing, pad_days=1):
    """True if the event's run intersects the listing's dates.

    A listing with an explicit "Through <date>" is a continuous run and is
    compared as a range; one built from separate day-page sightings is a set
    of discrete occurrences and is compared by membership, so a weekly series
    spanning three months cannot swallow an unrelated event in between.
    """
    es = _as_date(e_start)
    if es is None:
        return False
    ee = _as_date(e_end) or es
    pad = timedelta(days=pad_days)

    if not listing.get("is_range") and listing.get("dates"):
        try:
            occurrences = json.loads(listing["dates"])
        except (ValueError, TypeError):
            occurrences = []
        for occ in occurrences:
            od = _as_date(occ)
            if od and es - pad <= od <= ee + pad:
                return True
        if occurrences:
            return False

    ss = _as_date(listing.get("start_date"))
    if ss is None:
        return False
    se = _as_date(listing.get("end_date")) or ss
    return es - pad <= se and ss - pad <= ee


def score_pair(title_a, venue_a, title_b, venue_b):
    """-> (score, used_venue). Venue-less comparisons are flagged so callers
    can hold them to a stricter bar."""
    t = fuzz.token_set_ratio(norm_title(title_a), norm_title(title_b))
    va, vb = norm_venue(venue_a), norm_venue(venue_b)
    if va and vb:
        v = fuzz.token_set_ratio(va, vb)
        return 0.62 * t + 0.38 * v, True
    return float(t), False


def reset_verdicts(conn) -> None:
    """Verdicts are derived state; clear them so a re-run reflects current
    reality instead of inheriting a stale 'dupe' forever."""
    conn.execute(
        "UPDATE events SET verdict = NULL, verdict_reason = NULL, "
        "scene_match = NULL, dedupe_group = NULL"
    )
    conn.commit()


def self_dedupe(conn) -> int:
    """Group same-event records pulled from different sources."""
    rows = [dict(r) for r in conn.execute(
        "SELECT id, title, venue_name, start_date, start_time, description, url, "
        "relevance FROM events WHERE relevance != 'out' ORDER BY start_date"
    )]
    by_key = {}
    for r in rows:
        by_key.setdefault((norm_title(r["title"]), r["start_date"]), []).append(r)

    group_id = 0
    groups = 0
    for members in by_key.values():
        if len(members) < 2:
            continue
        clusters = []
        for m in members:
            v_new = norm_venue(m["venue_name"])
            placed = False
            for cl in clusters:
                v_ref = norm_venue(cl[0]["venue_name"])
                # Only merge when both venues are known and agree. A record
                # with no venue is not evidence of the same venue, so it gets
                # its own cluster rather than joining the first one greedily.
                if v_new and v_ref and fuzz.token_set_ratio(v_new, v_ref) >= 75:
                    cl.append(m)
                    placed = True
                    break
            if not placed:
                clusters.append([m])
        for cl in clusters:
            if len(cl) < 2:
                continue
            group_id += 1
            groups += 1
            # Representative = the record a human would rather import:
            # in-scope first, then richest description.
            cl.sort(key=lambda m: (m["relevance"] != "in", -len(m.get("description") or "")))
            keeper = cl[0]
            for m in cl[1:]:
                conn.execute(
                    "UPDATE events SET dedupe_group = ?, verdict = 'dupe', "
                    "verdict_reason = ? WHERE id = ?",
                    (group_id, f"self-dup of event {keeper['id']}", m["id"]),
                )
            conn.execute(
                "UPDATE events SET dedupe_group = ? WHERE id = ?", (group_id, keeper["id"])
            )
    conn.commit()
    return groups


def scene_match(conn) -> dict:
    listings = [dict(r) for r in conn.execute(
        "SELECT scene_event_id, title, venue, start_date, end_date, dates, is_range, "
        "origin FROM scene_listings"
    )]
    stats = {"dupe": 0, "new": 0, "review": 0}
    events = conn.execute(
        "SELECT id, title, venue_name, start_date, end_date FROM events "
        "WHERE relevance != 'out' AND (verdict IS NULL OR verdict != 'dupe')"
    ).fetchall()
    for ev in events:
        best, best_score, best_used_venue = None, 0.0, False
        for li in listings:
            if not _date_overlaps(ev["start_date"], ev["end_date"], li):
                continue
            s, used_venue = score_pair(ev["title"], ev["venue_name"], li["title"], li["venue"])
            if s > best_score:
                best, best_score, best_used_venue = li, s, used_venue

        dupe_bar = DUPE_SCORE if best_used_venue else TITLE_ONLY_DUPE
        if best_score >= dupe_bar:
            verdict = "dupe"
        elif best_score >= REVIEW_SCORE:
            verdict = "review"
        else:
            verdict = "new"
        stats[verdict] += 1

        reason = None
        match_text = None
        if best and best_score >= REVIEW_SCORE:
            ref = best["scene_event_id"] or f"xlsx:{(best['title'] or '')[:40]}"
            reason = (f"score {best_score:.0f}"
                      f"{'' if best_used_venue else ' (title only)'} vs scene {ref}")
            match_text = f"{best['title']} @ {best['venue'] or '?'} ({best['start_date']})"
        conn.execute(
            "UPDATE events SET verdict = ?, verdict_reason = ?, scene_match = ? WHERE id = ?",
            (verdict, reason, match_text, ev["id"]),
        )
    conn.commit()
    return stats


def run(conn) -> dict:
    reset_verdicts(conn)
    groups = self_dedupe(conn)
    stats = scene_match(conn)
    stats["self_dupe_groups"] = groups
    return stats
