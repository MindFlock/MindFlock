"""Detect a coding-agent CLI's "usage limit reached" screen and, when it says
so, when the limit resets — so the prompt queue can wait out the limit and
resume exactly when the window reopens (roadmap D).

Pure functions over the agent's terminal text. The patterns are deliberately
SPECIFIC (a bare "rate limit" mention in code/output must not trip it) and are
plain data a provider can override — see ``ProviderConfig.usage_limit_patterns``
and ``BaseProvider.usage_limit_state``.

The reset-time parse is best-effort and supports the shapes CLIs use:
  * relative — "resets in 2h 30m", "try again in 45 minutes", "resets in 2 days"
  * absolute — "resets at 3pm", "limit resets 15:00", and the dated/zoned forms
    weekly caps print: "resets Jul 24 at 10:59am (America/New_York)"
When no reset is parseable the caller applies a bounded fallback, so a detected
limit can never stall the queue forever.
"""

from __future__ import annotations

import datetime as _dt
import re
import time
from typing import Optional, Sequence

#: The paid credits THEMSELVES spent ("You've hit your extra usage limit",
#: "Out of usage credits"): a real wall again, even with credits on. Part of
#: both limit sets below.
CREDITS_SPENT_PATTERNS: tuple = (
    r"(?:hit|reached) your (?:extra[\s-]usage|usage credits?)(?: limit)?",
    r"out of (?:extra[\s-]usage|usage credits)",
    r"used (?:all|up)(?: of)? your (?:extra[\s-]usage|usage credits)",
)

#: Phrases specific enough that seeing one means the CLI is usage/rate limited.
DEFAULT_LIMIT_PATTERNS: tuple = (
    r"usage limit reached",
    r"reached your usage limit",
    r"you'?ve reached your usage limit",
    r"you'?ve hit your (?:usage )?limit",
    r"rate limit(?:ed|\s+reached|\s+exceeded)",
    r"too many requests",
    r"claude usage limit",
    # Claude Code's window-specific banners ("5-hour limit reached ∙ resets 3am",
    # "Weekly limit reached ∙ resets Jul 24 at 10:59am (America/New_York)").
    r"(?:5|five)[\s-]hour limit reached",
    r"weekly limit reached",
    r"reached your weekly limit",
) + CREDITS_SPENT_PATTERNS

#: High-precision subset for classifying the agent's *activity state* (the red
#: "limit" pill and the queue's escape-and-resume routing). These are the CLI's
#: own usage-limit SCREEN banners — phrases that never occur in ordinary agent
#: output. It deliberately EXCLUDES the looser ``too many requests`` / bare
#: ``rate limited`` members of :data:`DEFAULT_LIMIT_PATTERNS`, which show up
#: constantly in normal work (an HTTP 429 in test output, rate-limiting code, an
#: agent's own prose) and must never flip a working/idle/clarify session to
#: "limit". The queue's own :func:`_refresh_limit_state` gate still uses the
#: full set — a false positive there is bounded and self-corrects via the live
#: usage meter, whereas a wrong *state* mislabels the pill and can send an Esc
#: into a live turn.
LIMIT_SCREEN_PATTERNS: tuple = (
    r"usage limit reached",
    r"reached your usage limit",
    r"you'?ve reached your usage limit",
    r"you'?ve hit your (?:usage )?limit",
    r"claude usage limit",
    r"(?:5|five)[\s-]hour limit reached",
    r"weekly limit reached",
    r"reached your weekly limit",
) + CREDITS_SPENT_PATTERNS


#: The CLI's "the plan window is spent but paid credits carry on" notices
#: (Claude Code: "Now using usage credits", "You're now using extra usage").
#: Seeing one means the window is OPEN for sending (billed to credits),
#: whatever the plan meter says — the opposite of a limit screen. A provider
#: names its own (``BaseProvider.usage_credits_patterns``, TOML ``[usage]
#: credits_patterns``); these are the shared default.
CREDITS_OPEN_PATTERNS: tuple = (
    r"now using usage credits",
    r"using (?:extra[\s-])?usage credits",
    r"(?:now|you'?re now) using extra[\s-]usage",
    r"extra usage (?:is )?(?:on|enabled|active)",
)


def _last_match(text: str, patterns: Sequence[str]) -> int:
    """The END offset of the last match of any of ``patterns`` in ``text``,
    or -1 when none matches."""
    last = -1
    for p in patterns or ():
        for m in re.finditer(p, text, re.I):
            last = max(last, m.end())
    return last


def _credits_latest(
    text: str, limit_patterns: Sequence[str], credits_patterns: Sequence[str]
) -> tuple:
    """``(limited, credits)`` from the LATEST notice on the pane: a terminal
    keeps old lines, so a limit banner printed just before the CLI rolled
    onto credits is still there above the credits notice. Whichever ends
    last describes now; a credits notice on the banner's own line ("5-hour
    limit reached ∙ now using usage credits") ends after it, so it wins too."""
    lim = _last_match(text, limit_patterns)
    cred = _last_match(text, credits_patterns)
    if cred >= 0 and cred >= lim:
        return False, True
    return lim >= 0, False


