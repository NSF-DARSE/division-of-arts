"""LLM layer: event extraction from unstructured HTML and category/relevance
classification.

Backend resolution order (first that works wins, cached for the process):
  1. anthropic SDK   - zero-arg client; credentials resolve from ANTHROPIC_API_KEY,
                       ANTHROPIC_AUTH_TOKEN, or an `ant auth login` profile.
  2. claude CLI      - `claude -p` headless mode, if a claude binary is around
                       (dev convenience: works inside a Claude Code session).
  3. rules           - deterministic keyword classifier; extraction returns
                       nothing and flags the source for human review.

Every response is schema-validated JSON (output_config.format). Refusals
(stop_reason == "refusal") and backend errors degrade to the next backend.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading

MODEL = "claude-opus-5"

# DelawareScene category IDs (subcategory implies parent; never emit both).
CATEGORIES = """1 Attractions | 2 Dance | 3 Festivals & Special Events | 4 Film |
5 Free | 6 Kids & Family Friendly | 7 Lectures & Workshops | 8 Literature & Poetry |
9 Music | 12 Music: Bands | 13 Music: Choral | 14 Music: Classical/Opera |
15 Music: Country/Folk/Bluegrass | 16 Music: Hip Hop/R&B | 17 Music: Jazz/Blues |
18 Music: Rock/Pop | 19 Music: World | 10 Theater & Performance |
20 Theater: Comedy/Drama | 21 Theater: Musical | 22 Theater: Variety |
11 Visual Arts | 23 Visual Arts: Art Centers | 24 Visual Arts: Art/Antiques/Craft Shows |
25 Visual Arts: Art Tours | 26 Visual Arts: Exhibitions | 27 Visual Arts: Galleries |
28 Visual Arts: Museums | 29 Visual Arts: Public Art"""

_EVENT_FIELDS = {
    "title": {"type": ["string", "null"]},
    "start_date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
    "start_time": {"type": ["string", "null"], "description": "e.g. 7:30 pm"},
    "end_date": {"type": ["string", "null"]},
    "end_time": {"type": ["string", "null"]},
    "venue_name": {"type": ["string", "null"]},
    "venue_address": {"type": ["string", "null"]},
    "venue_city": {"type": ["string", "null"]},
    "presenter": {"type": ["string", "null"]},
    "url": {"type": ["string", "null"]},
    "ticket_url": {"type": ["string", "null"]},
    "phone": {"type": ["string", "null"]},
    "description": {"type": ["string", "null"]},
    "cost_text": {"type": ["string", "null"], "description": "e.g. FREE, $10, $18-32"},
}

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": _EVENT_FIELDS,
                "required": list(_EVENT_FIELDS),
                "additionalProperties": False,
            },
        },
        "none_found": {"type": "boolean"},
    },
    "required": ["events", "none_found"],
    "additionalProperties": False,
}

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "relevant": {
            "type": "boolean",
            "description": "true if this is an arts/culture event in Delaware in scope for DelawareScene.com",
        },
        "reason": {"type": "string"},
        "category_ids": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "up to 3 DelawareScene category IDs",
        },
    },
    "required": ["relevant", "reason", "category_ids"],
    "additionalProperties": False,
}

EXTRACT_PROMPT = """You are an event-data extractor for the Delaware Division of the Arts.
The text below is from {source_name} ({url}). Extract every distinct upcoming public
event you can find. Today is {today}; resolve relative or year-less dates against that.
Rules:
- Only real, dated public events (performances, exhibits, classes, festivals, screenings, talks).
- Skip navigation text, past seasons, generic descriptions without dates.
- An exhibit or run gets start_date and end_date; a single show gets one date.
- Use null for anything not present in the text. Never invent values.
- Season pages often list dates under a venue heading with no per-date title
  ("Save the dates!" followed by dates). Emit one event per date, carry that
  venue down to each, and title it after the season or organization — do not
  drop the dates and do not invent a specific programme.
- If there are no events, return none_found=true and an empty list.

PAGE TEXT:
{text}"""

CLASSIFY_PROMPT = """You screen events for DelawareScene.com, Delaware's public arts &
culture calendar. Decide if the event below is IN SCOPE: an arts/culture event open to
the public in Delaware — performances, concerts, exhibits, film screenings, author
talks, art classes, literary events, cultural festivals. OUT of scope: story time,
job fairs, blood drives, government meetings, sports, nature walks/camps with no arts
content, religious services, and events outside Delaware.

