#!/usr/bin/env python3
"""Search the web using the DuckDuckGo API (ddgs package).

Usage:
    python duck_search.py "your query here" [...more queries]
    python duck_search.py --sites-file sites.txt "Delaware arts"
    python duck_search.py -o results.txt -s sites.txt "Delaware arts"
    python duck_search.py -x 2025,2024 "Delaware arts"
    python duck_search.py --list-backends
    python duck_search.py --verbose "Delaware arts"

Requires the `ddgs` package (install with: pip install -r requirements.txt).

With --sites-file, each query is run against every site in the file, one
website at a time (serially). Each query must be approved once (y/N) before
any search is sent; the approved query is then reused for all sites. Transient
failures (timeouts / rate limits) are retried with backoff. Output is grouped
by website. When a search fails, the true underlying ddgs error is reported (not a
generic message), and `--verbose` shows ddgs's own diagnostics.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

from ddgs import DDGS, exceptions


def configure_ddgs_logging(verbose: bool) -> None:
    """Forward ddgs diagnostics to stderr.

    ddgs logs useful details (e.g. 'backend does not exist or is disabled',
    'using auto', 'Error in engine X: ...') but attaches a NullHandler by
    default, so nothing is shown. Wire a stderr handler so the true reason for a
    failed or filtered search is visible.
    """
    logger = logging.getLogger("ddgs")
    logger.setLevel(logging.DEBUG if verbose else logging.WARNING)
    for h in list(logger.handlers):
        if isinstance(h, logging.NullHandler):
            logger.removeHandler(h)
    if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("ddgs: %(levelname)s: %(message)s"))
        logger.addHandler(handler)


def available_text_backends() -> list[str]:
    """Return the text-search backends available in the installed ddgs package."""
    try:
        from ddgs import engines

        keys = list(engines.ENGINES.get("text", {}).keys())
    except Exception:
        keys = []
    return sorted(keys)


def describe_ddgs_exception(exc: BaseException) -> str:
    """Render a ddgs exception with its full, unwrapped detail.

    ddgs frequently wraps the real underlying error inside the exception (e.g.
    DDGSException(ValueError('HTTP 502 ...'))), which a plain str() can hide.
    This walks args and chained causes to expose the true error.
    """
    seen_ids: set[int] = set()
    seen_msgs: set[str] = set()
    lines: list[str] = []

    def walk(ex: BaseException) -> None:
        if id(ex) in seen_ids:
            return
        seen_ids.add(id(ex))
        msg = str(ex).strip()
        rendered = f"{type(ex).__name__}: {msg}" if msg else type(ex).__name__
        if rendered in seen_msgs:
            return
        seen_msgs.add(rendered)
        lines.append(rendered)
        for arg in ex.args:
            if isinstance(arg, BaseException):
                walk(arg)
        if ex.__cause__ is not None:
            walk(ex.__cause__)
        elif ex.__context__ is not None:
            walk(ex.__context__)

    walk(exc)
    return " | ".join(lines)


def search(
    query: str,
    max_results: int,
    region: str = "us-en",
    backend: str = "auto",
    timeout: int = 60,
) -> list[dict]:
    """Return a list of {title, href, body} dicts for a web search."""
    results = []
    with DDGS(timeout=timeout) as ddgs:
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


def parse_keywords(values: list[str] | None) -> list[str]:
    """Normalize repeatable, comma-separated keyword argument values."""
    keywords: list[str] = []
    for value in values or []:
        for kw in value.split(","):
            kw = kw.strip()
            if kw:
                keywords.append(kw)
    return keywords


def filter_by_keywords(
    results: list[dict], keywords: list[str]
) -> list[dict]:
    """Drop results whose title or snippet contains any keyword (case-insensitive)."""
    if not keywords:
        return results
    lowered = [kw.lower() for kw in keywords]
    kept = []
    for r in results:
        text = " ".join((r.get("title", ""), r.get("body", ""))).lower()
        if any(kw in text for kw in lowered):
            continue
        kept.append(r)
    return kept


def confirm_query(query: str, domains: list[str]) -> bool:
    """Show a confirmation page and require explicit approval before sending."""
    lines = [
        "\n=== Search Confirmation ===",
        f"Query:  {query}",
    ]
    if domains:
        lines.append(
            f"Sites:  {len(domains)}"
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
    backend: str = "auto",
    timeout: int = 60,
) -> list[dict]:
    """Run a search, retrying transient failures (timeout / rate limit / empty results)."""
    for attempt in range(1, retries + 2):
        try:
            return search(query, max_results, region, backend=backend, timeout=timeout)
        except exceptions.DDGSException as exc:
            if not is_transient_error(exc) or attempt > retries:
                raise
            print(
                f"     retry {attempt}/{retries} after {exc}...",
                file=sys.stderr,
            )
            time.sleep(retry_delay * attempt)
    raise exceptions.DDGSException(f"search failed for query: {query!r}")


def is_transient_error(exc: BaseException) -> bool:
    """Return True when a ddgs error is likely transient and worth retrying.

    Throttled backends often surface as empty result sets, which ddgs reports
    as a plain "No results found." exception rather than a dedicated
    RatelimitException. Treat both forms as retryable.
    """
    if isinstance(exc, (exceptions.TimeoutException, exceptions.RatelimitException)):
        return True
    return "no results" in str(exc).lower()


def query_site(
    index: int,
    total: int,
    query: str,
    domain: str,
    max_results: int,
    region: str,
    delay: float,
    retries: int,
    retry_delay: float,
    backend: str,
    timeout: int,
) -> tuple[str, list[dict] | None, str | None]:
    """Run `query` against one site; return (domain, results, error_detail)."""
    site_query = build_site_query(query, domain)
    print(
        f"  -> querying site {index}/{total}: {domain}",
        file=sys.stderr,
    )
    try:
        found = search_with_retries(
            site_query, max_results, region, retries, retry_delay, backend, timeout
        )
    except exceptions.DDGSException as exc:
        err_detail = describe_ddgs_exception(exc)
        print(f"     ERROR: {err_detail}", file=sys.stderr)
        return domain, None, err_detail
    except Exception as exc:  # defensive catch-all
        err_detail = describe_ddgs_exception(exc)
        print(f"     ERROR: {err_detail}", file=sys.stderr)
        return domain, None, err_detail
    if delay:
        time.sleep(delay)
    return domain, found, None


def collect_site_results(
    query: str,
    domains: list[str],
    max_results: int,
    region: str,
    delay: float,
    retries: int,
    retry_delay: float,
    backend: str = "auto",
    timeout: int = 60,
    workers: int = 1,
) -> tuple[list[tuple[str, list[dict]]], list[tuple[str, str]]]:
    """Run `query` against each site, up to `workers` at a time.

    Returns (per_site_results, errors) where per_site_results is a list of
    (domain, results) pairs in the same order as `domains`.
    """
    total = len(domains)
    n_workers = max(1, min(workers, total))

    def run_one(args: tuple[int, str]) -> tuple[str, list[dict] | None, str | None]:
        i, domain = args
        return query_site(
            i, total, query, domain, max_results, region, delay,
            retries, retry_delay, backend, timeout,
        )

    if n_workers == 1:
        outcomes = [run_one((i, domain)) for i, domain in enumerate(domains, 1)]
    else:
        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            outcomes = list(pool.map(run_one, enumerate(domains, 1)))

    per_site = [(d, found) for d, found, _ in outcomes if found is not None]
    errors = [(d, detail) for d, _, detail in outcomes if detail is not None]
    return per_site, errors


def effective_workers(requested: int | None, n_sites: int) -> int:
    """Return the worker count to use for a site list.

    When `requested` is None, default to the machine's CPU count minus 4 (never
    below 1), without exceeding the number of sites. An explicit value is used
    as-is (still capped at the number of sites).
    """
    if requested is None:
        requested = max(1, (os.cpu_count() or 1) - 4)
    return max(1, min(requested, n_sites))


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
        description=(
            "Search the web via DuckDuckGo (ddgs) for arts organizations and "
            "events, print the results, and optionally parse the result pages "
            "into a SQLite database for later reading with db_reader.py."
        ),
        epilog=(
            "examples:\n"
            "  Plain search:\n"
            '    python web_search_scripts/duck_search.py "Delaware arts events"\n'
            "\n"
            "  Restrict each query to a curated list of sites:\n"
            '    python web_search_scripts/duck_search.py -s url_lists/sites.txt "Delaware arts"\n'
            "\n"
            "  Save results to a file, as JSON:\n"
            '    python web_search_scripts/duck_search.py -j "Delaware arts" -o web_data/results.json\n'
            "\n"
            "  Drop stale results that mention 2025 or 2024:\n"
            '    python web_search_scripts/duck_search.py -x 2025,2024 "Delaware arts events"\n'
            "\n"
            "  Fetch and parse result pages into the SQLite database:\n"
            '    python web_search_scripts/duck_search.py "Delaware arts" --parse\n'
            "\n"
            "  See which search backends the installed ddgs provides:\n"
            "    python web_search_scripts/duck_search.py --list-backends\n"
            "\n"
            "Run `duck_search.py --help` for all options (result count, region, "
            "backend, retries, delays, exclude-keywords filtering, output format, "
            "page parsing, and more)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "queries",
        nargs="*",
        help=(
            "One or more search queries. With --sites-file, each query is run "
            "against every site in the file and results are grouped by site."
        ),
    )
    parser.add_argument(
        "-n", "--max-results", type=int, default=3,
        help="Maximum number of results to keep per site (default: 3)",
    )
    parser.add_argument(
        "-r", "--region", default="us-en",
        help=(
            "DuckDuckGo region code used to localize results, e.g. us-en "
            "(default: us-en); see the ddgs docs for supported codes"
        ),
    )
    parser.add_argument(
        "-b", "--backend", default="auto",
        help=(
            "Search engine backend(s) as a comma-separated list, e.g. "
            "gpt-4o or auto (default: auto). If a named backend is not "
            "available, the script falls back to auto and warns on stderr; use "
            "--list-backends to see what is available."
        ),
    )
    parser.add_argument(
        "-x", "--exclude-keywords", action="append", default=None,
        metavar="KW",
        help=(
            "Drop results whose title or snippet contains any of these keywords. "
            "Comma-separated and repeatable, e.g. -x 2025,2024 to remove "
            "outdated event listings."
        ),
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help=(
            "Show ddgs diagnostics -- engine errors and backend fallbacks -- on "
            "stderr. Useful when a search fails or returns 'No results found.'"
        ),
    )
    parser.add_argument(
        "--list-backends", action="store_true",
        help=(
            "Print the text-search backends the installed ddgs actually provides "
            "and exit."
        ),
    )
    parser.add_argument(
        "-j", "--json", action="store_true",
        help="Emit results as JSON (each query/site as a structured record) "
             "instead of plain text.",
    )
    parser.add_argument(
        "-s", "--sites-file", type=Path, default=None,
        help=(
            "Text file with one site per line, e.g. url_lists/sites.txt. Each "
            "query is run as one 'site:' query per line in the file, and "
            "results are grouped by website."
        ),
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=None,
        help=(
            "Write results to this file (plain text unless --json is given) "
            "instead of stdout."
        ),
    )
    parser.add_argument(
        "--delay", type=float, default=3.0,
        help=(
            "Seconds to wait between site queries to avoid search-engine "
            "throttling (default: 3.0). Raise it (e.g. --delay 8) when "
            "sites keep getting throttled."
        ),
    )
    parser.add_argument(
        "--retries", type=int, default=2,
        help=(
            "Additional attempts per site for transient failures -- timeouts, "
            "rate limits, and throttled 'No results found.' responses "
            "(default: 2)."
        ),
    )
    parser.add_argument(
        "--retry-delay", type=float, default=3.0,
        help=(
            "Base seconds for the exponential backoff between retries "
            "(default: 3.0)."
        ),
    )
    parser.add_argument(
        "--timeout", type=int, default=60,
        help=(
            "Per-request connection timeout in seconds (default: 60). Raise "
            "this for slow servers."
        ),
    )
    parser.add_argument(
        "--workers", type=int, default=None,
        help=(
            "Number of sites to query in parallel (default: system CPU count "
            "minus 4, never below 1, capped at the number of sites)."
        ),
    )
    parser.add_argument(
        "--parse", action="store_true",
        help=(
            "After searching, fetch each result page, extract its readable text, "
            "and store both the HTML and text in the parsed-pages SQLite "
            "database."
        ),
    )
    parser.add_argument(
        "--parse-db", type=Path, default=Path("web_data/parsed_pages.db"),
        help=(
            "Path to the SQLite parsed-pages database written by --parse "
            "(default: web_data/parsed_pages.db)."
        ),
    )
    args = parser.parse_args()

    if args.list_backends:
        print("Available ddgs text backends:")
        for name in available_text_backends():
            print(f"  - {name}")
        return 0

    if not args.queries:
        parser.error("the following arguments are required: queries")

    configure_ddgs_logging(args.verbose)

    requested = [
        b.strip()
        for b in (args.backend or "").split(",")
        if b.strip()
    ]
    available = set(available_text_backends())
    missing = [b for b in requested if b not in available and b not in ("auto", "all")]
    if missing and requested:
        print(
            f"WARNING: backend(s) {', '.join(missing)} are not available in the "
            "installed ddgs and will silently fall back to 'auto'. "
            f"Available: {', '.join(available_text_backends())}.",
            file=sys.stderr,
        )

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
                workers = effective_workers(args.workers, len(domains))
                if args.workers is None:
                    print(
                        f"  using {workers} worker(s) for {len(domains)} site(s) "
                        f"(CPU count {os.cpu_count() or '?'} minus 4)",
                        file=sys.stderr,
                    )
                per_site, errors = collect_site_results(
                    query,
                    domains,
                    args.max_results,
                    args.region,
                    args.delay,
                    args.retries,
                    args.retry_delay,
                    args.backend,
                    args.timeout,
                    workers,
                )
            else:
                try:
                    results = search_with_retries(
                        query, args.max_results, args.region, args.retries, args.retry_delay, args.backend, args.timeout
                    )
                    per_site, errors = [("(all)", results)], []
                except exceptions.DDGSException as exc:
                    per_site, errors = [], [(query, describe_ddgs_exception(exc))]
                except Exception as exc:
                    per_site, errors = [], [(query, describe_ddgs_exception(exc))]

            exclude_keywords = parse_keywords(args.exclude_keywords)
            if exclude_keywords:
                dropped = 0
                filtered: list[tuple[str, list[dict]]] = []
                for domain, results in per_site:
                    kept = filter_by_keywords(results, exclude_keywords)
                    dropped += len(results) - len(kept)
                    filtered.append((domain, kept))
                per_site = filtered
                if dropped:
                    print(
                        "  dropped "
                        f"{dropped} result(s) containing exclude keywords: "
                        f"{', '.join(exclude_keywords)}",
                        file=sys.stderr,
                    )

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
