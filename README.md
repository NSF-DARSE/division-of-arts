# SceneScout

**Finding the Delaware arts events the state calendar never hears about.**

[DelawareScene.com](https://delawarescene.com) is Delaware's public arts and
culture calendar. It fills up one way: a grantee organization remembers to log
in and submit an event. If a theater forgets, the show simply isn't on the state
calendar — and a resident looking for something to do that weekend never learns
it exists.

SceneScout goes and finds those events. It crawls 104 Delaware arts sources,
normalizes everything into one schema, keeps only arts and culture events that
actually take place in Delaware, drops anything already on the calendar, and
hands staff a workbook in the exact bulk-upload format DDOA already uses.

On the most recent run: **2,272 events pulled → 991 net-new events**, accepted by
the upload validator with zero errors.

> **The design in one line:** most of these sites already publish
> machine-readable events, so 78% of the data is extracted deterministically and
> the AI is spent only where structure runs out — reading the unstructured tail,
> judging what belongs on an arts calendar, and assigning categories.

`docs/presentation.html` is the seven-minute talk. [PLAN.md](PLAN.md) has the
architecture and the decision record.

---

## 1. Setup

Python 3.9 or newer. No database server, no API key required.

```bash
git clone https://github.com/talhaMah56/division-of-arts
cd division-of-arts
git submodule update --init          # the companion frontend; see the note
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

The second line matters: the companion web frontend
(`DODA-AI-Companion-2026-Hennovate`) is a submodule, and a plain clone leaves
that directory empty. Nothing in the pipeline or the site needs it, so skip it
if you only want SceneScout to run.

> Init it **without `--recursive`**, and do not use `git clone
> --recurse-submodules`. The two repositories declare each other as submodules,
> so recursing walks the cycle and clones both again one level down. Fixing that
> properly means dropping the `division-of-arts` submodule from the frontend
> repository, which lives outside this one.

If `lxml` fails to build on macOS, run `xcode-select --install` and retry.

## 2. See it working in 30 seconds

The repository ships the output of a full run, so you can look at the result
before running anything:

```bash
.venv/bin/python mock_site/app.py
```

Open **http://127.0.0.1:5050**. The calendar is already loaded with the events
DelawareScene currently lists — see [§5](#5-using-the-website) for what to click.

## 3. Running the pipeline

One command does everything:

```bash
.venv/bin/python -m scenescout run-all
```

Expect ~20 minutes with no AI backend, a few hours with one (it reads ~50
unstructured sites and classifies every ambiguous event). It writes
`out/scenescout-export-<date>.xlsx` and `out/review-report.html`.

Each stage is independent, idempotent, and re-runnable, so you can also step
through them:

| Command | What it does |
|---|---|
| `python -m scenescout scene` | Scrape DelawareScene: the organization directory (→ VenueIDs) and the live calendar (→ dedupe corpus) |
| `python -m scenescout registry` | Load `assets/websites.csv` and probe each source for its best extraction channel |
| `python -m scenescout extract` | Pull raw events through eight route workers |
| `python -m scenescout normalize` | Canonical schema, Delaware gate, relevance and category classification |
| `python -m scenescout dedupe` | Cross-source dedup, then match against the live calendar |
| `python -m scenescout export` | Write the bulk-upload workbook and the review report |
| `python -m scenescout stats` | Show what is in the database right now |

Useful flags:

```bash
python -m scenescout extract --workers 4          # parallelise (IO-bound)
python -m scenescout extract --routes tribe-rest  # only one channel
python -m scenescout extract --names "Delaware Art Museum"
python -m scenescout normalize --llm-budget 1200  # cap AI classification calls
python -m scenescout scene --days 180             # how far ahead to scrape
```

`--llm-budget` matters: set it above your event count for a definitive run. If
the budget runs out before every event is judged, the review report says how
many that affected rather than quietly dropping them.

Maintenance and tests:

```bash
python -m scenescout rediscover          # propose URLs for sources whose site moved
python -m scenescout rediscover --apply  # write back only page-verified matches
python tests/test_pipeline.py            # 38 regression tests, no network, no AI
```

## 4. Loading results into the site

```bash
.venv/bin/python mock_site/app.py
```

Open **http://127.0.0.1:5050**, and on the **Bulk upload** page choose
`out/scenescout-export-<date>.xlsx`. Every row is validated; new events are
added and anything already on the calendar is skipped. Then open **Calendar**.

## 5. Using the website

The site is a working stand-in for DDOA's intake, in two pages.

### Bulk upload (`/`)

- **Upload a workbook.** Pick an `.xlsx` in DDOA's 13-column format and press
  *Upload & validate*.
- **What you get back.** A summary line — how many events were added, how many
  skipped as duplicates, how many rejected — plus the running calendar total.
- **Rejected rows** are listed individually with the reason (bad date format,
  more than three categories, a price with cents, and so on).
- **Skipped duplicates** are collapsed into an expandable list, each saying
  whether it was already on DelawareScene or already imported.
- **Upload the same file twice** and the second one adds nothing. Duplicate
  checking runs against the whole calendar using the pipeline's own matcher.
- **Clear** wipes uploads and re-seeds from DelawareScene's currently-listed
  export.

### Calendar (`/calendar`)

- **Views.** *Month*, *Week*, and *Day*. Move with the arrow buttons, `←`/`→`,
  or jump back with *Today* or the `t` key. It opens on today.
- **Colour.** Events are coloured by discipline — music, theater, visual arts,
  dance, film, literature, festivals, lectures, kids — and subcategories inherit
  their parent's colour, so a jazz gig and a choral concert read alike.
- **Filter by discipline.** Click any legend chip to toggle it. Active chips are
  filled with their own colour; inactive ones go hollow and grey. *Show all* and
  *Hide all* are at the end of the legend.
- **Filter by origin.** *All* / *Already listed* / *Found by SceneScout* — the
  fastest way to see what the pipeline actually contributed. Events it added
  carry a doubled left border.
- **Search.** Filter by title or venue as you type.
- **Click any event** for its details: dates, admission, categories, VenueID,
  the source link, and whether it was already listed or newly found.
- **Multi-day exhibits** appear on every day of their run; all-day events sit in
  a strip above the timed grid.

Everything you upload is written back to `out/delawarescene-calendar.xlsx`, so
one file holds the existing listings and everything SceneScout found, together.

## 6. Running the site on AWS

**The site runs on AWS.** The pipeline is a batch job and stays on a laptop —
it needs no server — but the calendar is worth hosting so anyone with the link
can browse what SceneScout found. `deploy/` puts it on a single EC2 instance
with no container registry, no IAM roles, and no Docker.

To find the URL of a running deployment:

```bash
aws ec2 describe-instances \
  --filters Name=tag:Name,Values=scenescout-demo \
            Name=instance-state-name,Values=running \
  --query 'Reservations[].Instances[].PublicIpAddress' --output text
```

From anywhere that already has AWS credentials — CloudShell, a workshop VS Code
or desktop instance, an EC2 box with a role — it is one command:

```bash
./deploy/deploy-ec2.sh          # prints the URL once the site answers
```

From a laptop with no credentials, sign in first:

```bash
./deploy/aws-sso-login.sh                  # writes a short-lived 'kiro' profile
AWS_PROFILE=kiro ./deploy/deploy-ec2.sh
```

That script uses the OIDC **device flow**: it prints a URL and a code, you
approve them in any browser, and it writes temporary keys to `~/.aws/credentials`.
Nothing is stored in the repository, and the keys expire — rerun it to refresh.
Point it at a different tenant with `SSO_START_URL=... SSO_REGION=...`.

> An **Identity Center login is not automatically AWS account access.** A Kiro
> Pro start URL, for instance, authenticates the IDE subscription and returns an
> empty account list — the script says so and stops rather than failing
> obscurely. Deploy from a machine that already holds credentials instead.

`deploy-ec2.sh` finds the default VPC, reuses or creates a security group
opening port 80, resolves the current Amazon Linux 2023 AMI from SSM, and
launches a `t3.small` that clones this repository on boot
(`deploy/user-data.sh`), installs `deploy/requirements-site.txt`, and runs
gunicorn under systemd. **It deploys whatever is on GitHub, so push first.**
Re-running replaces the instance rather than adding another.

```bash
AWS_PROFILE=kiro ./deploy/deploy-ec2.sh --terminate   # tear it down
AWS_REGION=us-west-2 SCENESCOUT_INSTANCE_TYPE=t3.micro ./deploy/deploy-ec2.sh
```

A deployed instance sets `SCENESCOUT_PRELOAD=1`, which ingests
`assets/scenescout-export.xlsx` on first boot through the same code path an
upload takes — so the hosted calendar opens with all 1,616 events rather than
only the 660 DelawareScene already lists. It is off locally, so `python
mock_site/app.py` still starts from DelawareScene's own data and leaves you
something to demonstrate on the upload page.

If the site does not answer, read the boot log without needing an SSH key:

```bash
aws ec2 get-console-output --instance-id <id> --output text | tail -40
```

There is also a `Dockerfile` for App Runner, ECS, or Lightsail. It serves the
same app on `$PORT` (default 8080); the EC2 path above needs none of it.

> Port 80 is plain HTTP and the security group is open to the internet, which
> suits a short-lived demo and nothing else. The site has no login and accepts
> file uploads from anyone who finds it, so terminate it when you are done.

## 7. What you end up with

| File | What it is |
|---|---|
| `out/scenescout-export-<date>.xlsx` | **The deliverable** — net-new events in DDOA's 13-column bulk-upload format |
| `out/review-report.html` | Every event that did *not* make the workbook, and why |
| `out/delawarescene-calendar.xlsx` | The whole calendar: existing listings plus everything SceneScout added |
| `out/rediscovery-report.json` | Replacement-URL proposals for dead sources |
| `data/scenescout.db` | Pipeline database — sources, raw events, canonical events, scraped DelawareScene reference data |
| `data/mock_site.db` | The mock site's calendar |

To start over, delete `data/` for a clean pipeline run, or just
`data/mock_site.db` to reset the calendar to DelawareScene's listings.

## 8. Repository layout

```
scenescout/            the pipeline
  registry.py          source loading + eight-step route detection
  http.py              polite fetching: rate limits, robots.txt, cache, curl fallback
  scene.py             DelawareScene scrapers (org directory, live listings)
  extract/workers.py   eight route workers
  normalize.py         canonical schema, relevance gate, categories
  geo.py               the Delaware-only gate
  dedupe.py            cross-source dedup + matching against the live calendar
  export.py            bulk-upload workbook, validators, review report
  llm.py               AI backends and the deterministic keyword fallback
  rediscover.py        replacement URLs for sources whose site moved
mock_site/app.py       upload portal + calendar
wsgi.py                gunicorn entry point (seeds once, before workers fork)
deploy/                aws-sso-login.sh, deploy-ec2.sh, user-data.sh, slim deps
Dockerfile             same app as a container, for App Runner / ECS / Lightsail
tests/                 38 regression tests
assets/                curated source registry and DDOA templates/exports
docs/                  the presentation and the original case-study brief
prototypes/            early exploration, superseded (see its README)
```

## 9. How the extraction works

Each source is probed once and routed to the cheapest channel that actually
works. Structured channels are parsed exactly; only what is left goes to a model.

| Route | Sources | Events |
|---|---:|---:|
| The Events Calendar REST API | 15 | 1,370 |
| HTML read by an AI model | 53 | 479 |
| Library system JSON (33 branches) | 1 | 315 |
| Squarespace `?format=json` | 5 | 61 |
| RSS feeds | 12 | 27 |
| schema.org JSON-LD | 2 | 10 |
| iCal exports | 3 | 8 |
| WordPress custom post types | 7 | 2 |
| Bot-blocked or dead | 6 | — |

**1,764 of 2,272 events come from deterministic parsers with no AI calls.**

### AI backend

The model layer resolves in order, so the pipeline runs regardless of what is
available:

1. **Anthropic SDK** — set `ANTHROPIC_API_KEY`, or log in with `ant auth login`
2. **Claude CLI** — headless `claude -p`, picked up automatically if installed
3. **Keyword rules** — deterministic fallback; no extraction from unstructured
   HTML, but every other stage still works end to end

Force one with `SCENESCOUT_LLM=anthropic|claude-cli|rules`. A failed AI call
never silently downgrades the run: extraction raises rather than reporting
"found nothing", and a failed pull never deletes data it had already stored.

## 10. Delaware only

Several sources legitimately program out of state — the Delaware Academy of
Vocal Arts performs in Philadelphia and New York, and the Delaware Nature
Society runs programs at a preserve in Avondale, PA. `geo.py` drops those.

Every check is anchored to a *position* in the address rather than matched as a
substring, because the two collide constantly in real data:

- a state token must be the last comma-separated element — otherwise
  "3120 Barley Mill Rd, **Mt.** Cuba, DE 19807" reads as Montana;
- a ZIP must follow a state or end the string — Sussex addresses have five-digit
  house numbers like "15411 Abbotts Pond Road";
- localities are compared as whole segments — Clear Space Theatre is on
  "20 Baltimore Avenue" in Rehoboth Beach and belongs on the calendar;
- a leading `19` is not Delaware (Avondale PA is 19311, Kennett Square 19348).

Names shared with other states — Newark, Camden, Milford, Dover — resolve to
Delaware unless an address says otherwise, and an event with no location
evidence is kept rather than guessed, since every source is a Delaware
organization.

## 11. What reaches the workbook

The workbook holds only confirmed-relevant Delaware events, because staff
bulk-upload it directly. Everything else is accounted for in the review report
rather than dropped silently:

| Outcome | Where it goes |
|---|---|
| Confirmed relevant, not already listed | the `.xlsx` workbook |
| Venue missing from the directory, or a low-confidence match | in the workbook, flagged `NEEDS-DIRECTORY-ENTRY` / `CHECK-VENUE` |
| Already on DelawareScene | suppressed, listed under "Suppressed as duplicates" |
| Close-but-not-certain match to a listing | held out, under "Borderline scene matches" |
| An arts hint, but not enough to be sure | held out, under "Uncertain relevance" |
| Outside Delaware | excluded, under "Excluded as outside Delaware" |
| Already finished | held out, under "Past events" |
| No arts signal at all | filtered (counted in the report header) |

Relevance is decided cheapest-evidence-first: a disqualifying word in the
*title* rejects; the source's own tags decide where they are reliable; a
category word in the title admits; anything weaker goes to the model, and to a
human if the budget is spent.

## 12. Tests

```bash
.venv/bin/python tests/test_pipeline.py     # or: python -m pytest tests/ -q
```

38 tests, no network and no AI. Nearly every one encodes a defect an
adversarial code review found, so the file doubles as documentation of the
cases that are easy to get wrong:

- "20 Baltimore Avenue, Rehoboth Beach" is Delaware; "Mt. Cuba, DE" is not Montana
- "Free for members, $10 general" is ticketed, and "$0 to $43.98" keeps its ceiling
- "art" must match "arts"; "pop-up" must not mean Rock/Pop
- a matinee and an evening show on the same day are two events
- story time filed under "Arts and Crafts" is still out of scope

## 13. Known limitations

- **Six sources are unreachable.** Five are bot-blocked or fully client-rendered
  (OperaDelaware behind Cloudflare, wilmingtonde.gov behind a WAF, Theatre N,
  Schwartz Center, Inner City Cultural League) and one URL is dead. A headless
  browser would close most of this.
- **Venue directory coverage.** 402 of 637 productions resolve to a
  DelawareScene VenueID; the rest are flagged `NEEDS-DIRECTORY-ENTRY` because
  the organization is not in the directory yet — a real step in DDOA's workflow.
- **Source discovery** (finding venues not already in `assets/websites.csv`)
  remains a stretch goal; `rediscover` currently only repairs known sources.
