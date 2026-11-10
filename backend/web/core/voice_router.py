"""The voice router: one finished phrase → a command, or a prompt.

``POST /api/voice/route`` (routes/voice.py) hands every phrase the person
says to :func:`answer`; the phone page (``/m``) and the desktop mic both call
it, so the grammar lives here once. The phone keeps its own table
(mobile.js ``VOICE_CMD``) only as an offline fallback, and the rules below are
a port of it: the same normalisation (:func:`norm`), the same session
matcher (prefix → word overlap → loose containment, :func:`match_session`),
the same attention rule as the rail's bell (:func:`session_state`) and the
same sentences. Answers to a session's dialog ("yes", "docs, always", "tell
session three no") use the phone's dialog words too (mobile.js
``VOICE_YES`` … ``voiceParseAnswer``, :func:`parse_answer`).

A switch by NAME is said the way people talk ("switch to the sitecheck
session", "take me to login fix", "open sitecheck"): :func:`find_sessions`
ignores the words around a name and matches across hyphens, underscores,
camelCase and digits ("bot seven" is bot7), a plural, a wrong letter or a
word that merely SOUNDS like the name's (``voice_words.sounds_like``:
"sightcheck") allowed; several matches — two names a said word fits
equally — ask "Which one: a or b?" rather than guess. A new session is a short conversation: "In which project?", "What
should it do?". Each of those answers carries ``pending`` — what it asked
— which the client sends back with the next phrase: :func:`pick` and
:func:`continue_new` take it, and a phrase that is no answer to it is
routed as usual. A switch says the session's name — nothing more — and
its last reply only when that is news (:func:`switch_line`).

Sessions said by number ("session three", "next one") are numbered by the
CLIENT (the desktop's rail order, the phone's list order): the answer
carries ``index`` / ``delta`` and the client maps it, sending the title back
as ``title`` when it wants the rest (a dialog's keys, a ``say``).

Pure: functions take the ``/api/instances`` rows (``title``, ``name``,
``activity``, ``activity_since``, ``last_say``, ``last_turn``, …), the known
repositories, a session's ``/dialog`` body and the text, and return the
answer dict. No I/O — the route gathers the rows, reads the dialog, resolves
the title against the registry and creates the session. Nothing here
presses a key: an answer carries the keys, and the client posts them to
``/answer``.

Acting on the session itself: ``scroll`` ("scroll up", "page down", "a bit
more", "back to the bottom" — :func:`scroll_of`) and ``interrupt``
("interrupt", "press escape", "interrupt docs") come back for the route to
carry out on the pane (``core.voice_actions``); "more" / "keep going"
(``scroll_more``) repeat the last scroll only when the route remembers one,
else they are words. ``switch_back`` ("go back", "last session": the
client's previously focused session — "previous session" is still the rail
order's ``switch_rel``), ``where`` ("where am I") and ``commands`` ("what
can I say", answered from :data:`GUIDE`, the one table of every command)
complete the set. "Scratch that" is no command: it is the client's radio
word (``voice_words``' ``undo``).

The default is ``{"kind": "prompt"}``: anything that is not plainly a command
is words for the current session. A false command is worse than a missed one
(the missed one still reaches the agent), so every pattern matches the WHOLE
phrase. A prompt that nearly was a command — a plain switch, status, read,
question or interrupt naming no session, an answer with nothing asking —
says so in ``near`` (:func:`_near`: ``{"kind", "why", "arg"?, "suggest"?}``)
for the client to caption; the words are still typed. Fillers said before a
navigation or read command ("um", "so", "now", "okay so") are dropped
(:data:`_FILLER_RE`), never before an answer, a send or a new session. A
name that matches nothing but sounds like exactly one session's
(:func:`_near_row`) is that session after a switch verb.

Every "Which one: a or b?" carries a ``pending`` pick, and its ``then`` —
what the picked session was wanted for (an interrupt, an answer, a question,
a read, a status) — so "the first one" does that to it (:func:`pick`).
"""

from __future__ import annotations

import re
import time
from functools import lru_cache
from typing import Iterable, List, Optional, Tuple

from backend.web.core import voice_actions as _voice_actions
from backend.web.core import voice_words as _voice_words
from backend.web.core.session_create import title_from_prompt
from backend.web.core.spoken import strip_markdown

__all__ = [
    "ANSWER_AFTER_S",
    "ANSWERED_MARGIN_S",
    "GUIDE",
    "LOCAL_KINDS",
    "NO_TEXT_KINDS",
    "answer",
    "answer_in_time",
    "choice_of",
    "command",
    "commands_answer",
    "continue_new",
    "dialog_choices",
    "dialog_speech",
    "dialog_visible_at",
    "find_sessions",
    "finish",
    "fresh_reply",
    "heard_text",
    "is_select",
    "list_say",
    "match_repo",
    "match_session",
    "norm",
    "parse_answer",
    "phrase_timing",
    "pick",
    "plan_new",
    "scroll_of",
    "select_answer",
    "select_choices",
    "select_route",
    "select_speech",
    "session_state",
    "started",
    "status_say",
    "switch_line",
    "title_base",
]

#: Commands the client carries out itself: the answer has no ``say``.
LOCAL_KINDS = (
    "cancel",
    "stop",
    "hush",
    "off",
    "repeat",
    "condense",
    "switch_back",
    "where",
)
#: This module's kinds that carry none of the person's words (beside
#: ``speech_moonshine.NO_TEXT_KINDS``): the route reads them off the
#: phrase's Moonshine text (``heard``) when it has one.
NO_TEXT_KINDS = (
    "scroll",
    "scroll_more",
    "interrupt",
    "switch_back",
    "where",
    "commands",
)
#: At most this many names in a "Which one" / "No session called" sentence.
MAX_NAMES = 6
#: At most this many sessions read out by "what needs me".
MAX_NEEDS = 3
#: At most this many sessions read out by "list sessions".
MAX_LIST = 10
#: A reply this fresh (seconds since its turn ended) is read out when you
#: switch to its session; an older one only when asked ("what did it say").
FRESH_REPLY_S = 120
#: Idle this long on uncommitted work and the bell lists it as stuck
#: (mobile.js ``STUCK_IDLE_S``).
STUCK_IDLE_S = 20 * 60
#: Longest title made from a "called <name>" (the New dialog's cap).
TITLE_MAX = 32
#: Words of the task a title is made from.
TITLE_WORDS = 4
#: The title when there is neither a name nor a task.
DEFAULT_TITLE = "voice-session"

# Spoken counts: "session three", "number 12", "the second one". Ordinals
# stop at tenth; digits run to 99.
_NUMS = {
    w: i + 1
    for i, w in enumerate(
        "one two three four five six seven eight nine ten eleven twelve thirteen"
        " fourteen fifteen sixteen seventeen eighteen nineteen twenty".split()
    )
}
_ORDS = {
    w: i + 1
    for i, w in enumerate(
        "first second third fourth fifth sixth seventh eighth ninth tenth".split()
    )
}
#: The tens, said: "twenty" … "ninety", and "twenty one" … "ninety nine"
#: (a hyphen is a space by then) — a count to 99 said in words.
_TENS = {
    w: (i + 2) * 10
    for i, w in enumerate(
        "twenty thirty forty fifty sixty seventy eighty ninety".split()
    )
}
_SPOKEN = dict(_NUMS)
_SPOKEN.update(_TENS)
_SPOKEN.update(
    {"%s %s" % (t, u): n + _NUMS[u] for t, n in _TENS.items() for u in list(_NUMS)[:9]}
)
#: What a recogniser writes for a SESSION's number said alone after
#: "session" / "window" / "row" ("session to", "session for"): never read
#: as a number anywhere else (an option, a dialog's answer — a wrong number
#: there is costly).
_SESSION_HOMOPHONES = {
    "won": 1,
    "to": 2,
    "too": 2,
    "free": 3,
    "tree": 3,
    "for": 4,
    "fore": 4,
    "ate": 8,
}
_NUM_RX = (
    r"(?:[1-9][0-9]?|" + "|".join(sorted(_SPOKEN, key=lambda s: (-len(s), s))) + ")"
)
_ORD_RX = r"(?:[1-9][0-9]?(?:st|nd|rd|th)|" + "|".join(_ORDS) + ")"
# The verbs that move the focus: "(switch|go|…) (over )?to …" ("go back to the
# old approach" stays words for the agent).
_VERB_RX = r"(?:switch|go|change|jump|focus|open|show|select) (?:over )?to"


