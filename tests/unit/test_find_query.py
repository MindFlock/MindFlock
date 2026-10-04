"""The shared Ctrl+F query (core/find_query.py)."""

from __future__ import annotations

import pytest

from backend.web.core.find_query import Hit, Query, find_hits

LINES = [
    "Error: disk full",
    "an error occurred; errors pile up",
    "terror in the logs",
    "web-ui.md updated",
    "timeout after 30s",
    "retrying the upload",
    "upload failed: timeout",
]


def spans(q, lines=LINES):
    return [(h.line, h.col, h.length) for h in find_hits(lines, q)]


def test_plain_is_literal_and_case_insensitive():
    assert spans(Query("error")) == [(0, 0, 5), (1, 3, 5), (1, 19, 5), (2, 1, 5)]
    assert spans(Query("a.b", regex=False), ["a.b axb"]) == [(0, 0, 3)]


def test_match_case():
    assert spans(Query("Error", case=True)) == [(0, 0, 5)]


def test_whole_word_skips_words_inside_words():
    assert spans(Query("error", word=True)) == [(0, 0, 5), (1, 3, 5)]
    # Punctuation inside the term is fine; a word boundary is about \\w.
    assert spans(Query("web-ui", word=True)) == [(3, 0, 6)]
    assert spans(Query("ui", word=True)) == [(3, 4, 2)]


def test_regex():
    assert spans(Query(r"\d+s", regex=True)) == [(4, 14, 3)]
    assert spans(Query(r"err(or|ors)\b", regex=True)) == [
        (0, 0, 5),
        (1, 3, 5),
        (1, 19, 6),
        (2, 1, 5),
    ]
    with pytest.raises(ValueError, match="invalid regular expression"):
        find_hits(LINES, Query("err(", regex=True))


def test_empty_matches_are_not_hits():
    assert spans(Query("x*", regex=True), ["abc"]) == []


def test_proximity_same_line():
    hits = find_hits(LINES, Query("upload", near="timeout", within=0))
    assert [(h.line, h.col) for h in hits] == [(6, 0)]
    assert hits[0].partner == (6, 15, 7)


def test_proximity_within_lines_both_directions():
    hits = find_hits(LINES, Query("retrying", near="timeout", within=1))
    assert [(h.line, h.partner) for h in hits] == [(5, (4, 0, 7))]
    assert find_hits(LINES, Query("retrying", near="error", within=1)) == []


def test_proximity_partner_is_not_the_hit_itself():
    assert find_hits(["error only"], Query("error", near="error", within=0)) == []
    two = find_hits(["error then error"], Query("error", near="error", within=0))
    assert [(h.col, h.partner) for h in two] == [(0, (0, 11, 5)), (11, (0, 0, 5))]


def test_options_apply_to_both_terms():
    hits = find_hits(LINES, Query("upload", near="Timeout", within=0, case=True))
    assert hits == []


def test_from_payload_clamps():
    q = Query.from_payload({"query": "x", "case": 1, "within": "-4", "near": "y"})
    assert q == Query("x", case=True, near="y", within=0)
    assert Query.from_payload({"query": "x", "within": "zz"}).within == 0


def test_hit_defaults():
    assert Hit(1, 2, 3).partner is None