If in scope, assign up to 3 category IDs from this list (a subcategory implies its
parent — emit 17, never 9 and 17 together; add 5 only when admission is free; add 6
for explicitly family/children programming):
{categories}

EVENT:
{event}"""


class Refused(Exception):
    """Safety classifiers declined the request."""


class LLMUnavailable(RuntimeError):
    """No LLM backend could serve the call (auth, rate/session limit, network).

    Extraction raises this instead of returning an empty list, so callers can
    tell "this source has no events" apart from "we could not look".
    """


# Ordered so a child implies its parent (never emit both).
PARENT_OF = {12: 9, 13: 9, 14: 9, 15: 9, 16: 9, 17: 9, 18: 9, 19: 9,
             20: 10, 21: 10, 22: 10,
             23: 11, 24: 11, 25: 11, 26: 11, 27: 11, 28: 11, 29: 11}
VALID_CATEGORIES = set(range(1, 30))


def sanitize_categories(ids, cap: int = 3):
    """Drop unknown IDs, collapse parent+child pairs to the child, cap the list.

    The submission guidelines allow at most 3 IDs and treat a subcategory as
    implying its parent, so '9,17' must ship as '17'.
    """
    clean = []
    for cid in ids or []:
        try:
            cid = int(cid)
        except (TypeError, ValueError):
            continue
        if cid in VALID_CATEGORIES and cid not in clean:
            clean.append(cid)
    children = {c for c in clean if c in PARENT_OF}
    parents_implied = {PARENT_OF[c] for c in children}
    clean = [c for c in clean if c not in parents_implied]
    return clean[:cap]


class _AnthropicBackend:
    name = "anthropic"

    def __init__(self):
        import anthropic  # deferred so the rules backend works without the SDK

        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            # No env credentials; accept an `ant auth login` profile on disk.
            from pathlib import Path

            cfg = Path(os.environ.get("ANTHROPIC_CONFIG_DIR", Path.home() / ".config" / "anthropic"))
            if not (cfg / "credentials").exists():
                raise RuntimeError("no anthropic credentials available")
        self._anthropic = anthropic
        self.client = anthropic.Anthropic()

    def complete_json(self, prompt: str, schema: dict, effort: str = "low") -> dict:
        response = self.client.messages.create(
            model=MODEL,
            max_tokens=16000,
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": prompt}],
        )
        if response.stop_reason == "refusal":
            raise Refused(str(getattr(response, "stop_details", "")))
        text = next(b.text for b in response.content if b.type == "text")
        return json.loads(text)


class _ClaudeCLIBackend:
    name = "claude-cli"

    # Each call is a full CLI process; too many at once trip over each other,
    # so extraction parallelism is bounded here rather than at the call sites.
    _slots = threading.Semaphore(int(os.environ.get("SCENESCOUT_CLI_CONCURRENCY", "2")))

    def __init__(self):
        self.binary = shutil.which("claude") or os.environ.get("CLAUDE_CODE_EXECPATH")
        if not self.binary or not os.path.exists(self.binary):
            raise FileNotFoundError("no claude binary")

    def complete_json(self, prompt: str, schema: dict, effort: str = "low") -> dict:
        full = (
            prompt
            + "\n\nRespond with ONLY a JSON object matching this JSON Schema, no prose:\n"
            + json.dumps(schema)
        )
        with self._slots:
            proc = subprocess.run(
                [self.binary, "-p", "--output-format", "text"],
                input=full,
                capture_output=True,
                text=True,
                timeout=600,
            )
        if proc.returncode != 0:
            raise RuntimeError(f"claude CLI failed: {(proc.stderr or proc.stdout)[:300]}")
        m = re.search(r"\{.*\}", proc.stdout, re.DOTALL)
        if not m:
            raise ValueError(f"no JSON in CLI output: {proc.stdout[:200]}")
        return json.loads(m[0])


class _RulesBackend:
    """Deterministic fallback: no extraction; keyword classification.

    Two rules keep the keyword pass honest:
      - Disqualifying words are matched against the *title* only. One
        incidental word in a paragraph of marketing copy should not delete a
        grantee's concert.
      - Category words are matched against title and description, but the
        title matches are reported separately, because a category found only
        in body text is weak evidence that the event is an arts event.
    """

    name = "rules"

    EXCLUDE = re.compile(
        r"\b(story\s*times?|storytimes?|job fairs?|blood drives?|council meeting|"
        r"board meeting|town hall|bingo|yard sale|vaccination|tax prep|"
        r"summer camps?|day camps?|bird(ing)? walks?|nature hikes?|5k|fun run|"
        r"tabletop|board games?|video gam\w*|gaming|d&d|dungeons|"
        # food, drink, and nightlife promotions carried by tourism calendars
        r"happy hours?|karaoke|trivia nights?|wing nights?|taco tuesday|brunch|"
        r"drafts?|half off|drink specials?|food trucks?|wine down|bottomless|"
        r"ladies night|blue rocks|baseball|football|basketball|soccer match|"
        # facility bookings and notices, not public events. "closed" is
        # anchored to closure phrasing so "Artist Talk: Closed Forms" survives.
        r"reservations?|room booking|\breserved\b|orientation|tutoring|"
        r"office hours|passport services?|proctoring|notary|"
        r"(?:museum|library|gallery|office|building|park|center|centre)s?\s+closed|"
        r"closed\s+(?:for|due|on|all|today|this|monday|tuesday|wednesday|thursday|"
        r"friday|saturday|sunday)|closure)\b",
        re.IGNORECASE,
    )
    # Plurals and inflections are spelled out rather than using a trailing
    # \w*, which would let "art" match "artificial" and "book" match "booking".
    KEYWORDS = [
        (re.compile(r"\b(jazz|blues)\b", re.I), 17),
        (re.compile(r"\b(choir|choirs|choral|chorale|chorus)\b", re.I), 13),
        (re.compile(r"\b(symphony|orchestra|orchestral|opera|operas|classical|"
                    r"chamber music|recital|recitals)\b", re.I), 14),
        (re.compile(r"\b(folk|bluegrass|country music)\b", re.I), 15),
        (re.compile(r"\b(hip.?hop|r&b)\b", re.I), 16),
        # "pop" must not match the "pop" in "pop-up", where the hyphen is a
        # word boundary; require it to stand alone or precede "music".
        (re.compile(r"\b(rock|indie|punk)\b|\bpop\b(?!\s*[-–]?\s*up)", re.I), 18),
        (re.compile(r"\b(band|bands|concert|concerts|music|singer|singers|"
                    r"songwriter|songwriters|acoustic)\b", re.I), 9),
        (re.compile(r"\b(musical|musicals)\b", re.I), 21),
        (re.compile(r"\b(comedy|comedies|drama|improv|a play|stage play|plays)\b", re.I), 20),
        (re.compile(r"\b(theater|theaters|theatre|theatres|stage|performance|"
                    r"performances)\b", re.I), 10),
        (re.compile(r"\b(ballet|dance|dances|dancing)\b", re.I), 2),
        (re.compile(r"\b(film|films|movie|movies|screening|screenings|cinema|"
                    r"documentary|documentaries)\b", re.I), 4),
        (re.compile(r"\b(author|authors|book|books|poetry|poet|poets|literary|"
                    r"writer|writers|reading|readings)\b", re.I), 8),
        (re.compile(r"\b(lecture|lectures|workshop|workshops|class|classes|"
                    r"talk|talks|seminar|seminars|speaker|speakers)\b", re.I), 7),
        (re.compile(r"\b(festival|festivals|fair|fairs|parade|parades|"
                    r"celebration|celebrations)\b", re.I), 3),
        (re.compile(r"\b(exhibit|exhibits|exhibition|exhibitions)\b", re.I), 26),
        (re.compile(r"\b(gallery|galleries|art show|art shows|juried)\b", re.I), 27),
        (re.compile(r"\b(museum|museums)\b", re.I), 28),
        (re.compile(r"\b(art|arts|craft|crafts|paint|painting|paintings|sculpt|"
                    r"sculpture|sculptures|photograph|photography|photographs|"
                    r"pottery|drawing|drawings|watercolor|watercolors)\b", re.I), 11),
    ]
    ARTS_ANY = re.compile(
        r"\b(art|music|theat|danc|film|concert|exhibit|gallery|galleries|author|"
        r"poetry|craft|festival|opera|ballet|jazz|choir|museum|literar|cultur|"
        r"perform)",
        re.IGNORECASE,
    )

    @staticmethod
    def _event_fields(prompt: str) -> dict:
        """Recover the event dict the caller embedded after 'EVENT:'.

        Parsed as JSON rather than split on the marker, because an all-caps
        title like "SPECIAL EVENT: Symphony Gala" would otherwise truncate the
        text being classified.
        """
        start = prompt.find("{", prompt.find("EVENT:") if "EVENT:" in prompt else 0)
        if start == -1:
            return {"title": prompt}
        try:
            return json.loads(prompt[start:prompt.rfind("}") + 1])
        except ValueError:
            return {"title": prompt[start:]}

    def _categories(self, text):
        cats = []
        for rx, cid in self.KEYWORDS:
            if rx.search(text) and cid not in cats:
                cats.append(cid)
        return sanitize_categories(cats)

    def complete_json(self, prompt: str, schema: dict, effort: str = "low") -> dict:
        if schema is EXTRACT_SCHEMA:
            return {"events": [], "none_found": True}
        fields = self._event_fields(prompt)
        title = str(fields.get("title") or "")
        body = " ".join(str(v) for v in fields.values() if v)

        # Disqualifying words count only in the title: "summer camp" mentioned
        # once in a concert's description must not delete the concert.
        if self.EXCLUDE.search(title):
            return {"relevant": False, "reason": "rules: excluded keyword in title",
                    "category_ids": [], "title_category_ids": []}

        title_cats = self._categories(title)
        cats = title_cats or self._categories(body)
        relevant = bool(cats) or bool(self.ARTS_ANY.search(body))
        return {
            "relevant": relevant,
            "reason": "rules: keyword match" if relevant else "rules: no arts keyword",
            "category_ids": cats,
            "title_category_ids": title_cats,
        }


_backend = None
_backend_lock = threading.Lock()


def get_backend():
    """Resolve the best usable backend once, thread-safely.

    Selection happens at construction time (missing binary, missing
    credentials); a backend that constructs successfully is kept for the run
    so one flaky call cannot silently downgrade every later call.
    """
    global _backend
    with _backend_lock:
        if _backend is None:
            forced = os.environ.get("SCENESCOUT_LLM")  # anthropic | claude-cli | rules
            order = {
                "anthropic": [_AnthropicBackend],
                "claude-cli": [_ClaudeCLIBackend],
                "rules": [_RulesBackend],
            }.get(forced, [_AnthropicBackend, _ClaudeCLIBackend, _RulesBackend])
            for cls in order:
                try:
                    _backend = cls()
                    break
                except Exception:
                    continue
            if _backend is None:
                _backend = _RulesBackend()
    return _backend


def _call(prompt, schema, effort, retries: int = 2):
    """Run one LLM call, retrying transient failures.

    A failed call fails only its own caller: the backend is never downgraded
    for the rest of the run, because that silently turns "could not look" into
    "found nothing" for every source that follows.
    """
    import random
    import time

    backend = get_backend()
    last_error = None
    for attempt in range(retries + 1):
        try:
            return backend.complete_json(prompt, schema, effort=effort)
        except Refused:
            raise
        except Exception as e:  # noqa: BLE001 - backends raise assorted types
            last_error = e
            if attempt < retries:
                time.sleep(2 ** attempt * 3 + random.uniform(0, 2))
                continue
    raise LLMUnavailable(f"{type(last_error).__name__}: {str(last_error)[:300]}")


def extract_events(text: str, source_name: str, url: str, today: str) -> list:
    """Extract events from unstructured page text.

    Raises LLMUnavailable when no real LLM backend can serve the call; the
    rules backend cannot extract, so returning [] here would be a silent lie.
    """
    if get_backend().name == "rules":
        raise LLMUnavailable("no LLM backend available for extraction")
    text = text[:60000]
    prompt = EXTRACT_PROMPT.format(source_name=source_name, url=url, text=text, today=today)
    try:
        result = _call(prompt, EXTRACT_SCHEMA, effort="medium")
    except Refused:
        return []
    return result.get("events") or []


def classify(event: dict) -> dict:
    """Classify relevance + categories. Falls back to rules, which is a
    meaningful (if coarser) answer, so this never raises."""
    slim = {k: event.get(k) for k in (
        "title", "description", "venue_name", "presenter", "venue_city", "cost_text",
        "source_categories",
    ) if event.get(k)}
    desc = slim.get("description")
    if desc:
        slim["description"] = str(desc)[:1500]
    prompt = CLASSIFY_PROMPT.format(categories=CATEGORIES, event=json.dumps(slim, ensure_ascii=False))
    backend_name = get_backend().name
    try:
        result = _call(prompt, CLASSIFY_SCHEMA, effort="low")
    except Refused:
        return {"relevant": False, "reason": "llm refused", "category_ids": [],
                "backend": backend_name}
    except LLMUnavailable:
        result = _RulesBackend().complete_json(prompt, CLASSIFY_SCHEMA)
        backend_name = "rules"
    result["category_ids"] = sanitize_categories(result.get("category_ids"))
    result["backend"] = get_backend().name if backend_name != "rules" else "rules"
    return result