def is_credits_banner(
    text: str, patterns: Sequence[str] = CREDITS_OPEN_PATTERNS
) -> bool:
    """True when ``text`` shows the CLI running on paid credits: the plan's
    window is spent, yet prompts still go through (and are billed)."""
    if not text:
        return False
    return any(re.search(p, text, re.I) for p in patterns or ())


def is_limit_screen(
    text: str, credits_patterns: Sequence[str] = CREDITS_OPEN_PATTERNS
) -> bool:
    """True when ``text`` shows a CLI usage-limit SCREEN (high precision) and
    no credits notice came after it (:func:`_credits_latest`).

    Used for activity-state classification — see :data:`LIMIT_SCREEN_PATTERNS`
    for why this is stricter than :func:`detect_limit`."""
    if not text:
        return False
    limited, _credits = _credits_latest(text, LIMIT_SCREEN_PATTERNS, credits_patterns)
    return limited


# "resets in 2h 30m" / "try again in 45 minutes" / "resets in 2 days"
_REL = re.compile(
    r"(?:reset\w*|try again|available again|come back)\s+in\s+"
    r"(?:(\d+)\s*d(?:ays?)?)?[,\s]*"
    r"(?:(\d+)\s*h(?:ours?|rs?)?)?[,\s]*"
    r"(?:(\d+)\s*m(?:in(?:ute)?s?)?)?",
    re.I,
)
# "resets at 3pm" / "resets 15:00" / "resets Jul 24 at 10:59am (America/New_York)"
# — an optional weekday, an optional month+day(+year) (weekly caps print a
# date), the time, and an optional IANA zone in parens after it.
_ABS = re.compile(
    r"reset\w*\s+(?:on\s+)?"
    r"(?:(?:mon|tues?|wednes|thurs?|fri|satur|sun)day,?\s+)?"
    r"(?:(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+"
    r"(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\s+)?"
    r"(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?"
    r"(?:\s*\(([A-Za-z][A-Za-z_+\-/ ]{1,40})\))?",
    re.I,
)
_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        (
            "jan",
            "feb",
            "mar",
            "apr",
            "may",
            "jun",
            "jul",
            "aug",
            "sep",
            "oct",
            "nov",
            "dec",
        )
    )
}


def _zone(name: Optional[str]):
    """A ZoneInfo for an IANA name the CLI printed, or None (use local time)."""
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name.strip())
    except Exception:  # noqa: BLE001 — unknown zone: fall back to local
        return None


def _parse_reset_at(text: str, now: float) -> Optional[float]:
    """Best-effort epoch when the limit resets, or None if unparseable."""
    m = _REL.search(text)
    if m and (m.group(1) or m.group(2) or m.group(3)):
        secs = (
            int(m.group(1) or 0) * 86400
            + int(m.group(2) or 0) * 3600
            + int(m.group(3) or 0) * 60
        )
        if secs > 0:
            return now + secs
    m = _ABS.search(text)
    if not m:
        return None
    hh = int(m.group(4))
    mm = int(m.group(5) or 0)
    ap = (m.group(6) or "").lower()
    if ap == "pm" and hh < 12:
        hh += 12
    if ap == "am" and hh == 12:
        hh = 0
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    # The banner's zone (weekly resets print one) wins; otherwise local time —
    # a naive datetime's .timestamp() applies the local zone like mktime did.
    tz = _zone(m.group(7))
    base = _dt.datetime.fromtimestamp(now, tz)
    month = _MONTHS.get((m.group(1) or "")[:3].lower())
    try:
        cand = base.replace(
            year=int(m.group(3)) if m.group(3) else base.year,
            month=month if month else base.month,
            day=int(m.group(2)) if m.group(2) else base.day,
            hour=hh,
            minute=mm,
            second=0,
            microsecond=0,
        )
    except ValueError:
        return None
    ts = cand.timestamp()
    if ts <= now:
        if month is None:
            # Time-only, already past -> same wall-clock time tomorrow.
            ts += 86400.0
        elif not m.group(3) and now - ts > 86400.0:
            # Dated but yearless and clearly past (e.g. "Jan 2" read in
            # December) -> next year. Within a day it's just a stale banner —
            # keep the past value so the caller treats the window as open.
            try:
                ts = cand.replace(year=cand.year + 1).timestamp()
            except ValueError:
                return None
    return ts


def detect_limit(
    text: str,
    patterns: Sequence[str] = DEFAULT_LIMIT_PATTERNS,
    now: Optional[float] = None,
    credits_patterns: Sequence[str] = CREDITS_OPEN_PATTERNS,
) -> dict:
    """``{"limited": bool, "reset_at": float|None, "credits": bool}`` for the
    agent pane ``text``. ``credits``: the newest notice says the CLI runs on
    paid credits — the window is OPEN (``limited`` False), whatever the plan
    meter reads. A limit banner printed after it (the credits spent) is a
    limit again."""
    if not text:
        return {"limited": False, "reset_at": None, "credits": False}
    if now is None:
        now = time.time()
    limited, credits = _credits_latest(text, patterns, credits_patterns)
    if not limited:
        return {"limited": False, "reset_at": None, "credits": credits}
    return {"limited": True, "reset_at": _parse_reset_at(text, now), "credits": False}
