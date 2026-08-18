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

Parse the resulting webpages: fetch each result page's HTML, extract its readable
text, and store both in an SQLite database (`web_data/parsed_pages.db`):

    python web_search_scripts/duck_search.py "Delaware arts events" --parse
    python web_search_scripts/duck_search.py -s url_lists/sites.txt "Delaware arts" --parse --parse-db web_data/parsed_pages.db

To parse pages directly (standalone), pass URLs as arguments or on stdin:

    python web_search_scripts/web_parser.py "https://example.com" "https://example.org"
    python web_search_scripts/web_parser.py --db web_data/parsed_pages.db < urls.txt

Each query must be approved (`y`/`N`) before any search is sent. Use
`duck_search.py --help` for all options (result count, region, backend, retries,
delays, output format, page parsing, and more).

## Repository Structure
- `web_search_scripts/` – source code (`duck_search.py`)
- `web_search_scripts/web_parser.py` – fetches and parses webpages into SQLite
- `url_lists/` – site lists used to restrict searches (`sites.txt`)
- `web_data/` – search output files
- `docs/` – optional documentation (Sphinx scaffold)
- `env/` – local Python virtual environment

## Documentation
This repository includes an optional Sphinx documentation scaffold under `docs/`.
