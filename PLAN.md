# SceneScout — Revised Pipeline Plan

**Project:** AI-assisted event discovery for DelawareScene.com (Delaware Division of the Arts)
**Status:** Built and running — see §12 for implementation status and measured results
**Date:** 2026-08-18 (plan) · 2026-08-19 (implementation)
**Why this document exists:** The original plan treated every source as an HTML page for an AI agent to scrape. Live reconnaissance of 31 sources (2026-08-18) shows that most of our sources expose structured, machine-readable event data — so the revised pipeline is **structure-first, LLM-last**: deterministic extractors for the ~80% of volume that has clean channels, LLM extraction only for the unstructured tail, and LLM classification/relevance-filtering everywhere. This is cheaper, faster, more accurate, and re-runnable on a schedule — which is what DDOA actually needs.

---

## 1. What changed from the original plan, and why

| Original plan | Revision | Reason (verified today) |
|---|---|---|
| Agent 2 "scrapes the events page" of every site generically | **Per-source extraction router**: each source gets its best machine channel first (REST API, JSON, ICS, RSS, JSON-LD), HTML+LLM only as fallback | 7/7 sampled sites running The Events Calendar have a live open REST API (`/wp-json/tribe/events/v1/events`) returning title/dates/venue/cost as JSON. Squarespace sites return event JSON via `?format=json`. The entire state library system is 4 JSON requests. CivicPlus towns export iCal. AI-scraping these would be re-deriving data that's already structured. |
| Dedup against `assets/submitted_events.csv` | **Dedup against live DelawareScene listings + `DelawareScene Currently Listed Events.xlsx`**; keep submitted_events.csv only as a historical test corpus | `submitted_events.csv` ends exactly today (2026-08-18) — only 5 of 1,382 rows are still upcoming. The scraper surfaces *future* events, so comparing against it would mark everything "net-new". The `Currently Listed` export (701 rows through June 2027) plus a live scrape of delawarescene.com is the correct corpus. |
| No plan for VenueID / PresenterID | **Scrape the DelawareScene org directory once** → complete name→ID map | The import spreadsheet is unusable without VenueID/PresenterID. Recon found the entire directory (~900 orgs, IDs up to ≥1179) on a single unpaginated page: `https://delawarescene.com/about/directory.php`. One fetch solves the hardest import requirement. |
| URL refresh + new-source discovery as the first agent's job | **Demoted to a stretch goal** — health-check the 104 curated sources cheaply; search-based discovery only if time remains | The source list is already curated with sitemap + events-page columns. Discovery is nice-to-have; extraction, dedup, and a valid export are the judged deliverables (Goals 1–3). |
| Agents as the unit of design | **Pipeline stages with SQLite as the spine**; each stage is a resumable CLI step | Checkpointed stages are debuggable and re-runnable; "agents" become the LLM calls inside stages B/C, where they genuinely add value. |

What stays from the original plan: the three-way division of labor (§8 maps stages back to Agent 1/2/3), SQLite storage, dedup before export, the bulk-upload guideline compliance, and the mock DelawareScene upload site as the demo.

---

## 2. Architecture overview

```
websites.csv ──► [A] Source registry ──► [B] Extract ──► [C] Normalize+Classify ──► [D] Dedupe ──► [E] Validate+Export
                    (SQLite: sources)      (raw_events)     (events)                  (vs. scene_listings)   (13-col .xlsx + review report)
                                                                                          ▲
delawarescene.com ──► scene scrapers ──► scene_orgs (name→ID map) ── scene_listings ──────┘
```

Everything lives in one SQLite DB (`data/scenescout.db`). Every stage reads the previous stage's table and writes its own, so any stage can be re-run independently.

---

## 3. Stage A — Source registry

Load `assets/websites.csv` (104 sources, tiers 1–4 already represented) into a `sources` table, then:

1. **Health check** every `Organization URL` and `Events` URL (HEAD/GET, browser User-Agent, follow redirects). Record status.
2. **Route detection (automated probe cascade, cached per source).** For each live source, probe in order and store the first winner as `extract_route`:
   1. The Events Calendar REST: `<origin>/wp-json/tribe/events/v1/events` (14 sources have `tribe_events` sitemaps; all 7 sampled had the API live)
   2. Other WP event CPT via `<origin>/wp-json/wp/v2/<cpt>` (titles/links; dates come from the event page)
   3. Squarespace: events URL + `?format=json` (look for `events-stacked` collection)
   4. ICS: `?ical=1` (WP Events Manager / The Events Calendar), CivicPlus `iCalendar.aspx?catID=N&feed=calendar`, MEC `?mec-ical-feed=1`
   5. RSS feed advertised in HTML `<link>` or known paths (`/event/rss/`, `/programs/rss/`)
   6. schema.org **Event JSON-LD** in event-page HTML (Wix Events, Eventbrite)
   7. Server-rendered HTML listing → **LLM extraction**
   8. `headless` (Playwright) — flagged, not run in the MVP
