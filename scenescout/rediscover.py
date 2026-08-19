"""Find replacement URLs for sources whose site moved or died.

This is the surviving piece of the original "Agent 1" idea: when a curated URL
stops resolving, search for the organization by name and propose a new URL.
Proposals are never applied silently — each one is written to a report for a
human to confirm, because an automatic swap can quietly repoint a grantee at
someone else's website.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

from rapidfuzz import fuzz

from . import db, http, registry

OUT_DIR = Path(__file__).resolve().parent.parent / "out"

# Aggregators and directories that will rank for an org's name but are not
# the org's own website.
BLOCKED_HOSTS = (
    "facebook.", "instagram.", "twitter.", "x.com", "linkedin.", "yelp.",
    "tripadvisor.", "eventbrite.", "youtube.", "wikipedia.", "mapquest.",
    "yellowpages.", "bbb.org", "guidestar.", "charitynavigator.",
    "delawarescene.com", "visitdelaware.com", "facebook.com",
)


def _search(query, max_results=8):
    from ddgs import DDGS

    try:
        with DDGS() as ddgs:
            return list(ddgs.text(query, region="us-en", max_results=max_results))
    except Exception:  # noqa: BLE001 - search is best-effort
        return []


def _plausible(url, org_name):
    """Score a candidate URL as the org's own site."""
    host = urlparse(url).netloc.lower()
    if not host or any(b in host for b in BLOCKED_HOSTS):
        return 0.0
    stem = host.replace("www.", "").split(".")[0]
    name_key = "".join(ch for ch in org_name.lower() if ch.isalnum())
    stem_key = "".join(ch for ch in stem if ch.isalnum())
    # Domain resembling the org name is the strongest signal available.
    score = fuzz.partial_ratio(stem_key, name_key)
    if host.endswith(".org"):
        score += 5
    return min(score, 100.0)


STOPWORDS = {"the", "of", "and", "inc", "ltd", "association", "society",
             "center", "centre", "company", "delaware", "de", "arts", "art"}


def _verify_page(url, name):
    """Confirm the candidate page is this Delaware organization.

    A name-shaped domain is not enough: searching "Delaware Historical Society"
    surfaces delawareohiohistory.org, which matches on name but is in Ohio.
    """
    status, body, _ = http.fetch(url, max_age_hours=6)
    if status != 200:
        return {"reachable": False, "http_status": status,
                "name_in_page": False, "delaware_in_page": False}
    try:
        from bs4 import BeautifulSoup

        text = BeautifulSoup(body, "html.parser").get_text(" ", strip=True).lower()
    except Exception:  # noqa: BLE001
        text = body.decode("utf-8", "replace").lower()

    distinctive = [w for w in "".join(
        ch if ch.isalnum() else " " for ch in name.lower()
    ).split() if w not in STOPWORDS and len(w) > 2]
    hits = sum(1 for w in distinctive if w in text)
    name_in_page = bool(distinctive) and hits >= max(1, len(distinctive) // 2)
    # The bare word "delaware" is not a state signal — Delaware, Ohio and
    # Delaware County both use it. A 302 area code, a DE ZIP (197xx-199xx),
    # or a delaware.gov host are specific to the state.
    in_delaware = bool(
        re.search(r"\(?302\)?[\s.-]?\d{3}[\s.-]?\d{4}", text)
        or re.search(r"\b(19[789]\d{2})\b", text)
        or re.search(r",\s*de\s+19\d{3}", text)
        or "delaware.gov" in text
        or urlparse(url).netloc.lower().endswith("delaware.gov")
    )
    return {"reachable": True, "http_status": status,
            "name_in_page": name_in_page, "delaware_in_page": in_delaware}


def find_candidates(conn, name) -> list:
    results = _search(f"{name} Delaware official website events")
    seen, candidates = set(), []
    for r in results:
        url = r.get("href") or r.get("url") or ""
        if not url:
            continue
        host = urlparse(url).netloc.lower()
        if host in seen:
            continue
        seen.add(host)
        score = _plausible(url, name)
        if score <= 0:
            continue
        origin = f"{urlparse(url).scheme}://{host}"
        verdict = _verify_page(origin, name)
        candidates.append({
            "url": origin,
            "title": r.get("title", "")[:120],
            "name_match": round(score, 1),
            "confirmed": bool(verdict["reachable"] and verdict["name_in_page"]
                              and verdict["delaware_in_page"]),
            **verdict,
        })
    candidates.sort(
        key=lambda c: (c["confirmed"], c["reachable"], c["name_match"]), reverse=True
    )
    return candidates[:5]


def run(conn, apply_confident: bool = False) -> dict:
    """Propose replacement URLs for broken/blocked sources.

    With apply_confident=True, a reachable candidate whose domain closely
    matches the organization name is written back to the registry and
    re-probed; everything else is reported only.
    """
    broken = [dict(r) for r in conn.execute(
        "SELECT * FROM sources WHERE status IN ('broken', 'blocked') ORDER BY name"
    )]
    # A domain already claimed by a different source is almost always a
    # same-sector neighbour, not this organization's new home: searching
    # "Delaware Historical Society" surfaces history.delaware.gov, which is
    # the Division of Historical & Cultural Affairs — a different entity.
    claimed = {}
    for r in conn.execute("SELECT id, name, home_url FROM sources WHERE home_url IS NOT NULL"):
        host = urlparse(r["home_url"]).netloc.lower().replace("www.", "")
        if host:
            claimed.setdefault(host, r["name"])

    report = []
    for src in broken:
        candidates = find_candidates(conn, src["name"])
        for c in candidates:
            owner = claimed.get(urlparse(c["url"]).netloc.lower().replace("www.", ""))
            c["claimed_by"] = owner if owner and owner != src["name"] else None
            if c["claimed_by"]:
                c["confirmed"] = False
        applied = None
        best = candidates[0] if candidates else None
        # Auto-apply only when the page itself confirms the organization and
        # its Delaware locality — a name-shaped domain alone is not enough.
        if (apply_confident and best and best["confirmed"]
                and best["name_match"] >= 85
                and urlparse(best["url"]).netloc != urlparse(src["home_url"] or "").netloc):
            conn.execute(
                "UPDATE sources SET home_url = ?, events_url = NULL, extract_route = NULL, "
                "notes = ? WHERE id = ?",
                (best["url"], f"url rediscovered (was {src['home_url']})", src["id"]),
            )
            conn.commit()
            applied = best["url"]
        report.append({
            "source": src["name"],
            "status": src["status"],
            "old_url": src["home_url"],
            "old_events_url": src["events_url"],
            "candidates": candidates,
            "applied": applied,
        })

    if apply_confident and any(r["applied"] for r in report):
        registry.probe_all(conn, only_missing=True)

    OUT_DIR.mkdir(exist_ok=True)
    path = OUT_DIR / "rediscovery-report.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return {
        "checked": len(report),
        "with_candidates": sum(1 for r in report if r["candidates"]),
        "applied": sum(1 for r in report if r["applied"]),
        "report": str(path),
    }
