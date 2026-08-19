"""Stage E: write the DelawareScene bulk-upload workbook + review report.

Template columns (from assets/DelawareScene-Bulk-Upload-BLANK.xlsx):
  venue ID | presenter ID (if different) | title of program | categories | URL |
  box office phone | low price | high price | start date | start time |
  end date | ticket URL | description

Conventions from the DDOA how-to:
  - multi-performance productions: one full row, then per-performance rows
    carrying only start date / start time / ticket URL
  - dates MM/DD/YYYY, times '7:30 p.m.', phone XXX-XXX-XXXX
  - prices whole dollars or FREE; <= 3 categories; description <= 200 words
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from rapidfuzz import fuzz, process

from . import db
from .dedupe import norm_venue
from .llm import PARENT_OF, VALID_CATEGORIES
from .normalize import norm_title

OUT_DIR = Path(__file__).resolve().parent.parent / "out"

HEADERS = [
    "venue ID", "presenter ID (if different)", "title of program", "categories",
    "URL", "box office phone", "low price", "high price", "start date",
    "start time", "end date", "ticket URL", "description",
]

VENUE_MATCH_ACCEPT = 92.0
VENUE_MATCH_REVIEW = 84.0


def _as_date(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def clean_url(url):
    """Guidelines require the scheme; add or drop rather than ship bad URLs."""
    if not url:
        return None
    url = str(url).strip()
    if re.match(r"^https?://", url):
        return url
    if re.match(r"^[\w.-]+\.[a-z]{2,}(/|$)", url, re.I):
        return "https://" + url
    return None


def fmt_date(iso):
    if not iso:
        return None
    y, m, d = iso.split("-")
    return f"{int(m):02d}/{int(d):02d}/{y}"


def fmt_time(hhmm):
    if not hhmm:
        return None
    h, m = int(hhmm[:2]), int(hhmm[3:5])
    suffix = "a.m." if h < 12 else "p.m."
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {suffix}"


def words_cap(text, cap=200):
    if not text:
        return None
    words = text.split()
    return " ".join(words[:cap]) + ("…" if len(words) > cap else "")


class OrgResolver:
    """Fuzzy name -> DelawareScene organization ID."""

    def __init__(self, conn):
        # Several directory entries normalize to the same key ("Mt. Cuba
        # Center" and "Mt.Cuba Center"), so keep every colliding id and
        # report the ambiguity instead of letting the last row silently win.
        self.names = {}
        for r in conn.execute("SELECT scene_id, name FROM scene_orgs ORDER BY scene_id"):
            key = norm_venue(r["name"]) or r["name"].lower()
            self.names.setdefault(key, []).append((r["scene_id"], r["name"]))
        self.keys = list(self.names)

    def resolve(self, name):
        """-> (scene_id | None, matched_name | None, confidence)

        token_set_ratio alone scores any token subset 100, so a bare venue
        string like "Newark" would match "Newark Arts Alliance" perfectly and
        silently attach the wrong VenueID. Blending in token_sort_ratio
        penalizes the unmatched tokens, which is what makes that a miss.
        """
        if not name:
            return None, None, 0.0
        key = norm_venue(name)
        if not key:
            return None, None, 0.0
        if key in self.names:
            entries = self.names[key]
            sid, orig = entries[0]
            # An exact-key tie means the directory holds near-duplicate
            # entries; surface it rather than pick one at full confidence.
            conf = 100.0 if len(entries) == 1 else float(VENUE_MATCH_REVIEW)
            if len(entries) > 1:
                orig = f"{orig} (ambiguous: {len(entries)} directory entries)"
            return sid, orig, conf
        if not self.keys:
            return None, None, 0.0
        best = process.extractOne(key, self.keys, scorer=fuzz.token_set_ratio)
        if not best:
            return None, None, 0.0
        candidate = best[0]
        conf = (fuzz.token_set_ratio(key, candidate) + fuzz.token_sort_ratio(key, candidate)) / 2
        if conf >= VENUE_MATCH_REVIEW:
            sid, orig = self.names[candidate][0]
            return sid, orig, float(conf)
        return None, None, float(conf)


def validate_row(row: dict) -> list:
    """Hard checks from the submission guidelines. Returns a list of problems."""
    problems = []
    if not row["title of program"]:
        problems.append("missing title")
    if not row["start date"]:
        problems.append("missing start date")
    elif not re.match(r"^\d{2}/\d{2}/\d{4}$", row["start date"]):
        problems.append(f"bad date format {row['start date']}")
    if row["end date"] and not re.match(r"^\d{2}/\d{2}/\d{4}$", str(row["end date"])):
        problems.append(f"bad end date format {row['end date']}")
    if row["start time"] and not re.match(r"^\d{1,2}:\d{2} [ap]\.m\.$", row["start time"]):
        problems.append(f"bad time format {row['start time']}")
    if row["box office phone"] and not re.match(r"^\d{3}-\d{3}-\d{4}$", row["box office phone"]):
        problems.append("bad phone format")
    for key in ("URL", "ticket URL"):
        if row[key] and not re.match(r"^https?://", row[key]):
            problems.append(f"{key} missing scheme")
    cats = str(row["categories"] or "")
    if cats:
        tokens = [c.strip() for c in cats.split(",") if c.strip()]
        bad = [c for c in tokens if not c.isdigit()]
        if bad:
            problems.append(f"non-numeric category token(s): {', '.join(bad)}")
        ids = [int(c) for c in tokens if c.isdigit()]
        if len(ids) > 3:
            problems.append("more than 3 categories")
        if any(PARENT_OF.get(c) in ids for c in ids):
            problems.append("parent+child category pair")
        if any(c not in VALID_CATEGORIES for c in ids):
            problems.append("unknown category id")
    for key in ("low price", "high price"):
        v = row[key]
        if v not in (None, "", "FREE") and not isinstance(v, int):
            problems.append(f"{key} not whole number or FREE")
    if row["description"] and len(row["description"].split()) > 200:
        problems.append("description over 200 words")
    return problems


RUN_GAP_DAYS = 30


def build_rows(conn):
    """Group multi-performance productions; emit template-shaped dicts."""
    today = date.today().isoformat()
    events = [dict(r) for r in conn.execute(
        # An exhibit that opened last month and runs through next year is still
        # a current listing, so keep anything whose run has not ended.
        #
        # Only confirmed-relevant events go in the import workbook: it is what
        # staff bulk-upload, so precision matters more than volume there.
        # Uncertain ones are not discarded — write_review_report lists them
        # under "Uncertain relevance" for a human call.
        "SELECT * FROM events WHERE verdict = 'new' AND relevance = 'in' "
        "AND (start_date >= ? OR (end_date IS NOT NULL AND end_date >= ?)) "
        "ORDER BY venue_name, title, start_date",
        (today, today),
    )]
    resolver = OrgResolver(conn)

    productions = {}
    for ev in events:
        venue_key = norm_venue(ev["venue_name"] or "")
        # Without a venue there is no evidence two same-titled events are the
        # same production, so each stands alone.
        key = (norm_title(ev["title"]), venue_key or f"__id{ev['id']}")
        productions.setdefault(key, []).append(ev)

    # Same title and venue months apart is a different run, not extra
    # performances of one production - split on a long gap.
    split_productions = []
    for members in productions.values():
        members.sort(key=lambda e: (e["start_date"], e["start_time"] or ""))
        run_group = [members[0]]
        for prev, cur in zip(members, members[1:]):
            prev_end = _as_date(prev["end_date"]) or _as_date(prev["start_date"])
            cur_start = _as_date(cur["start_date"])
            if prev_end and cur_start and (cur_start - prev_end).days > RUN_GAP_DAYS:
                split_productions.append(run_group)
                run_group = [cur]
            else:
                run_group.append(cur)
        split_productions.append(run_group)
    productions = {i: g for i, g in enumerate(split_productions)}

    out_rows, notes = [], []
    for _, performances in sorted(productions.items(), key=lambda kv: kv[1][0]["start_date"]):
        performances.sort(key=lambda e: (e["start_date"], e["start_time"] or ""))
        first = performances[0]
        venue_id, venue_match, vconf = resolver.resolve(first["venue_name"])
        pres_id = None
        pres_match, pconf = None, 0.0
        if first["presenter"] and norm_venue(first["presenter"]) != norm_venue(first["venue_name"] or ""):
            pres_id, pres_match, pconf = resolver.resolve(first["presenter"])
            if pres_id == venue_id:
                pres_id = None

        flags = []
        if venue_id is None:
            flags.append(f"NEEDS-DIRECTORY-ENTRY: venue '{first['venue_name']}' not in DelawareScene directory")
        elif vconf < VENUE_MATCH_ACCEPT:
            flags.append(f"CHECK-VENUE: matched '{venue_match}' at {vconf:.0f}%")
        if pres_id is not None and pconf < VENUE_MATCH_ACCEPT:
            flags.append(f"CHECK-PRESENTER: matched '{pres_match}' at {pconf:.0f}%")
        elif pres_id is None and first["presenter"] and pconf and pconf < VENUE_MATCH_REVIEW:
            flags.append(f"PRESENTER-NOT-IN-DIRECTORY: '{first['presenter']}'")
        if not first["category_ids"]:
            # Categories are a required DelawareScene field.
            flags.append("NEEDS-CATEGORY: no category could be assigned")
        if first["url"] and clean_url(first["url"]) is None:
            flags.append(f"UNUSABLE-URL: source had '{first['url']}'")
        if first["relevance"] == "review":
            flags.append(f"CHECK-RELEVANCE: {first['relevance_reason'] or 'uncertain'}")
        distinct_urls = {p["url"] for p in performances if p["url"]}
        if len(performances) > 1 and len(distinct_urls) > 1:
            # Continuation rows carry only date/time/ticket URL, so if the
            # grouped performances have their own detail pages they may be
            # separate events rather than one production.
            flags.append(f"CHECK-GROUPING: {len(performances)} performances with "
                         f"{len(distinct_urls)} different detail URLs")

        low = "FREE" if first["is_free"] else first["price_low"]
        high = None if first["is_free"] else first["price_high"]
        if isinstance(low, int) and isinstance(high, int) and high == low:
            high = None

        row = {
            "venue ID": venue_id,
            "presenter ID (if different)": pres_id,
            "title of program": first["title"],
            "categories": first["category_ids"],
            "URL": clean_url(first["url"]),
            "box office phone": first["phone"],
            "low price": low,
            "high price": high,
            "start date": fmt_date(first["start_date"]),
            "start time": fmt_time(first["start_time"]),
            "end date": fmt_date(first["end_date"]),
            "ticket URL": clean_url(first["ticket_url"]),
            "description": words_cap(first["description"]),
        }
        problems = validate_row(row)
        out_rows.append(row)
        notes.append({"event_id": first["id"], "flags": flags, "problems": problems,
                      "source_url": first["url"], "venue_match": venue_match,
                      "continuation": False})
        for perf in performances[1:]:
            crow = {h: None for h in HEADERS}
            crow["start date"] = fmt_date(perf["start_date"])
            crow["start time"] = fmt_time(perf["start_time"])
            crow["ticket URL"] = clean_url(perf["ticket_url"])
            out_rows.append(crow)
            notes.append({"event_id": perf["id"], "flags": [], "problems": [],
                          "source_url": perf["url"], "venue_match": None,
                          "continuation": True})
    return out_rows, notes


def write_xlsx(rows, path):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "events"
    ws.append(HEADERS)
    for row in rows:
        ws.append([row.get(h) for h in HEADERS])
    wb.save(path)


def geo_excluded(conn):
    """Events dropped for being outside Delaware, recomputed from raw rows.

    Out-of-scope events are not stored, so the report derives this from the
    raw payloads — staff should be able to see what the state filter removed.
    """
    import json as _json

    from . import geo

    out = []
    seen = set()
    for r in conn.execute(
        "SELECT s.name AS source, e.payload FROM raw_events e "
        "JOIN sources s ON s.id = e.source_id"
    ):
        raw = _json.loads(r["payload"])
        where, why = geo.verdict(raw.get("venue_city"), raw.get("venue_address"),
                                 raw.get("venue_name"))
        if where != "out":
            continue
        key = ((raw.get("title") or "")[:60], raw.get("venue_city"))
        if key in seen:
            continue
        seen.add(key)
        out.append({"source": r["source"], "title": raw.get("title"),
                    "where": raw.get("venue_city") or raw.get("venue_name"),
                    "why": why})
    return sorted(out, key=lambda e: (e["source"], e["title"] or ""))


def write_review_report(conn, rows, notes, path, dedupe_stats=None):
    dupes = conn.execute(
        "SELECT title, venue_name, start_date, scene_match, verdict_reason "
        "FROM events WHERE verdict='dupe' ORDER BY start_date"
    ).fetchall()
    review = conn.execute(
        "SELECT title, venue_name, start_date, scene_match, verdict_reason FROM events "
        "WHERE verdict='review' ORDER BY start_date"
    ).fetchall()
    # Rejected events are deleted by normalize, so these counts come from the
    # stats it recorded rather than from a query that could only return zero.
    norm = db.load_stats(conn, "normalize")
    out_of_scope = norm.get("out_of_scope", 0)
    non_de = geo_excluded(conn)
    today = date.today().isoformat()
    expired = conn.execute(
        "SELECT title, venue_name, start_date, end_date FROM events "
        "WHERE verdict = 'new' AND relevance = 'in' "
        "AND start_date < ? AND (end_date IS NULL OR end_date < ?) "
        "ORDER BY start_date DESC",
        (today, today),
    ).fetchall()
    uncertain = conn.execute(
        "SELECT s.name AS source, e.title, e.venue_name, e.start_date, e.relevance_reason "
        "FROM events e JOIN sources s ON s.id = e.source_id "
        "WHERE e.relevance = 'review' AND e.verdict = 'new' "
        "ORDER BY s.name, e.start_date"
    ).fetchall()
    flagged = [(r, n) for r, n in zip(rows, notes) if n["flags"] or n["problems"]]

    def esc(s):
        import html
        return html.escape(str(s if s is not None else ""))

    parts = [
        "<meta charset='utf-8'><title>SceneScout review report</title>",
        "<style>body{font-family:system-ui;margin:2rem auto;max-width:70rem;line-height:1.5}"
        "table{border-collapse:collapse;width:100%;font-size:0.85rem}"
        "td,th{border:1px solid #ccc;padding:4px 8px;text-align:left}"
        "h2{margin-top:2rem}.flag{color:#a00;font-weight:600}</style>",
        f"<h1>SceneScout review report — {date.today().isoformat()}</h1>",
        f"<p>{len(rows)} export rows ({sum(1 for n in notes if not n['continuation'])} productions) · "
        f"{len(flagged)} rows flagged for review · {len(dupes)} suppressed as duplicates · "
        f"{len(review)} borderline scene matches (held out of the export) · "
        f"{len(uncertain)} uncertain relevance (held out, listed below) · "
        f"{len(expired)} already past · "
        f"{out_of_scope} filtered as not arts &amp; culture · "
        f"{len(non_de)} excluded as outside Delaware</p>",
        (f"<p><strong>Note:</strong> {norm['unclassified_no_budget']} events were "
         f"filtered without being classified because the LLM budget ran out. "
         f"Re-run <code>normalize --llm-budget</code> higher to give them a real "
         f"verdict.</p>" if norm.get("unclassified_no_budget") else ""),
    ]
    if flagged:
        parts.append("<h2>Export rows needing attention</h2><table><tr><th>Title</th><th>Start</th><th>Flags</th><th>Validation</th><th>Source</th></tr>")
        for r, n in flagged:
            parts.append(
                f"<tr><td>{esc(r['title of program'])}</td><td>{esc(r['start date'])}</td>"
                f"<td class=flag>{esc('; '.join(n['flags']))}</td><td>{esc('; '.join(n['problems']))}</td>"
                f"<td><a href='{esc(n['source_url'])}'>src</a></td></tr>")
        parts.append("</table>")
    if review:
        parts.append("<h2>Borderline scene matches — human call</h2><table><tr><th>Scraped event</th><th>Date</th><th>Possible existing listing</th><th>Reason</th></tr>")
        for r in review:
            parts.append(
                f"<tr><td>{esc(r['title'])} @ {esc(r['venue_name'])}</td><td>{esc(r['start_date'])}</td>"
                f"<td>{esc(r['scene_match'])}</td><td>{esc(r['verdict_reason'])}</td></tr>")
        parts.append("</table>")
    if uncertain:
        parts.append("<h2>Uncertain relevance — not exported, needs a human call</h2>"
                     "<p>Neither the keyword rules nor the classifier could decide whether "
                     "these are arts and culture events. They are kept out of the import "
                     "workbook so it stays clean, and listed here so nothing is lost.</p>"
                     "<table><tr><th>Source</th><th>Event</th><th>Venue</th><th>Date</th>"
                     "<th>Why uncertain</th></tr>")
        for r in uncertain:
            parts.append(
                f"<tr><td>{esc(r['source'])}</td><td>{esc(r['title'])}</td>"
                f"<td>{esc(r['venue_name'])}</td><td>{esc(r['start_date'])}</td>"
                f"<td>{esc(r['relevance_reason'])}</td></tr>")
        parts.append("</table>")
    if non_de:
        parts.append("<h2>Excluded as outside Delaware</h2>"
                     "<p>These are real events run by Delaware organizations, but they "
                     "take place in another state, so they do not belong on "
                     "DelawareScene.</p>"
                     "<table><tr><th>Source</th><th>Event</th><th>Location</th><th>Why</th></tr>")
        for e in non_de:
            parts.append(
                f"<tr><td>{esc(e['source'])}</td><td>{esc(e['title'])}</td>"
                f"<td>{esc(e['where'])}</td><td>{esc(e['why'])}</td></tr>")
        parts.append("</table>")
    if dupes:
        parts.append("<h2>Suppressed as duplicates</h2>"
                     "<p>Either already on DelawareScene, or the same event reached us "
                     "from more than one source.</p>"
                     "<table><tr><th>Scraped event</th><th>Date</th><th>Duplicate of</th></tr>")
        for r in dupes:
            match = r["scene_match"] or r["verdict_reason"] or "another source"
            parts.append(
                f"<tr><td>{esc(r['title'])} @ {esc(r['venue_name'])}</td>"
                f"<td>{esc(r['start_date'])}</td><td>{esc(match)}</td></tr>")
        parts.append("</table>")
    if expired:
        parts.append("<h2>Past events — not exported</h2>"
                     "<p>Relevant and net-new, but their run has already ended.</p>"
                     "<table><tr><th>Event</th><th>Venue</th><th>Ran until</th></tr>")
        for r in expired:
            parts.append(
                f"<tr><td>{esc(r['title'])}</td><td>{esc(r['venue_name'])}</td>"
                f"<td>{esc(r['end_date'] or r['start_date'])}</td></tr>")
        parts.append("</table>")
    Path(path).write_text("\n".join(parts), encoding="utf-8")


def run(conn) -> dict:
    OUT_DIR.mkdir(exist_ok=True)
    rows, notes = build_rows(conn)
    stamp = date.today().strftime("%Y%m%d")
    xlsx_path = OUT_DIR / f"scenescout-export-{stamp}.xlsx"
    report_path = OUT_DIR / "review-report.html"
    write_xlsx(rows, xlsx_path)
    write_review_report(conn, rows, notes, report_path)
    held = conn.execute(
        "SELECT COUNT(*) c FROM events WHERE relevance = 'review' AND verdict = 'new'"
    ).fetchone()["c"]
    return {
        "rows": len(rows),
        "productions": sum(1 for n in notes if not n["continuation"]),
        "flagged": sum(1 for n in notes if n["flags"] or n["problems"]),
        "held_for_review": held,
        "xlsx": str(xlsx_path),
        "report": str(report_path),
    }