3. **Broken URLs** → re-find via DuckDuckGo (`web_search_scripts/duck_search.py` already exists) — automated but logged for human confirmation.
4. **Stretch:** discovery of brand-new sources via search (original Agent-1 idea), appending to `websites.csv`.

Politeness rules (apply pipeline-wide): send a real browser User-Agent (destateparks.com and Simpleview 403 generic clients), ≤1 request/second/domain, cache raw responses, honor robots.txt (`find.py` already implements the check; delawarescene.com has no robots.txt — 404).

## 4. Stage B — Extraction (per-route workers)

Verified routes from today's recon, by source family:

| Source family | Route (verified example) | Volume seen today |
|---|---|---|
| WP + The Events Calendar (14+ sources) | `GET /wp-json/tribe/events/v1/events?per_page=50&page=N` → JSON with title, start/end, venue obj, cost | Winterthur 328 · Del. Nature Soc. 433 · DelArt 40 · Smyrna Opera House 21 · Riverfront 7 · Biggs 1 |
| Squarespace | events page + `?format=json` → `upcoming[]` with epoch-ms `startDate` | Clear Space 17 · Developing Arts 27 |
| Wix + Wix Events | `event-pages-sitemap.xml` → per-event **JSON-LD** (name, startDate, location) | City Theater |
| Eventbrite-backed (CCAC) | scrape eventbrite.com/e/ links → Eventbrite JSON-LD | 2+ |
| Delaware Libraries (LibCal, 33 branches) | `GET /ajax/calendar/list?c=-1&date=0000-00-00&perpage=100&page=N` — open JSON, rich fields incl. `categories_arr`, branch, cost, registration URL | 320 statewide in 4 requests |
| CivicPlus towns (Newark, Smyrna) | `iCalendar.aspx?catID=N&feed=calendar` → ICS (window-filter Smyrna's recurrences, they expand to 2029) | Newark 4+ · Smyrna dozens |
| Delaware State Parks | `/programs/?ical=1` → ICS (50 events) + `/programs/rss/` (needs browser UA) | 50 |
| Visit Delaware (Simpleview) | `/event/rss/` (30 items) now; token-gated JSON API as stretch | 30 |
| DSU (Drupal) | server-rendered `/events?year=&month=` → detail URLs with dates in path | month view |
| WP CPT without date meta (Del. Symphony `dso_events`, Candlelight `mec-events`) | wp/v2 REST for title/link + parse date text from event page | 21 · 4 |
| Unstructured HTML tail (Milton Theatre, The Queen, Newark Symphony, Hagley, Possum Point `/2026-season`, Brandywine Baroque, Pieces of a Dream, Rehoboth Art League) | fetch listing/detail HTML → strip boilerplate → **LLM extraction** into the canonical schema, with source URL retained | ~8 sources sampled; more in full run |
| Hard cases (deferred) | Grand Opera House (Tessitura TNEW, client-rendered), OperaDelaware (CueBox behind Cloudflare 403), Dover (EvoGov client-rendered), Wilmington (WAF-blocks all non-browser traffic) | needs Playwright / alternate aggregator; Grand + OperaDelaware events partly recoverable via Visit Delaware & TicketMaster links |

Every fetch is stored in `raw_events` (source, channel, URL, payload, fetched_at) for provenance and cheap re-runs.

**LLM extractor (the AI in "AI-assisted"):** one function, `extract_events(html_text, source) -> [Event]`, using a structured-output prompt against the canonical schema; used only for route 7. Temperature 0, JSON-schema-validated output, and a "no events found" escape hatch. Model calls are batched per page, not per event.

## 5. Stage C — Normalization + classification

Canonical `events` table (superset of Goal 2's schema):

```
events(id, source_id, title, presenter, venue_name, venue_address, venue_city,
       start_date, start_time, end_date, end_time, occurrences_json,   -- each performance datetime
       category_ids,           -- DelawareScene IDs, ≤3
       url, ticket_url, phone, description,
       price_low, price_high, is_free,
       relevance, relevance_reason,        -- arts/culture gate
       content_hash, first_seen, last_seen)
```

- **Dates:** everything → ISO internally; ranges keep start+end; multi-performance productions keep every occurrence in `occurrences_json` (needed for the export's continuation-row convention). Sanity gates: within horizon (today → +18 months), end ≥ start.
- **Relevance gate + categories in one LLM call** per event: assign up to 3 DelawareScene category IDs (1–29) and an in-scope verdict. Rules encoded in the prompt: subcategory implies parent (never emit both, e.g. 17 not 9+17); add 5 (Free) when admission is free; add 6 (Kids & Family) when explicitly family programming. The gate is what excludes library story time/job fairs (pre-filtered by LibCal categories 38196 Arts & Crafts / 38197 Book Discussions / 38206 Community & Culture, then LLM-screened) and Nature Society summer camps.
- **Venue canonicalization:** alias table seeded from `scene_orgs` names + the 227 venue spellings in `submitted_events.csv`; fuzzy-match (RapidFuzz token_set) with manual override file.
- **Formatting to spec** happens at export, not here (keep canonical data clean).

## 6. Stage D — Deduplication

**Scene-side corpus (`scene_listings`):**
1. Bootstrap from `assets/DelawareScene Currently Listed Events.xlsx` (701 rows → June 2027).
2. Live scrape: `https://delawarescene.com/search/?start=YYYY-MM-DD` — one unpaginated page per day (~79 events/day), links match `/event/{id}/{slug}` → dedupe scene-side by numeric ID. Iterate the horizon day-by-day (~90–540 requests, cached). First probe the search form's "Anytime!" dates option — it may return everything in one request.

**Matching (scraped event vs. scene listing):**
- **Block** on date overlap (scene start–end window ±1 day) — cuts comparisons to a few hundred per event.
- **Score** = weighted title similarity (normalized: lowercase, strip punctuation/ordinals/"2026"/"annual") + venue similarity (via canonical venue) + date proximity.
- **Verdicts:** `dupe` (suppress), `new` (export), `review` (borderline — shown to staff, never silently dropped). Same-title-different-venue is *not* a dupe ("13: The Musical" runs at both Clear Space and Griffin Theatre — both legit).
- **Cross-source self-dedup** first, same scorer: the same event often appears on the venue site + Visit Delaware + a library calendar; keep the richest record, merge URLs.

`submitted_events.csv` (historical, 33 column-shifted rows — parse defensively) is used to *test* the matcher: scrape a past window, confirm known-submitted events get flagged as dupes.

## 7. Stage E — Validation + export (+ demo site)

**Exporter** writes the exact 13-column workbook (`venue ID, presenter ID (if different), title of program, categories, URL, box office phone, low price, high price, start date, start time, end date, ticket URL, description`) from template `assets/DelawareScene-Bulk-Upload-BLANK.xlsx`:

- **VenueID/PresenterID** resolved by fuzzy match against `scene_orgs` (scraped once from `directory.php`). No match → blank + row flagged `NEEDS-DIRECTORY-ENTRY` in the review report (staff must add the org first — that's the real DDOA workflow).
- **Multi-performance convention:** first row full; each further performance a continuation row with only start date, start time, ticket URL.
- **Format rules encoded as hard validators:** dates MM/DD/YYYY; times "7:30 p.m."; phone XXX-XXX-XXXX; prices whole numbers or FREE; ≤3 categories, no parent+child pairs; URLs with scheme; description ≤200 words.
- Output: `out/scenescout-export-YYYYMMDD.xlsx` + `out/review-report.html` (per-row provenance links, dedupe verdicts, validation flags).

**Demo site ("mock DelawareScene"):** a small static/Flask page that mimics the bulk-upload intake — drag-drop the .xlsx, run the same validators client/server-side, and render accepted rows as a DelawareScene-style event list. This demonstrates the full loop end-to-end without touching the real site.

## 8. Division of labor (maps to the original 3 agents)

- **Agent 1 → Stage A**: registry loader, health checker, route-prober; stretch: search-based discovery (reuse `duck_search.py`).
- **Agent 2 → Stages B–D**: scene_orgs + scene_listings scrapers, route workers, LLM extractor/classifier, dedupe.
- **Agent 3 → Stage E**: validators, xlsx exporter, review report, mock upload site.

## 9. Build order (hackathon-pragmatic)

1. `scene_orgs` (directory.php, one fetch) + `scene_listings` (day-scrape + xlsx bootstrap) — *unlocks IDs and dedup for everyone else*
2. Structured route workers: tribe REST → LibCal → Squarespace JSON → ICS/RSS *(covers the large majority of event volume on day one)*
3. Normalizer + LLM classifier/relevance gate
4. Dedupe (self, then vs. scene)
5. Exporter + validators + review report
6. LLM HTML-tail extractor
7. Mock upload site
8. Stretch: Playwright for TNEW/CueBox/EvoGov, Simpleview token capture, discovery agent

## 10. Risks & mitigations

- **Bot blocking:** Wilmington WAF-blocks everything non-browser; OperaDelaware sits behind Cloudflare. → Defer to Playwright stretch; recover their events via aggregators (Visit Delaware RSS, Ticketmaster links on The Queen's pages).
- **WebFetch-style proxies get 403'd** where plain `curl` with a Chrome UA succeeds (destateparks, Simpleview) → all pipeline fetches use a browser UA.
- **Tribe sitemaps include years of past events** → always prefer the REST API (upcoming-only by default) over sitemap crawls.
- **Recurring-event explosions** (Smyrna ICS → 2029) → horizon window filter (18 months) everywhere.
- **LLM hallucination in the HTML tail** → structured output schema, require a source URL quote per event, spot-check queue in the review report; dedup + validators as backstops.
- **Data-quality lesson from `submitted_events.csv`** (33 rows column-shifted by commas) → our exporter always quotes; validators reject malformed rows before they ship.

## 11. Appendix — key verified endpoints (recon 2026-08-18)

```
DelawareScene directory (→ Venue/Presenter IDs):  https://delawarescene.com/about/directory.php
DelawareScene day listing:                        https://delawarescene.com/search/?start=YYYY-MM-DD
Libraries (all-state JSON, no auth):              https://delawarelibraries.libcal.com/ajax/calendar/list?c=-1&date=0000-00-00&perpage=100&page=N
  arts categories: 38196 Arts&Crafts · 38197 Book Discussions · 38206 Community&Culture (exclude 38203 Story Time, 38200 Jobs, 38202 Social Services)
Tribe REST (any The Events Calendar site):        <origin>/wp-json/tribe/events/v1/events?per_page=50&page=N
Squarespace JSON:                                 <events-page-url>?format=json
Newark ICS:                                       https://newarkde.gov/common/modules/iCalendar/iCalendar.aspx?catID=21&feed=calendar
Smyrna ICS:                                       https://smyrna.delaware.gov/common/modules/iCalendar/iCalendar.aspx?catID=27&feed=calendar
State Parks ICS / RSS:                            https://www.destateparks.com/programs/?ical=1  ·  /programs/rss/   (browser UA required)
Visit Delaware RSS:                               https://www.visitdelaware.com/event/rss/
DSU month listing:                                https://www.desu.edu/events?year=YYYY&month=M
```

---

## 12. Implementation status (2026-08-19)

The pipeline described above is built and running end to end. Code lives in
`scenescout/`; run it with `python -m scenescout run-all` (see README).

### What each stage actually does now

| Stage | Module | State |
|---|---|---|
| A. Registry + route detection | `registry.py` | 104 sources loaded and tier-tagged; an 8-step probe cascade auto-detects each source's best channel |
| B. Extraction | `extract/workers.py` | 8 route workers: tribe REST, LibCal JSON, Squarespace JSON, ICS, RSS, WP custom post types, schema.org JSON-LD, and HTML→LLM |
| C. Normalize + classify | `normalize.py` | Canonical schema, price/phone/date parsing, tiered relevance gate, DelawareScene category IDs |
| D. Dedupe | `dedupe.py` | Cross-source self-dedup, then fuzzy match against live DelawareScene listings + the Currently Listed export |
| E. Export | `export.py` | 13-column bulk-upload workbook, VenueID/PresenterID resolution, continuation rows, hard validators, HTML review report |
| Demo | `mock_site/app.py` | Mock DelawareScene intake: upload the workbook, same validators, renders accepted events |

### Route distribution across the 104 curated sources

| Route | Sources | Kind |
|---|---|---|
| `html-llm` | 53 | LLM reads the page (the unstructured tail) |
| `tribe-rest` | 14 | The Events Calendar REST API |
| `squarespace-json` | 5 | `?format=json` |
| `rss` | 10 | Advertised feeds |
| `wpv2:*` | 7 | WordPress custom post types |
| `ics` | 3 | iCal exports |
| `jsonld` | 2 | schema.org Event markup |
| `libcal` | 1 | Delaware Libraries (33 branches in one endpoint) |
| `headless` / `none` | 7 | Bot-blocked, client-rendered, or dead URLs — deferred |

Deterministic routes (everything except `html-llm`, `rss`, and the date-less
`wpv2` post types) produce **~1,750 events with zero LLM calls**, which is what
"structure-first" buys.

### Course corrections made during implementation

- **robots.txt matching.** Python's `RobotFileParser` does substring matching on
  the user-agent, so a legacy `User-agent: es` block on delart.org matched
  "SceneScout" and denied the whole site. `http.py` now does RFC 9309 exact
  product-token matching.
- **TLS.** This machine's Python links an old LibreSSL that cannot negotiate
  with some hosts (desu.edu). `http.py` falls back to system `curl` on a
  transport error.
- **State Parks ICS.** The `?ical=1` export returns the 50 *oldest* events
  (2024), not upcoming ones — the route was overridden to `html-llm`.
- **LLM failure semantics.** A session-cap failure originally demoted the
  backend to keyword-rules for the rest of the run, which silently turned
  "could not look" into "found nothing" *and* wiped those sources' previously
  good rows. Extraction now raises `LLMUnavailable`, a failed pull never
  deletes stored data, and CLI concurrency is capped so parallel workers do not
  trip over each other.

### Review and testing

A multi-dimension adversarial code review (correctness, spec compliance,
robustness, data quality) produced 14 confirmed findings, all fixed. The
highest-impact ones:

1. Events whose relevance was uncertain were dropped from *both* the export and
   the review report — they now export with a `CHECK-RELEVANCE` flag.
2. The row identity hash ignored start time and stripped ordinals, so a matinee
   overwrote its evening performance and "3rd Grade Art Show" overwrote
   "4th Grade Art Show".
3. Venue resolution used `token_set_ratio`, which scores any token subset 100 —
   a bare venue string like "Newark" matched "Newark Arts Alliance" perfectly
   and silently attached the wrong VenueID.
4. Events described as "Free for members, $10 general" exported as FREE.
5. `norm_venue` did substring replacement, turning "Lincoln" into "Loln".

`tests/test_pipeline.py` locks in every one of these as a regression test
(17 tests, no network or LLM required).

### Known gaps

- 5 sources are bot-blocked or fully client-rendered (OperaDelaware/CueBox,
  wilmingtonde.gov, Theatre N, Schwartz Center, Inner City Cultural League) and
  2 URLs are dead (Delaware Historical Society, UD Arts). Playwright and
  search-based URL rediscovery are the stretch goals that would close these.
- Source discovery (finding venues not already in `websites.csv`) remains a
  stretch goal; the DuckDuckGo scripts in `web_search_scripts/` are the
  starting point.

### Delaware-only constraint (added 2026-08-19)

DelawareScene is a Delaware calendar, but the relevance gate originally only
judged *arts relevance*, not geography — so out-of-state programming by
Delaware organizations passed straight through. A measurement of the first
full run found 21 such events: seven Delaware Academy of Vocal Arts concerts
in Philadelphia, New York, and Rahway NJ; a Philadelphia youth dance festival;
and thirteen Delaware Nature Society bird-banding sessions at Bucktoe Creek
Preserve in Avondale, PA.

`geo.py` now gates every event on location, and it parses the state/ZIP
*position* of an address rather than matching substrings — the naive version
rejected Clear Space Theatre because its address is "20 Baltimore Avenue,
Rehoboth Beach, DE 19971". Two further traps the implementation handles:
a leading `19` in a ZIP does not mean Delaware (Avondale PA is 19311), and a
five-digit house number is not a ZIP ("37401 Malloy St ... 19971").

Delaware municipalities are checked before out-of-state localities so that
names shared with other states — Newark, Camden, Milford, Dover, Georgetown,
Milton — resolve to Delaware unless an address explicitly says otherwise.
Events with no location evidence are kept rather than guessed, since every
source is a Delaware organization. On the live data the gate removed 33 raw
events across 14 distinct programs, with zero false positives.

Auditing the resulting workbook surfaced a second scope problem: the
destination-marketing aggregators (Visit Rehoboth, Visit Delaware, Visit
Wilmington, Riverfront Wilmington) were tier 2, so the "trusted arts
organization" rule admitted everything they listed — "Happy Hour at Bodhi
Kitchen", "$1 Wings & $4 Yuengling Drafts", "Sunday Night Karaoke", Blue Rocks
baseball. They are now screened like libraries and government calendars, and
the keyword filter rejects food, drink, nightlife, and sports promotions.

Screening those sources raised a third question: what happens to a title the
keywords cannot judge, like the gallery show "Diane Billas | Superficial"?
Dropping it silently is wrong, and exporting it floods the workbook (an early
attempt produced 513 such rows). Uncertain events are now held out of the
import workbook — which staff bulk-upload, so precision matters most there —
and listed in the review report under "Uncertain relevance" for a human call.

### Second review round (2026-08-19)

A second adversarial review — this one aimed at the Delaware gate, the
relevance rules, and the export — raised 21 findings and reproduced most of
them against the live database. They fell into three groups.

**The geographic gate was wrong in both directions.** It leaked real
out-of-state events: "Festival of Fountains at Longwood" was in the export,
because Longwood Gardens is in Kennett Square PA but its name carries no
locality word, and a Philadelphia address passed as unknown because
localities were only compared against the city field, never the address. It
also rejected genuine Delaware venues: "Washington Street Ale House",
"Delaware Public Media — WDDE", "The Reading Room, Dover Public Library" and
"Cape May-Lewes Ferry" all matched generic words — *washington*, *media*,
*reading*, *cape may* — that were being tested as substrings. Worst of all,
"3120 Barley Mill Rd, Mt. Cuba, DE 19807" read as **Montana**, because the
first two-letter token after a comma beat the real trailing `DE`.

The rewrite fixes the class rather than the instances. Every check is anchored
to a position: a state token must be the last comma-separated element
(optionally followed by a ZIP), and a ZIP must follow a state or end the
string. Localities are compared as whole comma-separated segments, never
substrings. Out-of-state venues are matched by specific full names rather than
generic words. Fields are evaluated independently, because concatenating them
moves the "state position" onto whichever field happens to come last.

**The keyword rules were quietly losing events.** Every category pattern ended
in `\b` *after* the alternation, so "art" never matched "arts", nor "exhibit"
"exhibitions" — a large silent recall loss. `\bpop\b` matched the "pop" in
"pop-up", tagging a pop-up opera as Music: Rock/Pop. Disqualifying words were
tested against whole descriptions, so a single incidental "summer camp" was
deleting grantee concerts — 41 of them. Disqualifying words now count only in
the title; category words are read from title and body, but a title match is
reported separately, because a category found only in body text is weak
evidence that an event is an arts event.

**The report was not telling the truth.** "Filtered as out of scope" was
structurally always zero, since normalize deletes the rows it rejects and the
count queried for them. Self-duplicates and already-past events appeared in no
section at all. Counters now come from stats normalize persists, and every
terminal state an event can reach has a section in the report.

Tightening relevance then produced a different failure worth recording: 771
events landed in the "needs a human call" bucket, and reading it showed mostly
"Library Closed", "GED TEST PREP", "Passport Services" and nature walks. A
review queue full of non-events is as useless as no queue. So LibCal's own
tags are now trusted (they are better data than a guess from the title), an
event with *some* arts hint goes to the LLM and then to a human, and an event
with no arts signal anywhere is filtered. When the LLM budget runs out before
those decisions are made, the report says how many events that affected and
what to re-run — the budget should never be a silent arbiter.

A final read of the shipped workbook caught two more: story time was reaching
the calendar because the LibCal tag shortcut ran *before* the title-exclusion
check, so an event the library filed under "Arts and Crafts" was admitted on
the tag and never tested against its own name; and facility notices ("Video
Studio Reservation", "Museum Closed: Maintenance Week") were being exported as
events. Adding "closed" as a disqualifying word then immediately over-rejected
"Artist Talk: Closed Forms", so the pattern is anchored to closure phrasing
rather than the bare word.

That trap recurred four times in one session — a filter that is right about
the common case and wrong about a specific, predictable exception. Each one is
now a regression test; the suite stands at 37, no network, no LLM.

**Final run (2026-08-19):** 2,272 events from 98 reachable sources → 991
export rows across 637 productions, accepted by the mock intake portal with
zero validation errors. 162 suppressed as duplicates, 35 excluded as outside
Delaware, 968 filtered as not arts and culture, and — with a large enough LLM
budget — nothing left undecided: 901 classification calls, zero events
filtered unclassified, zero queued for a human on relevance grounds.
