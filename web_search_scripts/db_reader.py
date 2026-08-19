#!/usr/bin/env python3
"""Read/search the parsed-pages SQLite database (web_data/parsed_pages.db).

Commands:
    list              List stored URLs (optionally filtered by a substring).
    show <url>        Print the readable text content of one page.
    search <terms>    Case-insensitive search of URLs and text content.
    stats             Row count and total size of the database.

Use --output <file> with `list` or `search` to save just the matching URLs
to a file (one per line) instead of / in addition to the console listing.

Usage:
    python db_reader.py list
    python db_reader.py list --filter theatre
    python db_reader.py list --output urls.txt
    python db_reader.py show https://arts.delaware.gov/
    python db_reader.py search "class for kids"
    python db_reader.py search "gallery" --output gallery_urls.txt
    python db_reader.py --db web_data/parsed_pages.db stats
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from web_parser import DEFAULT_DB_PATH

MAIN_EPILOG = """\
examples:
  List every stored URL:
    python db_reader.py list

  List URLs whose address contains "theatre":
    python db_reader.py list --filter theatre

  Save all matching URLs to a file (one per line):
    python db_reader.py list --output web_data/urls.txt

  Search the stored text for a phrase:
    python db_reader.py search "class for kids"

  Print the full text of one page:
    python db_reader.py show "https://arts.delaware.gov/"

  Show row count and total size of the database:
    python db_reader.py stats

  Run any command against a different database:
    python db_reader.py --db web_data/parsed_pages.db list
"""

LIST_EPILOG = """\
examples:
  python db_reader.py list
  python db_reader.py list --filter theatre
  python db_reader.py list --filter chapel --output web_data/chapel_urls.txt
"""

SEARCH_EPILOG = """\
The match is a simple, case-insensitive substring search over both the page URL
and its extracted text content. Queries are NOT boolean: there are no AND/OR
operators or wildcards, so `search "kids classes"` matches pages containing the
literal phrase "kids classes" (or that phrase appearing in the URL) and no
others.

To broaden a search, drop words or use --filter against URLs. Each result
shows a short preview of the matching page; a result may still be cut short by
--limit. To read a full page, run:

    python db_reader.py search classes | python db_reader.py show "$(head -1)"

examples:
  python db_reader.py search "gallery"
  python db_reader.py search "kids classes" --limit 5
  python db_reader.py search gallery --output web_data/gallery_urls.txt
  python db_reader.py search "class for kids" --output web_data/classes.txt
  python db_reader.py list --filter dance
"""

SHOW_EPILOG = """\
example:
  python db_reader.py show "https://arts.delaware.gov/"
"""

STATS_EPILOG = """\
example:
  python db_reader.py stats
