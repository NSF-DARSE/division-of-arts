# Delaware Arts Web Search

## Overview
Search the web via the DuckDuckGo API (`ddgs` package) for arts organizations
and events in Delaware. Queries can optionally be restricted to a curated list of
Delaware arts websites, one site at a time (serially), so each organization's
results are reported separately.

## Setup
Install dependencies:

    pip install -r requirements.txt

## Usage
Run a plain web search with one or more queries:

    python web_search_scripts/duck_search.py "Delaware arts events"

Restrict each query to the sites listed in `url_lists/sites.txt` and show
results grouped by website:

    python web_search_scripts/duck_search.py -s url_lists/sites.txt "Delaware arts"

Save results to a file (plain text or JSON) instead of stdout:

    python web_search_scripts/duck_search.py -s url_lists/sites.txt "Delaware arts" -o web_data/results.txt
    python web_search_scripts/duck_search.py -j "Delaware arts" -o web_data/results.json

Drop stale results that mention negative keywords (repeatable, comma-separated),
for example old events dated 2025 or earlier:

    python web_search_scripts/duck_search.py -x 2025,2024 "Delaware arts events"
    python web_search_scripts/duck_search.py -x 2025 -x 2024 "Delaware arts events"

Search engines under heavy querying often throttle with an empty response
that ddgs reports as `DDGSException: No results found.`. The script treats
this (like timeouts and rate limits) as a transient failure and retries it up
to `--retries` times, so a throttled site usually recovers on a later attempt.
If a site still returns nothing, it is reported as an error rather than a
result. Retrying a site list with `--retries 2` and a longer `--delay` (for
example `--delay 8`) keeps these errors rare.

If a site's server is slow to respond, raise the per-request connection timeout
with `--timeout` (default 60 seconds):

    python web_search_scripts/duck_search.py --timeout 30 "Delaware arts events"

Site lists are queried in parallel, `--workers` at a time, instead of one site
at a time. When `--workers` is not given, it defaults to the system's CPU count
minus 4 (never below 1), capped at the number of sites:

    python web_search_scripts/duck_search.py -s url_lists/alt_sites.txt "Delaware arts" --workers 8

Note: the site list is intentionally run as one `site:` query per site in
parallel rather than as a single `site:A OR site:B` query. The `auto` backend's
engines handle the OR syntax inconsistently (DuckDuckGo in particular times out
on it), and it loses the per-site grouping that this tool reports.

See exactly which search backends the installed `ddgs` actually provides, and enable
its diagnostics (engine errors, backend fallbacks) when a search fails or returns
"No results found.":

    python web_search_scripts/duck_search.py --list-backends
    python web_search_scripts/duck_search.py --verbose "Delaware arts events"

Note: the default backend is `auto`, which uses whatever search engines the
installed `ddgs` provides. If `--backend` names an engine the installed `ddgs`
does not provide (for example `bing` or `google` in current ddgs versions,
which are disabled), ddgs silently falls back to `auto`; the script warns about
that on stderr so you know why results may look wrong.

Parse the resulting webpages: fetch each result page's HTML, extract its readable
text, and store both in an SQLite database (`web_data/parsed_pages.db`):

    python web_search_scripts/duck_search.py "Delaware arts events" --parse
    python web_search_scripts/duck_search.py -s url_lists/sites.txt "Delaware arts" --parse --parse-db web_data/parsed_pages.db

To parse pages directly (standalone), pass URLs as arguments or on stdin:

    python web_search_scripts/web_parser.py "https://example.com" "https://example.org"
    python web_search_scripts/web_parser.py --db web_data/parsed_pages.db < urls.txt

Each query must be approved (`y`/`N`) before any search is sent. Use
`duck_search.py --help` for all options (result count, region, backend, retries,
delays, exclude-keywords filtering, output format, page parsing, and more).

## Repository Structure
- `web_search_scripts/` – source code (`duck_search.py`)
- `web_search_scripts/web_parser.py` – fetches and parses webpages into SQLite
- `url_lists/` – site lists used to restrict searches (`sites.txt`)
- `web_data/` – search output files
- `docs/` – optional documentation (Sphinx scaffold)
- `env/` – local Python virtual environment

## Documentation
This repository includes an optional Sphinx documentation scaffold under `docs/`.
