# SceneScout — Delaware Arts Event Discovery Pipeline

AI-assisted, multi-source event discovery for the Delaware Division of the Arts'
[DelawareScene.com](https://delawarescene.com) calendar. SceneScout crawls 100+
Delaware arts sources (grantee websites, non-grantee venues, public libraries,
government calendars), normalizes everything into one schema, suppresses events
already listed on DelawareScene, and produces a validated bulk-upload workbook
ready for staff review. See [PLAN.md](PLAN.md) for the architecture and the
recon evidence behind it.

**Design principle: structure-first, LLM-last.** Most sources expose structured
data (WordPress Events Calendar REST, LibCal JSON, Squarespace JSON, iCal, RSS,
schema.org JSON-LD) — those are extracted deterministically. The LLM does what
only it can: reading the genuinely unstructured tail, judging arts-and-culture
relevance, and assigning DelawareScene category IDs.

## Presentation

`docs/presentation.html` is the seven-minute talk — open it in a browser. Eight
full-screen sections with timing markers, a pipeline flow diagram, and the
measured results from a live run.

## Running it

Everything below assumes you are in the repository root. Nothing needs a
database server, an API key, or network access beyond the sites being scraped.

### 1. Set up (once)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Python 3.9 or newer. If `lxml` fails to build, install Xcode command line tools
(`xcode-select --install`) and retry.

### 2. See the result without running anything

The repository ships the output of a full run, so you can look at the
deliverable straight away:

```bash
.venv/bin/python mock_site/app.py       # then open http://127.0.0.1:5050
```

The site starts with DelawareScene's currently-listed events already loaded and
keeps whatever you upload, so it is ready to demo on first launch.

### 3. Run the pipeline yourself

```bash
.venv/bin/python -m scenescout run-all
```

Roughly 20 minutes without an LLM backend, a few hours with one (the LLM reads
about 50 unstructured sites and classifies every ambiguous event). It writes
`out/scenescout-export-<date>.xlsx` and `out/review-report.html`.

To run it in pieces — each stage is independent and re-runnable:

```bash
.venv/bin/python -m scenescout scene       # DelawareScene org IDs + live listings
.venv/bin/python -m scenescout registry    # load websites.csv, auto-detect routes
.venv/bin/python -m scenescout extract     # pull raw events (add --workers 4 to parallelise)
.venv/bin/python -m scenescout normalize   # canonical schema + classification
.venv/bin/python -m scenescout dedupe      # suppress already-listed events
.venv/bin/python -m scenescout export      # bulk-upload .xlsx + review report
.venv/bin/python -m scenescout stats       # what is in the database right now
```

`normalize --llm-budget N` caps classification calls (default 60). Set it above
the number of events for a definitive run; the review report tells you if the
budget ran out before every event was judged.

Two maintenance commands:

```bash
.venv/bin/python -m scenescout rediscover          # propose URLs for dead sources
.venv/bin/python -m scenescout rediscover --apply  # write back confirmed matches only
.venv/bin/python tests/test_pipeline.py            # 37 regression tests, no network
```

### 4. Load the results into the mock site

Start the site, open http://127.0.0.1:5050, and upload
`out/scenescout-export-<date>.xlsx` on the **Bulk upload** page. Every row is
validated against the DDOA submission guidelines; new events are added and
anything already on the calendar is skipped. Then open **Calendar**.

### What you end up with

| File | What it is |
|---|---|
| `out/scenescout-export-<date>.xlsx` | The deliverable — net-new events in DDOA's 13-column bulk-upload format |
| `out/review-report.html` | Every event that did *not* make the workbook, and why |
| `out/delawarescene-calendar.xlsx` | The whole calendar: DelawareScene's existing listings plus everything SceneScout added |
| `out/rediscovery-report.json` | Replacement-URL proposals for dead sources |
| `data/scenescout.db` | Pipeline database — sources, raw events, canonical events, scraped DelawareScene reference data |
| `data/mock_site.db` | The mock site's calendar |

To start over: delete `data/` for a clean pipeline run, or just
`data/mock_site.db` to reset the calendar back to DelawareScene's listings.

### LLM backend

The extraction/classification LLM resolves in this order:

1. **Anthropic SDK** (`claude-opus-5`) — set `ANTHROPIC_API_KEY` (or log in via
   `ant auth login`).
2. **claude CLI** — headless `claude -p`, picked up automatically if Claude Code
   is installed.
3. **Rules** — deterministic keyword classifier; no extraction from unstructured
   HTML, but the rest of the pipeline still works end to end.

Force one with `SCENESCOUT_LLM=anthropic|claude-cli|rules`. `normalize` takes
`--llm-budget N` to cap classification calls (default 60).

## Delaware only

DelawareScene lists events *in Delaware*, but several sources legitimately
program out of state — the Delaware Academy of Vocal Arts performs in
Philadelphia and New York, and the Delaware Nature Society runs programs at a
preserve in Avondale, PA. `geo.py` drops those regardless of how arts-relevant
they are.

Every check is anchored to a *position* rather than matched as a substring,
because the two collide constantly in real addresses:

- a state token must be the last comma-separated element, optionally followed
  by a ZIP — otherwise "3120 Barley Mill Rd, **Mt.** Cuba, DE 19807" reads as
  Montana;
- a ZIP must follow a state or end the string — Sussex and Kent addresses have
  five-digit house numbers like "15411 Abbotts Pond Road";
- localities are compared as whole comma-separated segments — Clear Space
  Theatre is on "20 Baltimore Avenue" in Rehoboth Beach;
- out-of-state venues are matched by full name, not by generic words, so
  "Washington Street Ale House" and "Delaware Public Media" survive while
  "Longwood Gardens" (Kennett Square PA) does not.

Fields are evaluated independently, since concatenating them moves the state
position onto whichever field comes last. The Delaware municipality list is
consulted before the out-of-state list, so names shared with other states
(Newark, Camden, Milford, Dover) resolve to Delaware unless an address
explicitly says otherwise, and a leading `19` in a ZIP does not mean Delaware
(Avondale PA is 19311, Kennett Square is 19348).
Events with no location evidence are **kept**, not guessed — every source is a
Delaware organization, so a missing address is not evidence of absence. The LLM
relevance gate independently screens descriptions for out-of-state events, so
the two layers back each other up.

## What reaches the workbook

The import workbook is what staff bulk-upload, so it holds only
confirmed-relevant Delaware events. Everything else is accounted for in
`out/review-report.html` rather than dropped silently:

| Outcome | Where it goes |
|---|---|
| Confirmed relevant, not already listed | the `.xlsx` workbook |
| Venue not in the DelawareScene directory, or a low-confidence venue match | in the workbook, flagged `NEEDS-DIRECTORY-ENTRY` / `CHECK-VENUE` |
| Already on DelawareScene | suppressed, listed under "Suppressed as already listed" |
| Close-but-not-certain match to an existing listing | held out, listed under "Borderline scene matches" |
| An arts hint, but not enough to be sure | held out, listed under "Uncertain relevance" |
| Outside Delaware | excluded, listed under "Excluded as outside Delaware" |
| Already finished | held out, listed under "Past events" |
| No arts signal at all | filtered out (counted in the report header) |

Relevance is decided in this order, cheapest evidence first: a disqualifying
word in the *title* rejects; the source's own tags decide where they are
reliable (LibCal labels every library event); a category word in the title
admits; anything with a weaker hint goes to the LLM, and to a human if the
budget is spent; anything with no arts signal at all is filtered. If the LLM
budget runs out before those calls are made, the report says how many events
that affected — the budget should never be a silent arbiter.

## Tests

`tests/test_pipeline.py` covers parsing, the Delaware gate, keyword
classification, dedup, and export validation — no network, no LLM. Almost every
test encodes a specific bug an adversarial code review found, so the file
doubles as documentation of the cases that are easy to get wrong:

- a street called "Baltimore Avenue" in Rehoboth Beach is not Maryland, and
  "Mt. Cuba, DE" is not Montana;
- "Free for members, $10 general" is a ticketed event, and "$0 to $43.98"
  keeps its ceiling;
- "art" must match "arts", and "pop-up" must not mean Rock/Pop;
- a matinee and an evening show on the same day are two events, and "3rd Grade
  Art Show" is not "4th Grade Art Show";
- "Lincoln Theatre" must survive venue-name normalization that strips "Inc".

```bash
.venv/bin/python tests/test_pipeline.py      # or: python -m pytest tests/ -q
```

## Mock upload portal

A small Flask app that mimics the DelawareScene intake, in two pages.

```bash
.venv/bin/python mock_site/app.py   # http://127.0.0.1:5050
```

The calendar is **persistent and pre-seeded**. On first run it loads
`assets/DelawareScene Currently Listed Events.xlsx` — the 700 events already on
DelawareScene — so the site has real content before you upload anything.
Uploads merge into that same calendar, and the combined result is written to
`out/delawarescene-calendar.xlsx` after every upload, so one file holds the
existing listings and everything SceneScout found, together.

**`/` — bulk upload.** Drop in `out/scenescout-export-YYYYMMDD.xlsx`. Every row
is validated against the DDOA submission guidelines (date and time formats,
phone and price rules, at most three categories with no parent+child pair,
200-word descriptions). Multi-performance continuation rows are reassembled
onto their parent production first, exactly as the real importer would, so a
skeleton row is not rejected for "missing title".

Then each event is checked against the *whole* calendar — DelawareScene's own
listings and everything uploaded before — using the same matcher the pipeline's
dedupe stage uses. Uploading the same workbook twice adds nothing the second
time; the skipped events are listed so you can see what matched. Rejected rows
are reported with the reason.

**`/calendar` — the calendar.** Month, week, and day views over everything
uploaded, with:

- events coloured by discipline (music, theater, visual arts, dance, film,
  literature, festivals, lectures, kids), subcategories inheriting the parent
  colour, so a jazz gig and a choral concert read alike at a glance;
- a clickable legend that filters by discipline — active chips are filled with
  their own colour so the legend doubles as the colour key, inactive ones go
  hollow and grey, plus show-all / hide-all shortcuts (every chip clears WCAG
  AA contrast, minimum 5.4:1);
- a title/venue search;
- multi-day exhibits shown on every day of their run, and all-day events
  separated from timed ones;
- overlapping events at the same hour sharing the column width instead of
  hiding each other;
- VenueIDs resolved back to organization names from the scraped DelawareScene
  directory, since the workbook carries only IDs;
- an **All / Already listed / Found by SceneScout** filter, with events the
  pipeline contributed marked by a doubled left border, so you can see at a
  glance what SceneScout added to what was already there;
- click any event for its full detail — dates, admission, categories, VenueID,
  source link, and whether it was already listed or newly found.

Keyboard: `←`/`→` to move, `t` for today.

## Repository structure

- `scenescout/` — the pipeline package
  - `db.py` schema · `http.py` polite fetching (rate limits, robots, cache,
    curl fallback) · `registry.py` source registry + route auto-detection
  - `scene.py` DelawareScene scrapers (org directory → VenueIDs, day listings)
  - `extract/` per-route workers (tribe REST, LibCal, Squarespace, ICS, RSS,
    WP CPT, JSON-LD, HTML→LLM)
  - `normalize.py` canonical schema + tiered relevance/category classification
  - `geo.py` Delaware-only gate (see below)
  - `dedupe.py` self-dedup + fuzzy match against DelawareScene listings
  - `export.py` 13-column bulk-upload workbook + validators + review report
  - `llm.py` Claude backends (SDK / CLI / rules fallback)
- `mock_site/` — mock DelawareScene upload portal
- `assets/` — source list (`websites.csv`), DDOA templates and exports
- `out/` — generated export + `review-report.html`
- `web_search_scripts/`, `url_lists/` — legacy DuckDuckGo discovery scripts
  (kept for the source-discovery stretch goal)

## Known limitations

- 5 sources are bot-blocked or fully client-rendered (OperaDelaware/CueBox,
  Wilmington WAF, Theatre N, Schwartz Center, Inner City Cultural League) —
  marked `headless` and deferred; their events partly surface via aggregators.
- 2 source URLs in websites.csv are dead (Delaware Historical Society,
  UD Arts) — candidates for the search-based rediscovery stretch goal.
- Rules-only classification is conservative; the LLM backends materially
  improve category quality on the unstructured tail.
