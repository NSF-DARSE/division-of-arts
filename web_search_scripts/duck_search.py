#!/usr/bin/env python3
"""Search the web using the DuckDuckGo API (ddgs package).

Usage:
    python duck_search.py "your query here" [...more queries]
    python duck_search.py --sites-file sites.txt "Delaware arts"
    python duck_search.py -o results.txt -s sites.txt "Delaware arts"

Requires the `ddgs` package (install with: pip install -r requirements.txt).

With --sites-file, each query is run against every site in the file, one
website at a time (serially). Each query must be approved once (y/N) before
any search is sent; the approved query is then reused for all sites. Transient
failures (timeouts / rate limits) are retried with backoff. Output is grouped
by website.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from ddgs import DDGS, exceptions


def search(
    query: str,
    max_results: int,
    region: str = "us-en",
    backend: str = "google",
) -> list[dict]:
    """Return a list of {title, href, body} dicts for a web search."""
    results = []
    with DDGS() as ddgs:
        for r in ddgs.text(
            query,
            region=region,
            max_results=max_results,
            backend=backend,
        ):
            results.append(
                {
                    "title": r.get("title", ""),
                    "href": r.get("href", ""),
                    "body": r.get("body", ""),
                }
            )
    return results


def read_sites_file(sites_file: Path) -> list[str]:
    """Read a list of sites from a text file.

    One site per line. Blank lines and lines starting with '#' are ignored.
    Lines may be bare domains ('example.com'), full URLs
    ('https://example.com/path'), or subdomains. A '*' wildcard is kept as-is
    (e.g. '*.example.com' restricts to all subdomains).
    """
    domains: list[str] = []
    for raw in sites_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "://" in line:
            netloc = urlparse(line).netloc
            if not netloc:
                continue
            domains.append(netloc)
        else:
            domains.append(line)
    return domains


def build_site_query(query: str, domain: str) -> str:
    """Append a single site: operator to restrict results to that site."""
    return f"({query}) (site:{domain})"


def confirm_query(query: str, domains: list[str]) -> bool:
    """Show a confirmation page and require explicit approval before sending."""
    lines = [
        "\n=== Search Confirmation ===",
        f"Query:  {query}",
    ]
    if domains:
        lines.append(
            f"Sites:  {len(domains)} (queried serially, one website at a time)"
        )
        for d in domains:
            lines.append(f"  - {d}")
    lines.append("Send this query to the search engine? [y/N]: ")
    sys.stderr.write("\n".join(lines))
    sys.stderr.flush()
    try:
        answer = input().strip().lower()
    except EOFError:
        sys.stderr.write("\n  (no input received; skipping query)\n")
        return False
    if answer in ("y", "yes"):
        sys.stderr.write("  approved\n")
        return True
    sys.stderr.write("  declined\n")
    return False


def search_with_retries(
    query: str,
    max_results: int,
    region: str,
    retries: int,
    retry_delay: float,
    backend: str = "google",
) -> list[dict]:
    """Run a search, retrying transient failures (timeout / rate limit)."""
    for attempt in range(1, retries + 2):
        try:
            return search(query, max_results, region, backend=backend)
        except (exceptions.TimeoutException, exceptions.RatelimitException) as exc:
            if attempt > retries:
                raise
            print(
                f"     retry {attempt}/{retries} after {exc}...",
                file=sys.stderr,
            )
            time.sleep(retry_delay * attempt)
    return []


def collect_site_results(
    query: str,
    domains: list[str],
    max_results: int,
    region: str,
    delay: float,
    retries: int,
    retry_delay: float,
    backend: str = "google",
) -> tuple[list[tuple[str, list[dict]]], list[tuple[str, str]]]:
    """Run `query` against each site serially.

    Returns (per_site_results, errors) where per_site_results is a list of
    (domain, results) pairs in the same order as `domains`.
    """
    per_site: list[tuple[str, list[dict]]] = []
    errors: list[tuple[str, str]] = []
    for i, domain in enumerate(domains, 1):
        site_query = build_site_query(query, domain)
        print(
            f"  -> querying site {i}/{len(domains)}: {domain}",
            file=sys.stderr,
        )
        try:
            found = search_with_retries(
                site_query, max_results, region, retries, retry_delay, backend
            )
        except exceptions.DDGSException as exc:
            errors.append((domain, str(exc)))
            print(f"     ERROR: {exc}", file=sys.stderr)
            continue
        except Exception as exc:  # defensive catch-all
            errors.append((domain, str(exc)))
            print(f"     ERROR: {exc}", file=sys.stderr)
            continue
        per_site.append((domain, found))
        if delay and i < len(domains):
            time.sleep(delay)
    return per_site, errors


def render_results(
    query: str,
    per_site: list[tuple[str, list[dict]]],
    errors: list[tuple[str, str]],
    stream,
) -> None:
    """Write results grouped by website to `stream`."""
    stream.write(f"\n=== Results by website for query: {query} ===\n")
    for domain, results in per_site:
        if not results:
            stream.write(f"\n-- {domain} --\n  (no results)\n")
            continue
        stream.write(f"\n-- {domain} --\n")
        for i, r in enumerate(results, 1):
            stream.write(f"   {i}. {r['title']}\n")
            stream.write(f"      {r['href']}\n")
            if r["body"]:
                stream.write(f"      {r['body'][:200]}\n")
    for domain, err in errors:
        stream.write(f"\n-- {domain} --\n  ERROR: {err}\n")


def parse_results(
    per_site: list[tuple[str, list[dict]]],
    db_path: Path,
) -> int:
    """Fetch and parse the unique result webpages from a search into SQLite.

    Args:
        per_site: (domain, results) pairs from a search.
        db_path: Path to the SQLite parsed-pages database.

    Returns:
        int: Number of webpages successfully parsed and stored.
    """
    try:
        from web_parser import parse_urls
    except ImportError as exc:
        print(
            "ERROR: --parse requires 'requests' and 'beautifulsoup4' "
            "(install with: pip install -r requirements.txt)",
            file=sys.stderr,
        )
        raise

    urls = sorted(
        {
            r["href"]
            for _, results in per_site
            for r in results
            if r.get("href")
        }
    )
    if not urls:
        print("  (no result URLs to parse)", file=sys.stderr)
        return 0
    print(f"\n  parsing {len(urls)} unique result webpage(s)...", file=sys.stderr)
    return parse_urls(urls, db_path=db_path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Search the web via DuckDuckGo and print results."
    )
    parser.add_argument("queries", nargs="+", help="Search query or queries")
    parser.add_argument(
        "-n", "--max-results", type=int, default=3,
        help="Max results per site (default: 3)",
    )
    parser.add_argument(
        "-r", "--region", default="us-en",
        help="DuckDuckGo region code (default: us-en)",
    )
    parser.add_argument(
        "-b", "--backend", default="google",
        help="Search engine backend (default: google)",
    )
    parser.add_argument(
        "-j", "--json", action="store_true",
        help="Print results as JSON instead of plain text",
    )
    parser.add_argument(
        "-s", "--sites-file", type=Path, default=None,
        help="Text file with one site per line to restrict search to",
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=None,
        help="Write results to this file instead of stdout",
    )
    parser.add_argument(
        "--delay", type=float, default=3.0,
        help="Seconds to wait between site queries (default: 3.0)",
    )
    parser.add_argument(
        "--retries", type=int, default=3,
        help="Retries per site for transient failures (timeout/rate limit) (default: 3)",
    )
    parser.add_argument(
        "--retry-delay", type=float, default=3.0,
        help="Base seconds for retry backoff (default: 3.0)",
    )
    parser.add_argument(
        "--parse", action="store_true",
        help="Fetch and parse the resulting webpages, storing HTML and text in SQLite",
    )
    parser.add_argument(
        "--parse-db", type=Path, default=Path("web_data/parsed_pages.db"),
        help="Path to the SQLite parsed-pages database (default: web_data/parsed_pages.db)",
    )
    args = parser.parse_args()

    domains: list[str] = []
    if args.sites_file:
        if not args.sites_file.is_file():
            print(f"ERROR: sites file not found: {args.sites_file}", file=sys.stderr)
            return 1
        domains = read_sites_file(args.sites_file)

    stream = None
    if args.output:
        try:
            stream = open(args.output, "w", encoding="utf-8")
        except OSError as exc:
            print(f"ERROR: cannot write output file: {exc}", file=sys.stderr)
            return 1

    try:
        for query in args.queries:
            if not confirm_query(query, domains):
                print(
                    f"  SKIPPED (query not confirmed): {query}",
                    file=sys.stderr,
                )
                continue

            if domains:
                per_site, errors = collect_site_results(
                    query,
                    domains,
                    args.max_results,
                    args.region,
                    args.delay,
                    args.retries,
                    args.retry_delay,
                    args.backend,
                )
            else:
                try:
                    results = search_with_retries(
                        query, args.max_results, args.region, args.retries, args.retry_delay, args.backend
                    )
                    per_site, errors = [("(all)", results)], []
                except exceptions.DDGSException as exc:
                    per_site, errors = [], [(query, str(exc))]
                except Exception as exc:
                    per_site, errors = [], [(query, str(exc))]

            out = stream or sys.stdout
            if args.json:
                payload = {
                    "query": query,
                    "sites": [
                        {"site": domain, "results": results}
                        for domain, results in per_site
                    ],
                    "errors": [{"site": d, "error": e} for d, e in errors],
                }
                json.dump(payload, out)
                out.write("\n")
                continue

            render_results(query, per_site, errors, out)

            if args.parse:
                parse_results(per_site, args.parse_db)
        return 0
    finally:
        if stream:
            stream.close()


if __name__ == "__main__":
    sys.exit(main())
