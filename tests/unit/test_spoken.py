"""``backend.web.core.spoken``: the line the phone speaks when a turn ends.

Pure text helpers exercised on realistic Claude replies (markdown, code
fences, paths, URLs, a trailing ``Say:`` line, a ``Say:`` in the middle that
must lose to a later one, no ``Say:`` at all, a reply that is only code), the
cached reply-file reader, and ``spoken_for``'s fallback ladder with a fake
instance/provider. Every file lives under ``tmp_path``.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from backend.providers import activity_markers as am
from backend.web.core import spoken

CLAUDE_REPLY = """I looked into the login redirect loop.

## What I found

The `after_login` handler in `backend/web/auth.py` re-reads `next=` from the
query string **after** the session cookie is set, so it bounces back to
`/login`. See https://github.com/MindFlock/app/issues/42 for the report.

```python
def after_login(request):
    nxt = request.query.get("next") or "/"
    return redirect(nxt)
```

- Fixed the handler (backend/web/auth.py:118)
- Added `tests/unit/test_auth_redirect.py`

Say: I found the cause but have not fixed it yet.

I then applied the fix and ran the suite: 412 passed.

> **Say:** I fixed the login redirect loop and the tests pass. Should I push it?
"""


# --------------------------------------------------------------------------- #
# INSTRUCTION
# --------------------------------------------------------------------------- #
def test_instruction_is_the_contracts_text_and_shared_with_the_hook():
    assert spoken.INSTRUCTION is am.VOICE_INSTRUCTION
    assert spoken.INSTRUCTION.startswith("Voice mode is on: the person is listening")
    assert "`Say:`" in spoken.INSTRUCTION
    assert spoken.INSTRUCTION.endswith("the question.")
    assert "\n" not in spoken.INSTRUCTION  # one line: a hook prints it as one


# --------------------------------------------------------------------------- #
# extract_say
# --------------------------------------------------------------------------- #
def test_extract_say_takes_the_last_say_line_and_strips_markdown():
    assert spoken.extract_say(CLAUDE_REPLY) == (
        "I fixed the login redirect loop and the tests pass. Should I push it?"
    )


@pytest.mark.parametrize(
    "line,expected",
    [
        ("Say: Done, all green.", "Done, all green."),
        ("**Say:** Done, all green.", "Done, all green."),
        ("**Say**: Done, all green.", "Done, all green."),
        ("SAY: Done, all green.", "Done, all green."),
        ("say: done, all green.", "done, all green."),
        ("Say — Done, all green.", "Done, all green."),
        ("Say - Done, all green.", "Done, all green."),
        ("- Say: Done, all green.", "Done, all green."),
        ("> Say: Done, all green.", "Done, all green."),
        ("* **Say:** Done, all green.", "Done, all green."),
        ("1. Say: Done, all green.", "Done, all green."),
        (
            "Say: I renamed `foo` to `bar` in *two* places.",
            "I renamed foo to bar in two places.",
        ),
        ("Say: see [the PR](https://x/y) for details.", "see the PR for details."),
    ],
)
def test_extract_say_accepts_the_spellings(line, expected):
    assert spoken.extract_say("Intro.\n\n" + line + "\n") == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        None,
        "No spoken line here.\n\nJust prose.",
        "Say hello to the team for me.",  # no marker after the word
        "I would say: this is fine.",  # not at the start of a line
        "Essay: a long one.",
        "Say:   \n",  # empty after the marker
        "**Say:**\n",
        "```\nSay: quoted in a fence\n```\n",  # a quoted instruction is not a say
    ],
)
def test_extract_say_none_when_there_is_no_say_line(text):
    assert spoken.extract_say(text) is None


def test_extract_say_a_later_line_beats_a_fenced_one_and_an_earlier_one():
    text = "Say: first\n```\nSay: inside\n```\nSay: last one\n"
    assert spoken.extract_say(text) == "last one"


def test_extract_say_is_capped_at_320_chars_on_a_word_boundary():
    long = "Say: " + " ".join(["word%d" % n for n in range(120)])
    got = spoken.extract_say(long)
    assert got is not None and len(got) <= spoken.SAY_MAX_CHARS
    assert got.endswith("…") and "word0 word1" in got
    assert not got[:-1].endswith(" ")


def test_extract_say_handles_crlf_replies():
    assert spoken.extract_say("Done.\r\nSay: all set.\r\n") == "all set."


# --------------------------------------------------------------------------- #
# strip_markdown
# --------------------------------------------------------------------------- #
def test_strip_markdown_unwraps_and_removes():
    text = (
        "# Title\n\n**bold** and _italic_ and `code` and ~~gone~~ and "
        "[a link](https://x.y/z) and ![img](p.png) <b>tag</b>\n\n"
        "- item one\n2. item two\n> quoted\n\n---\n\n"
        "```\nfenced block\n```\n\nsnake_case_name stays."
    )
    got = spoken.strip_markdown(text)
    assert got == (
        "Title bold and italic and code and gone and a link and img tag "
        "item one item two quoted snake_case_name stays."
    )


def test_strip_markdown_empty_and_whitespace():
    assert spoken.strip_markdown("") == ""
    assert spoken.strip_markdown(None) == ""
    assert spoken.strip_markdown("  a \n\n  b  ") == "a b"


# --------------------------------------------------------------------------- #
# digest
# --------------------------------------------------------------------------- #
def test_digest_of_a_markdown_reply_drops_code_paths_urls_and_keeps_the_question():
    reply = CLAUDE_REPLY.replace(
        "Say: I found the cause but have not fixed it yet.\n", ""
    )
    reply = reply.replace(
        "> **Say:** I fixed the login redirect loop and the tests pass. Should I push it?\n",
        "",
    )
    got = spoken.digest(reply)
    assert got == "I looked into the login redirect loop."
    reply2 = reply + "\nShould I open a PR for it?\n"
    got2 = spoken.digest(reply2)
    assert got2 == "I looked into the login redirect loop. Should I open a PR for it?"
    for bad in ("```", "`", "http", "backend/web", ".py", "#", "**"):
        assert bad not in got2


def test_digest_drops_tables_headings_and_paths_but_keeps_sentence_ends():
    text = (
        "## Summary\n\n| file | lines |\n|---|---|\n| a.py | 12 |\n\n"
        "I edited server.py and ./docs/web-api.md and ran `pytest`. "
        "Everything at https://ci.example/run/9 is green. Which one should I merge first?"
    )
    got = spoken.digest(text)
    assert (
        got == "I edited and and ran . Which one should I merge first?"
        or got.endswith("Which one should I merge first?")
    )
    assert "server.py" not in got and "docs" not in got and "https" not in got
    assert "|" not in got and "Summary" not in got


def test_digest_keeps_at_most_two_sentences_and_only_questions_after_the_first():
    text = "First thing. Second thing. Is this ok? Third thing. And this?"
    assert spoken.digest(text) == "First thing. Is this ok?"


def test_digest_caps_words_and_ends_with_a_period():
    text = " ".join(["w%d" % n for n in range(100)]) + " end"
    got = spoken.digest(text, max_words=10)
    assert got == "w0 w1 w2 w3 w4 w5 w6 w7 w8 w9…"
    assert spoken.digest("no terminal punctuation") == "no terminal punctuation."
    assert spoken.digest("Shouting!") == "Shouting!"


def test_digest_of_code_only_or_empty_is_empty():
    assert spoken.digest("```python\nprint(1)\n```") == ""
    assert spoken.digest("`just code`") == ""
    assert spoken.digest("") == ""
    assert spoken.digest(None) == ""
    assert spoken.digest("### \n\n| a | b |\n") == ""


def test_digest_ignores_an_unclosed_fence_tail():
    assert spoken.digest("Done with it.\n```\nnever closed\nmore") == "Done with it."


# --------------------------------------------------------------------------- #
# has_question
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text,expected",
    [
        ("Should I push it?", True),
        ("Should I push it", True),
        ("Do you want the PR opened", True),
        ("Which branch", True),
        ("Can I delete the old worktree", True),
        ("I fixed it and the tests pass.", False),
        ("Done.", False),
        ("", False),
        (None, False),
        ("Whatever you say.", False),  # "what" is not "whatever"
        ("It is fixed; is it ok? yes", True),
    ],
)
def test_has_question(text, expected):
    assert spoken.has_question(text) is expected


# --------------------------------------------------------------------------- #
# read_reply (the Stop hook's file) — cached on (mtime, size)
# --------------------------------------------------------------------------- #
@pytest.fixture
def marker_dir(tmp_path, monkeypatch):
    d = tmp_path / "markers"
    monkeypatch.setenv("MINDFLOCK_ACTIVITY_MARKER_DIR", str(d))
    spoken._REPLY_CACHE.clear()
    yield d
    spoken._REPLY_CACHE.clear()


def _write_reply(marker_dir, name, text, ts=None, mtime=None):
    marker_dir.mkdir(parents=True, exist_ok=True)
    p = am.reply_path(name)
    p.write_text(json.dumps({"ts": ts or int(time.time()), "text": text}))
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def test_read_reply_returns_ts_and_text_and_none_when_absent(marker_dir):
    assert spoken.read_reply("mindflock_a") is None
    _write_reply(marker_dir, "mindflock_a", "Say: hi", ts=1700000000)
    assert spoken.read_reply("mindflock_a") == (1700000000.0, "Say: hi")
    assert spoken.read_reply("") is None


def test_read_reply_sanitises_the_name_like_the_marker(marker_dir):
    _write_reply(marker_dir, "mindflock_a b/c", "x")
    assert am.reply_path("mindflock_a b/c").name == "mindflock_a_b_c.reply.json"
    assert spoken.read_reply("mindflock_a b/c") == pytest.approx(
        spoken.read_reply("mindflock_a_b_c")
    )


def test_read_reply_is_cached_on_mtime_and_size(marker_dir):
    p = _write_reply(marker_dir, "mindflock_c", "one", ts=1, mtime=1_000_000)
    assert spoken.read_reply("mindflock_c") == (1.0, "one")
    # Same size, same mtime, different bytes: the cache answers (one stat).
    p.write_text(json.dumps({"ts": 1, "text": "two"}))
    os.utime(p, (1_000_000, 1_000_000))
    assert spoken.read_reply("mindflock_c") == (1.0, "one")
    # A new mtime invalidates.
    os.utime(p, (1_000_001, 1_000_001))
    assert spoken.read_reply("mindflock_c") == (1.0, "two")


def test_read_reply_garbled_file_is_none_and_not_sticky(marker_dir):
    p = _write_reply(marker_dir, "mindflock_d", "ok", ts=5, mtime=2_000_000)
    p.write_text("{not json")
    os.utime(p, (2_000_000, 2_000_000))
    assert spoken.read_reply("mindflock_d") is None
    p.write_text(json.dumps({"ts": 6, "text": "fixed"}))
    os.utime(p, (2_000_002, 2_000_002))
    assert spoken.read_reply("mindflock_d") == (6.0, "fixed")


def test_forget_drops_the_file_and_the_cache(marker_dir):
    p = _write_reply(marker_dir, "mindflock_e", "bye")
    assert spoken.read_reply("mindflock_e") is not None
    spoken.forget("mindflock_e")
    assert not p.exists() and "mindflock_e" not in spoken._REPLY_CACHE
    spoken.forget("mindflock_e")  # idempotent, never raises


# --------------------------------------------------------------------------- #
# spoken_for: hook → digest(reply text) → digest(provider) → none
# --------------------------------------------------------------------------- #
class _Inst:
    Program = "claude"

    def __init__(self, title, wt="/tmp/wt"):
        self.Title = title
        self._wt = wt

    def GetWorktreePath(self):  # noqa: N802
        return self._wt


@pytest.fixture
def provider(monkeypatch):
    from backend import providers

    class _Prov:
        asked = None
        reply = None

        def last_assistant_text(self, session_name, workdir, **kw):
            _Prov.asked = (session_name, workdir)
            return _Prov.reply

    monkeypatch.setattr(providers, "resolve", lambda prog: _Prov())
    return _Prov


def test_spoken_for_prefers_the_hooks_say_line(marker_dir, provider):
    _write_reply(marker_dir, "mindflock_s1", "Done.\n\nSay: All green. Ship it?", ts=42)
    provider.reply = "ignored"
    got = spoken.spoken_for("s1", _Inst("s1"))
    assert got == {
        "say": "All green. Ship it?",
        "source": "hook",
        "has_question": True,
        "ts": 42.0,
    }
    assert provider.asked is None  # no transcript read when the hook spoke


def test_spoken_for_digests_the_reply_text_without_a_say_line(marker_dir, provider):
    _write_reply(marker_dir, "mindflock_s2", "I renamed the module. Tests pass.", ts=7)
    provider.reply = "newer transcript text"
    got = spoken.spoken_for("s2", _Inst("s2"))
    assert got == {
        "say": "I renamed the module.",
        "source": "digest",
        "has_question": False,
        "ts": 7.0,
    }


def test_spoken_for_falls_back_to_the_providers_transcript(marker_dir, provider):
    provider.reply = "Here is what I did.\n\n```\ncode\n```\nWant me to continue?"
    got = spoken.spoken_for("s.3", _Inst("s.3", wt="/w/t"))
    assert got["say"] == "Here is what I did. Want me to continue?"
    assert got["source"] == "digest" and got["has_question"] is True
    assert got["ts"] is None
    assert provider.asked == ("mindflock_s_3", "/w/t")  # per-window reader, tmux name


def test_spoken_for_none_when_nothing_is_known(marker_dir, provider):
    provider.reply = None
    assert spoken.spoken_for("s4", _Inst("s4")) == {
        "say": None,
        "source": "none",
        "has_question": False,
        "ts": None,
    }
    # An empty reply file (a CLI whose Stop payload lacks the text) keeps its
    # stamp but says nothing when the provider has no transcript either.
    _write_reply(marker_dir, "mindflock_s4", "", ts=9)
    assert spoken.spoken_for("s4", _Inst("s4")) == {
        "say": None,
        "source": "none",
        "has_question": False,
        "ts": 9.0,
    }
    # And a reply that is only code digests to nothing.
    _write_reply(marker_dir, "mindflock_s4", "```\nx = 1\n```", ts=10)
    assert spoken.spoken_for("s4", _Inst("s4"))["source"] == "none"


def test_spoken_for_never_raises(marker_dir, monkeypatch):
    from backend import providers

    def boom(prog):
        raise RuntimeError("no provider")

    monkeypatch.setattr(providers, "resolve", boom)
    assert spoken.spoken_for("s5", _Inst("s5"))["source"] == "none"
    assert spoken.spoken_for("s5", None)["source"] == "none"


# --- review follow-ups ------------------------------------------------------- #
def test_extract_say_takes_the_last_line_even_inside_an_unclosed_fence():
    text = "Here is the patch:\n```python\nprint(1)\n\nSay: Patched it. Run the tests?"
    assert spoken.extract_say(text) == "Patched it. Run the tests?"


def test_extract_say_accepts_an_unspaced_em_dash():
    assert spoken.extract_say("Done.\nSay—Ready.") == "Ready."
    assert spoken.extract_say("Done.\nSay–Ready.") == "Ready."
    # The ASCII dash still needs a space: "say-so" is a word.
    assert spoken.extract_say("the say-so of the team") is None


@pytest.mark.parametrize(
    "line,expected",
    [
        ("Got it, done.", False),
        ("Ok so I pushed it.", False),
        ("Any objections?", True),
        ("Should I push?", True),
        ("Which branch do you want", True),
    ],
)
def test_has_question_is_not_fooled_by_ok_got_any(line, expected):
    assert spoken.has_question(line) is expected


def test_read_reply_misses_the_cache_for_a_replaced_file_of_equal_size(marker_dir):
    import json as _json
    import os as _os

    from backend.providers import activity_markers as _am

    path = _am.reply_path("s1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps({"ts": 1, "text": "AAAA"}))
    st = _os.stat(path)
    assert spoken.read_reply("s1") == (1.0, "AAAA")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(_json.dumps({"ts": 1, "text": "BBBB"}))  # same length
    _os.replace(tmp, path)  # a new inode
    _os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))  # same mtime, same size
    assert spoken.read_reply("s1") == (1.0, "BBBB")
