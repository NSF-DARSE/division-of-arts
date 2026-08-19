"""Regression tests for the SceneScout pipeline.

Pure-function tests only — no network, no LLM. Run with:

    .venv/bin/python -m pytest tests/ -q
    .venv/bin/python tests/test_pipeline.py      # also works without pytest

Each test names the failure it guards against; most were written from bugs a
code review found in the first working version.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scenescout import dedupe, export, geo, llm, normalize  # noqa: E402


# ------------------------------------------------------- Delaware-only gate
def test_street_name_matching_another_city_is_not_out_of_state():
    # Clear Space Theatre is on Baltimore Avenue — in Rehoboth Beach, DE.
    assert geo.verdict(None, "20 Baltimore Avenue, Rehoboth Beach, DE, 19971")[0] == "de"


def test_house_number_is_not_mistaken_for_a_zip():
    # Two 5-digit numbers; 37401 is the house number, 19971 is the ZIP.
    assert geo.verdict(None, "37401 Malloy St, Rehoboth Beach, Delaware, 19971")[0] == "de"


def test_out_of_state_programming_is_rejected():
    assert geo.verdict("Philadelphia, PA")[0] == "out"
    assert geo.verdict("New York City, NY")[0] == "out"
    assert geo.verdict("Rahway, NJ")[0] == "out"
    # Delaware Nature Society programs at a preserve in Avondale, PA.
    assert geo.verdict("Avondale", "432 Sharp Road")[0] == "out"
    assert geo.verdict(None, "432 Sharp Rd, Avondale, PA 19311")[0] == "out"


def test_shared_city_names_resolve_to_delaware_unless_a_state_says_otherwise():
    # Newark, Camden, and Milford exist in Delaware and elsewhere.
    for city in ("Newark", "Camden", "Milford", "Dover", "Georgetown", "Milton"):
        assert geo.verdict(city)[0] == "de", city
    assert geo.verdict("Newark, NJ")[0] == "out"
    assert geo.verdict("Camden, NJ")[0] == "out"


def test_pennsylvania_zips_starting_19_are_not_delaware():
    assert geo.verdict(None, "Kennett Square, PA 19348")[0] == "out"
    assert geo.verdict(None, "Wilmington, DE 19801")[0] == "de"


def test_delaware_evidence_inside_a_venue_name_is_honoured():
    # The ICS route puts the raw VEVENT LOCATION in venue_name.
    assert geo.verdict(
        None, None,
        "Wilmington Campus | 4101 Washington Street, Wilmington, DE, 19802")[0] == "de"


def test_address_words_that_look_like_state_codes_do_not_win():
    # "Mt. Cuba" contains MT; the state is the trailing DE.
    assert geo.verdict(None, "3120 Barley Mill Rd, Mt. Cuba, DE 19807",
                       "Mt. Cuba Center")[0] == "de"


def test_generic_words_in_venue_names_are_not_localities():
    # Real Delaware venues whose names contain out-of-state city words.
    for name in ("Washington Street Ale House", "Delaware Public Media - WDDE",
                 "The Reading Room, Dover Public Library", "Cape May-Lewes Ferry"):
        assert geo.verdict(None, None, name)[0] != "out", name


def test_named_out_of_state_venues_are_rejected():
    # Longwood Gardens is in Kennett Square, PA and carries no locality word.
    assert geo.verdict(None, None, "Longwood Gardens")[0] == "out"
    assert geo.verdict(None, None, "World Cafe Live Philadelphia")[0] == "out"


def test_five_digit_house_numbers_are_not_zips():
    # Sussex and Kent addresses routinely have them.
    assert geo.verdict(None, "15411 Abbotts Pond Road")[0] == "unknown"
    assert geo.verdict(None, "Carvel State Office Building, 820 N. French Street")[0] == "unknown"


def test_missing_location_is_kept_not_guessed():
    # Every source is a Delaware organization, so no address is not evidence
    # of being out of state.
    assert geo.verdict(None, None, "Delaware Art Museum")[0] == "unknown"
    assert geo.verdict(None, "12345 Main St")[0] == "unknown"


# ----------------------------------------------------------- categories
def test_subcategory_implies_parent():
    # Guidelines: pick the subcategory, never the parent alongside it.
    assert llm.sanitize_categories([9, 17]) == [17]
    assert llm.sanitize_categories([11, 26, 4]) == [26, 4]


def test_categories_capped_and_cleaned():
    assert len(llm.sanitize_categories([2, 4, 7, 8, 9])) == 3
    assert llm.sanitize_categories([99, 4, "x", None]) == [4]
    assert llm.sanitize_categories([4, 4, 4]) == [4]


# ------------------------------------------------------------- identity
def test_ordinal_titles_are_distinct_events():
    # norm_title deliberately strips ordinals for fuzzy matching; the identity
    # hash must not, or the 4th-grade show overwrites the 3rd-grade show.
    a = normalize.content_hash(1, "3rd Grade Art Show", "2026-09-01", None, "Biggs")
    b = normalize.content_hash(1, "4th Grade Art Show", "2026-09-01", None, "Biggs")
    assert a != b


def test_matinee_and_evening_are_distinct_rows():
    matinee = normalize.content_hash(1, "Hamlet", "2026-09-01", "14:00", "DTC")
    evening = normalize.content_hash(1, "Hamlet", "2026-09-01", "19:30", "DTC")
    assert matinee != evening


# ---------------------------------------------------------------- price
def test_free_plus_price_is_ticketed():
    # "Free for members, $10 general" is a ticketed event, not a free one.
    assert normalize.parse_price("Free for members, $10 general") == (10, 10, 0)


def test_price_formats():
    assert normalize.parse_price("FREE") == (None, None, 1)
    assert normalize.parse_price("$28 - $32") == (28, 32, 0)
    assert normalize.parse_price("18-25") == (18, 25, 0)
    assert normalize.parse_price("$0") == (0, 0, 1)


def test_prose_numbers_are_not_prices():
    assert normalize.parse_price("Ages 7 to 11 welcome")[0] is None


def test_a_free_tier_does_not_make_a_ticketed_event_free():
    # "$0 to $43.98" is ticketed; calling it FREE would drop the $44 ceiling.
    assert normalize.parse_price("$0 to $43.98") == (0, 44, 0)


# ------------------------------------------------- keyword classification
def _rules(**event):
    return llm._RulesBackend().complete_json(
        "EVENT:" + json.dumps(event), llm.CLASSIFY_SCHEMA)


def test_plural_arts_words_are_recognized():
    # A trailing \b after the alternation used to stop "art" matching "arts".
    assert _rules(title="Fine Arts Festival")["category_ids"]
    assert _rules(title="Photography Exhibitions")["category_ids"] == [26]
    assert _rules(title="Film Screenings")["category_ids"] == [4]
    assert _rules(title="Book Readings")["category_ids"] == [8]


def test_pop_up_is_not_rock_pop():
    # The hyphen is a word boundary, so \bpop\b matched "pop-up".
    assert 18 not in _rules(title="Fresh Art Pop-Up Show")["category_ids"]
    assert 18 not in _rules(title="Pop-Up Opera With OperaDelaware")["category_ids"]
    assert 18 in _rules(title="Pop Music Night")["category_ids"]


def test_disqualifying_words_count_only_in_the_title():
    # One "summer camp" in a concert's description must not delete the concert.
    kept = _rules(title="Delaware Symphony Gala Concert",
                  description="Our summer camp students also perform.")
    assert kept["relevant"] is True
    dropped = _rules(title="Summer Camp Registration", description="Arts camp")
    assert dropped["relevant"] is False


def test_title_signal_is_reported_separately_from_body_signal():
    # A category found only in body text is weak evidence for screened sources.
    dining = _rules(title="Venison, A Field to Fork Dining Experience",
                    description="A performance by the chef, theater-style seating")
    assert dining["title_category_ids"] == []
    concert = _rules(title="An Evening of Chamber Music")
    assert concert["title_category_ids"] == [14]


def test_all_caps_event_prefix_does_not_truncate_the_payload():
    r = _rules(title="SPECIAL EVENT: Delaware Symphony Gala", description="orchestra")
    assert r["category_ids"] == [14]


def test_facility_notices_are_not_events_but_closed_is_not_a_banned_word():
    # Room bookings and closure notices are not public events...
    assert _rules(title="Video Studio Reservation")["relevant"] is False
    assert _rules(title="Museum Closed: Maintenance Week")["relevant"] is False
    assert _rules(title="Library Closed for Thanksgiving")["relevant"] is False
    # ...but "closed" appears in real exhibition titles.
    assert _rules(title="Artist Talk: Closed Forms")["relevant"] is True
    assert _rules(title="Closed Forms: A Sculpture Exhibition")["relevant"] is True


def test_a_disqualifying_title_beats_the_sources_own_tag():
    # Libraries file story time under "Arts and Crafts", but the case study
    # puts story time out of scope by name.
    library = {"tier": 3, "name": "delaware libraries", "extract_route": "libcal"}
    stats = {"llm_calls": 0}
    story = normalize._classify(
        {"title": "Story Time: Plant a Story", "source_categories": ["Arts and Crafts"]},
        library, stats, 0)
    assert story[0] == "out"
    craft = normalize._classify(
        {"title": "Adult Craft Bag", "source_categories": ["Arts and Crafts"]},
        library, stats, 0)
    assert craft[0] == "in"


# ------------------------------------------------------------- date/time
def test_utc_feed_time_lands_on_the_eastern_day():
    # 00:30 UTC is 8:30 p.m. the previous evening in Delaware.
    day, _ = normalize.parse_dt("2026-09-06T00:30:00+00:00")
    assert day == "2026-09-05"


def test_plain_date_is_unchanged():
    assert normalize.parse_dt("2026-09-05")[0] == "2026-09-05"


# ---------------------------------------------------------------- venue
def test_venue_normalization_uses_word_boundaries():
    # A substring replace of "inc" would turn Lincoln into "loln".
    assert "lincoln" in dedupe.norm_venue("Lincoln Theatre")
    assert dedupe.norm_venue("Bootless Stageworks, Inc.") == "bootless stageworks"
    assert dedupe.norm_venue("The Theatre") == "theatre"


# ----------------------------------------------------------------- dedup
def test_title_only_match_is_flagged_as_weaker_evidence():
    _, used_venue = dedupe.score_pair("Hamlet", None, "Hamlet", "Somewhere Else")
    assert used_venue is False
    score, used = dedupe.score_pair("Hamlet", "Grand Opera House", "Hamlet", "Grand Opera House")
    assert used is True and score > 95


def test_weekly_series_does_not_swallow_dates_between_occurrences():
    weekly = {
        "start_date": "2026-06-05", "end_date": "2026-08-28", "is_range": 0,
        "dates": json.dumps(["2026-06-05", "2026-06-12", "2026-08-28"]),
    }
    assert dedupe._date_overlaps("2026-06-12", None, weekly) is True
    assert dedupe._date_overlaps("2026-07-15", None, weekly) is False


def test_ongoing_exhibit_matches_across_its_whole_run():
    exhibit = {"start_date": "2026-06-01", "end_date": "2026-12-31",
               "is_range": 1, "dates": None}
    assert dedupe._date_overlaps("2026-09-01", None, exhibit) is True


# ---------------------------------------------------------------- export
def _row(**overrides):
    row = {h: None for h in export.HEADERS}
    row.update({"title of program": "X", "start date": "09/05/2026"})
    row.update(overrides)
    return row


def test_valid_row_passes():
    assert export.validate_row(_row()) == []


def test_validators_catch_guideline_violations():
    assert any("end date" in p for p in export.validate_row(_row(**{"end date": "2026-09-06"})))
    assert any("parent+child" in p for p in export.validate_row(_row(categories="9,17")))
    assert any("non-numeric" in p for p in export.validate_row(_row(categories="music,4")))
    assert any("more than 3" in p for p in export.validate_row(_row(categories="2,4,7,8")))
    assert any("whole number" in p for p in export.validate_row(_row(**{"low price": "12.50"})))
    assert any("phone" in p for p in export.validate_row(_row(**{"box office phone": "302.555.1212"})))
    assert any("scheme" in p for p in export.validate_row(_row(URL="example.org")))
    assert any("time" in p for p in export.validate_row(_row(**{"start time": "7:30 PM"})))


def test_url_cleaning():
    assert export.clean_url("example.org/x") == "https://example.org/x"
    assert export.clean_url("https://example.org") == "https://example.org"
    assert export.clean_url("TBA") is None


def test_formatters_match_the_submission_guidelines():
    assert export.fmt_date("2026-09-05") == "09/05/2026"
    assert export.fmt_time("19:30") == "7:30 p.m."
    assert export.fmt_time("09:00") == "9:00 a.m."
    assert export.fmt_time("12:15") == "12:15 p.m."
    assert export.fmt_time("00:30") == "12:30 a.m."
    capped = export.words_cap("word " * 300)
    assert len(capped.split()) <= 200 and capped.endswith("…")


# ------------------------------------------------------------- deployed site
def test_venue_names_fall_back_to_the_shipped_directory():
    # data/scenescout.db is gitignored, so a fresh clone — and every deployed
    # instance — had no venue names at all: uploads showed a blank venue for
    # every row. The committed directory snapshot is the fallback.
    import pathlib
    import tempfile

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.join(root, "mock_site"))
    import app as mock_site

    real_root = mock_site.ROOT
    try:
        # A directory with no data/scenescout.db in it.
        mock_site.ROOT = pathlib.Path(tempfile.mkdtemp())
        names = mock_site._venue_names()
    finally:
        mock_site.ROOT = real_root

    assert names, "no venue names without the pipeline database"
    assert names["3"] == "Delaware Art Museum"
    assert names["27"] == "The Grand Opera House"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except AssertionError as e:
                failures += 1
                print(f"  FAIL  {name}: {e}")
    print("ALL PASS" if not failures else f"{failures} FAILURES")
    sys.exit(1 if failures else 0)