def norm(text) -> str:
    """mobile.js ``voiceNorm``: lowercase, curly quotes straightened, anything
    but letters, digits, ``'`` and spaces made a space, spaces collapsed."""
    s = str(text or "").lower().replace("’", "'").replace("`", "'")
    s = re.sub(r"[^a-z0-9' ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# A default "stop" / "hush" phrase (core.voice_words.DEFAULTS) → the spoken
# variants heard while that phrase is in the person's list; any other phrase
# (a default without variants, or one the person added) is matched whole.
_VARIANTS = {
    "stop listening": r"stop (?:listening|recording|dictating|the mic|the microphone)",
    "mic off": (
        r"(?:mic|microphone) off"
        r"|turn (?:off the (?:mic|microphone)|the (?:mic|microphone) off)"
    ),
    "that's all": r"that'?s all|that is all",
    "done talking": r"(?:i'?m |i am )?done talking",
    "stop talking": r"stop (?:talking|speaking|reading|saying that)",
    "enough": r"(?:that'?s |that is )?enough|enough (?:talking|of that)",
}
#: The kinds whose words the person may change (``voice.words``).
_WORD_KINDS = ("hush", "stop")
#: ``voice_words.KIN`` by phrase, whatever the kind.
_KIN = {p: tuple(kin) for by in _voice_words.KIN.values() for p, kin in by.items()}
#: The :data:`_CMDS` kinds matched past the fillers (``command``'s ``nav``).
_NAV_CMDS = ("list", "needs", "status", "condense", "repeat")


@lru_cache(maxsize=32)
def _words_rx(phrases: Tuple[str, ...]) -> "re.Pattern":
    """The whole-phrase pattern for one kind's ``phrases``: each default's
    :data:`_VARIANTS`, the rest as said (normalised like the text, an
    apostrophe optional), and what the recogniser writes for a default
    (``voice_words.HEARD``: "stop listing", "be quite") while it is in the
    list."""
    parts = [_VARIANTS[p] for p in phrases if p in _VARIANTS]
    literal = [norm(p) for p in phrases if p not in _VARIANTS]
    # The older wordings each phrase brings along, as voice_words.KIN (the
    # clients' table) lists them: the one source, so "ok stop" means here
    # what it means to voice_words.control_kind.
    literal += [norm(k) for p in phrases for k in _KIN.get(p, ())]
    literal += [norm(f) for f in _voice_words.heard_of(phrases)]
    alt = _voice_words.phrase_regex([p for p in literal if p])
    parts.append(alt.replace("'", "'?"))
    return re.compile(r"^(?:%s)$" % "|".join(parts))


def _kind_rx(kind: str, words: Optional[dict]) -> "re.Pattern":
    """``kind``'s pattern for the effective ``words`` (None = the defaults)."""
    got = (words or {}).get(kind)
    if not isinstance(got, (list, tuple)) or not got:
        got = _voice_words.DEFAULTS[kind]
    return _words_rx(tuple(str(p) for p in got))


# The phone's table, in its order (mobile.js VOICE_CMD), plus "list" and the
# wider "stop" (the client stops listening; typed text stays). "hush" and
# "stop" are the person's words (``voice.words``): :func:`command` swaps in
# their patterns; these are the defaults'.
_CMDS = [
    # "Scratch that" is the client's radio word (voice_words' ``undo``): it
    # takes back the words said before it, so it is never a command here.
    ("cancel", re.compile(r"^(?:cancel|never ?mind|never mine|forget it)$")),
    ("switch", re.compile(r"^%s (.+)$" % _VERB_RX)),
    (
        "list",
        re.compile(
            r"^(?:(?:what|which|with) sessions (?:do i have|are (?:up|open|running))"
            r"|list (?:the |my )?sessions"
            r"|what(?:'?s| is) (?:up|open|running))$"
        ),
    ),
    ("needs", re.compile(r"^(?:what|who) needs? (?:me|attention|my attention)$")),
    (
        "status",
        re.compile(
            r"^(?:what'?s it doing|what is it doing|status|what'?s happening"
            r"|what'?s going on)$"
        ),
    ),
    (
        "condense",
        re.compile(r"^(?:summ[ae]ri[sz]e(?: that| it)?|summary)$"),
    ),
    ("repeat", re.compile(r"^(?:(?:say|save) that again|repeat(?: that)?)$")),
    ("off", re.compile(r"^(?:voice off|turn voice off|turn off voice)$")),
    # "Stop talking": the client cuts what is being said and drops the queue,
    # and keeps listening if it was — unlike "stop", which stops the mic.
    ("hush", _kind_rx("hush", None)),
    ("stop", _kind_rx("stop", None)),
]

# "switch to session three", "go to 2", "session three", "number 4", "the
# third session" — the client's own numbering (its rail / list order).
# "Session number three", "row 2" (the rail's rows: the client says when one
# is a window), "session twenty one"; "session to" / "session for" — the
# recogniser's homophones of a number — only right after the noun.
_NOUN_NUM_RX = r"(?:(?:session|window|row)(?: number)?|number)"
_INDEX_RES = [
    re.compile(r"^%s (?:%s )?(%s)$" % (_VERB_RX, _NOUN_NUM_RX, _NUM_RX)),
    re.compile(r"^%s the (%s) (?:one|session|window|row)$" % (_VERB_RX, _ORD_RX)),
    re.compile(r"^%s (%s)$" % (_NOUN_NUM_RX, _NUM_RX)),
    re.compile(r"^the (%s) (?:session|window|row)$" % _ORD_RX),
    re.compile(
        r"^(?:%s )?(?:session|window|row)(?: number)? (%s)$"
        % (_VERB_RX, "|".join(_SESSION_HOMOPHONES))
    ),
]
# "the second one" alone: an option when the session is asking, else a session.
_PICK_RE = re.compile(r"^(?:the )?(%s) one$" % _ORD_RX)
# "next session", "go to the previous one", a bare "next" — the client's own
# order ("go back" and "last session" are ``switch_back``: :data:`_BACK_RE`).
_REL_RE = re.compile(
    r"^(?:(?:(?:can|could|would) you |let'?s |i want to |i'd like to |i wanna )?"
    r"(?:%s )?(?:the )?(next|previous|prev) (?:session|one|window)"
    r"|(next|previous))$" % _VERB_RX
)
# Said before a navigation or read command and never part of it: "um,
# switch to docs", "so scroll up", "okay so, status" (the phone's "ok" /
# "okay" lead is already gone: _Words). Only for the commands that move or
# read — never before an answer, a send or a new session.
_FILLER_RE = re.compile(
    r"^(?:(?:um+|uh+|uhm|er+|erm|hmm+|mm+|ah|oh|so|alright|all right|right|well"
    r"|and|now|then|actually|anyway) )+"
)
# --- acting on the session: scroll, interrupt; where am I, back, help --------
# Said around a command and never part of it: "now scroll up", "mindflock,
# interrupt", "can you go back".
_FILL_LEAD_RE = re.compile(
    r"^(?:(?:mind ?flock|now|please|ok|okay|hey|just|to|can you|could you"
    r"|would you|go ahead and) )+"
)
_FILL_TAIL_RE = re.compile(r"(?: (?:now|please|for me))+$")
# The output being scrolled, when it is named: "scroll the output up".
_SCROLL_OBJ = (
    r"(?: (?:it|this|that|the (?:output|screen|terminal|session|window|pane|text"
    r"|page|log|logs|conversation)))?"
)
# How far: "a bit", "half a page", "a line", "three pages", "5 lines", "twice".
_SCROLL_AMT = (
    r"(?: (?:by )?(?:(?P<bit>a bit|a little(?: bit)?|a touch|slightly|a tad)"
    r"|(?P<half>half a page|half a screen|half page|a half page)"
    r"|(?:a|one) (?P<unit1>page|line|screen)"
    r"|(?P<n>%s|a couple(?: of)?|a few) (?P<unit>pages|lines|screens|page|line"
    r"|screen|times)"
    r"|(?P<twice>twice)))?" % _NUM_RX
)
_SCROLL_RE = re.compile(
    r"^(?:scroll|roll|growl|crawl)%s (?:back )?(?P<dir>up|down)%s$"
    % (_SCROLL_OBJ, _SCROLL_AMT)
)
# The looser ways a scroll is said: "scroll a bit up" (the amount first),
# "scroll up more", "scroll way up" (three pages), "go up", "move down",
# "up" / "down" alone, "next page" / "previous page".
_SCROLL_LOOSE_RE = re.compile(
    r"^(?:(?:scroll|roll)(?: (?:(?P<bit>a bit|a little(?: bit)?|a touch|a tad"
    r"|slightly)|(?P<way>way|way way|a lot|lots)|some|further))?"
    r" (?P<dir>up|down)(?: (?:more|further|some more|(?P<bit3>a bit|a little)(?: more)?))?"
    r"|(?:go|move) (?P<dir2>up|down)(?: (?P<bit2>a bit|a little(?: bit)?))?"
    r"|(?P<dir3>up|down)"
    r"|(?P<pg>next|previous|prev) page)$"
)
_PAGE_RE = re.compile(
    r"^(?P<half>half )?page ?(?P<dir>up|down)(?: (?P<n>%s) times| (?P<twice>twice))?$"
    % _NUM_RX
)
_SCROLL_END_RE = re.compile(
    r"^(?:(?:(?:scroll|roll|crawl|growl|go|jump|skip|take me|bring me|get me)"
    r"(?: (?:all the way|right))?(?: back)?(?: up| down)? to"
    r"|(?:go |scroll |jump )?back to) (?:the )?(?:very )?"
    r"(?P<end>top|bottom|end|beginning|latest|newest)"
    r"(?: of (?:it|the (?:output|screen|terminal|session|page|log|conversation)))?"
    r"|scroll all the way (?P<dir>up|down)"
    r"|show(?: me)? the (?P<last>latest|newest|end|bottom)(?: output)?"
    r"|(?:(?:all the way )?to )?the (?:very )?(?P<bare>top|bottom)|(?P<bot>bottom))$"
)
_SCROLL_ENDS = {
    "top": "top",
    "beginning": "top",
    "bottom": "bottom",
    "end": "bottom",
    "latest": "bottom",
    "newest": "bottom",
}
# "More", "keep going": the last scroll again — only right after one (the
# route remembers it), else words for the agent.
_MORE_RE = re.compile(
    r"^(?:(?:scroll )?(?:some |a (?:bit|little(?: bit)?|touch) )?(?:more|further)"
    r"|keep (?:going|scrolling|on scrolling)|(?:scroll )?again|once more"
    r"|one more time)$"
)
# "Interrupt", "press escape", "interrupt docs": Escape in the session's pane.
_INTERRUPT_RE = re.compile(
    r"^(?:interrupts?(?: (?P<who>.+))?"
    r"|(?:(?:press|hit|send|tap|push)(?: the| an)? ?)?(?:escape|esc)(?: key)?)$"
)
#: Who "interrupt …" means when it is the session being talked to.
_INTERRUPT_SELF = frozenset(
    (
        "it",
        "this",
        "that",
        "him",
        "her",
        "them",
        "the session",
        "the agent",
        "this session",
        "this agent",
        "this one",
        "the current session",
        "the current one",
    )
)
# Back to the session focused before this one (the client's own memory).
_BACK_RE = re.compile(
    r"^(?:(?:go|switch|jump|flip|hop|change|take me|bring me) back"
    r"(?: (?:again|to (?:the )?(?:last|previous|other) (?:one|session|window)"
    r"|to where i was))?"
    r"|back|(?:(?:switch|go|change|jump) (?:over )?to )?(?:the )?last (?:session|window"
    r"|one)|the one before(?: (?:that|this))?"
    r"|(?:(?:switch|go|change|jump|flip|hop) (?:over )?to )?the other (?:one|session|window))$"
)
_WHERE_RE = re.compile(
    r"^(?:where am i|where are we|(?:which|what) session (?:is this|am i (?:in|on)"
    r"|are we (?:in|on)|is open)|what(?:'?s| is) (?:this|the current) session"
    r"(?: called)?|which one am i (?:in|on)|what am i looking at"
    r"|(?:which|what) session is (?:that|it)|which one is (?:this|that))$"
)
_COMMANDS_RE = re.compile(
    r"^(?:what can i say(?: to you| here)?|what (?:can|do) i say"
    r"|what (?:voice )?commands (?:are there|can i (?:say|use)|do you (?:know"
    r"|understand|have|take))|what are (?:the|my)(?: voice)? commands"
    r"|(?:list|show|tell|read|give)(?: me)?(?: (?:the|all the|all|my))?(?: voice)?"
    r" commands|voice commands?|commands|help|voice help)$"
)
# "What's docs doing?", "how is the parser going?": a session's status by name.
_STATUS_OF_RE = re.compile(
    r"^(?:what(?:'?s| is) (?P<a>.+?) (?:doing|up to|working on)"
    r"|how(?:'?s| is) (?P<b>.+?) (?:doing|going|getting on|coming along))$"
)
# "Bring docs up", "pull the parser up": a soft switch, like "open docs".
_UP_RE = re.compile(r"^(?:bring|pull|call) (?P<a>.+) up$")
# "what is it asking", "what does docs want", "read the question".
_QUESTION_RE = re.compile(
    r"^(?:what(?:'?s| is) (?P<a>.+?) asking(?: me)?(?: for| about)?"
    r"|what does (?P<b>.+?) want(?: me to do)?"
    r"|what(?:'?s| is) the question"
    r"|read(?: me)? (?:the|its|it's) question(?: again)?"
    r"|read (?:that|it) again)$"
)

# --- answers: the phone's dialog words (mobile.js VOICE_YES … VOICE_NUM) -------
_YES_RE = re.compile(
    r"\b(?:yes|yeah|yep|yup|allow|approve|okay|ok|go ahead|do it|sure)\b"
)
_NO_RE = re.compile(r"\b(?:no|nope|deny|reject|don'?t|cancel|skip|stop)\b")
# "always" must be SAID, and said as the whole answer: "always", "yes,
# always", "always allow that", "yes and don't ask again".
_ALWAYS_RE = re.compile(
    r"^(?:(?:yes|yeah|yep|yup|ok|okay) ?)?(?:and )?"
    r"(?:always(?: allow(?: (?:this|it|that|all))?)?"
    r"|don'?t ask(?: me)? again|do not ask(?: me)? again|never ask(?: me)? again)$"
)
_PNUM_RE = re.compile(
    r"\b(one|first|1|two|second|2|three|third|3|four|fourth|4|five|fifth|5|six|sixth"
    r"|6|seven|seventh|7|eight|eighth|8|nine|ninth|9)\b"
)
_PNUM = {
    w: (i // 3) + 1
    for i, w in enumerate(
        "one first 1 two second 2 three third 3 four fourth 4 five fifth 5 six sixth 6"
        " seven seventh 7 eight eighth 8 nine ninth 9".split()
    )
}
#: mobile.js ``VOICE_ORD``: options read out by number.
_ORD_WORDS = ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
# Every word an answer may be made of — a phrase with any other word in it is
# not an answer ("yes, but run the tests first" is words for the session).
# Numbers count only after option/choice ("option two") or as an ordinal
# before one/option/choice ("the second one").
_BODY_WORDS = frozenset(
    "yes yeah yep yup sure ok okay approve approved allow allowed go ahead do it"
    " that this all no nope deny denied reject don't dont not skip cancel stop"
    " always and ask me again never thanks thank you".split()
)
_PICK_NOUNS = ("one", "option", "choice")
# "Stop it", "stop that": No to a permission dialog, else an interrupt
# (:func:`finish`).
_STOP_IT_RE = re.compile(r"^stop (?:it|that|this)$")
# Who "it" is: the session being talked to.
_SELF = frozenset(
    (
        "it",
        "him",
        "her",
        "them",
        "they",
        "he",
        "she",
        "the agent",
        "the session",
        "this session",
        "this one",
    )
)
_NOUNS = ("session", "window", "number")

# "(start|open|create|make|spin up)? (a|another)? new session (called|named
# <name>)? (in|for|on <repo>)? (to|and|that <task>)?" — on the normalised
# text, where ":", "—" and "," are already spaces (so "new session: fix it"
# is the bare form below).
# Said the way people ask: "I want to start a new session", "can you spin
# one up", "create a session to fix the login bug" — a plain "a session"
# only with a verb and something after it ("start a session" alone stays
# words for the agent).
_NEW_RE = re.compile(
    r"^(?:(?:i want to|i'd like to|i would like to|i wanna|let'?s|let us"
    r"|can you|could you|would you|go ahead and) )?"
    r"(?:(?P<verb>start|open|create|make|spin up|launch|fire up|kick off|set up"
    r"|give me|i want|i need|i'd like) )?"
    r"(?:(?:a |another )?new (?:session|agent)|another (?:session|agent)"
    r"|(?P<plain>a session)|spin (?:one|another one|a new one) up"
    r"|spin up (?:a new|another) one)"
    r"(?: (?:called|named) (?P<name>.+?))?"
    r"(?: (?P<prep>in|for|on) (?P<repo>.+?))?"
    r"(?: (?:to|and|that) (?P<task>.+))?$"
)
# The bare "new session <task>".
_NEW_BARE_RE = re.compile(r"^new session (?P<task>.+)$")
_LEAD = ("ok", "okay", "hey", "please")
# Words around a spoken repository name that are not part of it.
_REPO_LEAD = ("the", "my", "our")
_REPO_TAIL = ("repo", "repository", "project", "folder", "codebase", "checkout")

# --- switching by name, in plain speech -----------------------------------------
# The verbs that move to a session by NAME: "switch to the sitecheck
# session", "take me to login fix", "put me on the settings one". Said
# after a polite lead ("can you switch to …") the name must match, or the
# phrase stays words for the agent.
_POLITE_RX = (
    r"(?:(?:can|could|would) you |let'?s |let me |i want to |i'd like to "
    r"|i would like to |i wanna |i need to |go ahead and |now )"
)
_GO_RE = re.compile(
    r"^(%s)?(?:(?:switch|switched|twitch|go|change|jump|hop)(?: me)? (?:over )?to"
    r"|(?:take|bring) me (?:over )?to|(?:put|get) me (?:on|onto|in)) (.+)$" % _POLITE_RX
)
# Softer verbs ("open the file", "show me the logs", "go back to the old
# approach" are words for the agent): only a session's whole name or its
# start in whole words — or any name match when "session" is said.
_SHOW_RE = re.compile(
    r"^%s?(?:open(?: up)?|show(?: me)?|focus(?: on)?|select|pull up|bring up"
    r"|go back to|back to|talk to|let me see) (?!(?:over )?to )(.+)$" % _POLITE_RX
)
#: Said before a name that is likely no session's ("the tests", "my code").
_ARTICLES = frozenset(("the", "my", "a", "an", "this", "that", "our", "your", "these"))
_SWITCH_THE_RE = re.compile(r"^(?:switch|switched) the (.+)$")
#: Words said around a session's name, never part of it.
_AROUND = frozenset(
    "the my a an our this that session sessions window windows one ones called"
    " named".split()
)
#: Words that may be part of a name but need not be said or matched
#: ("sitecheck bot" for sitecheck-bot7, "the login bot" for login-fix).
_SOFT = frozenset(("bot", "bots", "agent"))
# "What did it say?", "read it", "what did docs reply": the last reply, read
# on demand (a switch reads it only when it is news).
_READ_RE = re.compile(
    r"^(?:what did (?P<a>.+?)(?: just)? (?:say|reply|answer|write)(?: last)?"
    r"|what was (?:the|its|it's) (?:last )?(?:reply|answer|message)"
    r"|what was the last thing (?:it|they) said"
    r"|read (?:me )?(?:it|that|it out|the (?:last )?(?:reply|answer|message)"
    r"|its (?:last )?(?:reply|answer|message)|what it said))$"
)
# --- answering the router's own question ("Which one?", "In which project?") ---
_PICK_LEAD_RE = re.compile(
    r"^(?:(?:i mean|i meant|um|uh|how about|make it|go with|pick|choose|take"
    r"|i'll take|let'?s do|let'?s go with|the one called|that'?s|it'?s|its"
    r"|switch to|go to|open) )+"
)
_PICK_NUM_RE = re.compile(
    r"^(?:the )?(?:(%s)|(?:number |option )?(%s)|(last))(?: one)?$" % (_ORD_RX, _NUM_RX)
)
_PICK_STATE_RE = re.compile(
    r"^(?:the )?(?:one )?(?:(?:that'?s|that is|which is|thats|who'?s) )?"
    r"(working|busy|running|idle|done|finished|asking|waiting(?: on me)?"
    r"|with (?:a|the) question|offline)(?: one)?$"
)
_PICK_STATES = {
    "working": "working",
    "busy": "working",
    "running": "working",
    "idle": "idle",
    "done": "idle",
    "finished": "idle",
    "offline": "offline",
}
# The project, while a new session asks for one: "this one", "the same".
_HERE_RE = re.compile(
    r"^(?:this|here|current|same|this one|the same|the current"
    r"|(?:this|the same|the current|same) (?:one|project|repo|repository)"
    r"|the one i'?m (?:on|in))$"
)
_REPO_ANSWER_LEAD_RE = re.compile(
    r"^(?:(?:um|uh|i mean|how about|let'?s (?:do|use|go with)|use|go with"
    r"|make it|it'?s|its|in|into|on|for|from|the one called) )+"
)
# The task, while a new session asks for one: none at all …
_NO_TASK_RE = re.compile(
    r"^(?:nothing(?: yet| for now)?|no task|none|skip(?: it)?|just start it"
    r"|just open it|leave it (?:empty|blank)|no(?:pe)?(?: thanks)?|not yet"
    r"|i'?ll tell it later|later)$"
)
# … the words before it that are not the task ("to fix it", "it should …") …
_TASK_LEAD_RE = re.compile(
    r"^(?:(?:i want it to|i'd like it to|it should|have it|tell it to|ask it to"
    r"|so it can|to|and) )+"
)
# … and no new session after all (the "cancel" command's words too).
_NEW_CANCEL_RE = re.compile(
    r"^(?:no(?:pe)? (?:cancel|forget it|never ?mind)|don'?t (?:start|create|bother)"
    r"(?: it| one)?|forget the new session|cancel that|abort)$"
)


#: Every voice command — "what can I say" (``commands``) reads it out and
#: lists it, and tests/unit/test_voice_router.py routes every phrase to its
#: kind, so it can't drift from the grammar. ``(kind, phrase, what it
#: does)``: the kind :func:`command` gives the phrase ("radio": the
#: client's own words, never a command here); ``<name>``, ``<project>`` and
#: ``<task>`` stand for the person's own. The radio, stop and hush phrases
#: are the defaults: :func:`commands_answer` says the person's own.
GUIDE = (
    ("switch", "switch to <name>", "Go to a session by its name"),
    ("switch", "show me <name>", "Go to a session by its name"),
    ("switch_index", "session three", "Go to a session by its number in the list"),
    ("switch_back", "the other one", "Back to the session you were on before"),
    ("switch_rel", "next session", "Go to the next session in the list"),
    ("switch_rel", "previous session", "Go to the session above in the list"),
    ("switch_back", "go back", "Back to the session you were on before"),
    ("where", "where am I", "Hear which session this is"),
    ("scroll", "scroll up", "Scroll the session's output up a page"),
    ("scroll", "scroll down", "Scroll the output down a page"),
    ("scroll", "scroll up a bit", "Scroll half a page"),
    ("scroll", "scroll up five lines", "Scroll by lines or pages"),
    ("scroll", "page up", "Scroll up a page"),
    ("scroll", "go down", "Scroll down a page"),
    ("scroll", "scroll to the top", "Jump to the oldest output"),
    ("scroll", "back to the bottom", "Back to the latest output"),
    ("scroll_more", "more", "Scroll again the same way, right after a scroll"),
    ("interrupt", "interrupt", "Press Escape in the session: stop the agent"),
    ("interrupt", "interrupt <name>", "Interrupt another session by name"),
    ("needs", "what needs me", "Hear which sessions are waiting on you"),
    ("list", "list sessions", "Hear every session and what it is doing"),
    ("status", "what's it doing", "Hear what this session is doing"),
    ("status", "what's <name> doing", "Hear what another session is doing"),
    ("read", "what did it say", "Hear the session's last reply"),
    ("question", "what is it asking", "Hear the question a session is asking"),
    ("repeat", "say that again", "Hear the last thing said again"),
    ("condense", "summarize that", "Hear a short summary of the last reply"),
    ("new", "new session in <project> to <task>", "Start a new session"),
    ("answer", "yes", "Answer a question: yes, no, always, option two"),
    ("answer", "tell <name> yes", "Answer another session's question"),
    (
        "answer",
        "stop it",
        "No to a session's question; with none, stop the agent",
    ),
    ("radio", "over", "Send what you said, and keep listening"),
    ("radio", "over and out", "Send it and stop listening"),
    ("radio", "disregard", "Throw away everything said so far"),
    ("radio", "scratch that", "Take back the words just said"),
    ("cancel", "cancel", "Drop the command"),
    ("hush", "stop talking", "Stop speaking, keep listening"),
    ("stop", "stop listening", "Turn the microphone off"),
    ("off", "voice off", "Turn voice off for this session"),
    ("commands", "what can I say", "Hear and see this list"),
    (
        "assistant",
        "MindFlock, open intake",
        "Ask MindFlock itself: open a screen, change a setting, start a session",
    ),
)
#: What "what can I say" says (the list itself is on the screen).
COMMANDS_SAY = (
    "You can switch sessions by name or number, go back, scroll, interrupt, "
    "hear what needs you, and answer questions. Every command is on the screen."
)
#: A GUIDE radio phrase → the ``voice.words`` kind it is the default of.
_GUIDE_WORDS = {
    "over": "send",
    "over and out": "end",
    "disregard": "clear",
    "scratch that": "undo",
    "stop talking": "hush",
    "stop listening": "stop",
}


def commands_answer(words: Optional[dict] = None) -> dict:
    """The ``commands`` answer: :data:`COMMANDS_SAY` and every
    :data:`GUIDE` row as ``{say, does}`` — a radio, stop or hush phrase as
    the person's ``words`` say it (their first phrase of that kind; "stop
    listening" while it is still theirs)."""
    items = []
    for _kind, said, does in GUIDE:
        kind = _GUIDE_WORDS.get(said)
        got = (words or {}).get(kind) if kind else None
        if isinstance(got, (list, tuple)) and got and said not in got:
            said = str(got[0])
        items.append({"say": said, "does": does})
    return {"kind": "commands", "say": COMMANDS_SAY, "items": items}


def _end(text) -> str:
    """mobile.js ``endSentence``: a full stop unless it ends in one."""
    s = str(text or "").strip()
    return s if not s or s[-1] in ".!?" else s + "."


# --- words ↔ the original text ---------------------------------------------
class _Words:
    """The phrase as normalised words, each remembering the whitespace token
    of the ORIGINAL text it came from, so a task is handed on as it was said
    ("fix auth.py", not "fix auth py"). The phone's lead word (ok/okay/hey/
    please) and trailing "please" are dropped, as in ``voiceCommand``."""

    def __init__(self, text) -> None:
        self.toks = str(text or "").split()
        words: List[str] = []
        owner: List[int] = []
        for i, tok in enumerate(self.toks):
            for w in norm(tok).split():
                words.append(w)
                owner.append(i)
        if len(words) > 1 and words[0] in _LEAD:
            words, owner = words[1:], owner[1:]
        if len(words) > 1 and words[-1] == "please":
            words, owner = words[:-1], owner[:-1]
        self.words = words
        self.owner = owner
        self.text = " ".join(words)

    def at(self, offset: int) -> int:
        """The word index of character ``offset`` in :attr:`text`."""
        return self.text[:offset].count(" ")

    def original(self, start: int, end: Optional[int] = None) -> str:
        """Words ``start``..``end`` as the person said them (whole tokens),
        stray leading punctuation (":", "—") and a trailing comma dropped."""
        end = len(self.words) if end is None else end
        if start >= end:
            return ""
        out = " ".join(self.toks[self.owner[start] : self.owner[end - 1] + 1])
        return out.strip().lstrip(":;,—–-").strip().rstrip(",;:").strip()


def _parse_new(w: _Words) -> Optional[dict]:
    """The "new session" phrase → word spans, or None.

    ``{"name": (a, b) | None, "prep": str, "repo": (a, b) | None, "task": a |
    None}`` — word indexes into ``w.words``; the task runs to the end."""
    m = _NEW_RE.match(w.text) or _NEW_BARE_RE.match(w.text)
    if m is None:
        return None
    gd = m.groupdict()
    if gd.get("plain") and not (
        gd.get("verb") and (gd.get("name") or gd.get("repo") or gd.get("task"))
    ):
        return None  # "start a session" alone

    def span(group: str):
        if group not in m.re.groupindex or m.group(group) is None:
            return None
        return (w.at(m.start(group)), w.at(m.end(group)) + 1)

    task = span("task")
    return {
        "name": span("name"),
        "prep": m.groupdict().get("prep") or "",
        "repo": span("repo"),
        "task": task[0] if task else None,
    }


def _count(word: str) -> Optional[int]:
    """``"three"`` / ``"3"`` / ``"third"`` / ``"3rd"`` → 3, else None."""
    m = re.fullmatch(r"([1-9][0-9]?)(?:st|nd|rd|th)?", word or "")
    if m:
        return int(m.group(1))
    return _SPOKEN.get(word) or _ORDS.get(word)


def _ordinal(word: str) -> Optional[int]:
    if re.fullmatch(_ORD_RX, word or ""):
        return _count(word)
    return None


def _switch_command(text: str) -> Optional[dict]:
    """A switch by name said in plain speech → ``{"kind": "switch", "arg",
    "mode"?}``, or None. ``mode``: absent — any match, else ``no-session``;
    ``"must"`` — a match, else the phrase is a prompt (a polite lead, or a
    soft verb with "session" said); ``"strict"`` — a soft verb: only a whole
    name or its start in whole words (:func:`find_sessions`)."""
    m = _GO_RE.match(text)
    if m:
        return dict(
            {"kind": "switch", "arg": m.group(2)},
            **({"mode": "must"} if m.group(1) else {}),
        )
    m = _SHOW_RE.match(text) or _UP_RE.match(text)
    if m:
        named = any(x in ("session", "sessions", "window") for x in m.group(1).split())
        # A soft verb is no plain switch: a miss is words, no ``near``
        # ("open a new session tab") unless the name sounds like one.
        return {
            "kind": "switch",
            "arg": m.group(1),
            "mode": "must" if named else "strict",
            "quiet": True,
        }
    m = _SWITCH_THE_RE.match(text)
    if m:
        # "switch the docs": the recogniser's "the" for "to" — a switch only
        # when a session is called that ("switch the driver to postgres" is
        # words for the agent).
        return {"kind": "switch", "arg": m.group(1), "mode": "must", "quiet": True}
    return None


def _bare(text: str) -> str:
    """``text`` (normalised) without the fillers said around a command
    (:data:`_FILL_LEAD_RE`, :data:`_FILL_TAIL_RE`)."""
    return _FILL_TAIL_RE.sub("", _FILL_LEAD_RE.sub("", text))


def _scroll_amount(m) -> Tuple[str, int]:
    """``(amount, count)`` of a :data:`_SCROLL_RE` match."""
    gd = m.groupdict()
    if gd.get("bit") or gd.get("half"):
        return "half", 1
    if gd.get("twice"):
        return "page", 2
    unit = gd.get("unit1") or gd.get("unit") or ""
    amount = "line" if unit.startswith("line") else "page"
    n = gd.get("n")
    if not n:
        return amount, 1
    count = {"a couple": 2, "a couple of": 2, "a few": 3}.get(n) or _count(n) or 1
    return amount, count


def scroll_of(text) -> Optional[dict]:
    """A scroll phrase → ``{"kind": "scroll", "dir": "up"|"down", "amount":
    "page"|"half"|"line"|"top"|"bottom", "count"}``, or None. The whole
    phrase: "scroll up", "scroll the output down", "scroll up a bit" (half),
    "scroll down three lines", "page up", "scroll back up", "scroll to the
    top", "go to the bottom", "back to the bottom", "show me the latest";
    "scroll down to the footer" is words for the agent."""
    t = _bare(norm(text))
    m = _SCROLL_RE.match(t)
    if m:
        amount, count = _scroll_amount(m)
        return {
            "kind": "scroll",
            "dir": m.group("dir"),
            "amount": amount,
            "count": count,
        }
    m = _PAGE_RE.match(t)
    if m:
        count = 2 if m.group("twice") else _count(m.group("n") or "") or 1
        return {
            "kind": "scroll",
            "dir": m.group("dir"),
            "amount": "half" if m.group("half") else "page",
            "count": count,
        }
    m = _SCROLL_LOOSE_RE.match(t)
    if m:
        pg = m.group("pg")
        direction = (
            m.group("dir")
            or m.group("dir2")
            or m.group("dir3")
            or ("down" if pg == "next" else "up")
        )
        if m.group("bit") or m.group("bit2") or m.group("bit3"):
            amount, count = "half", 1
        else:
            amount, count = "page", 3 if m.group("way") else 1
        return {"kind": "scroll", "dir": direction, "amount": amount, "count": count}
    m = _SCROLL_END_RE.match(t)
    if m:
        # "top" alone is no scroll: it is how "stop" is often written.
        end = m.group("end") or m.group("last") or m.group("bare") or m.group("bot")
        if end:
            amount = _SCROLL_ENDS[end]
        else:
            amount = "top" if m.group("dir") == "up" else "bottom"
        return {
            "kind": "scroll",
            "dir": "up" if amount == "top" else "down",
            "amount": amount,
            "count": 1,
        }
    return None


def _interrupt_command(text: str) -> Optional[dict]:
    """ "Interrupt", "press escape" → ``{"kind": "interrupt"}`` (the session
    being talked to); "interrupt docs" → ``{"kind": "interrupt", "name",
    "mode"}`` — the name matched as a soft switch's (:func:`_switch_command`):
    only a whole name or its start, any match once "session" is said, and no
    match leaves the phrase words ("interrupt handler is broken")."""
    m = _INTERRUPT_RE.match(text)
    if m is None:
        return None
    who = (m.group("who") or "").strip()
    if not who or who in _INTERRUPT_SELF:
        return {"kind": "interrupt"}
    named = any(x in ("session", "sessions", "window", "agent") for x in who.split())
    return {"kind": "interrupt", "name": who, "mode": "must" if named else "strict"}


def _status_command(text: str) -> Optional[dict]:
    """ "What's docs doing?" → ``{"kind": "status", "target"}`` (a name only
    as :func:`_strict_match` finds it: "what is the test doing" is words for
    the agent), or None."""
    m = _STATUS_OF_RE.match(text)
    if m is None:
        return None
    tgt = _target((m.group("a") or m.group("b")).split(), False)
    if tgt is None or "index" in tgt:
        return None
    if tgt.get("self"):
        return {"kind": "status", "arg": ""}
    tgt["strict"] = True
    return {"kind": "status", "arg": "", "target": tgt}


def _read_command(text: str) -> Optional[dict]:
    """ "What did it say?" → ``{"kind": "read", "target"}`` (as
    :func:`_target` reads it; a name only as :func:`_strict_match` finds
    it), or None."""
    m = _READ_RE.match(text)
    if m is None:
        return None
    said = m.group("a")
    if not said:
        return {"kind": "read", "target": None}
    tgt = _target(said.split(), False)
    if tgt is None:
        return None
    if tgt.get("self"):
        tgt = None
    elif "name" in tgt:
        tgt["strict"] = True  # "what did the test say" is words for the agent
    return {"kind": "read", "target": tgt}


def choice_of(text) -> Optional[dict]:
    """What an answer says, the phone's order (mobile.js ``voiceParseAnswer``):
    the whole-answer always forms, then a NO anywhere, then a yes, then a
    picked number → ``{"choice": "always"|"no"|"yes"}`` or ``{"choice":
    "number", "number": N}``; None when ``text`` is not made only of answer
    words (``_BODY_WORDS``, "option two", "the second one")."""
    words = norm(text).split()
    if not words:
        return None
    number = None
    i = 0
    while i < len(words):
        w = words[i]
        if w in ("option", "choice"):
            j = i + 1
            if j < len(words) and words[j] == "number":
                j += 1
            n = _count(words[j]) if j < len(words) and not _ordinal(words[j]) else None
            if n is None:
                return None
            number = number or n
            i = j + 1
            continue
        k = i + 1 if w == "the" else i
        if k + 1 < len(words) and _ordinal(words[k]) and words[k + 1] in _PICK_NOUNS:
            number = number or _ordinal(words[k])
            i = k + 2
            continue
        if w not in _BODY_WORDS:
            return None
        i += 1
    t = " ".join(words)
    if _ALWAYS_RE.match(t):
        return {"choice": "always"}
    if _NO_RE.search(t):
        return {"choice": "no"}
    if _YES_RE.search(t):
        return {"choice": "yes"}
    if number is not None:
        return {"choice": "number", "number": number}
    return None


def _target(words: List[str], loose: bool) -> Optional[dict]:
    """Who an answer or a question is for: ``{"self": True}`` ("it"),
    ``{"index": N}`` ("session three", "number 2", "the third session") or
    ``{"name": words, "strict": loose}``; None for no target at all (a bare
    "session", or "it" where only a name or number may stand)."""
    t = " ".join(words)
    if not t or t in _NOUNS:
        return None
    if t in _SELF:
        return None if loose else {"self": True}
    m = re.fullmatch(r"(?:session|window|number) (%s)" % _NUM_RX, t) or re.fullmatch(
        r"the (%s) (?:session|window)" % _ORD_RX, t
    )
    if m:
        return {"index": _count(m.group(1))}
    if choice_of(t) is not None or t in ("all", "everything", "that", "this"):
        return None
    return {"name": t, "strict": loose}


def _answer_command(words: List[str]) -> Optional[dict]:
    """An answer phrase → ``{"kind": "answer", "choice", "number"?, "words",
    "target"}``, or None. The forms, first match wins:

    * bare — ``yes``, ``no, don't``, ``always allow``, ``option two``, and
      ``answer <answer>``: target None (the session being talked to);
    * ``(answer|say) <answer> (to|on|for) <target>``, ``tell <target> (to)?
      <answer>`` — explicit: an unknown name is ``no-session``;
    * ``<answer> to <target>``, ``<target>, <answer>`` — loose: the target
      must be a number or a session's whole name (or the start of it, whole
      words); anything else leaves the phrase a prompt."""

    def hit(body, tgt):
        c = choice_of(" ".join(body))
        if c is None or tgt is None:
            return None
        return dict(c, kind="answer", words=" ".join(body), target=tgt)

    if not words:
        return None
    c = choice_of(" ".join(words))
    if c is not None:
        return dict(c, kind="answer", words=" ".join(words), target=None)
    n = len(words)
    if words[0] == "answer":
        c = choice_of(" ".join(words[1:]))
        if c is not None:
            return dict(c, kind="answer", words=" ".join(words[1:]), target=None)
    if words[0] in ("answer", "say"):
        for p in range(2, n - 1):
            if words[p] in ("to", "on", "for"):
                got = hit(words[1:p], _target(words[p + 1 :], False))
                if got:
                    return got
    if words[0] == "tell":
        for k in range(2, n):
            body = words[k + 1 :] if words[k] == "to" else words[k:]
            got = hit(body, _target(words[1:k], False))
            if got:
                return got
    for p in range(1, n - 1):
        if words[p] == "to":
            got = hit(words[:p], _target(words[p + 1 :], True))
            if got:
                return got
    for k in range(1, n):
        got = hit(words[k:], _target(words[:k], True))
        if got:
            return got
    return None


def _question_command(text: str) -> Optional[dict]:
    m = _QUESTION_RE.match(text)
    if m is None:
        return None
    said = m.group("a") or m.group("b")
    if not said:
        return {"kind": "question", "target": None}
    tgt = _target(said.split(), False)
    if tgt is None:
        return None
    if tgt.get("self"):
        tgt = None
    elif "name" in tgt:
        tgt["strict"] = True  # "what does the user want" is words for the agent
    return {"kind": "question", "target": tgt}


# --- help: a question about MindFlock itself (core.voice_help answers it) ----
# The shape of a question: "how do I …", "what is …", "where do I …", "can I
# …", "is there …", "explain …", "tell me about …". "can you …" / "could you
# …" ask the agent to do something: never help.
_HELP_Q_RE = re.compile(
    r"^(?:how(?: (?:do|does|can|should|would|to|is|are|come)\b|$)"
    r"|what(?:'?s|s)?\b|which\b|where\b|why\b|when\b"
    r"|(?:can|could|should|do|does|is|are) (?:i|we|mindflock|there)\b"
    r"|is it possible\b|explain\b|describe\b"
    r"|tell me (?:about|how|what|where|why|when|whether|if)\b)"
)
# Said first, they address MindFlock itself: "mindflock, how does the fence
# work", "help: what is a team run" ("help me fix the login bug" is not a
# question, so it stays a prompt).
_HELP_PREFIX_RE = re.compile(
    r"^(?:(?:ask |hey )?mind ?flock(?:'?s)?|help|help me understand) (?P<q>.+)$"
)
_HELP_NAMED_RE = re.compile(r"\b(?:in|about|does|do|can|with|of|on) mind ?flock\b")
# MindFlock's own things, from the docs' headings and the Vocabulary table —
# a how/what question about one of these is about MindFlock.
_HELP_FEATURES = re.compile(
    r"\b(?:sessions?|templates?|schedul(?:e|es|ed|ing)|cron"
    r"|fences?|fenced|(?:red|green|keep ?out) zones?|zones?|autopilot|fast ?track"
    r"|team runs?|split(?:ting)? (?:a |one )?task|lanes?|outbox|mailbox"
    r"|devices?|my (?:mac|laptop|pc|computer|phone)|phone page|mobile page|the phone"
    r"|voice(?: words| mode| replies)?|mic|microphone|radio words|wake word|say all"
    r"|spoken replies|whisper|tickets?|intake|prs?|pull requests?|issues?"
    r"|worktrees?|settings|peers?|peer links?|people|shared folder|extensions?"
    r"|add ?ons?|the rail|rail|notifications?|the bell|toasts?|code (?:map|tree)"
    r"|the map|map tab|tours?|onboarding|verify|checklists?|saved prompts?"
    r"|prompt queue|pin(?:ned)? (?:a )?prompts?|recently closed|customi[sz]e|themes?"
    r"|keyboard shortcuts?|shortcuts?|hotkeys?|usage|token usage|costs?"
    r"|providers?|agent clis?|mcp|orchestrators?|workers?|lineage|tailscale"
    r"|tailnet|desktop app|doctor|workflow optimi[sz]er|branch(?:ing)? a session"
    r"|window rows?|windows|panes?|tabs?|diff tab|ask anywhere|assistant"
    r"|make pr|open pr|merge when green|commit button|ship(?:ping)?"
    r"|saved routes?|the sky|goes as far as|behaviors?|wait for me)\b"
)
# The agent's work (a prompt), the session's state (a status question), or
# someone spoken to: never help unless MindFlock was addressed first.
_HELP_NOT = re.compile(
    r"\b(?:fix(?:es|ed|ing)?|bugs?|tests?|testing|code(?! map| tree)|coding|files?|functions?"
    r"|methods?|class(?:es)?|errors?|exceptions?|stack ?traces?|crash(?:es|ed)?"
    r"|fail(?:s|ed|ing)?|broken|refactor(?:ing)?|implement(?:ed|ing|ation)?"
    r"|compil(?:e|es|ed|ing)|deploy(?:s|ed|ing)?|login|endpoints?|variables?"
    r"|modules?|imports?|lines?|components?|css|(?:my|this|the|our|that) "
    r"(?:project|repo|repository|codebase|app|branch|pr|ticket|issue|session|window)"
    r"|this|that|these|those|(?<!is )it|its|doing|did|done|finished|working on|said|saying"
    r"|asking|asked|stuck|status|progress|right now|currently|yet|how many|running"
    r"|(?<!how do )(?<!how can )(?<!where do )(?<!where can )you|your)\b"
)


# --- the assistant: a phrase addressed to MindFlock itself -------------------
# "MindFlock, open intake", "Hey MindFlock what needs me", "Flock, turn on
# the bell": the name FIRST, then the request. The wake word as the
# recogniser writes it counts (core.voice_words.wake_at: "mind flock", "mine
# flock", "mindlock" as they come; "My flock, …", "Mind block, …" — ordinary
# words — only with the pause the recogniser writes after a name said to
# it). "flock" alone, a name mid-sentence, "flock mode …" (the feature, said
# to the agent), "my flock open settings" (no pause) or "MindFlock's …"
# (about it, not to it) never address it.
_ASSIST_FLOCK_RE = re.compile(r"^(?:ask )?flock(?! mode\b) (?P<rest>.+)$")
#: An addressed phrase whose rest is one of these commands is that command
#: (the client's own words and the pane actions the assistant can't do):
#: "MindFlock, stop listening" stops the mic.
_ASSIST_DIRECT = frozenset(LOCAL_KINDS) | frozenset(
    ("scroll", "scroll_more", "interrupt", "commands")
)


def _assistant_command(w: "_Words", words: Optional[dict]) -> Optional[dict]:
    """``{"kind": "assistant", "text"}`` — the request as said, the name
    dropped — for a phrase addressed to MindFlock (``core.voice_words.wake_at``,
    or "flock …"), or the deterministic command its rest is when that is one
    of :data:`_ASSIST_DIRECT`; None when not addressed."""
    at = _voice_words.wake_at(" ".join(w.toks), words)
    if at is not None:
        # The first word from the token after the wake word on.
        start = next((i for i, o in enumerate(w.owner) if o >= at), len(w.words))
    else:
        m = _ASSIST_FLOCK_RE.match(w.text)
        if m is None:
            return None
        start = w.at(m.start("rest"))
    rest = w.original(start)
    if not norm(rest):
        return None
    got = command(rest, words)
    if got is not None and got["kind"] in _ASSIST_DIRECT:
        return got
    return {"kind": "assistant", "text": rest}


def _help_command(text: str) -> Optional[dict]:
    """A question about MindFlock itself → ``{"kind": "help", "text"}`` (the
    question, a "mindflock," / "help:" lead dropped), or None. Help when the
    phrase is question-shaped (:data:`_HELP_Q_RE`) AND either MindFlock was
    addressed first ("mindflock, …", "help: …") or it names MindFlock or one
    of its features (:data:`_HELP_FEATURES`) and says nothing of the agent's
    work, the session's state or "you" (:data:`_HELP_NOT`). A missed
    question still reaches the agent; when unsure it is a prompt."""
    m = _HELP_PREFIX_RE.match(text)
    if m and _HELP_Q_RE.match(m.group("q")):
        return {"kind": "help", "text": m.group("q")}
    if not _HELP_Q_RE.match(text) or _HELP_NOT.search(text):
        return None
    if _HELP_NAMED_RE.search(text) or _HELP_FEATURES.search(text):
        return {"kind": "help", "text": text}
    return None


def command(text, words: Optional[dict] = None) -> Optional[dict]:
    """The command ``text`` is, or None (a prompt): ``{"kind", "arg"}``, and
    for ``new`` ``{"kind": "new", "name", "repo", "task"}`` as said (the repo
    words unresolved — :func:`plan_new` resolves them); ``{"kind":
    "switch_index", "index"}``, ``{"kind": "switch_rel", "delta"}``, ``{"kind":
    "pick", "index"}`` ("the second one": :func:`answer` decides), ``{"kind":
    "answer", "choice", "number"?, "words", "target"}`` and ``{"kind":
    "question", "target"}`` (targets unresolved — :func:`answer` resolves
    them against the rows). ``words``: the person's effective voice words
    (``core.voice_words.effective``) — ``hush`` and ``stop`` are theirs;
    None = the defaults."""
    w = _Words(text)
    if not w.text:
        return None
    # A recogniser's doubled word ("stop stop", "Stop. Stop. Stop.", "repeat
    # repeat that"): the phrase with each run of one word said once first.
    once = [x for i, x in enumerate(w.words) if i == 0 or x != w.words[i - 1]]
    if len(once) < len(w.words):
        got = command(" ".join(once), words)
        if got is not None:
            return got
    for kind, rx in _CMDS[:1]:
        if rx.match(w.text):
            return {"kind": kind, "arg": ""}
    got = _assistant_command(w, words)
    if got is not None:
        return got
    p = _parse_new(w)
    if p is not None:
        return {
            "kind": "new",
            "name": " ".join(w.words[p["name"][0] : p["name"][1]]) if p["name"] else "",
            "repo": " ".join(w.words[p["repo"][0] : p["repo"][1]]) if p["repo"] else "",
            "task": w.original(p["task"]) if p["task"] is not None else "",
        }
    # The navigation and read commands are matched past the fillers said
    # before them ("um, switch to docs", "so scroll up", "now status"):
    # ``nav``. Answers, sends and new sessions never are.
    nav = _FILLER_RE.sub("", w.text) or w.text
    filled = nav != w.text
    bare = _bare(nav)
    got = scroll_of(bare)
    if got is not None:
        return got
    if _MORE_RE.match(bare):
        return {"kind": "scroll_more"}
    got = _interrupt_command(bare)
    if got is not None:
        return got
    for kind, rx in (
        ("switch_back", _BACK_RE),
        ("where", _WHERE_RE),
        ("commands", _COMMANDS_RE),
    ):
        if rx.match(bare):
            return {"kind": kind}
    m = _REL_RE.match(nav)
    if m:
        rel = m.group(1) or m.group(2)
        return {"kind": "switch_rel", "delta": 1 if rel == "next" else -1}
    for rx in _INDEX_RES:
        m = rx.match(nav)
        if m:
            said = m.group(1)
            return {
                "kind": "switch_index",
                "index": _SESSION_HOMOPHONES.get(said) or _count(said),
            }
    got = _switch_command(nav)
    if got is not None and filled and not got.get("mode"):
        got["mode"] = "must"  # "so switch to …": a match, else words
    got = got or _read_command(nav) or _status_command(nav)
    if got is not None:
        return got
    full = norm(text)
    for kind, rx in _CMDS[1:]:
        if kind in _WORD_KINDS:
            if words is not None:
                rx = _kind_rx(kind, words)
            # A stop or hush phrase is matched as said, the "ok" lead kept:
            # "ok stop" is hush (voice_words.KIN), "stop" alone is stop.
            m = rx.match(full) or rx.match(w.text)
        else:
            m = rx.match(nav if kind in _NAV_CMDS else w.text)
        if m:
            return {
                "kind": kind,
                "arg": (m.group(1) or "").strip() if m.groups() else "",
            }
    # A stop or hush phrase heard a letter off ("pipe town", "hault
    # listening"): the whole phrase, within voice_words.sounds_like's margin.
    for kind in _WORD_KINDS:
        got = (words or {}).get(kind)
        if not isinstance(got, (list, tuple)) or not got:
            got = _voice_words.DEFAULTS[kind]
        kin = _voice_words.KIN.get(kind) or {}
        said = [str(p) for p in got] + [k for p in got for k in kin.get(str(p), ())]
        if any(_voice_words.sounds_like(w.text, p) for p in said):
            return {"kind": kind, "arg": ""}
    q = _question_command(w.text) or _help_command(w.text)
    if q is not None:
        return q
    m = _PICK_RE.match(w.text)
    if m:
        return {"kind": "pick", "index": _count(m.group(1)), "words": w.text}
    return _answer_command(w.words)


def heard_text(text, heard, words: Optional[dict] = None, kinds=NO_TEXT_KINDS):
    """Which text the route reads a phrase from, given whisper's ``text``
    and Moonshine's ``heard`` → ``(text to route, name text or None)``.

    Moonshine (keyterm-biased, fast) decides the control words only when
    whisper's text is no command at all: whisper is the better command ear
    (the 2026-11 bench: the kind alone 264/272 against 240/272), so when
    both parse as commands — and disagree — whisper's wins. Moonshine's
    counts only when it is a command of ``kinds`` (no words of the person's
    in it). A switch by NAME Moonshine heard takes its name from whisper's
    words when it can ("switch to site check about seven" over whisper's
    "which is sitecheck bot seven"): the second value is Moonshine's verb
    with whisper's words in place of its name, for the route to try first —
    it stands only if it switches."""
    text = str(text or "")
    if not isinstance(heard, str) or not heard.strip():
        return text, None
    hcmd = command(heard, words)
    if hcmd is None or hcmd["kind"] not in kinds:
        return text, None
    if command(text, words) is not None:
        return text, None
    alt = None
    if hcmd["kind"] == "switch" and hcmd.get("arg"):
        hw = norm(heard).split()
        verb = len(hw) - len(norm(hcmd["arg"]).split())
        tail = norm(text).split()[verb:]
        if verb > 0 and tail:
            alt = " ".join(hw[:verb] + tail)
    return heard, alt


# --- sessions ----------------------------------------------------------------
def _name(row: dict) -> str:
    """What a session is called (mobile.js ``nameOf``): its ``name``, else
    its title."""
    return str(row.get("name") or row.get("title") or "")


def _listed(rows: Iterable) -> List[dict]:
    """The rows a person can talk about: titled, and not hidden off the rail
    by the feature that owns them (a Verify checklist's session)."""
    return [
        r
        for r in rows or ()
        if isinstance(r, dict) and r.get("title") and not r.get("hidden_by")
    ]


def match_session(query, rows) -> Tuple[Optional[dict], List[dict]]:
    """mobile.js ``voiceMatchSession`` → ``(hit, candidates)``.

    Names and titles, normalised: a unique prefix match, else the unique best
    word overlap, else a unique loose containment; a tie lists the tied
    rows, no match an empty list."""
    q = norm(query)
    cands = [
        {"row": r, "n": norm(_name(r)), "t": norm(r.get("title"))}
        for r in _listed(rows)
    ]
    if not q:
        return None, [c["row"] for c in cands]
    pre = [c for c in cands if c["n"].startswith(q) or c["t"].startswith(q)]
    if len(pre) == 1:
        return pre[0]["row"], []
    if pre:
        return None, [c["row"] for c in pre]
    qw = q.split(" ")
    scored = []
    for c in cands:
        words = (c["n"] + " " + c["t"]).split(" ")
        n = sum(1 for x in qw if x in words)
        if n > 0:
            scored.append((n, c))
    scored.sort(key=lambda x: -x[0])  # stable, like Array.prototype.sort
    if len(scored) == 1 or (len(scored) > 1 and scored[0][0] > scored[1][0]):
        return scored[0][1]["row"], []
    if scored:
        return None, [c["row"] for _, c in scored]
    loose = [c for c in cands if q in c["n"] or q in c["t"] or (c["n"] and c["n"] in q)]
    if len(loose) == 1:
        return loose[0]["row"], []
    return None, [c["row"] for c in loose]


_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _toks(text) -> List[str]:
    """A name as spoken tokens: camelCase apart ("SiteCheck" → site check),
    normalised, letter and digit runs apart ("bot7" → bot 7), number words
    as digits ("seven" → 7)."""
    out: List[str] = []
    for w in norm(_CAMEL.sub(" ", str(text or ""))).replace("'", "").split():
        for part in re.findall(r"[a-z]+|[0-9]+", w):
            out.append(str(_NUMS[part]) if part in _NUMS else part)
    return out


def _said_variants(query) -> List[List[str]]:
    """The words of a spoken name, :data:`_AROUND` dropped — once with a
    "one" kept as 1 ("copy one"), once without it ("the settings one")."""
    raw = [
        p
        for w in norm(query).replace("'", "").split()
        for p in re.findall(r"[a-z]+|[0-9]+", w)
    ]
    canon = lambda ws: [str(_NUMS[p]) if p in _NUMS else p for p in ws]  # noqa: E731
    keep = canon([p for p in raw if p not in _AROUND or p == "one"])
    drop = canon([p for p in raw if p not in _AROUND])
    return [keep, drop] if keep != drop else [drop]


def _one_edit(a: str, b: str) -> bool:
    """``a`` and ``b`` differ by one letter: changed, added, dropped or two
    swapped."""
    if abs(len(a) - len(b)) > 1 or a == b:
        return False
    if len(a) == len(b):
        diff = [i for i in range(len(a)) if a[i] != b[i]]
        return len(diff) == 1 or (
            len(diff) == 2
            and diff[1] == diff[0] + 1
            and a[diff[0]] == b[diff[1]]
            and a[diff[1]] == b[diff[0]]
        )
    if len(a) > len(b):
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    return a[i:] == b[i + 1 :]


def _word_hit(q: str, n: str) -> int:
    """How well the said word ``q`` names the name token ``n``: 3 the same,
    2 a plural of it, 1 its start (three letters at least), one letter off
    (five at least) or a sound-alike of it ("sightcheck" for sitecheck:
    ``voice_words.sounds_like``, five letters at least, its margin), 0 not
    at all. Numbers only match whole. A sound-alike scores no higher than a
    start, so a name said right always beats one that merely sounds right,
    and two that sound alike tie — and are asked about."""
    if q == n:
        return 3
    if q.isdigit() or n.isdigit():
        return 0
    if q + "s" == n or n + "s" == q or q + "es" == n or n + "es" == q:
        return 2
    if len(q) >= 3 and n.startswith(q):
        return 1
    if (
        len(q) >= 5
        and len(n) >= 5
        and (_one_edit(q, n) or _voice_words.sounds_like(q, n))
    ):
        return 1
    return 0


def _label_score(said: List[str], label: List[str], strict: bool):
    """``said`` against one name's tokens → ``(matched, quality, soft,
    exact, tail, leftover)``, or None when a word matches nothing. Two or
    three said words may make one token ("site check" ↔ sitecheck — spelt
    right, or as a sound-alike: "sight check"), one said word two tokens
    ("sitecheckbot"); a :data:`_SOFT` word that matches nothing is skipped.
    ``strict``: ``said`` must be the whole name or its start, word for
    word."""
    if not said or not label:
        return None
    exact = "".join(said) == "".join(label)
    tail = label[-len(said) :] == said
    if strict:
        if label[: len(said)] != said:
            return None
        return (len(said), 3 * len(said), 0, exact, tail, len(label) - len(said))
    pool = label + [label[i] + label[i + 1] for i in range(len(label) - 1)]
    used = set()
    matched = quality = soft = 0
    i = 0
    while i < len(said):
        step, best, at = 1, 0, None
        for k in (3, 2):
            if i + k <= len(said) and "".join(said[i : i + k]) in label:
                step, best, at = k, 3, label.index("".join(said[i : i + k]))
                break
        if not best:
            for j, n in enumerate(pool):
                h = _word_hit(said[i], n)
                if h > best:
                    best, at = h, j
        if not best:
            # Two or three words heard a little off, run together: "sight
            # check" for sitecheck (the shorter join first: it leaves the
            # next word to match on its own merits).
            for k in (2, 3):
                if i + k <= len(said):
                    joined = "".join(said[i : i + k])
                    for j, n in enumerate(pool):
                        h = _word_hit(joined, n)
                        if h > best:
                            step, best, at = k, h, j
                    if best:
                        break
        if not best:
            if said[i] not in _SOFT:
                return None
            i += 1
            continue
        used.add(at)
        if at >= len(label):  # two tokens run together: both are spoken for
            used.update((at - len(label), at - len(label) + 1))
        chunk = said[i : i + step]
        soft += sum(1 for x in chunk if x in _SOFT)
        matched += sum(1 for x in chunk if x not in _SOFT)
        quality += best * step
        i += step
    if not matched:
        return None
    leftover = len([j for j in range(len(label)) if j not in used])
    return (matched, quality, soft, exact, tail, leftover)


def find_sessions(query, rows, mode: str = "", pick: bool = False):
    """The session a name said in plain speech means → ``(hit, candidates)``.

    Every word said (but "the", "my", "session", "one" …) must name a word
    of the session's name or title, across hyphens, underscores and digits
    ("sitecheck" ↔ sitecheck-bot7, "bot seven" ↔ bot7, "login fix" ↔
    login-fix), a plural or one wrong letter allowed. One session's whole
    name said wins; then the best words (whole over a start). Several left
    is a tie — the caller asks which. ``mode``: ``""`` also takes, when
    nothing matches every word, the unique row matching most of them (at
    least half); ``"strict"`` only a whole name or its start, word for
    word. ``pick``: choosing among candidates already read out, so a tie
    goes to the one the words END ("bot seven" over bot7-copy-2). Among
    equals, a name the words cover WHOLE wins over one with words left
    ("sightcheck bot seven" is sitecheck-bot7, not its copy-2 as well) —
    "bot seven" covers neither whole and is asked about."""
    listed = _listed(rows)
    for said in _said_variants(query):
        if not [w for w in said if w not in _SOFT]:
            continue
        scored = []
        partial = []
        for r in listed:
            best = None
            for label in {_name(r), str(r.get("title") or "")}:
                got = _label_score(said, _toks(label), mode == "strict")
                if got is not None and (best is None or got > best):
                    best = got
            core = [w for w in said if w not in _SOFT]
            if best is not None and best[0] == len(core):
                scored.append((best, r))
            elif best is None and mode == "":
                n = max(
                    (
                        sum(1 for w in core if any(_word_hit(w, t) for t in _toks(lb)))
                        for lb in (_name(r), str(r.get("title") or ""))
                    ),
                    default=0,
                )
                if n and n * 2 >= len(core):
                    partial.append((n, r))
        if not scored and partial:
            top = max(n for n, _ in partial)
            tied = [r for n, r in partial if n == top]
            return (tied[0], []) if len(tied) == 1 else (None, tied)
        if not scored:
            continue
        exact = [r for s, r in scored if s[3]]
        if len(exact) == 1:
            return exact[0], []
        for key in (lambda s: s[1], lambda s: s[2]):  # whole words, then "bot" said
            top = max(key(s) for s, _ in scored)
            scored = [(s, r) for s, r in scored if key(s) == top]
        whole = [(s, r) for s, r in scored if s[5] == 0]
        if len(whole) == 1 and len(scored) > 1:
            return whole[0][1], []
        if pick and len(scored) > 1:
            scored = [(s, r) for s, r in scored if s[4]] or scored
        if len(scored) == 1:
            return scored[0][1], []
        return None, [r for _, r in scored]
    return None, []


def _row(rows, title) -> Optional[dict]:
    if not title:
        return None
    return next((r for r in _listed(rows) if r.get("title") == title), None)


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def session_state(row: dict, now: Optional[float] = None) -> dict:
    """mobile.js ``sessionState`` → ``{"state", "label", "reason", "in_bell"}``.

    ``in_bell`` is the rail bell's attention set — a question, a failed
    setup or pre-commit, failing checks, a gone terminal, or idle with
    unfinished work for :data:`STUCK_IDLE_S` — the one rule the phone's "what
    needs me" reads too."""
    now = time.time() if now is None else now

    def st(state, label, reason="", in_bell=False):
        return {"state": state, "label": label, "reason": reason, "in_bell": in_bell}

    status = str(row.get("status") or "")
    setup = row.get("setup") if isinstance(row.get("setup"), dict) else {}
    if row.get("workspace_missing"):
        return st("offline", "Missing")
    if status == "paused":
        return st("idle", "Paused")
    if row.get("pending") or status == "loading":
        return st("starting", "Starting")
    if row.get("sleeping"):
        return st("idle", "Sleeping")
    if setup.get("state") == "running":
        return st("starting", "Starting")
    if setup.get("state") == "failed":
        return st("needs_you", "Setup failed", "setup_failed", True)
    act = row.get("activity") or "idle"
    if act == "clarify" or (act == "idle" and _prose_question(row)):
        return st("needs_you", "Question", "question", True)
    if act == "working":
        return st("working", "Working")
    if act == "limit":
        q = row.get("queue") if isinstance(row.get("queue"), dict) else {}
        until = _num(q.get("limited_until"))
        label = "Waiting on limit"
        if until > now:
            label += ", resumes " + time.strftime("%H:%M", time.localtime(until))
        return st("limit", label)
    if act == "offline":
        return st("offline", "Offline", "", True)
    if row.get("stage") == "interrupt":
        return st("needs_you", "Pre-commit failed", "precommit_failed", True)
    check = row.get("check") if isinstance(row.get("check"), dict) else {}
    if check.get("state") == "failed" and not check.get("stale"):
        return st("needs_you", "Checks failing", "checks_failing", True)
    since = _num(row.get("activity_since"))
    idle_for = now - since if since > 0 else 0
    ds = row.get("diff_stat") if isinstance(row.get("diff_stat"), dict) else {}
    un = ds.get("uncommitted") if isinstance(ds.get("uncommitted"), dict) else {}
    dirty = _num(un.get("additions")) + _num(un.get("deletions")) > 0
    if idle_for > STUCK_IDLE_S and dirty:
        return st("idle", "Idle", "stuck", True)
    return st("idle", "Idle")


def _prose_question(row: dict) -> Optional[str]:
    """The question ``row``'s session asked in prose (its ``asking`` of kind
    ``question``, core.asking), or None."""
    asking = row.get("asking") if isinstance(row.get("asking"), dict) else {}
    if asking.get("kind") != "question":
        return None
    return str(asking.get("text") or "") or None


def asking_text(row: dict) -> Optional[str]:
    """What ``row``'s session is asking, as a row says it: its ``asking``
    text (a dialog's question line or a prose question), else None."""
    asking = row.get("asking") if isinstance(row.get("asking"), dict) else {}
    return str(asking.get("text") or "") or None


def _attn_rank(row: dict, now: float) -> int:
    """mobile.js ``attnRank`` for a bell row: a question first, then whatever
    is broken, then failing checks."""
    reason = session_state(row, now)["reason"]
    return 0 if reason == "question" else 2 if reason == "checks_failing" else 1


def _state_word(ss: dict) -> str:
    """mobile.js ``voiceStateWord``: the state as a voice says it."""
    return {
        "question": "waiting on a question",
        "precommit_failed": "stuck on a failed pre-commit",
        "checks_failing": "waiting on failing checks",
        "setup_failed": "stuck on a failed setup",
    }.get(ss["reason"], ss["label"].lower())


def _names(rows: List[dict], n: int = MAX_NAMES) -> str:
    return ", ".join(_name(r) for r in rows[:n])


def _or(names: List[str]) -> str:
    """``a``, ``a or b``, ``a, b or c``."""
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


def _ambiguous(cands: List[dict], lead: str = "", then: Optional[dict] = None) -> dict:
    """ "Which one: a or b?" — and the candidates, as the ``pending`` choice
    the client sends back with the next phrase (:func:`pick`). ``then``:
    what the picked session is for when it is not a switch — ``{"kind":
    "interrupt"}``, ``{"kind": "answer", "choice", "number"?, "words"}``,
    ``{"kind": "question"}``, ``{"kind": "read"}`` or ``{"kind":
    "status"}`` — carried in ``pending`` so "the first one" does THAT to
    it."""
    picks = [{"title": r["title"], "name": _name(r)} for r in cands[:MAX_NAMES]]
    pending = {"kind": "pick", "candidates": picks}
    if then:
        pending["then"] = dict(then)
    return {
        "kind": "ambiguous",
        "say": "%sWhich one: %s?" % (lead, _or([p["name"] for p in picks])),
        "candidates": picks,
        "pending": pending,
    }


#: How close (difflib ratio, 0..1) a said name must SOUND to one session's
#: — and no other's — for a switch to take it ("docks" for docs).
NEAR_RATIO = 0.75


def _near_row(arg: str, rows) -> Optional[dict]:
    """The one session whose name the said ``arg`` is a near miss of — the
    words around a name dropped, spelt together ("docks" ↔ docs, "site
    check bot seven" ↔ sitecheck-bot7) at least :data:`NEAR_RATIO` alike,
    and clearly closer than any other — else None."""
    from difflib import SequenceMatcher

    said = "".join(_said_variants(arg)[-1])
    if len(said) < 3:
        return None
    scored = []
    for r in _listed(rows):
        best = 0.0
        for label in {_name(r), str(r.get("title") or "")}:
            name = "".join(_toks(label))
            if name:
                best = max(best, SequenceMatcher(None, said, name).ratio())
        scored.append((best, r))
    scored.sort(key=lambda x: -x[0])
    if not scored or scored[0][0] < NEAR_RATIO:
        return None
    if len(scored) > 1 and scored[1][0] >= scored[0][0] - 0.1:
        return None
    return scored[0][1]


def _near(kind: str, arg: str, why: str, rows=(), suggest: Optional[dict] = None):
    """A ``prompt`` that says what command it nearly was (the client
    captions it; the words are typed): ``{"kind": "prompt", "near":
    {"kind", "why", "arg"?, "suggest"?}}`` — ``why`` ``no_session`` (no
    session called ``arg``; ``suggest``: the one whose name it sounds like),
    ``no_dialog`` (an answer with nothing asking), ``no_focus`` (no session
    open). "More" with nothing to repeat stays a bare prompt: "more", "again"
    are as likely words for the agent."""
    near = {"kind": kind, "why": why}
    if arg:
        near["arg"] = arg
    if suggest is None and why == "no_session" and arg:
        suggest = _near_row(arg, rows)
    if suggest is not None:
        near["suggest"] = _name(suggest)
    return {"kind": "prompt", "near": near}


def _unmatched(arg: str, rows, cands, then: Optional[dict] = None) -> dict:
    """The ``ambiguous`` / ``no-session`` answer for a name that picked no
    single session (``then``: as :func:`_ambiguous`'s)."""
    if cands:
        return _ambiguous(cands, then=then)
    everyone = _listed(rows)
    if not everyone:
        return {"kind": "no-session", "say": "There are no sessions."}
    return {
        "kind": "no-session",
        "say": "No session called %s. Sessions: %s." % (arg, _names(everyone)),
    }


def fresh_reply(row: dict, now: float) -> Optional[str]:
    """``row``'s last reply (its Say line) when it is NEWS: the turn that
    wrote it ended at most :data:`FRESH_REPLY_S` ago — an idle row's
    ``activity_since`` is when its turn ended. Else None."""
    said = str(row.get("last_say") or "").strip()
    since = _num(row.get("activity_since"))
    if not said or row.get("activity") != "idle" or since <= 0:
        return None
    return said if 0 <= now - since <= FRESH_REPLY_S else None


def switch_line(row: dict, now: float) -> str:
    """What a switch says: the session's name, and the last reply only when
    it is news (:func:`fresh_reply`) — a session done a while ago is not
    read out again ("what did it say" reads it). It used to add the state
    word and the task headline ("Now on hackathon-2, idle: explain how to
    add …"); the owner heard that as "the whole thing" on every switch and
    asked for the name alone (2026-10-28). The state is on the screen, and
    "what is it doing" / "what did it say" ask for the rest."""
    say = _end(_name(row))
    news = fresh_reply(row, now)
    if news:
        say += " " + _end(strip_markdown(news))
    return say


def _switched(hit: dict, now: Optional[float] = None) -> dict:
    now = time.time() if now is None else now
    return {
        "kind": "switch",
        "title": hit["title"],
        "name": _name(hit),
        "say": switch_line(hit, now),
    }


def _switch(
    arg: str, rows, now: Optional[float] = None, mode: str = "", quiet: bool = False
) -> dict:
    """``mode``: :func:`_switch_command`'s — "must" / "strict" without a
    match is a prompt, with ``near`` (:func:`_near`) when the verb was
    plainly a switch (not ``strict``, not ``quiet``: "switch the …") or the
    name sounds like one session's. A name that matches nothing but SOUNDS
    like exactly one session (:func:`_near_row`: "switch to docks") is that
    session, said with a switch verb ("switch to", "go to", "take me to");
    after a soft verb ("open docks") or "switch the" it is only suggested."""
    hit, cands = find_sessions(
        arg, rows, "strict" if mode == "strict" else "" if not mode else "must"
    )
    if hit is None and not cands:
        near = _near_row(arg, rows)
        if near is not None and mode != "strict" and not quiet:
            return _switched(near, now)
        if mode:
            # A miss after a polite lead or a soft verb is most likely words
            # for the agent: ``near`` only for a short name, or one that
            # sounds like a session's.
            if near is None and (
                mode == "strict" or quiet or len(norm(arg).split()) > 3
            ):
                return {"kind": "prompt"}
            return _near("switch", arg, "no_session", rows, near)
    if hit is None:
        if cands:
            return _ambiguous(cands)
        return _unmatched(arg, rows, cands)
    return _switched(hit, now)


def pick(text, pending, rows, now: Optional[float] = None) -> Optional[dict]:
    """The next phrase after "Which one: a or b?" (``pending``: ``{"kind":
    "pick", "candidates": [{title, name}]}``, sent back by the client) →
    the ``switch`` it picks, ``ambiguous`` again (narrowed, or nothing
    matched a state), or None when the phrase is no pick — it is routed as
    usual and the choice is dropped.

    A pick: "the first one", "second", "number two", "the last one"
    (in the order read out); "the one that's working", "the idle one", "the
    one asking"; or a name fragment of one candidate ("copy two", "bot
    seven"), leads like "I mean" or "go to" dropped."""
    now = time.time() if now is None else now
    titles = [
        str(c.get("title") or "")
        for c in (pending or {}).get("candidates") or ()
        if isinstance(c, dict)
    ]
    cands = [r for r in (_row(rows, t) for t in titles) if r is not None]
    if not cands:
        return None
    then = (pending or {}).get("then")
    then = then if isinstance(then, dict) and then.get("kind") else None

    def picked(row):
        return _picked(row, then, rows, now)

    def again(cs, lead=""):
        return _ambiguous(cs, lead, then)

    t = _PICK_LEAD_RE.sub("", _Words(text).text)
    if not t:
        return None
    m = _PICK_NUM_RE.match(t)
    if m:
        n = len(cands) if m.group(3) else _count(m.group(1) or m.group(2))
        if n and 1 <= n <= len(cands):
            return picked(cands[n - 1])
        return again(cands, "There are %d. " % len(cands))
    m = _PICK_STATE_RE.match(t)
    if m:
        want = _PICK_STATES.get(m.group(1))
        got = []
        for r in cands:
            ss = session_state(r, now)
            if want == "working" and ss["state"] == "working":
                got.append(r)
            elif want == "idle" and ss["state"] == "idle":
                got.append(r)
            elif want == "offline" and ss["state"] == "offline":
                got.append(r)
            elif want is None and ss["reason"] == "question":
                got.append(r)
        if len(got) == 1:
            return picked(got[0])
        if got:
            return again(got)
        said = "has a question" if m.group(1).startswith("with") else "is " + m.group(1)
        return again(cands, "None of them %s. " % said)
    hit, more = find_sessions(t, cands, "must", pick=True)
    if hit is not None:
        return picked(hit)
    if more:
        return again(more)
    return None


#: The ``then`` kinds :func:`_picked` carries out (a switch otherwise).
_THEN_KINDS = ("interrupt", "answer", "question", "read", "status")


def _picked(row: dict, then: Optional[dict], rows, now: float) -> dict:
    """What a pick does to the session picked: a switch, or the ``then`` the
    question was asked for (:func:`_ambiguous`) — ``interrupt`` (the route
    presses Escape), ``answer`` / ``question`` (the route finishes them with
    the dialog, :func:`finish`), ``read`` / ``status`` (said here)."""
    kind = (then or {}).get("kind")
    if kind not in _THEN_KINDS:
        return _switched(row, now)
    if kind == "read":
        return _read({"target": None}, row["title"], rows, now, "")
    if kind == "status":
        return _status(row["title"], rows, now)
    got = {"kind": kind, "title": row["title"], "name": _name(row)}
    if kind == "answer":
        choice = str(then.get("choice") or "")
        if choice not in ("yes", "no", "always", "number"):
            return _switched(row, now)
        got["choice"] = choice
        if choice == "number":
            got["number"] = then.get("number")
        got["words"] = str(then.get("words") or choice)
    return got


def _strict_match(query: str, rows) -> Tuple[Optional[dict], List[dict]]:
    """A name said without a verb ("docs, yes"): only a session's whole name
    or title, or its start in whole words ("invoice" for invoice-parser), a
    leading "the"/"my" ignored — never :func:`match_session`'s word overlap,
    so "update the docs, yes" is no answer to ``docs``."""
    q = norm(query)
    for lead in ("the ", "my "):
        if q.startswith(lead):
            q = q[len(lead) :]
    if not q:
        return None, []
    cands = []
    for r in _listed(rows):
        for said in (norm(_name(r)), norm(r.get("title"))):
            if said == q or said.startswith(q + " "):
                cands.append(r)
                break
    exact = [r for r in cands if q in (norm(_name(r)), norm(r.get("title")))]
    if len(exact) == 1:
        return exact[0], []
    if len(cands) == 1:
        return cands[0], []
    return None, cands


def _resolve(tgt, session: str, rows, title: str, then: Optional[dict] = None):
    """An answer's or a question's target → ``(row, None)``, ``(None,
    {"index": N})`` (the client maps it), or ``(None, final answer)``.
    ``tgt`` None is the session being talked to; ``title`` is the client's
    own mapping of an index (or of next/previous) to a title. ``then``: what
    the target is wanted for (:func:`_ambiguous`), so a "Which one" asked
    here carries it; a read, status or question whose name matched nothing
    is a prompt with ``near`` (an answer's stays a bare prompt: "update the
    docs, yes" is words)."""
    kind = (then or {}).get("kind", "")
    if tgt is None or tgt.get("self"):
        row = _row(rows, session)
        if row is not None:
            return row, None
        return None, (
            {"kind": "prompt"}
            if kind == "answer" or not kind
            else _near(kind, "", "no_focus")
        )
    if "index" in tgt:
        if not title:
            return None, {"index": tgt["index"]}
        row = _row(rows, title)
        if row is None:
            return None, {"kind": "no-session", "say": "%s is not listed." % title}
        return row, None
    said = tgt["name"]
    if tgt.get("strict"):
        hit, cands = _strict_match(said, rows)
        if hit is None and not cands:
            # "What did narnia say": ``near``; "what did the tests say" is
            # words for the agent, unless it sounds like a session's name.
            near = _near_row(said, rows)
            if kind in ("read", "status", "question") and (
                near is not None or said.split()[0] not in _ARTICLES
            ):
                return None, _near(kind, said, "no_session", rows, near)
            return None, {"kind": "prompt"}
    else:
        hit, cands = match_session(said, rows)
    if hit is None:
        return None, _unmatched(said, rows, cands, then)
    return hit, None


def _list(rows, now: float) -> dict:
    listed = _listed(rows)
    if not listed:
        return {"kind": "list", "say": "There are no sessions."}
    parts = []
    for r in listed[:MAX_LIST]:
        ss = session_state(r, now)
        word = "needs you" if ss["reason"] == "question" else _state_word(ss)
        parts.append("%s (%s)" % (_name(r), word))
    more = len(listed) - MAX_LIST
    tail = ", and %d more" % more if more > 0 else ""
    return {"kind": "list", "say": "Sessions: %s%s." % (", ".join(parts), tail)}


def _needs(rows, now: float) -> dict:
    bell = [r for r in _listed(rows) if session_state(r, now)["in_bell"]]
    bell.sort(key=lambda r: _attn_rank(r, now))
    lines = []
    for r in bell[:MAX_NEEDS]:
        ss = session_state(r, now)
        if ss["reason"] == "question":
            said = asking_text(r) or r.get("last_say") or "a question"
            lines.append(_end("%s is asking: %s" % (_name(r), strip_markdown(said))))
        else:
            lines.append(_end("%s is %s" % (_name(r), _state_word(ss))))
    return {"kind": "needs", "say": " ".join(lines) if lines else "Nothing needs you."}


def _status(session: str, rows, now: float) -> dict:
    if not session:
        return {"kind": "status", "say": "No session is open."}
    row = _row(rows, session)
    if row is None:
        return {"kind": "status", "say": "%s is not listed." % session}
    ss = session_state(row, now)
    since = _num(row.get("activity_since"))
    mins = round((now - since) / 60) if since else 0
    dur = ""
    if row.get("activity") == "working" and mins >= 1:
        dur = " for %d minute%s" % (mins, "" if mins == 1 else "s")
    say = "%s is %s%s." % (_name(row), _state_word(ss), dur)
    if row.get("last_turn"):
        say += " " + _end(strip_markdown(row["last_turn"]))
    return {"kind": "status", "say": say}


def status_say(session: str, rows, now: Optional[float] = None) -> str:
    """What "what's <session> doing" says (the ``status`` answer's ``say``);
    ``session`` empty → what "what needs me" says. For the assistant
    (``core.voice_assistant``'s ``status`` action)."""
    now = time.time() if now is None else now
    if not session:
        return _needs(rows, now)["say"]
    return _status(session, rows, now)["say"]


def list_say(rows, now: Optional[float] = None) -> str:
    """What "list sessions" says."""
    return _list(rows, time.time() if now is None else now)["say"]


def _read(cmd: dict, session: str, rows, now: float, title: str) -> dict:
    """ "What did it say?": the session's last reply (its Say line), read
    whatever its age."""
    tgt = cmd.get("target")
    if tgt is not None and "index" in tgt and not title:
        return {"kind": "read", "say": "Say its name: what did <name> say."}
    row, out = _resolve(tgt, session, rows, title, {"kind": "read"})
    if row is None:
        if tgt is None:
            return {"kind": "read", "say": "No session is open."}
        return out
    name = _name(row)
    said = strip_markdown(str(row.get("last_say") or "")).strip()
    if not said:
        say = "%s hasn't said anything yet." % name
    elif row.get("activity") == "working":
        say = "%s is still working. Before that it said: %s" % (name, _end(said))
    else:
        say = "%s said: %s" % (name, _end(said))
    return {"kind": "read", "title": row["title"], "name": name, "say": say}


def _then_of(cmd: dict) -> dict:
    """What an ``answer`` / ``question`` command does once its session is
    picked (:func:`_ambiguous`'s ``then``)."""
    if cmd["kind"] != "answer":
        return {"kind": cmd["kind"]}
    then = {"kind": "answer", "choice": cmd["choice"], "words": cmd["words"]}
    if cmd["choice"] == "number":
        then["number"] = cmd["number"]
    return then


def _answer_or_question(cmd: dict, session: str, rows, title: str) -> dict:
    row, out = _resolve(cmd.get("target"), session, rows, title, _then_of(cmd))
    if row is None:
        if cmd["kind"] == "question" and cmd.get("target") is None:
            return {"kind": "question", "say": "No session is open."}
        if "kind" in out:
            return out
        if cmd["kind"] == "question":
            return {"kind": "question", "index": out["index"]}
        got = {"kind": "answer", "choice": cmd["choice"]}
        if cmd["choice"] == "number":
            got["number"] = cmd["number"]
        got.update(index=out["index"], words=cmd["words"])
        return got
    got = {"kind": cmd["kind"], "title": row["title"], "name": _name(row)}
    if cmd["kind"] == "answer":
        got["choice"] = cmd["choice"]
        if cmd["choice"] == "number":
            got["number"] = cmd["number"]
        got["words"] = cmd["words"]
        tgt = cmd.get("target")
        if tgt is None or tgt.get("self"):
            got["bare"] = True
    return got


def answer(
    text,
    session: str,
    rows,
    now: Optional[float] = None,
    title: str = "",
    words: Optional[dict] = None,
    pending: Optional[dict] = None,
) -> dict:
    """The answer to ``text`` said to ``session`` (a title, ``""`` for none).

    ``title``: the client's mapping of the phrase's numbered target ("session
    three, yes") or next/previous to one of its titles, sent back — the
    numbering is the client's (its rail or list order), so without it those
    answers carry ``index`` / ``delta`` and no ``say``. ``words``: as
    :func:`command`'s.

    ``new`` comes back as :func:`command` parsed it — the route resolves it
    with :func:`plan_new` and creates the session; ``answer`` and
    ``question`` that name a session come back for :func:`finish` (the route
    reads that session's dialog). Every other kind is final.

    ``pending``: the choice the last answer asked for and the client sent
    back (``{"kind": "pick", "candidates"}``, :func:`pick`) — a phrase that
    picks answers it; any other is routed as usual."""
    now = time.time() if now is None else now
    if isinstance(pending, dict) and pending.get("kind") == "pick":
        got = pick(text, pending, rows, now)
        if got is not None:
            return got
    cmd = command(text, words)
    if cmd is None:
        return {"kind": "prompt"}
    kind = cmd["kind"]
    if kind in LOCAL_KINDS:
        return {"kind": kind}
    if kind == "new":
        return cmd
    if kind == "switch":
        return _switch(
            cmd["arg"], rows, now, cmd.get("mode", ""), bool(cmd.get("quiet"))
        )
    if kind == "read":
        return _read(cmd, session, rows, now, title)
    if kind == "pick":
        cur = _row(rows, session)
        if cur is not None and cur.get("activity") == "clarify":
            cmd = {
                "kind": "answer",
                "choice": "number",
                "number": cmd["index"],
                "words": cmd["words"],
                "target": None,
            }
        else:
            cmd = {"kind": "switch_index", "index": cmd["index"]}
        kind = cmd["kind"]
    if kind in ("switch_index", "switch_rel"):
        if not title:
            return dict(cmd)
        row = _row(rows, title)
        if row is None:
            return {"kind": "no-session", "say": "%s is not listed." % title}
        return _switched(row, now)
    if kind in ("answer", "question"):
        return _answer_or_question(cmd, session, rows, title)
    if kind == "help":
        return {"kind": "help", "text": cmd["text"]}
    if kind == "assistant":
        return {"kind": "assistant", "text": cmd["text"]}
    if kind == "list":
        return _list(rows, now)
    if kind == "needs":
        return _needs(rows, now)
    if kind == "scroll":
        return dict(cmd, title=title or session)
    if kind == "scroll_more":
        return {"kind": "prompt"}  # nothing to repeat: the route remembers
    if kind == "interrupt":
        return _interrupt(cmd, session, rows, title)
    if kind == "commands":
        return commands_answer(words)
    if cmd.get("target"):
        row, out = _resolve(cmd["target"], session, rows, title, {"kind": "status"})
        if row is None:
            return out
        return _status(row["title"], rows, now)
    return _status(session, rows, now)


def _interrupt(cmd: dict, session: str, rows, title: str) -> dict:
    """``{"kind": "interrupt", "title"}`` — the session to press Escape in
    (the route presses it): the named one, else the one the client names
    (``title``), else the one talked to. A name that matches no session is
    a prompt (with ``near`` when "session" was said or the name sounds like
    one); several are asked about, the ``pending`` pick interrupting the one
    picked."""
    said = cmd.get("name")
    if not said:
        return {"kind": "interrupt", "title": title or session}
    mode = cmd.get("mode") or "strict"
    hit, cands = find_sessions(said, rows, mode)
    if hit is None and not cands:
        near = _near_row(said, rows)
        if near is None and mode == "strict":
            return {"kind": "prompt"}  # "interrupt handler is broken"
        return _near("interrupt", said, "no_session", rows, near)
    if hit is None:
        return _ambiguous(cands, then={"kind": "interrupt"})
    return {"kind": "interrupt", "title": hit["title"], "name": _name(hit)}


# --- answering dialogs -----------------------------------------------------------
def parse_answer(text, options) -> Optional[dict]:
    """mobile.js ``voiceParseAnswer``: the option ``text`` picks, or None.

    Permission dialogs (any yes/no/always option) take WORDS only: the
    whole-answer always forms first (an always without an always option is
    a yes), then a no anywhere — whatever else was said — then a yes.
    Numbers never pick a permission (recognisers turn "too"/"to" into "2",
    and "2" is the standing-permission option of Claude's dialog). Dialogs
    made only of ``other`` options take a number, matched to the option's
    own key."""
    t = norm(text)
    opts = [o for o in options or () if isinstance(o, dict)]

    def by_kind(k):
        return next((o for o in opts if o.get("kind") == k), None)

    if any(o.get("kind") in ("yes", "no", "always") for o in opts):
        if _ALWAYS_RE.match(t):
            return by_kind("always") or by_kind("yes")
        if _NO_RE.search(t):
            return by_kind("no")
        if _YES_RE.search(t):
            return by_kind("yes")
        return None
    m = _PNUM_RE.search(t)
    if m:
        key = str(_PNUM[m.group(1)])
        return next((o for o in opts if str(o.get("key")) == key), None)
    return None


# --- Claude's question tool (``/dialog``'s ``kind: "select"``) -------------------
#: The keys that switch tabs / press the cursor's row, when a body has none.
_SELECT_NAV = {"next": ["Right"], "prev": ["Left"], "submit": ["Enter"]}
_SEL_NEXT_RE = re.compile(r"^(?:next|next question|next one|go on|skip|skip it)$")
_SEL_BACK_RE = re.compile(
    r"^(?:back|go back|previous|previous question|prev|last question)$"
)
_SEL_SUBMIT_RE = re.compile(
    r"^(?:submit|submit (?:it|them|that|answers|my answers|the answers)"
    r"|send (?:it|them)|done|finish|finished)$"
)
_SEL_TEXT_RE = re.compile(
    r"^(?:type something|other|something else|write in|write my own|my own answer"
    r"|type my own|type an answer)$"
)
#: What leads a pick ("pick two", "choose red and blue") — dropped.
_SEL_LEAD = frozenset(
    "pick choose select take tick check option options number numbers the".split()
)
#: What leads an UN-pick in a multi-select ("untick cheese").
_SEL_UNDO = frozenset("untick uncheck unselect remove drop deselect".split())
#: Words that only join picks ("one and three", "red then blue").
_SEL_JOIN = frozenset("and plus also then".split())


def is_select(dialog) -> bool:
    """Whether ``dialog`` (a ``/dialog`` body) is Claude's question tool."""
    return _parsed(dialog) and dialog.get("kind") == "select"


def _select_opts(dialog: dict) -> List[dict]:
    return [o for o in dialog.get("options") or () if isinstance(o, dict)]


def _opt_keys(opt: dict) -> List[str]:
    return [str(k) for k in (opt.get("keys") or [opt.get("key")])]


def _said_label(opt: dict) -> str:
    return strip_markdown(opt.get("label") or "").rstrip(".") or "that"


def _select_picks(words: List[str], opts: List[dict]) -> Optional[List[dict]]:
    """The choices ``words`` pick, in order, or None when any word is
    neither a pick nor a joiner: a label (the longest run of words that is
    one — "salt and pepper" stays whole — else the unique label those words
    begin, "plain" for "Plain HTML"), a number ("two", "3"), an ordinal
    ("the second one"); "pick", "option", "and", "the"… only join them."""
    labels = [(norm(o.get("label")), o) for o in opts]

    def by_key(n) -> Optional[dict]:
        return next((o for o in opts if str(o.get("key")) == str(n)), None)

    picks: List[dict] = []
    i, n = 0, len(words)
    while i < n:
        w = words[i]
        hit = None
        for j in range(n, i, -1):
            said = " ".join(words[i:j])
            exact = [o for lab, o in labels if lab == said]
            if exact:
                hit = (exact[0], j)
                break
        if hit is None:
            for j in range(n, i, -1):
                said = " ".join(words[i:j])
                starts = [o for lab, o in labels if lab.startswith(said + " ")]
                if len(said) >= 3 and len(starts) == 1:
                    hit = (starts[0], j)
                    break
        if hit is None and _ordinal(w) and i + 1 < n and words[i + 1] in _PICK_NOUNS:
            hit = (by_key(_ordinal(w)), i + 2)
        if hit is None and _count(w) is not None:
            hit = (by_key(_count(w)), i + 1)
        if hit is None:
            if w in _SEL_LEAD or w in _SEL_JOIN:
                i += 1
                continue
            return None
        if hit[0] is None:
            return None  # a number past the last choice
        picks.append(hit[0])
        i = hit[1]
    return picks


def select_answer(text, dialog) -> Optional[dict]:
    """A phrase said to Claude's question tool → ``{"keys", "said"}`` (and
    ``"text"`` — typed before the keys — for a free-text answer), or None.

    * The review screen (``review`` in the body): "submit" / a yes → Submit
      answers, "cancel" / a no → Cancel, "back" → the previous tab.
    * A question: "next" / "back" switch tabs (``nav``); "submit" / "done"
      goes on to the review tab (Right up to it) — never Enter, which would
      pick whatever row the cursor is on. "type something" / "other" →
      the free-text row. Numbers and labels pick: one for a single-select
      ("two", "red", "option two"); any number for a multi-select ("one and
      three", "cheese and mushrooms") — boxes already ticked are left alone,
      "untick cheese" clears one. A bare "yes" is no pick. With the cursor
      on the free-text row, any other phrase is the answer typed into it,
      then Enter."""
    if not isinstance(dialog, dict):
        return None
    t = norm(text)
    if len(t.split()) > 1 and t.split()[-1] == "please":
        t = t.rsplit(" ", 1)[0]
    if not t:
        return None
    opts = _select_opts(dialog)
    nav = dialog.get("nav") or _SELECT_NAV
    steps = [s for s in dialog.get("steps") or () if isinstance(s, dict)]
    step = dialog.get("step") if isinstance(dialog.get("step"), int) else 0
    if "review" in dialog:
        if _SEL_BACK_RE.match(t) and step > 0:
            return {"keys": list(nav["prev"]), "said": "back"}
        if _NO_RE.search(t):
            opt = next((o for o in opts if o.get("kind") == "no"), None)
        elif _SEL_SUBMIT_RE.match(t) or _YES_RE.search(t):
            opt = next((o for o in opts if o.get("kind") == "yes"), None)
        else:
            picks = _select_picks(t.split(), opts) or []
            opt = picks[0] if len(picks) == 1 else None
        if opt is None:
            return None
        return {"keys": _opt_keys(opt), "said": _said_label(opt)}
    if _SEL_NEXT_RE.match(t):
        if step + 1 < len(steps):
            return {"keys": list(nav["next"]), "said": "next"}
        return None
    if _SEL_BACK_RE.match(t):
        return {"keys": list(nav["prev"]), "said": "back"} if step > 0 else None
    if _SEL_SUBMIT_RE.match(t):
        review = next((k for k, s in enumerate(steps) if s.get("submit")), None)
        if review is None or review <= step:
            return None
        return {"keys": list(nav["next"]) * (review - step), "said": "submit"}
    text_opt = next((o for o in opts if o.get("kind") == "text"), None)
    if text_opt is not None and _SEL_TEXT_RE.match(t):
        return {"keys": _opt_keys(text_opt), "said": "type something"}
    words = t.split()
    undo = bool(dialog.get("multi")) and bool(words) and words[0] in _SEL_UNDO
    picks = _select_picks(words[1:] if undo else words, opts)
    if picks:
        if not dialog.get("multi"):
            if len(picks) != 1:
                return None
            return {"keys": _opt_keys(picks[0]), "said": _said_label(picks[0])}
        keys: List[str] = []
        said: List[str] = []
        seen = set()
        for opt in picks:
            if str(opt.get("key")) in seen:
                continue
            seen.add(str(opt.get("key")))
            if opt.get("checked") is (not undo) and opt.get("kind") == "other":
                continue  # already as asked
            for k in _opt_keys(opt):
                if k != "Up" or not keys:
                    keys.append(k)
            said.append(_said_label(opt))
        if not keys:
            return {"keys": [], "said": "already " + ("cleared" if undo else "ticked")}
        return {"keys": keys, "said": " and ".join(said)}
    if (
        text_opt is not None
        and str(dialog.get("cursor")) == str(text_opt.get("key"))
        and not _ANSWERED_WORDS_RE.match(t)
    ):
        return {
            "keys": ["Enter"],
            "text": str(text or "").strip(),
            "said": str(text or "").strip(),
        }
    return None


#: Phrases that are never a typed free-text answer.
_ANSWERED_WORDS_RE = re.compile(
    r"^(?:yes|yeah|yep|no|nope|cancel|stop|never ?mind|ok|okay)$"
)


def select_choices(dialog: dict) -> str:
    """How to answer Claude's question tool, as said."""
    opts = _select_opts(dialog)
    if "review" in dialog:
        answered = [
            "%s %s"
            % (
                _end(strip_markdown(r.get("question") or "")),
                _end(strip_markdown(r.get("answer") or "")),
            )
            for r in dialog.get("review") or ()
            if isinstance(r, dict)
        ]
        return " ".join(answered + ["Say submit or cancel."])
    names = ", ".join(_said_label(o) for o in opts if o.get("kind") == "other")
    if dialog.get("multi"):
        ticked = [_said_label(o) for o in opts if o.get("checked") is True]
        out = "Options: %s — pick as many as you like, then say next." % names
        if ticked:
            out += " Ticked: %s." % ", ".join(ticked)
        return out
    return "Options: %s — say a number or the option." % names


def select_speech(name: str, dialog: dict) -> str:
    """``<name> asks: <question> <choices>`` for Claude's question tool."""
    question = strip_markdown(dialog.get("question") or "") or "a question"
    return "%s asks: %s %s" % (name, _end(question), select_choices(dialog))


def _kind_word(opt: dict) -> str:
    """mobile.js ``voiceKindWord``."""
    k = opt.get("kind")
    if k in ("yes", "no", "always"):
        return k
    return strip_markdown(opt.get("label") or "") or "that"


def dialog_choices(dialog: dict) -> str:
    """mobile.js ``voiceDialogChoices``: how to answer, as said."""
    opts = [o for o in dialog.get("options") or () if isinstance(o, dict)]
    kinds = {o.get("kind") for o in opts}
    if "yes" in kinds or "no" in kinds:
        return "Say yes, no, or always." if "always" in kinds else "Say yes or no."
    n = min(len(opts), len(_ORD_WORDS))
    names = [
        "%s: %s" % (_ORD_WORDS[i], strip_markdown(opts[i].get("label") or ""))
        for i in range(n)
    ]
    nums = list(_ORD_WORDS[:n])
    if len(nums) == 1:
        said = nums[0]
    elif len(nums) == 2:
        said = "%s or %s" % (nums[0], nums[1])
    else:
        said = ", ".join(nums[:-1]) + ", or " + nums[-1]
    return ". ".join(names) + ". Say " + said + "."


def dialog_speech(name: str, dialog: dict) -> str:
    """mobile.js ``voiceDialogSpeech``: ``<name>: <question> [It wants to
    run: <command>.] <choices>`` — Claude's question tool:
    :func:`select_speech`."""
    if dialog.get("kind") == "select":
        return select_speech(name, dialog)
    parts = [
        "%s: %s"
        % (name, strip_markdown(dialog.get("question") or "") or "a permission prompt")
    ]
    if dialog.get("command"):
        parts.append("It wants to run: %s." % str(dialog["command"])[:200])
    parts.append(dialog_choices(dialog))
    return " ".join(parts)


def _parsed(dialog) -> bool:
    return (
        isinstance(dialog, dict)
        and bool(dialog.get("parsed"))
        and bool(dialog.get("options"))
    )


def finish(ans: dict, dialog, spoken=None, remote: bool = False) -> dict:
    """An ``answer`` / ``question`` from :func:`answer` that names a session,
    finished with that session's live dialog (the ``/dialog`` body, None when
    it is not asking) and, for a question, its spoken record (``{"say",
    "has_question"}``). ``remote``: another device's session, whose dialog
    this server cannot read.

    An answer to a parsed dialog → ``dialog_id`` + ``keys`` (the option
    :func:`parse_answer` picks; ``[]`` and a "didn't catch that" ``say`` when
    none) — never pressed here, the client POSTs ``/answer``. No parsed
    dialog → ``no_dialog`` and why; but a BARE answer ("yes" said to the
    session being talked to) with no parsed dialog is ``{"kind": "prompt"}``
    (``near``: ``{"kind": "answer", "why": "no_dialog"}``): the agent may
    have asked in words, and the words reach it as typed — except "stop it"
    / "stop that", which then is ``interrupt`` (the route presses Escape). A
    question asked in prose (``/dialog``'s ``kind: "question"``) is read out
    like a dialog's; an answer to it that names the session is
    ``no_dialog`` with a ``say`` that it was asked in words — switch to it
    and say the answer, which goes in as a prompt."""
    out = dict(ans)
    bare = out.pop("bare", False)
    name = out.get("name") or out.get("title") or ""
    if out.get("kind") == "question":
        if _parsed(dialog):
            say = dialog_speech(name, dialog)
        elif isinstance(dialog, dict) and dialog.get("question"):
            say = _end("%s: %s" % (name, strip_markdown(dialog["question"])))
        elif (
            isinstance(spoken, dict)
            and spoken.get("has_question")
            and spoken.get("say")
        ):
            say = _end("%s: %s" % (name, strip_markdown(spoken["say"])))
        else:
            say = "%s: Nothing is being asked." % name
        out["say"] = say
        return out
    if not _parsed(dialog):
        if bare:
            if _STOP_IT_RE.match(norm(out.get("words") or "")):
                # "Stop it" with nothing asking: the agent, stopped (Escape —
                # the route presses it), as the desktop gate reads it.
                return {
                    "kind": "interrupt",
                    "title": out.get("title") or "",
                    "name": name,
                }
            return _near("answer", str(out.get("words") or ""), "no_dialog")
        out["no_dialog"] = True
        if remote:
            out["say"] = "%s is on another device: answer it there." % name
        elif isinstance(dialog, dict) and dialog.get("kind") == "question":
            out["say"] = (
                "%s asked in words, not a dialog: switch to it and say your answer."
                % name
            )
        elif isinstance(dialog, dict):
            out["say"] = "%s needs you: open the screen." % name
        else:
            out["say"] = "%s is not asking anything." % name
        return out
    out["dialog_id"] = dialog.get("id")
    if is_select(dialog):
        return _finish_select(out, name, dialog, out.get("words") or "")
    opt = parse_answer(out.get("words") or "", dialog.get("options"))
    if opt is None:
        out["keys"] = []
        choices = dialog_choices(dialog)
        out["say"] = "I didn't catch that for %s: %s" % (
            name,
            "say " + choices.split("Say ", 1)[-1] if "Say " in choices else choices,
        )
        return out
    out["keys"] = [str(opt.get("key"))]
    out["say"] = "Answered %s on %s." % (_kind_word(opt), name)
    return out


def _finish_select(out: dict, name: str, dialog: dict, words: str) -> dict:
    """``out`` (an ``answer``) finished against Claude's question tool:
    :func:`select_answer`'s ``keys`` (and ``text``), or ``[]`` and how to
    answer it."""
    sel = select_answer(words, dialog)
    if sel is None or not sel["keys"]:
        out["keys"] = []
        out["say"] = (
            "%s: %s." % (name, _end(sel["said"]).rstrip("."))
            if sel is not None
            else "I didn't catch that for %s. %s" % (name, select_choices(dialog))
        )
        return out
    out["keys"] = sel["keys"]
    if sel.get("text"):
        out["text"] = sel["text"]
    out["say"] = "Answered %s on %s." % (sel["said"], name)
    return out


def select_route(text, session: str, dialog, rows=()) -> Optional[dict]:
    """A phrase said to ``session`` while its agent shows Claude's question
    tool → the ``answer`` (``dialog_id``, ``keys``, maybe ``text``), or None
    to route it as usual. :func:`command` reads "red", "one and three",
    "next" or "submit" as a prompt (or "go back" as a session switch);
    against a question they are its answer."""
    if not session or not is_select(dialog):
        return None
    if select_answer(text, dialog) is None:
        return None
    row = _row(rows, session) or {"title": session}
    out = {"kind": "answer", "title": session, "name": _name(row), "words": text}
    out["dialog_id"] = dialog.get("id")
    return _finish_select(out, out["name"], dialog, text)


# --- an answer said before its dialog was there ---------------------------------
#: A phrase answers a dialog only when the person BEGAN saying it at least
#: this long after the dialog was there to see. 300 ms is under the fastest
#: human reaction to something appearing on a screen (simple visual
#: reaction ≈ 200–250 ms, then a word's onset): a word that starts sooner
#: was not said to that dialog. A "yes" said in a dictation 300 ms before
#: a permission prompt came up answered it (R8-bis, cycle 2).
ANSWER_AFTER_S = 0.3
#: A session's dialog that comes up after one of its dialogs was answered
#: was there to see no sooner than that answer (``voice_actions.answered_at``)
#: plus this: the network delay the client-measured phrase age carries
#: (``phrase_timing`` reads it at arrival, so it looks that much LATER than
#: it began) against a stamp on this server's own clock.
ANSWERED_MARGIN_S = 0.2
#: A ``spoke_ms_ago`` / ``dialog_seen`` age past this is not believed.
_TIMING_MAX_S = 600.0
#: At most this many sessions' ``dialog_seen`` entries are read.
_SEEN_MAX = 64


def _ago(v, now: float, clamp: bool = False) -> Optional[float]:
    """``now`` minus ``v`` milliseconds, for a sane ``v`` (else None).
    ``clamp``: an age past :data:`_TIMING_MAX_S` counts as that age instead
    of being dropped — for a dialog sighting, where a LATER sighting only
    makes the check stricter (a dialog that waited 30 minutes stays
    answerable). Never for the phrase's own age: there a smaller age would
    loosen it."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        return None
    if v < 0:
        return None
    if v > _TIMING_MAX_S * 1000:
        if not clamp:
            return None
        v = _TIMING_MAX_S * 1000
    return now - float(v) / 1000.0


def phrase_timing(payload, now: Optional[float] = None) -> Tuple[Optional[float], dict]:
    """``(spoke, seen)`` from a ``/api/voice/route`` body, on THIS server's
    clock: ``spoke`` — when the person began saying the phrase
    (``spoke_ms_ago``, measured by the client from its own clock at send
    time, so a skewed client clock does not matter; None when absent or
    insane) — and ``seen`` — ``{title: (dialog_id, epoch)}`` from
    ``dialog_seen: {title: {id, ms_ago}}``, when the client first had each
    session's current dialog. ``spoke_at`` (the client's epoch ms) is
    informational only: never compared with this clock. Both ages ride the
    same request, so the network's delay shifts them alike."""
    now = time.time() if now is None else now
    if not isinstance(payload, dict):
        return None, {}
    spoke = _ago(payload.get("spoke_ms_ago"), now)
    seen: dict = {}
    raw = payload.get("dialog_seen")
    if isinstance(raw, dict):
        for title, v in list(raw.items())[:_SEEN_MAX]:
            if not isinstance(v, dict) or not v.get("id"):
                continue
            at = _ago(v.get("ms_ago"), now, clamp=True)
            if at is not None:
                seen[str(title)] = (str(v["id"]), at)
    return spoke, seen


def dialog_visible_at(
    title: str, dialog_id, seen: dict, row, answered: Optional[float] = None
) -> Optional[float]:
    """When ``title``'s dialog ``dialog_id`` was there to see, as late as the
    evidence allows — or None when the CLIENT never reported having it (an
    answer then waits for one that did). The evidence:

    - the client's own first sighting of that same id (``seen``) — a
      sighting of another id does not count, so a dialog that changed since
      the client saw it is unanswerable until the client has the new one;
    - this server's stamp of the session going to ``clarify`` (the row's
      ``asking.ts`` — core.asking ``dialog_asking``:
      ``agent_state.state_since``);
    - the session's last answer, any path (``answered``, epoch; default
      ``voice_actions.answered_at(title)``) plus :data:`ANSWERED_MARGIN_S`:
      the dialog up now came after it. A client that kept its sighting of
      an identical prompt (same id) asked again after that answer — or that
      missed the clarify gap between them — can no longer make a phrase
      begun before the answer look in time.

    The LATEST of them counts: each is no earlier than the dialog appeared,
    so the latest is the safest bound."""
    mine = (seen or {}).get(title)
    if not mine or mine[0] != str(dialog_id):
        return None
    at = mine[1]
    asked = row.get("asking") if isinstance(row, dict) else None
    if isinstance(asked, dict) and asked.get("kind") == "dialog":
        at = max(at, _num(asked.get("ts")))
    if answered is None:
        try:
            answered = _voice_actions.answered_at(title)
        except Exception:  # noqa: BLE001 — the other evidence still holds
            answered = 0.0
    answered = _num(answered)
    if answered > 0:
        at = max(at, answered + ANSWERED_MARGIN_S)
    return at


def answer_in_time(ans: dict, spoke: Optional[float], seen: dict, rows=()) -> dict:
    """``ans`` (any route answer) unless it presses keys on a dialog the
    person began speaking BEFORE it was there to see plus
    :data:`ANSWER_AFTER_S` — then ``{kind: "early", title, name,
    dialog_id, say, early_ms}``: nothing is pressed, and the client reads
    the dialog out (or says ``say``) so the next words answer it. With no
    phrase timing, or no sighting of the dialog by the client, an answer
    is early too: the server never presses on a guess. ``early_ms``: how
    much sooner than allowed the phrase began (None without timing)."""
    if (
        not isinstance(ans, dict)
        or ans.get("kind") != "answer"
        or not ans.get("dialog_id")
        or not ans.get("keys")
    ):
        return ans
    title = str(ans.get("title") or "")
    visible = dialog_visible_at(title, ans["dialog_id"], seen, _row(rows, title))
    if spoke is not None and visible is not None and spoke >= visible + ANSWER_AFTER_S:
        return ans
    name = ans.get("name") or title
    early_ms = (
        int(round((visible + ANSWER_AFTER_S - spoke) * 1000))
        if spoke is not None and visible is not None
        else None
    )
    return {
        "kind": "early",
        "title": title,
        "name": name,
        "dialog_id": ans["dialog_id"],
        "say": "%s asked just now: say your answer again." % name,
        "early_ms": early_ms,
    }


# --- new sessions --------------------------------------------------------------
def _compact(text) -> str:
    return re.sub(r"[^a-z0-9]", "", norm(text))


def match_repo(words: List[str], repos) -> Optional[dict]:
    """The repository the spoken ``words`` name, or None.

    ``repos``: ``[{"name", "path", …}]``, most recent first. Spacing, case
    and punctuation don't count ("site check bot 5" is ``sitecheck-bot5``)
    and "the … repo" is not part of a name. An exact name wins, else a name
    the words begin (three letters at least) — the most recent on a tie."""
    ws = list(words)
    while ws and ws[0] in _REPO_LEAD:
        ws = ws[1:]
    while ws and ws[-1] in _REPO_TAIL:
        ws = ws[:-1]
    q = "".join(ws)
    if not q:
        return None
    known = [r for r in repos or () if isinstance(r, dict) and r.get("path")]
    # "site check bot six" is sitecheck-bot6 too.
    for said in dict.fromkeys((q, "".join(str(_NUMS.get(w, w)) for w in ws))):
        for r in known:
            if _compact(r.get("name")) == said:
                return r
        if len(said) >= 3:
            for r in known:
                if _compact(r.get("name")).startswith(said):
                    return r
    return None


def title_base(name: str, task: str) -> str:
    """The title before it is made unique: the ``called`` name hyphenated,
    else the task's first :data:`TITLE_WORDS` words the way the New dialog
    names a session from its task (``title_from_prompt``), else
    :data:`DEFAULT_TITLE`."""
    if name:
        out = "-".join(re.findall(r"[a-z0-9]+", name.lower()))[:TITLE_MAX].strip("-")
        if out:
            return out
    if task:
        out = "-".join(title_from_prompt(task).split("-")[:TITLE_WORDS])
        if out:
            return out
    return DEFAULT_TITLE


def _known(repos) -> List[dict]:
    return [r for r in repos or () if isinstance(r, dict) and r.get("path")]


def _current_repo(session: str, rows) -> Optional[dict]:
    """The repository of the session being talked to (a local row), else
    None."""
    cur = _row(rows, session)
    if cur is not None and not cur.get("device") and cur.get("path"):
        return {"name": cur.get("repo") or "", "path": cur["path"]}
    return None


def _repo_choices(session: str, rows, repos) -> List[str]:
    """The project names read out by "In which project?": the current
    session's first, then the most recent, at most :data:`MAX_NAMES`."""
    cur = _current_repo(session, rows)
    known = _known(repos)
    if cur is not None:
        same = [r for r in known if r.get("path") == cur["path"]]
        known = same + [r for r in known if r.get("path") != cur["path"]]
        if not same and cur["name"]:
            known = [cur] + known
    seen: List[str] = []
    for r in known:
        nm = str(r.get("name") or "")
        if nm and nm not in seen:
            seen.append(nm)
    return seen[:MAX_NAMES]


def _ask_repo(name: str, task: str, session: str, rows, repos, lead: str = "") -> dict:
    choices = _repo_choices(session, rows, repos)
    return {
        "kind": "ask",
        "say": "%sIn which project? %s?" % (lead, _or(choices)),
        "pending": {
            "kind": "new",
            "await": "repo",
            "name": name,
            "task": task,
            "choices": choices,
        },
    }


def _new_next(
    name: str, repo, task: str, task_said: bool, session: str, rows, repos
) -> dict:
    """The next step of a new session: ask for the project (unless only one
    is known), then for the task (unless said, or declined), then the
    finished plan ``{"kind": "new", "title_base", "name", "repo_name",
    "repo_path", "task"}``."""
    known = _known(repos)
    if repo is None:
        if not known:
            return {
                "kind": "new-failed",
                "say": "I don't know any projects yet: open one from the New dialog first.",
            }
        if len(known) > 1:
            return _ask_repo(name, task, session, rows, repos)
        repo = known[0]
    path = str(repo["path"])
    repo_name = str(repo.get("name") or "") or path.rstrip("/").rsplit("/", 1)[-1]
    if not task and not task_said:
        return {
            "kind": "ask",
            "say": "What should it do in %s?" % repo_name,
            "pending": {
                "kind": "new",
                "await": "task",
                "name": name,
                "task": "",
                "repo_name": repo_name,
                "repo_path": path,
            },
        }
    return {
        "kind": "new",
        "title_base": title_base(name, task),
        "name": name,
        "repo_name": repo_name,
        "repo_path": path,
        "task": task,
    }


def plan_new(text, session: str, rows, repos) -> dict:
    """``text`` (a ``new`` phrase) → what to create: ``{"kind": "new",
    "title_base", "name", "repo_name", "repo_path", "task"}``; an ``ask``
    (``say`` + the ``pending`` fields gathered so far, :func:`continue_new`)
    when the project or the task is missing; ``{"kind": "new-failed",
    "say"}``; ``{"kind": "prompt"}`` when ``text`` is no ``new`` phrase after
    all.

    The repository: the one named — the longest run of the words after
    "in/for/on" that names a known repository, the rest of them starting the
    task ("in sitecheck add tests"), or a known one the task ENDS on ("… to
    fix the login bug in mindflock"); else the only one known; else it asks
    "In which project?". Words after "for" or "on" that name no repository
    were the task ("new session for the login bug"); after "in" they were
    meant as one: it asks again, saying so.
    """
    w = _Words(text)
    p = _parse_new(w)
    if p is None:
        return {"kind": "prompt"}
    name = " ".join(w.words[p["name"][0] : p["name"][1]]) if p["name"] else ""
    task_at = p["task"]
    task_end = len(w.words)
    repo = None
    if p["repo"] is not None:
        a, b = p["repo"]
        for k in range(b - a, 0, -1):
            repo = match_repo(w.words[a : a + k], repos)
            if repo is not None:
                if a + k < b:
                    task_at = a + k  # "in sitecheck add tests …": the rest is the task
                break
        if repo is None:
            if p["prep"] == "in":
                task = w.original(task_at) if task_at is not None else ""
                return _ask_repo(
                    name,
                    task,
                    session,
                    rows,
                    repos,
                    "I don't know a project called %s. " % w.original(a, b),
                )
            task_at = a - 1  # "for the login bug" / "on the parser" is the task
    if repo is None and task_at is not None:
        # "… to fix the login bug in mindflock": the project at the end.
        for j in range(task_at + 1, len(w.words) - 1):
            if w.words[j] == "in":
                repo = match_repo(w.words[j + 1 :], repos)
                if repo is not None:
                    task_end = j
                    break
    task = w.original(task_at, task_end) if task_at is not None else ""
    return _new_next(name, repo, task, False, session, rows, repos)


def _pending_repo(pending: dict, session: str, rows, repos) -> Optional[dict]:
    """The project a pending new session already has — only one this server
    knows (the client sent it back)."""
    path = str(pending.get("repo_path") or "")
    if not path:
        return None
    cur = _current_repo(session, rows)
    for r in _known(repos) + ([cur] if cur else []):
        if str(r.get("path")) == path:
            return {
                "name": str(pending.get("repo_name") or r.get("name") or ""),
                "path": path,
            }
    return None


def _said_repo(w: "_Words", pending: dict, session: str, rows, repos):
    """The project the answer to "In which project?" names → ``(repo, task
    word index or None)``, or ``(None, None)``: "this one" (the current
    session's), "the second one" (in the order read out), a name ("the
    sitecheck repo"), a name and then the task ("parser, add tests")."""
    t = _REPO_ANSWER_LEAD_RE.sub("", w.text)
    skip = len(w.words) - len(t.split()) if t else len(w.words)
    if _HERE_RE.match(t):
        return _current_repo(session, rows), None
    m = _PICK_NUM_RE.match(t)
    if m and not m.group(3):
        choices = [str(c) for c in pending.get("choices") or ()]
        n = _count(m.group(1) or m.group(2))
        if n and 1 <= n <= len(choices):
            hit = next(
                (r for r in _known(repos) if r.get("name") == choices[n - 1]), None
            )
            return hit, None
    words = t.split()
    while words and words[-1] in ("one", "please"):
        words = words[:-1]
    for k in range(len(words), 0, -1):
        repo = match_repo(words[:k], repos)
        if repo is not None:
            rest = skip + k
            if rest < len(w.words):
                return repo, rest
            return repo, None
    return None, None


def continue_new(
    text, pending, session: str, rows, repos, words=None
) -> Optional[dict]:
    """The next phrase of a new session's conversation (``pending``: the
    ``ask``'s, sent back by the client) → the finished plan, the next
    ``ask``, ``new-failed`` (said "never mind", or no project is known), or
    None when the phrase is not part of it ("stop listening", "switch to
    docs" while it asks for the project) — routed as usual, the
    conversation dropped.

    Asked for the project: a project the words name, or "this one"; else it
    asks again. Asked for the task: the phrase IS the task, as said ("to"
    / "it should" before it dropped); "nothing" / "just start it" starts it
    without one."""
    w = _Words(text)
    if not w.text or not isinstance(pending, dict):
        return None
    cmd = command(text, words)
    kind = cmd["kind"] if cmd else None
    if kind == "cancel" or _NEW_CANCEL_RE.match(w.text):
        return {"kind": "new-failed", "say": "Okay, no new session.", "cancelled": True}
    if kind in ("stop", "hush", "off"):
        return None
    name = str(pending.get("name") or "")
    task = str(pending.get("task") or "")
    repo = _pending_repo(pending, session, rows, repos)
    if pending.get("await") == "task" and repo is not None:
        if _NO_TASK_RE.match(w.text):
            return _new_next(name, repo, "", True, session, rows, repos)
        m = _TASK_LEAD_RE.match(w.text)
        skip = m.group(0).count(" ") if m else 0
        said = w.original(skip) or w.original(0)
        return _new_next(name, repo, said, True, session, rows, repos)
    repo, task_at = _said_repo(w, pending, session, rows, repos)
    if repo is not None:
        if task_at is not None and not task:
            rest = _Words(w.original(task_at))
            m = _TASK_LEAD_RE.match(rest.text)
            task = rest.original(m.group(0).count(" ") if m else 0)
        return _new_next(name, repo, task, bool(task), session, rows, repos)
    if kind is not None and kind not in ("pick", "answer", "switch"):
        return None
    if kind == "switch" and find_sessions(cmd["arg"], rows)[0] is not None:
        return None
    return _ask_repo(
        name,
        task,
        session,
        rows,
        repos,
        "I don't know a project called %s. " % text.strip().rstrip(".?!"),
    )


def started(title: str, repo_name: str) -> dict:
    """The answer once ``title`` was created in ``repo_name``."""
    return {
        "kind": "new",
        "title": title,
        "name": title,
        "say": "Started %s in %s." % (title, repo_name),
    }
