"""Property: reading a transcript in pieces equals reading it once.

``backend.providers.usage_history`` memoizes each transcript's parsed turns
and, when the file grows, parses only the bytes after the last complete line
(also across a restart, from the memo saved on disk). For ANY split of a
transcript into appended chunks — splits inside a line included, with server
restarts between some of them — the final rolling-window totals and the
file's ``(turns, first_ts)`` must equal those of one full parse of the
finished file: no turn lost, none counted twice, the final content block of
a response still winning.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import time

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from backend.providers import pricing
from backend.providers import usage_history as uh

_RATES = {"in": 1.0, "out": 10.0, "cache_read": 0.1, "cache_write": 2.0}


def _iso(epoch):
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )


# One transcript line: an API response block (an id shared by several blocks,
# or none — keyed by the raw line), a line without usage, or junk.
_usage_line = st.fixed_dictionaries(
    {
        "mid": st.sampled_from(["m1", "m2", "m3", None]),
        "inp": st.integers(0, 20),
        "out": st.integers(0, 20),
        "cr": st.integers(0, 5),
        "age": st.integers(1, 3000),
    }
)
_line = st.one_of(
    _usage_line,
    st.just({"junk": '{"type": "user", "message": {"content": "hi"}}'}),
    st.just({"junk": 'not json but "usage" appears'}),
)


def _render(spec, now):
    if "junk" in spec:
        return spec["junk"]
    e = {
        "timestamp": _iso(now - spec["age"]),
        "message": {
            "model": "m",
            "usage": {
                "input_tokens": spec["inp"],
                "output_tokens": spec["out"],
                "cache_read_input_tokens": spec["cr"],
                "cache_creation_input_tokens": 0,
            },
        },
    }
    if spec["mid"]:
        e["message"]["id"] = spec["mid"]
        e["requestId"] = "r" + spec["mid"]
    return json.dumps(e)


def _fresh_process(mp):
    """What a restart loses: the in-memory memo, its load state, the cache."""
    mp.setattr(uh, "_FILE_MEMO", {})
    mp.setattr(uh, "_memo_state", {"path": None, "dirty": False, "saved_at": 0.0})
    mp.setattr(uh, "_cache", {"at": 0.0, "windows": None})


def _windows(mp):
    mp.setattr(uh, "_cache", {"at": 0.0, "windows": None})
    return uh.windows()


def _isolated(mp, base):
    root = os.path.join(base, "claude")
    pdir = os.path.join(root, "projects", "proj")
    os.makedirs(pdir)
    mp.setenv("MINDFLOCK_ASSISTANT_DIR", os.path.join(base, "assistant"))
    mp.setattr(uh, "_roots", lambda: {root: uh.AMBIENT_ACCOUNT})
    mp.setattr(pricing, "price_per_token", lambda model: dict(_RATES))
    _fresh_process(mp)
    return os.path.join(pdir, "a.jsonl")


def _assert_same_windows(got, want):
    assert got.keys() == want.keys()
    for k in want:
        assert {f: v for f, v in got[k].items() if f != "cost"} == {
            f: v for f, v in want[k].items() if f != "cost"
        }, k
        assert got[k]["cost"] == pytest.approx(want[k]["cost"]), k


@settings(max_examples=60)
@given(specs=st.lists(_line, min_size=1, max_size=12), data=st.data())
def test_any_split_of_a_transcript_reads_like_one_full_parse(specs, data):
    now = time.time()
    blob = "".join(_render(s, now) + "\n" for s in specs).encode()
    cuts = sorted(
        data.draw(
            st.lists(st.integers(1, len(blob) - 1), unique=True, max_size=6)
            if len(blob) > 1
            else st.just([])
        )
    )
    bounds = [0] + cuts + [len(blob)]
    restarts = data.draw(
        st.lists(st.booleans(), min_size=len(bounds) - 1, max_size=len(bounds) - 1)
    )

    with tempfile.TemporaryDirectory() as once, pytest.MonkeyPatch.context() as mp:
        path = _isolated(mp, once)
        with open(path, "wb") as f:
            f.write(blob)
        want = _windows(mp)
        want_turns, want_first, _off = uh._parse_turns(path)

    with tempfile.TemporaryDirectory() as pieces, pytest.MonkeyPatch.context() as mp:
        path = _isolated(mp, pieces)
        for lo, hi, restart in zip(bounds, bounds[1:], restarts):
            if restart:
                _fresh_process(mp)  # the next scan starts from the saved memo
            with open(path, "ab") as f:
                f.write(blob[lo:hi])
            got = _windows(mp)
        _assert_same_windows(got, want)
        turns, first = uh.read_turns(path)

    assert sorted(turns) == sorted(want_turns)
    assert first == want_first
