"""SceneScout pipeline CLI.

    python -m scenescout scene            # scrape delawarescene.com reference data
    python -m scenescout registry         # load websites.csv + health check + route probe
    python -m scenescout extract          # pull raw events from all routed sources
    python -m scenescout normalize        # canonicalize + classify
    python -m scenescout dedupe           # self-dedup + match vs DelawareScene
    python -m scenescout export           # write bulk-upload .xlsx + review report
    python -m scenescout run-all          # everything above, in order
    python -m scenescout stats            # database summary
"""

from __future__ import annotations

import argparse
from datetime import date

from . import db


def main(argv=None):
    parser = argparse.ArgumentParser(prog="scenescout")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scene", help="scrape DelawareScene orgs + listings")
    p.add_argument("--days", type=int, default=120, help="live listing horizon (days)")
    p.add_argument("--skip-live", action="store_true")

    sub.add_parser("registry", help="load sources + probe extraction routes")

    p = sub.add_parser("extract", help="run route workers")
    p.add_argument("--routes", help="comma-separated route prefixes (e.g. tribe-rest,libcal)")
    p.add_argument("--names", help="comma-separated source names")
    p.add_argument("--limit", type=int)
    p.add_argument("--workers", type=int, default=1,
                   help="parallel sources (IO-bound; useful for LLM routes)")

    p = sub.add_parser("normalize", help="canonicalize + classify raw events")
    p.add_argument("--llm-budget", type=int, default=60)

    p = sub.add_parser("rediscover", help="propose replacement URLs for dead sources")
    p.add_argument("--apply", action="store_true",
                   help="write back high-confidence matches and re-probe them")

    sub.add_parser("dedupe", help="dedupe + match against DelawareScene")
    sub.add_parser("export", help="write bulk-upload xlsx + review report")

    p = sub.add_parser("run-all", help="full pipeline")
    p.add_argument("--days", type=int, default=120)
    p.add_argument("--llm-budget", type=int, default=60)
    p.add_argument("--workers", type=int, default=4)

    sub.add_parser("stats", help="database summary")

    args = parser.parse_args(argv)
    conn = db.connect()

    if args.cmd == "scene":
        _scene(conn, args.days, args.skip_live)
    elif args.cmd == "registry":
        _registry(conn)
    elif args.cmd == "extract":
        _extract(conn, args.routes, args.names, args.limit, args.workers)
    elif args.cmd == "normalize":
        from . import normalize
        print("normalize:", normalize.run(conn, llm_budget=args.llm_budget))
    elif args.cmd == "rediscover":
        from . import rediscover
        result = rediscover.run(conn, apply_confident=args.apply)
        print("rediscover:", result)
        import json as _json
        for entry in _json.loads(open(result["report"]).read()):
            best = entry["candidates"][0] if entry["candidates"] else None
            print(f"  {entry['source']} ({entry['status']}) was {entry['old_url']}")
            if best:
                mark = "APPLIED" if entry["applied"] else (
                    "confirmed" if best["confirmed"] else "unconfirmed")
                print(f"    {mark}: {best['url']} (name {best['name_match']}%, "
                      f"http {best['http_status']}, org-on-page={best['name_in_page']}, "
                      f"delaware-on-page={best['delaware_in_page']})")
            else:
                print("    no plausible replacement found")
    elif args.cmd == "dedupe":
        from . import dedupe
        print("dedupe:", dedupe.run(conn))
    elif args.cmd == "export":
        from . import export
        print("export:", export.run(conn))
    elif args.cmd == "run-all":
        _scene(conn, args.days, False)
        _registry(conn)
        _extract(conn, None, None, None, args.workers)
        from . import dedupe, export, normalize
        print("normalize:", normalize.run(conn, llm_budget=args.llm_budget))
        print("dedupe:", dedupe.run(conn))
        print("export:", export.run(conn))
    elif args.cmd == "stats":
        _stats(conn)


def _scene(conn, days, skip_live):
    from . import scene
    print("scene orgs:", scene.scrape_orgs(conn))
    print("xlsx listings:", scene.bootstrap_from_xlsx(conn))
    if not skip_live:
        print(f"live listings ({days}d):", scene.scrape_listings(conn, days=days))


def _registry(conn):
    from . import registry
    print("sources loaded:", registry.load_sources(conn))
    registry.probe_all(conn)
    for row in conn.execute(
        "SELECT extract_route, COUNT(*) c FROM sources GROUP BY 1 ORDER BY c DESC"
    ):
        print(f"  {row['extract_route']}: {row['c']}")


def _extract(conn, routes, names, limit, workers=1):
    from . import extract
    results = extract.run_all(
        conn,
        routes=routes.split(",") if routes else None,
        names=names.split(",") if names else None,
        limit=limit,
        workers=workers,
    )
    summary = extract.summarize(results)
    print(f"extracted {summary.pop('events')} raw events from {len(results)} sources")
    for status, count in sorted(summary.items()):
        print(f"  {status}: {count}")
    for name, res in results.items():
        if res["status"] in ("error", "llm-unavailable"):
            print(f"  ! {name}: {res['status']} — {res.get('detail', '')[:160]}")


def _stats(conn):
    print("as of", date.today().isoformat())
    for label, q in [
        ("sources by status", "SELECT status, COUNT(*) FROM sources GROUP BY 1"),
        ("raw events", "SELECT COUNT(*), NULL FROM raw_events"),
        ("events by verdict", "SELECT COALESCE(verdict,'(pending)'), COUNT(*) FROM events GROUP BY 1"),
        ("events by relevance", "SELECT relevance, COUNT(*) FROM events GROUP BY 1"),
        ("scene listings", "SELECT origin, COUNT(*) FROM scene_listings GROUP BY 1"),
        ("scene orgs", "SELECT COUNT(*), NULL FROM scene_orgs"),
    ]:
        print(f"-- {label}")
        for row in conn.execute(q):
            print("  ", row[0], row[1] if row[1] is not None else "")


if __name__ == "__main__":
    main()
