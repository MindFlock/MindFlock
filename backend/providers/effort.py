"""One neutral thinking-effort ladder, translated per coding CLI.

"Use more thinking on this one" is a thing every modern coding CLI can do and no
two of them spell the same way: ``claude --effort xhigh``, ``codex -c
model_reasoning_effort=high``, ``agy --effort high``, and several CLIs (aider,
goose, cline, opencode) have no such setting at all. A picker that showed one
CLI's spelling would be wrong for the next queue, and a picker that showed the
union would be a menu of flags rather than a decision.

So the UI offers ONE ladder — :data:`EFFORTS`, cheapest first — and this module
translates a rung into whatever the CLI that is about to run actually
understands, via each provider's :class:`~backend.providers.base.EffortSpec`:

* a rung the CLI supports is passed through by name;
* a rung ABOVE its ceiling clamps to its top rung (asking codex for ``max``
  gets ``xhigh``) rather than being forwarded — claude warns and silently uses
  its default for an unknown level, and codex forwards the string to the API,
  which 400s. Clamping is the difference between "as hard as this CLI goes" and
  a start that fails or quietly ignores you;
* the top rung, ``ultra``, is whatever that CLI calls its beyond-the-ladder mode:
  a level name of its own passed to the same flag (Claude Code's ``--effort
  ultracode`` — xhigh effort plus standing multi-agent orchestration), or, for a
  CLI that only recognises such a mode as a word in the prompt, that keyword
  appended to the seed prompt. On a CLI with neither, ``ultra`` is just its
  highest level;
* a CLI with no effort control at all resolves to nothing, and says so
  (:attr:`EffortPlan.note`) instead of pretending the pick landed.

The result is data — argv tokens and an optional prompt suffix — so the caller
decides where it goes. The intake force-start routes put the tokens in the
session's launch args (which persist, so a relaunch or a reboot-resume keeps the
effort) and run the prompt through :func:`decorate_prompt`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence, Tuple

#: The rungs a caller may ask for, cheapest first. Deliberately Claude Code's
#: own names (the richest ladder of the CLIs MindFlock bundles) plus ``ultra`` on
#: top, so the common case is a pass-through and only the ceilings clamp.
EFFORTS: Tuple[str, ...] = ("low", "medium", "high", "xhigh", "max", "ultra")

#: What ``ultra`` falls back to as an ORDINARY level, for a CLI that has no mode
#: of its own (no :attr:`~backend.providers.base.EffortSpec.ultra_level` and no
#: keyword): its highest rung at or below this.
_ULTRA_LEVEL = "max"


@dataclass(frozen=True)
class EffortPlan:
    """What a requested rung actually does to one launch.

    * ``requested`` — the neutral rung asked for (``""`` = nothing asked).
    * ``applied`` — the level name handed to the CLI (``""`` = none was).
    * ``args`` — argv tokens to append to the launch command.
    * ``prompt_keyword`` — keyword to append to the seed prompt (``""`` = none).
    * ``note`` — a human sentence when the request did NOT land as asked
      (clamped, or unsupported), else ``""``. Callers surface it; nothing here
      raises, because an effort pick must never be the reason a start fails.
    """

    requested: str = ""
    applied: str = ""
    args: Tuple[str, ...] = ()
    prompt_keyword: str = ""
    note: str = ""

    @property
    def changes_launch(self) -> bool:
        return bool(self.args or self.prompt_keyword)


def normalize(value) -> str:
    """A rung name from user input, or ``""`` for anything unrecognised."""
    v = str(value or "").strip().lower()
    return v if v in EFFORTS else ""


def validate(value) -> str:
    """Like :func:`normalize`, but raises ``ValueError`` on a non-empty junk
    value — for a request body, where a typo that silently ran at the CLI's
    default effort is worse than a refused start."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    level = normalize(raw)
    if not level:
        raise ValueError(
            "unknown effort %r — pick one of: %s" % (raw, ", ".join(EFFORTS))
        )
    return level


