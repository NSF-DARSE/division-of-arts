#!/usr/bin/env python3
"""Module for fetching and parsing webpages.

This module provides functionality to fetch a webpage's HTML, parse it into readable
text content, and store both in an SQLite database. It handles potential errors,
ensures data integrity, and utilizes the `requests` and `BeautifulSoup` libraries
for web scraping.

Functions:
    fetch_page(url): Fetches the HTML content of a webpage.
    extract_text_content(html): Parses HTML and extracts text content, removing scripts and styles.
    save_page(url, html, text_content): Stores a parsed webpage's content in the SQLite database.
    parse_urls(urls, db_path): Fetches, parses, and stores a collection of webpages.
    main(): Reads URLs from command-line arguments or stdin and stores each parsed webpage.

Usage:
    python web_parser.py "https://example.com" "https://example.org"
    python web_parser.py --db web_data/parsed_pages.db < urls.txt
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Iterable

import requests
from bs4 import BeautifulSoup

DEFAULT_DB_PATH = Path("web_data/parsed_pages.db")


def fetch_page(url: str, timeout: float = 30.0) -> str | None:
    """Fetch the HTML content of a webpage.

    Args:
        url (str): URL of the webpage to fetch.
        timeout (float): Seconds to wait for a server response (default: 30).

    Returns:
        str: HTML content of the webpage, or None if an error occurs.
    """
    try:
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        return response.text
    except requests.RequestException as exc:
        print(f"Error fetching {url}: {exc}", file=sys.stderr)
        return None


def extract_text_content(html: str) -> str:
    """Extract text content from HTML, removing scripts and styles.

    Args:
        html (str): HTML content to extract text from.

    Returns:
        str: Extracted text content.
    """
    soup = BeautifulSoup(html, "html.parser")
    for element in soup(["script", "style"]):
        element.decompose()
    return soup.get_text().strip()


def save_page(
    url: str,
    html: str,
    text_content: str,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> None:
    """Store a parsed webpage's HTML and text content in the SQLite database.

    Args:
        url (str): URL of the webpage.
        html (str): HTML content of the webpage.
        text_content (str): Extracted text content of the webpage.
        db_path (Path | str): Path to the SQLite database.
    """
    db = Path(db_path)
    db.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(db)
        try:
            cursor = conn.cursor()
            cursor.execute(
                """CREATE TABLE IF NOT EXISTS pages
                   (url text PRIMARY KEY, html text NOT NULL, text_content text NOT NULL)"""
            )
            cursor.execute(
                """INSERT INTO pages (url, html, text_content) VALUES (?, ?, ?)
                   ON CONFLICT(url) DO UPDATE SET
                       html = excluded.html,
                       text_content = excluded.text_content""",
                (url, html, text_content),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f"Database error: {exc}", file=sys.stderr)


def parse_urls(
    urls: Iterable[str],
    db_path: Path | str = DEFAULT_DB_PATH,
) -> int:
    """Fetch, parse, and store each of the given webpages in the SQLite database.

    Args:
        urls (Iterable[str]): URLs of webpages to fetch and parse.
        db_path (Path | str): Path to the SQLite database.

    Returns:
        int: Number of webpages successfully parsed and stored.
    """
    saved = 0
    for url in urls:
        if not url:
            continue
        html = fetch_page(url)
        if html:
            text_content = extract_text_content(html)
            save_page(url, html, text_content, db_path=db_path)
            print(f"Parsed: {url}")
            saved += 1
    return saved


def main() -> int:
    """Read URLs from arguments or stdin, parse and store each webpage's content."""
    parser = argparse.ArgumentParser(
        description="Fetch and parse webpages, storing HTML and text in an SQLite database."
    )
    parser.add_argument(
        "urls",
        nargs="*",
        help="URLs of webpages to parse (or provide them on stdin, one per line)",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"Path to the SQLite database (default: {DEFAULT_DB_PATH})",
    )
    args = parser.parse_args()

    urls = list(args.urls)
    if not sys.stdin.isatty():
        urls.extend(line.strip() for line in sys.stdin if line.strip())
    if not urls:
        parser.error("no URLs provided (pass them as arguments or on stdin)")

    parse_urls(urls, db_path=args.db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
