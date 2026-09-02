"""Matching helpers for assertions against the built frontend bundle.

``backend/web/static/app.js`` is generated, and its whitespace is the bundler's
business rather than ours. Rollup + esbuild indented with spaces and packed short
object literals onto a single line; Rolldown + Oxc (Vite 8) indents with tabs and
breaks those same literals across lines. Both emit the same program. Assertions
that quote a run of bundle source therefore have to be written against the tokens,
not the layout — otherwise every toolchain bump turns into a day of re-typing
string literals, and the pressure is to delete the check rather than fix it.

``squash`` collapses each whitespace run to one space. That is deliberately the
*only* thing it forgives: token order, punctuation, quoting and identifier
spelling all still have to match exactly, so a snippet that no longer describes
the shipped code still fails.
"""

import re


def squash(text):
    """Collapse every whitespace run in ``text`` to a single space."""
    return re.sub(r"\s+", " ", text)


def in_bundle(needle, js):
    """Is ``needle`` present in ``js``, ignoring how the bundler laid it out?"""
    return squash(needle) in squash(js)