def _clamp(level: str, supported: Sequence[str]) -> str:
    """The highest ``supported`` level that is no higher than ``level``.

    Walks DOWN the neutral ladder from the request, so a CLI whose ceiling is
    ``high`` answers a ``max`` request with ``high``, and one that only offers
    exotic names of its own answers with its top rung. ``""`` when it supports
    nothing.
    """
    if not supported:
        return ""
    want = _ULTRA_LEVEL if level == "ultra" else level
    known = [x for x in EFFORTS if x != "ultra"]
    if want not in known:
        return ""
    for candidate in reversed(known[: known.index(want) + 1]):
        if candidate in supported:
            return candidate
    # Nothing at or below the request: this CLI's cheapest rung is still above
    # it (an exotic ladder). Give it that rather than nothing — the caller asked
    # for a specific effort, and its floor is the closest honest answer.
    return supported[0]


def plan(program: str, level) -> EffortPlan:
    """Resolve a rung against whichever provider claims ``program``."""
    requested = normalize(level)
    if not requested:
        return EffortPlan()
    try:
        from . import resolve as _resolve

        provider = _resolve(program)
        spec = provider.effort_spec()
        name = provider.name
        if name == "generic":
            # The catch-all claims arbitrary typed-in programs; "generic has no
            # effort setting" would name a provider the user never chose, so the
            # note says which command it actually is.
            import os

            name = os.path.basename((program or "").split()[0]) if program else name
    except Exception:  # noqa: BLE001 — an effort pick never breaks a launch
        return EffortPlan(requested=requested)

    # ``ultra`` on a CLI whose flag names that mode (claude: ``--effort
    # ultracode``) is passed straight to the flag, NOT clamped down the ordinary
    # ladder — clamping is for rungs above a ceiling, and this is a mode beside
    # it. The flag holds for the session, so the prompt keyword is redundant
    # there and only the keyword-only CLIs still use it.
    ultra_mode = requested == "ultra" and bool(spec.ultra_level) and bool(spec.args)
    keyword = spec.prompt_keyword if requested == "ultra" and not ultra_mode else ""
    applied = spec.ultra_level if ultra_mode else _clamp(requested, tuple(spec.levels))
    args: Tuple[str, ...] = ()
    if applied and spec.args:
        args = tuple(a.format(level=applied) for a in spec.args)
    elif applied and not spec.args:
        # Levels declared with no flag to carry them: nothing to pass.
        applied = ""

    note = ""
    if not args and not keyword:
        note = "%s has no effort setting — started at its own default" % name
    elif (
        applied
        and applied != requested
        and not ultra_mode
        and not (requested == "ultra" and keyword)
    ):
        note = "%s tops out at %s — started there" % (name, applied)
    return EffortPlan(
        requested=requested,
        applied=applied,
        args=args,
        prompt_keyword=keyword,
        note=note,
    )


def launch_args(program: str, level) -> Tuple[str, ...]:
    """The argv tokens ``level`` adds to ``program``'s launch (possibly none)."""
    return plan(program, level).args


def decorate_prompt(prompt: str, program: str, level) -> str:
    """``prompt`` with the CLI's effort keyword appended, when the rung has one.

    Appended (not prepended) and set off by a rule, so the ticket/PR body the
    agent reads first is untouched. The keyword stands alone on its line: it is a
    token the CLI scans the turn for, and wrapping it in a sentence of our own
    invention would be putting words in the requester's mouth.
    """
    keyword = plan(program, level).prompt_keyword
    if not keyword or not prompt:
        return prompt
    return prompt.rstrip("\n") + "\n\n---\n\n" + keyword + "\n"


def capability(provider) -> dict:
    """One provider's effort vocabulary, as the UI needs it.

    ``levels`` are the NEUTRAL rungs this CLI can actually distinguish (so the
    picker can say "codex tops out at Extra high" without knowing a flag);
    ``ultra_level`` is what it calls the top rung when that is a mode of its own
    (claude: ``ultracode``), and ``keyword`` is the prompt keyword its top rung
    adds instead, if any. At most one of the two is ever set — they are the two
    ways a CLI can be asked for the same thing — and either one puts ``ultra`` on
    the ladder.
    """
    try:
        spec = provider.effort_spec()
    except Exception:  # noqa: BLE001
        return {"levels": [], "ultra_level": "", "keyword": ""}
    supported = tuple(spec.levels)
    levels = [
        rung for rung in EFFORTS if rung != "ultra" and spec.args and rung in supported
    ]
    ultra_level = spec.ultra_level if spec.args else ""
    keyword = "" if ultra_level else spec.prompt_keyword
    if ultra_level or keyword:
        levels.append("ultra")
    return {"levels": levels, "ultra_level": ultra_level, "keyword": keyword}