"""


def connect(db_path: Path | str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def write_urls(urls: list[str], output: Path | None) -> None:
    """Write each URL on its own line to output, or print them to stdout."""
    lines = [f"{url}\n" for url in urls]
    if output is None:
        sys.stdout.writelines(lines)
    else:
        output.write_text("".join(lines), encoding="utf-8")
        print(f"Wrote {len(urls)} URL(s) to {output}", file=sys.stderr)


def cmd_list(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    """Print every URL, optionally restricted to those containing a substring."""
    query = "SELECT url, length(text_content) AS text_len FROM pages"
    params: tuple = ()
    if args.filter:
        query += " WHERE url LIKE ?"
        params = (f"%{args.filter}%",)
    query += " ORDER BY url"
    rows = conn.execute(query, params).fetchall()

    if args.output is not None:
        write_urls([row["url"] for row in rows], args.output)
        return 0

    for row in rows:
        print(f"{row['text_len']:>10} chars  {row['url']}")
    print(f"\n{len(rows)} page(s)")
    return 0


def cmd_show(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    """Print the readable text content of a single page."""
    row = conn.execute(
        "SELECT text_content FROM pages WHERE url = ?", (args.url,)
    ).fetchone()
    if row is None:
        print(f"No page stored for: {args.url}", file=sys.stderr)
        return 1
    print(row["text_content"])
    return 0


def cmd_search(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    """Case-insensitive search over URLs and text content."""
    like = f"%{args.terms}%"
    rows = conn.execute(
        """SELECT url, text_content FROM pages
           WHERE url LIKE ? OR lower(text_content) LIKE lower(?)
           ORDER BY url
           LIMIT ?""",
        (like, like, args.limit),
    ).fetchall()
    if not rows:
        print(f"No matches for: {args.terms!r}")
        return 1

    if args.output is not None:
        write_urls([row["url"] for row in rows], args.output)
        return 0

    for i, row in enumerate(rows, 1):
        text = row["text_content"]
        snippet = text[:200].replace("\n", " ")
        print(f"[{i}] {row['url']}")
        print(f"    {snippet}{'...' if len(text) > 200 else ''}")
    return 0


def cmd_stats(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    """Report row count and total stored text/HTML size."""
    row = conn.execute(
        """SELECT COUNT(*) AS pages,
                  COALESCE(SUM(length(text_content)), 0) AS text_bytes,
                  COALESCE(SUM(length(html)), 0)       AS html_bytes
           FROM pages"""
    ).fetchone()
    print(f"Pages:        {row['pages']}")
    print(f"Text bytes:   {row['text_bytes']:,}")
    print(f"HTML bytes:   {row['html_bytes']:,}")
    return 0


def add_output_arg(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument(
        "--output",
        type=Path,
        metavar="FILE",
        help=(
            "write just the matching URLs to FILE, one per line; suppresses the "
            "console listing and prints a confirmation message to stderr instead"
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="db_reader.py",
        description=(
            "Read, search, and export the webpages stored in the parsed-pages "
            "SQLite database."
        ),
        epilog=MAIN_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=(
            "path to the SQLite database written by web_parser.py "
            f"(default: {DEFAULT_DB_PATH})"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    parser_list = subparsers.add_parser(
        "list",
        help="list stored URLs",
        description=(
            "List every URL stored in the database, showing the size in "
            "characters of each page's extracted text. Restrict the listing "
            "with --filter and/or save just the URLs with --output."
        ),
        epilog=LIST_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser_list.add_argument(
        "--filter",
        metavar="TEXT",
        help=(
            "only list URLs whose address contains this substring, "
            "e.g. --filter theatre (case-sensitive, matches anywhere in the URL)"
        ),
    )
    add_output_arg(parser_list)
    parser_list.set_defaults(func=cmd_list)

    parser_show = subparsers.add_parser(
        "show",
        help="print one page's text content",
        description=(
            "Print the readable text content extracted from a single stored page. "
            "The URL must match the database exactly; use `list` to find it. "
            "The raw HTML is kept in the database but not displayed."
        ),
        epilog=SHOW_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser_show.add_argument(
        "url", metavar="URL", help="the full URL of the page to print"
    )
    parser_show.set_defaults(func=cmd_show)

    parser_search = subparsers.add_parser(
        "search",
        help="case-insensitive search of URLs and text",
        description=(
            "Search the database for pages whose URL or extracted text content "
            "contains the given terms (a case-insensitive substring match; no "
            "boolean operators or wildcards). Prints a short preview of each "
            "match, up to --limit. Read a full page by piping the URL into "
            "`show`, or save just the matching URLs with --output."
        ),
        epilog=SEARCH_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser_search.add_argument(
        "terms", metavar="TERMS", help="terms to search for, e.g. gallery"
    )
    parser_search.add_argument(
        "--limit",
        type=int,
        default=20,
        metavar="N",
        help="maximum number of matches to return (default: 20)",
    )
    add_output_arg(parser_search)
    parser_search.set_defaults(func=cmd_search)

    parser_stats = subparsers.add_parser(
        "stats",
        help="row count and sizes",
        description=(
            "Report the number of stored pages plus the total size of the "
            "extracted text and of the raw HTML in the database."
        ),
        epilog=STATS_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser_stats.set_defaults(func=cmd_stats)

    args = parser.parse_args()
    conn = connect(args.db)
    try:
        return args.func(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
