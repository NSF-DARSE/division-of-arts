"""Stage B dispatcher: route each source to its worker, store raw events."""

from __future__ import annotations

from .. import db
from ..llm import LLMUnavailable
from . import workers


def worker_for(route: str):
    if not route:
        return None
    if route == "tribe-rest":
        return workers.pull_tribe
    if route == "libcal":
        return workers.pull_libcal
    if route == "squarespace-json":
        return workers.pull_squarespace
    if route == "ics":
        return workers.pull_ics
    if route.startswith("rss:"):
        return workers.pull_rss
    if route.startswith("wpv2:"):
        return workers.pull_wpv2
    if route == "jsonld":
        return workers.pull_jsonld
    if route == "html-llm":
        return workers.pull_html_llm
    return None  # headless / none: deferred


def run_source(conn, src) -> dict:
    """Pull one source. Returns {status, count}.

    An empty pull never deletes a source's previous raw events: "we looked and
    found nothing" and "we could not look" are indistinguishable at this layer,
    and wiping good data on a transient failure is the worse error.
    """
    fn = worker_for(src["extract_route"])
    if fn is None:
        return {"status": "skipped", "count": 0}

    raw = fn(src, conn)
    if not raw and src["extract_route"].split(":")[0] in ("rss", "wpv2") and src["events_url"]:
        # Feed had no extractable events (blog noise, date-less CPT):
        # fall back to reading the events page itself.
        raw = workers.pull_html_llm(src, conn)

    prior = conn.execute(
        "SELECT COUNT(*) c FROM raw_events WHERE source_id = ?", (src["id"],)
    ).fetchone()["c"]
    if not raw:
        return {"status": "empty-kept" if prior else "empty", "count": prior}

    conn.execute("DELETE FROM raw_events WHERE source_id = ?", (src["id"],))
    for ev in raw:
        conn.execute(
            "INSERT INTO raw_events (source_id, channel, url, payload, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (src["id"], src["extract_route"], ev.get("url"), db.to_json(ev), db.now()),
        )
    conn.commit()
    return {"status": "ok", "count": len(raw)}


def run_all(conn, routes=None, names=None, limit=None, workers: int = 1) -> dict:
    q = "SELECT * FROM sources WHERE status = 'ok'"
    args = []
    if routes:
        q += " AND (" + " OR ".join(["extract_route LIKE ?"] * len(routes)) + ")"
        args += [f"{r}%" for r in routes]
    if names:
        placeholders = ",".join("?" * len(names))
        q += f" AND name IN ({placeholders})"
        args += list(names)
    if limit:
        q += f" LIMIT {int(limit)}"

    sources = [dict(r) for r in conn.execute(q, args).fetchall()]
    if workers <= 1:
        return {src["name"]: _guarded(conn, src) for src in sources}

    # Sources are IO-bound (HTTP fetches and LLM calls); each thread gets its
    # own SQLite connection since a connection cannot be shared across threads.
    from concurrent.futures import ThreadPoolExecutor

    def worker(src):
        thread_conn = db.connect()
        try:
            return _guarded(thread_conn, src)
        finally:
            thread_conn.close()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(zip([s["name"] for s in sources], pool.map(worker, sources)))


def _guarded(conn, src) -> dict:
    try:
        return run_source(conn, src)
    except LLMUnavailable as e:
        return {"status": "llm-unavailable", "count": 0, "detail": str(e)}
    except Exception as e:  # noqa: BLE001 - one bad source must not kill the run
        return {"status": "error", "count": 0, "detail": f"{type(e).__name__}: {e}"}


def summarize(results: dict) -> dict:
    """Collapse run_all output into counts by status, for logging."""
    summary = {"events": 0}
    for res in results.values():
        summary[res["status"]] = summary.get(res["status"], 0) + 1
        if res["status"] in ("ok", "empty-kept"):
            summary["events"] += res["count"]
    return summary
