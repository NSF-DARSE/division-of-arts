"""Module for scraping a fixed list of URLs (no recursion into child pages).

This module fetches each URL listed in an input file (e.g. sites.txt) and
extracts the links found *on that page only*. It does NOT follow any of
those extracted links, and it does NOT follow "next page" pagination links.
Each site in the input file is treated as a single, standalone page to scrape.

It respects robots.txt for each domain before fetching.

Functions:
    can_fetch(url, user_agent): Checks robots.txt to see if a URL may be fetched.
    scrape_page(url): Fetches a single URL and extracts same-domain links found on it.
    load_sites(path): Reads a list of URLs from a text file, one per line.
    main(): Parses command-line arguments, scrapes each listed site, writes results to a file.
"""

import argparse
import time
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

USER_AGENT = "SubpageScraperBot/1.0"


def can_fetch(url, user_agent=USER_AGENT):
    """
    Check robots.txt for the given URL's domain to see if fetching is allowed.

    Args:
        url (str): The URL to check.
        user_agent (str): The user agent string to check permissions for.

    Returns:
        bool: True if fetching is allowed (or robots.txt is unavailable), False otherwise.
    """
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = RobotFileParser()
    try:
        rp.set_url(robots_url)
        rp.read()
        return rp.can_fetch(user_agent, url)
    except Exception:
        # If robots.txt can't be read, default to allowing the fetch.
        return True


def scrape_page(url, timeout=15):
    """
    Fetch a single URL and extract same-domain links found ONLY on that page.
    Does not follow any of the extracted links and does not paginate.

    Args:
        url (str): The exact URL to scrape.
        timeout (int): Request timeout in seconds.

    Returns:
        list: List of same-domain URLs found on this page. Empty list on error
              or if robots.txt disallows fetching.
    """
    if not url.startswith("http"):
        url = f"https://{url}"

    if not can_fetch(url):
        print(f"Blocked by robots.txt: {url}")
        return []

    found = []
    try:
        headers = {"User-Agent": USER_AGENT}
        response = requests.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "")

        if "xml" in content_type or url.endswith(".xml"):
            # Sitemap-style XML: parse <loc> entries as the page's content,
            # but do not fetch/recurse into them.
            soup = BeautifulSoup(response.content, "xml")
            for loc in soup.find_all("loc"):
                if loc.text:
                    found.append(loc.text.strip())
        else:
            soup = BeautifulSoup(response.text, "html.parser")
            domain = urlparse(url).netloc
            for link in soup.find_all("a"):
                href = link.get("href")
                if not href:
                    continue
                absolute_url = urljoin(url, href)
                if urlparse(absolute_url).netloc == domain:
                    found.append(absolute_url)

    except requests.RequestException as e:
        print(f"Error fetching {url}: {e}")

    return found


def load_sites(path):
    """
    Read a list of URLs from a text file, one per line.

    Args:
        path (str): Path to the input file.

    Returns:
        list: List of non-empty, stripped URL strings.
    """
    with open(path, "r") as f:
        return [line.strip() for line in f if line.strip()]


def main():
    """
    Main entry point.

    Reads a list of site URLs from an input file, scrapes each one
    individually (no recursion into child pages), and writes the results
    to an output file grouped by source site.
    """
    parser = argparse.ArgumentParser(
        description="Scrape a fixed list of site URLs (no child-page crawling)"
    )
    parser.add_argument(
        "sites_file", type=str, help="Path to a text file with one URL per line"
    )
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        default="subpage_list.txt",
        help="Output file path (default: subpage_list.txt)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Delay in seconds between requests (default: 1.0)",
    )
    args = parser.parse_args()

    sites = load_sites(args.sites_file)

    with open(args.output, "w") as f:
        for site in sites:
            print(f"Scraping: {site}")
            links = scrape_page(site)
            f.write(f"# {site}\n")
            for link in links:
                f.write(f"{link}\n")
            f.write("\n")
            time.sleep(args.delay)

    print(f"Done. Results written to {args.output}")


if __name__ == "__main__":
    main()