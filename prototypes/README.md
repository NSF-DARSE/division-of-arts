# Prototypes

Early exploration, kept for the record. None of it runs as part of the
pipeline — `scenescout/` superseded all of it — but each script answered a
question that shaped the final design.

| Script | Author | What it explored | What it taught us |
|---|---|---|---|
| `find.py` | talha | Fetch a list of URLs and pull the same-domain links off each page, honouring robots.txt | Sitemaps are the cheapest way to enumerate a WordPress site's events, and robots handling has to be built in from the start — both carried into `scenescout/registry.py` and `scenescout/http.py` |
| `duck_search.py` | Andrew Kallai | Search DuckDuckGo, optionally restricted to one site at a time | Search can find an organization's site, but ranking alone cannot confirm it is the *right* organization — which is why `scenescout/rediscover.py` verifies a candidate page before proposing it |
| `web_parser.py` | Andrew Kallai | Fetch result pages, extract readable text, store both in SQLite | Caching raw fetches in SQLite makes iteration bearable; `scenescout/http.py` does exactly this |
| `url_lists/` | — | The hand-built site lists these scripts ran against | Grew into the curated registry at `assets/websites.csv` |

To run one:

```bash
.venv/bin/python prototypes/find.py prototypes/url_lists/sites.txt -o /tmp/links.txt
.venv/bin/python prototypes/duck_search.py "Delaware arts events"
```
