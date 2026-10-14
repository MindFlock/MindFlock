"""Task headlines: a prompt condensed to a short verb phrase, without a model.

The sidebar shows what each session was last asked to do. A raw prompt is a
poor label — "ok so can you please fix the bug where the pin bar draws below
the terminal" — and a good one reads like a to-do item: "Fix pin bar drawing
below terminal". :func:`task_headline` makes that conversion with plain rules
over curated word tables, so it is deterministic, instant (it runs per row per
poll) and needs nothing beyond the stdlib.

How it reads a prompt:

1. **Machine text** first. MindFlock's own envelopes (``[MindFlock message …]``
   loses its header and keeps the body; a ``note`` or ``result`` is
   bookkeeping), the revive "Your previous run was cut off …" prompt, bare
   "continue"/"yes"/"progress?" — these get a fixed headline and are flagged
   NOT meaningful, so a caller can keep showing the last real task instead.
2. **Normalise**: drop pasted blocks, code fences, quoted and pasted terminal
   output, image placeholders, parentheticals, emoji and markdown; URLs become
   ``PR #12`` / ``issue #3`` / ``sc-123`` / a bare host; long paths become
   their basename.
3. **Split** into sentences, then into clauses at conjunctions and request
   markers ("…, and then deploy it", "… can you …") — but only where a new
   action starts, so "articles and posts" stays one noun phrase.
4. **Read each clause** with the first rule that fits: an imperative ("fix
   X" once politeness and preambles are peeled off), "make sure X is fixed"
   → "Fix X", a question ("why is X slow" → "Investigate X slow"), a wish
   ("I want X dropped" → "Drop X"), a requirement ("X needs to work on mac"
   → "Make X work on mac"), or a complaint ("X looks lazy" → "Polish X").
5. **Pick** the best clause: real work beats a question beats shipping steps
   beats meta-instructions ("spawn agents", "read the brief first"); a
   specific object (a name, a number, a file) beats a vague one ("this",
   "all of it"); earlier beats later. Shipping steps alone ("commit, push and
   pr") render as one chain.
6. **Render**: canonical verb + at most :data:`MAX_OBJECT_WORDS` object words,
   articles and possessives dropped, trailing function words trimmed,
   sentence case, known acronyms upper-cased, no trailing punctuation.

Pure: no I/O, no clock, no randomness. Never raises on any input.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

#: Words after the verb. Keeps a headline at ~2–6 words on a narrow rail.
MAX_OBJECT_WORDS = 5
#: Only the head of a prompt is read; the rest is pasted context.
MAX_INPUT_CHARS = 4000
#: Sentences considered; the ask is near the top of every real prompt.
MAX_SENTENCES = 14


class Headline(NamedTuple):
    """A headline plus whether it says anything about the TASK, and whether
    a rule built it.

    ``meaningful`` is False for confirmations, "continue", status pings,
    MindFlock's own notes and bare shipping steps ("commit and push") — a
    caller tracking what a session is FOR keeps its previous headline then.

    ``confident`` is True when a rule matched cleanly (an imperative, wish,
    requirement, question or complaint with a real object; the shipping
    chain; a canned reply). When False, ``text`` is NOT a constructed phrase:
    it is the person's own words, cleaned and cut ("I like this but I dont
    like how slow…") — never word salad.
    """

    text: str
    meaningful: bool
    confident: bool = True


def _phrases(spec: str) -> Tuple[Tuple[str, ...], ...]:
    """``"a b|c"`` → ``(("a", "b"), ("c",))``, longest first — the tables
    below are written as ``|``-separated phrases for readability."""
    out = {tuple(p.split()) for p in spec.replace("\n", " ").split("|") if p.strip()}
    return tuple(sorted(out, key=lambda p: (-len(p), p)))


# --------------------------------------------------------------------------- #
# Verbs
# --------------------------------------------------------------------------- #

# Verb classes, weighted when picking between clauses (see _score).
WORK = "work"  # changes something: fix, add, refactor, build …
LOOK = "look"  # finds something out: investigate, explain, review …
SHIP = "ship"  # moves work along: commit, push, PR, merge, release …
META = "meta"  # about the conversation, not the code: spawn, follow, tell …
VAGUE = "vague"  # carries no task by itself: do, go, try …

_CLASS_WEIGHT = {WORK: 3.0, LOOK: 2.4, SHIP: 1.4, META: 0.8, VAGUE: 0.3}

#: Single-word verbs → class. The display form is the capitalised word
#: unless :data:`_CANONICAL` renames it.
_VERB_TABLE = {
    WORK: """
        add remove delete drop fix repair implement build create write rewrite
        refactor rename move update upgrade downgrade migrate port replace
        change convert split consolidate simplify clean polish redesign design
        document make wire hook integrate support handle enable disable
        configure install uninstall extract inline restore revert reset retry
        rerun run test bump pin unpin hide display expose cache batch dedupe
        deduplicate dedup sort filter group parse validate sanitize optimize
        optimise tune shrink trim cap limit raise lower increase decrease pass
        reduce generate scaffold prototype draft sketch plan outline restart
        stop start kill launch provision amend resolve address apply adopt
        copy clone sync import export upload download fetch grab pull scrape
        crawl scan index reindex backfill seed load save store persist log
        track monitor alert notify schedule automate triage close reopen
        finish complete finalize finalise redo undo revise edit tweak adjust
        align center centre resize reorder swap toggle flip archive prune
        purge wipe clear nuke mock stub patch harden secure encrypt decrypt
        debug execute put set allow prevent avoid skip sweep ticket speed
        shorten tighten loosen extend expand collapse standardize standardise
        unify separate isolate wrap unwrap rotate translate localize style
        restyle theme format reformat lint annotate rebuild recreate
        regenerate refresh reload reinstall reproduce cover stabilize
        stabilise unblock unstick replay reconnect connect disconnect attach
        detach mount unmount bind strip escape capture record render send post
        emit print call process ingest classify train finetune label checkout
        rerun redeploy rollback undeploy bootstrap init initialize spike
        port uncomment comment silence mute quiet throttle retire deprecate
        setup bring relaunch rerender reroute route redirect improve enhance
        tidy organize organise rearrange reword rephrase
        """,
    LOOK: """
        investigate diagnose profile benchmark measure analyze analyse explain
        describe summarize summarise compare evaluate assess review check
        inspect explore research find search locate list count confirm verify
        audit read study trace identify estimate answer recommend propose
        suggest justify brainstorm consider decide weigh know learn
        """,
    SHIP: """
        commit push pr merge rebase squash release tag deploy ship land
        approve backport publish roll
        """,
    META: """
        spawn follow tell show give point help let ask report use keep take
        see view watch wait note remember mention ping message send reply
        respond orchestrate
        """,
    VAGUE: """do go try get continue resume proceed work deal""",
}
_VERBS: Dict[str, str] = {}
for _cls, _words in _VERB_TABLE.items():
    for _w in _words.split():
        _VERBS.setdefault(_w, _cls)

#: Display names that differ from the capitalised word.
_CANONICAL = {
    "pr": "Open PR",
    "analyse": "Analyze",
    "optimise": "Optimize",
    "finalise": "Finalize",
    "summarise": "Summarize",
    "standardise": "Standardize",
    "stabilise": "Stabilize",
    "centre": "Center",
    "nuke": "Remove",
    "dedup": "Dedupe",
    "deduplicate": "Dedupe",
    "speed": "Speed up",
    "grab": "Fetch",
    "checkout": "Check out",
    "know": "Explain",
    "learn": "Explain",
    "see": "Show",
    "view": "Show",
    "init": "Initialize",
    "rollback": "Roll back",
    "setup": "Set up",
}

#: Multi-word verbs → (display, class). Matched before single words.
_PHRASAL: Sequence[Tuple[Tuple[str, ...], str, str]] = tuple(
    sorted(
        (
            (tuple(p.split()), disp, cls)
            for p, disp, cls in (
                ("get rid of", "Remove", WORK),
                ("make a pr", "Open PR", SHIP),
                ("make the pr", "Open PR", SHIP),
                ("make pr", "Open PR", SHIP),
                ("make prs", "Open PRs", SHIP),
                ("make the prs", "Open PRs", SHIP),
                ("open a pr", "Open PR", SHIP),
                ("open the pr", "Open PR", SHIP),
                ("open pr", "Open PR", SHIP),
                ("create a pr", "Open PR", SHIP),
                ("raise a pr", "Open PR", SHIP),
                ("cut a release", "Cut release", SHIP),
                ("cut a new release", "Cut release", SHIP),
                ("cut release", "Cut release", SHIP),
                ("make a commit", "Commit", SHIP),
                ("do a commit", "Commit", SHIP),
                ("do a pr", "Open PR", SHIP),
                ("do a release", "Release", SHIP),
                ("make a release", "Release", SHIP),
                ("figure out", "Investigate", LOOK),
                ("look into", "Investigate", LOOK),
                ("dig into", "Investigate", LOOK),
                ("find out", "Investigate", LOOK),
                ("look at", "Review", LOOK),
                ("take a look at", "Review", LOOK),
                ("have a look at", "Review", LOOK),
                ("go through", "Review", LOOK),
                ("go over", "Review", LOOK),
                ("run through", "Run through", WORK),
                ("check on", "Check on", LOOK),
                ("check out", "Check out", WORK),
                ("clean up", "Clean up", WORK),
                ("set up", "Set up", WORK),
                ("speed up", "Speed up", WORK),
                ("hook up", "Hook up", WORK),
                ("wire up", "Wire up", WORK),
                ("spin up", "Spin up", WORK),
                ("tear down", "Tear down", WORK),
                ("break down", "Break down", LOOK),
                ("write up", "Write up", WORK),
                ("roll back", "Roll back", SHIP),
                ("roll forward", "Roll forward", SHIP),
                ("roll out", "Roll out", SHIP),
                ("turn off", "Turn off", WORK),
                ("turn on", "Turn on", WORK),
                ("tell me", "Report", LOOK),
                ("let me know", "Report", LOOK),
                ("show me", "Show", META),
                ("let me see", "Show", META),
                ("give me", "Get", META),
                ("point me to", "Find", LOOK),
                ("point me towards", "Find", LOOK),
                ("point me at", "Find", LOOK),
                ("try again", "Retry", WORK),
                ("start over", "Restart", WORK),
                ("execute on", "Execute", WORK),
                ("dont worry about", "Skip", WORK),
                ("do not worry about", "Skip", WORK),
                ("forget about", "Skip", WORK),
                ("keep an eye on", "Watch", META),
                ("work on", "Work on", WORK),
                ("bring back", "Restore", WORK),
                ("pick up", "Resume", WORK),
                ("pick back up", "Resume", VAGUE),
            )
        ),
        key=lambda p: -len(p[0]),
    )
)

#: Phrasal verbs that also split around a pronoun: "set this up", "figure it
#: out", "roll it back" → the particle, and the pronoun becomes the object.
_SEPARABLE = {
    ("set", "up"): "Set up",
    ("clean", "up"): "Clean up",
    ("figure", "out"): "Investigate",
    ("roll", "back"): "Roll back",
    ("roll", "forward"): "Roll forward",
    ("speed", "up"): "Speed up",
    ("hook", "up"): "Hook up",
    ("wire", "up"): "Wire up",
    ("spin", "up"): "Spin up",
    ("tear", "down"): "Tear down",
    ("break", "down"): "Break down",
    ("turn", "off"): "Turn off",
    ("turn", "on"): "Turn on",
    ("set", "back"): "Revert",
    ("look", "up"): "Look up",
    ("check", "out"): "Check out",
    ("bring", "back"): "Restore",
    ("pick", "up"): "Resume",
}
#: Verb words that are nouns before these ("merge conflicts").
_NOUN_AFTER = {
    "merge": ("conflicts", "conflict", "request", "requests", "queue", "commit"),
    "test": (
        "suite",
        "suites",
        "file",
        "files",
        "plan",
        "run",
        "runs",
        "results",
        "output",
    ),
    "release": ("notes", "branch", "flow", "process"),
    "commit": ("message", "messages", "hook", "hooks", "history"),
    "log": ("lines", "line", "file", "files", "output"),
    "build": ("output", "step", "steps", "log", "logs"),
    "check": ("list", "box", "boxes"),
    "review": ("comments", "comment", "page", "details"),
    "plan": ("mode",),
    "scan": ("results", "pool", "run", "runs"),
    "index": ("page", "file"),
    "pull": ("request", "requests"),
    "push": ("notification", "notifications"),
}
_PARTICLE_OBJECTS = frozenset("it this that them these those everything".split())

#: Nouns that name an action: "do a thorough scan of X" → "Scan X", "make a
#: copy of X" → "Copy X", "give it a review" → "Review".
_ACTION_NOUNS = {
    "scan": ("Scan", WORK),
    "sweep": ("Sweep", WORK),
    "review": ("Review", LOOK),
    "pass": ("Review", LOOK),
    "look": ("Review", LOOK),
    "check": ("Check", LOOK),
    "audit": ("Audit", LOOK),
    "analysis": ("Analyze", LOOK),
    "investigation": ("Investigate", LOOK),
    "dive": ("Investigate", LOOK),
    "comparison": ("Compare", LOOK),
    "summary": ("Summarize", LOOK),
    "writeup": ("Write up", WORK),
    "copy": ("Copy", WORK),
    "backup": ("Back up", WORK),
    "cleanup": ("Clean up", WORK),
    "refactor": ("Refactor", WORK),
    "rewrite": ("Rewrite", WORK),
    "test": ("Test", WORK),
    "plan": ("Plan", WORK),
    "fix": ("Fix", WORK),
    "release": ("Release", SHIP),
}

#: "make X <comparative>" → one verb: "make the hook faster" → "Speed up hook".
_COMPARATIVES = {
    "faster": "Speed up",
    "quicker": "Speed up",
    "snappier": "Speed up",
    "smaller": "Shrink",
    "shorter": "Shorten",
    "simpler": "Simplify",
    "cleaner": "Clean up",
    "clearer": "Clarify",
    "better": "Improve",
    "nicer": "Polish",
    "prettier": "Polish",
    "safer": "Harden",
    "stronger": "Harden",
    "robust": "Harden",
    "reliable": "Harden",
    "consistent": "Unify",
}

# --------------------------------------------------------------------------- #
# Filler, noise and vague words
# --------------------------------------------------------------------------- #

#: Spoken shorthand, typos and run-together words seen in real prompts,
#: rewritten before anything reads the words (keys: lower-case, no
#: apostrophes).
_RESPELL = {
    "u": "you",
    "ur": "your",
    "urself": "yourself",
    "pls": "please",
    "plz": "please",
    "thx": "thanks",
    "makesure": "make sure",
    "figureout": "figure out",
    "leftoff": "left off",
    "waht": "what",
    "teh": "the",
    "ytou": "you",
    "yu": "you",
    "wanna": "want to",
    "gonna": "going to",
    "gotta": "have to",
    "lemme": "let me",
    "expain": "explain",
    "commited": "committed",
    "commiting": "committing",
    "explian": "explain",
    "setup": "setup",
}

#: Peeled off the FRONT of a clause, repeatedly, longest match first:
#: greetings, acknowledgements, politeness and "I want you to"-style
#: preambles.
_FILLER = _phrases("""
    hey|hi|hello|ok|okay|k|kk|so|yeah|yea|yes|yep|yup|sure|alright|right|
    well|also|and|then|but|now|just|actually|basically|honestly|please|
    kindly|btw|oh|hmm|um|uh|great|cool|nice|perfect|awesome|thanks|thank you|
    lol|ah|wait|fine|anyway|anyways|o|eh|hmm ok|oh yeah|cool idea|
    can you|could you|would you|will you|can you please|you can|you could|
    you should|you need to|you have to|you must|you will|you might|you may|
    you might want to|you are to|youre to|you gotta|
    we should|we need to|we have to|we must|we can|we could|we want to|
    we gotta|we might|we may|i will|ill|well need to|i want you|
    i need you to|i want you to|i would like you to|id like you to|
    i need to|i want to|id like to|i would like to|i do want you to|
    i also want you to|i also want to|i really want you to|i really want to|
    i just want you to|i just want to|im asking you to|i asked you to|
    lets|let us|go ahead and|try to|try and|make sure to|be sure to|
    remember to|dont forget to|help me|help me to|i think|i feel like|
    i guess|i believe|maybe|probably|perhaps|for now|first|first of all|
    next|finally|lastly|at the end|in the meantime|meanwhile|
    one more thing|another thing|in addition|additionally|
    it seems like|seems like|it looks like|i noticed|i noticed that|
    i see that|i realized|i realised|i just realized|
    the next step is to|next step is to|your job is to|your task is to|
    the goal is to|the task is to|all you need to do is|
    in a separate branch|on a separate branch|in a new branch|
    ok so|so now|and then|and also|but also|if so|if not|yes please|
    ok please|ok now|now please|ok i want you to|
    be able to|start to|begin to|pick up where you left off|
    just pick up where you left off|after that|once thats done|
    once that is done|when thats done|in sequence|in order|
    now that|alright so|right so|okay so|also please|one thing though is|
    the one thing is|find a way to|figure out a way to|find a way for|
    come up with a way to|think of a way to|you def need to|
    """)

#: Removed wherever they appear: they never change what the task is.
_NOISE_WORDS = frozenset(
    """please pls kindly just really actually basically literally simply
    honestly totally definitely def btw lol asap also""".split()
)

#: Tails dropped from the end of an object ("fix the tests as well please").
_TAILS = _phrases("""
    as well|too|for me|for us|right now|now|asap|if possible|if needed|
    if need be|if necessary|if you can|if you need to|if you want|when done|
    when youre done|when you are done|when ready|when youre ready|
    when you can|for now|again|yourself|first|already|then|thanks|
    thank you|for real|real quick|quickly|please|here|there|today|tonight|
    right away|immediately|as needed|or something|or whatever|and stuff|
    and such|etc|etc etc|right here|by yourself|on your own|as well please|
    and|or|if|so|but|then|too please|or no|or not
    """)

#: Objects that point at something instead of naming it.
_VAGUE_WORDS = frozenset(
    """it this that them these those everything anything something all
    both stuff things thing rest one ones same so what whatever there here
    which others other way lot bunch work changes change fix fixes above
    below again happen happens well sense""".split()
)

#: Nouns too generic to be a task on their own ("make a ticket", "fix the
#: issue") — fine inside a longer object, vague when they are all of it.
_GENERIC_NOUNS = frozenset(
    """issue issues bug bugs problem problems thing things ticket tickets
    task tasks todo change changes work stuff item items step steps part
    parts one ones request requests error errors""".split()
)

_ARTICLES = frozenset("the a an".split())
_POSSESSIVES = frozenset("my our your his her their its ur".split())
_DEMONSTRATIVES = frozenset("this that these those".split())
_QUANTIFIERS = _phrases("""
    all|all of|all the|all of the|every|every single|each|some|some of|
    some of the|a few|a few of|a few of the|few|a bunch of|bunch of|
    a lot of|lots of|a couple of|a couple|couple of|any|many|most|most of|
    most of the|several|the whole|the entire|entire|whole|so many|
    a million|like a million|any of the|one of the|few of|few of the
    """)
_PREPOSITIONS = frozenset(
    """to for in on at by with from into onto of about over under below
    above across through between via per without within inside""".split()
)

#: Function words a headline never ENDS on.
_TRAILING_STOP = frozenset(
    """of to for the a an and or in on at by with from into onto as is are
    be was were so that which who if when then but than its my our your
    their this these those all every each some any very more most also
    just like via about up down out over under it they we i you he she
    what including without whether how where why no not""".split()
)

#: Ordinary English function words: not "content" for specificity or for
#: the plain-words fallback.
_STOP = frozenset("""a an the and or but if then so to of in on at by for with from into
    onto as is are was were be been being am do does did done have has had
    i me my we us our you your he she it its they them their this that
    these those there here what which who whom whose when where why how
    all any both each few more most other some such no nor not only own
    same than too very can will just should would could might may must
    shall now also like get got gets getting go goes going went im ive id
    ill dont doesnt didnt isnt arent wasnt werent cant wont wouldnt
    shouldnt couldnt hasnt havent hadnt thats theres whats lets yes yeah
    ok okay please thanks hey hi hello really actually basically still
    even already yet ever never always again maybe probably think feel
    want need know see say said make made way thing things stuff one
    ones lot bunch kind sort""".split())

#: Auxiliaries — the joints of questions and "make sure X is fixed".
_AUX = frozenset("""is are was were be been am do does did has have had can could will
    would should shall may might must isnt arent wasnt werent dont doesnt
    didnt hasnt havent cant couldnt wont wouldnt shouldnt""".split())
_SUBJECT_PRONOUNS = frozenset("i we you they he she it im".split())
_INTENSIFIERS = frozenset(
    """very so super really kinda pretty quite too extremely totally always
    still now sometimes often constantly much many that generally overall
    more""".split()
)

#: Upper-cased wherever they appear in a headline.
_ACRONYMS = {
    w: w.upper() for w in """pr ci ui ux api url sql aws iam mcp llm css html json yaml
    db cli ssh sso dns ssl vpc sqs ssm eks gpu cpu ram ocr qa ip tls http
    https jwt sdk ide pdf csv svg ttl dag etl rbac s3 ec2 rds alb tmux e2e
    sns ecr ecs k8s""".split()
}
_ACRONYMS.update({"prs": "PRs", "apis": "APIs", "urls": "URLs"})

# --------------------------------------------------------------------------- #
# Complaints, wishes and requirements
# --------------------------------------------------------------------------- #

#: Complaint predicates → the fix they ask for. The subject before the
#: predicate becomes the object: "the wrapping looks lazy" → "Polish
#: wrapping". Matched after intensifiers ("so", "super") are dropped.
_COMPLAINT_TABLE = (
    (
        "Fix",
        """
        doesnt work|dont work|isnt working|arent working|not working|
        stopped working|never works|wont work|is broken|are broken|broken|
        broke|is wrong|are wrong|crashes|crashed|is crashing|keeps crashing|
        errors out|is erroring|throws|is buggy|buggy|hangs|is hanging|
        is stuck|are stuck|freezes|is frozen|does nothing|do nothing|
        has nothing|shows nothing|says nothing|is empty|are empty|
        isnt showing|arent showing|doesnt show|dont show|not showing|
        doesnt load|wont load|isnt loading|not loading|disappeared|vanished|
        is gone|are gone|is missing|are missing|keeps happening|
        doesnt update|isnt updating|not updating|doesnt render|
        isnt rendering|not rendering|is not working|are not working|
        does not work|do not work|is duplicated|is ignored|gets ignored|
        makes no sense|is blank|are blank|is off|doesnt make sense|
        doesnt make much sense|dont make sense|make no sense
        """,
    ),
    (
        "Fix failing",
        """
        fail|fails|failed|failing|is failing|are failing|seem to fail|
        seems to fail|seem to be failing|seems to be failing|keep failing|
        keeps failing|are red|is red
        """,
    ),
    (
        "Speed up",
        """
        is slow|are slow|slow|takes forever|takes long|is laggy|lags|
        is sluggish|sluggish|take long|take forever|take so long|
        take too long|takes a long time|take a long time|takes too long|
        takes so long|slows down
        """,
    ),
    (
        "Polish",
        """
        looks bad|looks weird|looks off|looks lazy|looks wrong|looks ugly|
        looks like shit|looks terrible|looks awful|looks janky|looks broken|
        looks cramped|looks cluttered|looks messy|is ugly|ugly|sucks|suck|
        is janky|janky|is cramped|is cluttered|is annoying|annoying
        """,
    ),
    ("Rework", "is weird|is messy|is a mess|is clunky|clunky|is overkill"),
    (
        "Clarify",
        """
        is confusing|confusing|isnt intuitive|not intuitive|is not intuitive|
        is unclear|unclear|is hard to read|hard to read|hard to understand
        """,
    ),
    (
        "Resolve conflicts in",
        "have conflicts|has conflicts|have merge conflicts|has merge conflicts",
    ),
)
_COMPLAINTS: Sequence[Tuple[Tuple[str, ...], str]] = tuple(
    sorted(
        ((p, verb) for verb, spec in _COMPLAINT_TABLE for p in _phrases(spec)),
        key=lambda c: -len(c[0]),
    )
)
#: Words that make a predicate a complaint even mid-sentence only when the
#: subject is right there ("broken", "slow" alone are too ambiguous at the
#: very start of a clause).
_STANDALONE_COMPLAINTS = frozenset(["broken", "ugly", "slow", "sucks", "annoying"])

#: First-person complaints that name their subject AFTER: "I don't like X".
_DISLIKE_TABLE = (
    (
        "Rework",
        """
        i dont like|i do not like|i dont love|i do not love|i really dont like|
        i really do not love|i really dont love|i really do not like|i hate|
        i dislike|im not a fan of|i am not a fan of|not a fan of|
        i dont really like|i dont lik
        """,
    ),
    ("Restore", "i miss"),
    (
        "Explain",
        """
        i dont understand|i do not understand|im not understanding|
        i am not understanding|i dont get|im confused about|im confused by|
        i am confused about|im confused on|i dont really understand|
        i really dont understand|i want to know|i wanna know|
        i want to understand|i would like to know|id like to know
        """,
    ),
    (
        "Fix missing",
        """
        i dont see|i do not see|i cant see|i can not see|i cannot see|
        i have no|there are no|there is no|theres no|we have no|i dont have|
        i still dont see|im not seeing|i am not seeing
        """,
    ),
    ("Remove", "i still see|im still seeing|i am still seeing"),
    (
        "Fix",
        "i get the weird|i get a weird|i get weird|im getting|still seeing|im seeing|i see a|i keep seeing",
    ),
)
_DISLIKE: Sequence[Tuple[Tuple[str, ...], str]] = tuple(
    sorted(
        ((p, verb) for verb, spec in _DISLIKE_TABLE for p in _phrases(spec)),
        key=lambda c: -len(c[0]),
    )
)
_LACKS = _phrases("doesnt have|dont have|does not have|has no|have no|lacks|is missing")

#: "X needs to / should / has to (be) …" — a requirement on X.
_MODALS = _phrases("""
    should be|needs to be|need to be|has to be|have to be|must be|
    should|needs to|need to|has to|have to|must|ought to
    """)

#: Generic nouns that introduce the real object: "fix the bug where the pin
#: bar draws below the terminal" → "Fix pin bar drawing below terminal".
_INTRO_NOUNS = frozenset(
    "bug issue problem thing case behaviour behavior glitch error".split()
)
_INTRO_LINKS = frozenset("where when that in which with".split())

#: Problem nouns for "there are conflicts in X" → "Fix conflicts in X".
_PROBLEM_NOUNS = frozenset(
    """conflicts conflict errors error bugs bug failures failure issues
    issue problems problem warnings warning regressions regression typos
    typo leaks leak crashes crash""".split()
)

# --------------------------------------------------------------------------- #
# Labels, and the vocabulary of prompts that carry no task
# --------------------------------------------------------------------------- #

#: Line labels ("Task: …") whose text IS the task, and labels whose text is
#: only background.
_TASK_LABELS = frozenset(
    p.strip() for p in """task|story|stories|job|goal|objective|todo|ask|request|
    your task|your story|your stories|your job|your goal|the task|the ask|
    the goal|feature|fix|bug|ticket|plan this change|plan a change""".split("|")
)
_CONTEXT_LABELS = frozenset(
    p.strip() for p in """context|background|facts|known|note|notes|rules|evidence|
    branch|summary|changes|diff|output|log|logs|error|traceback|status|why|
    constraints|details|description|fyi|ps|update|orchestrator note|
    live check|tenant|issue|pr description as pasted|changes summary|
    part a|part b|what""".split("|")
)

_CONTINUE_TRIGGERS = ("continue", "resume", "proceed", "carry", "keep", "pick")
_CONTINUE_WORDS = frozenset(
    """continue continuecontinue resume proceed carry on keep going go
    ahead pick up where you left off from it the task please ok okay yes
    yeah so just and then if need be needed necessary now again with
    working back""".split()
)
_CONFIRM_WORDS = frozenset(
    """yes yeah yep yup sure ok okay k kk alright fine go ahead do it that
    this both all please for sounds good great perfect lgtm thanks thank
    you cool nice y approved approve agreed agree i want to correct
    exactly true right makes sense what think is best we same as before
    one first second option them those good wonderful love awesome
    amazing works work worked looks lgtm and the most makes would like
    do""".split()
)
_GREETING_WORDS = frozenset("hello hi hey yo sup there morning good gm".split())
_STATUS_WORDS = frozenset(
    """progress status update eta how hows is it going are we doing done
    much time left longer whats the where at so far any you finished
    complete yet still working on ready things what and do think long
    will take be wise""".split()
)
_STATUS_TRIGGERS = frozenset(
    "progress status eta going doing done left longer finished ready".split()
)
#: First-person reports with nothing asked ("I signed in", "I merged it"):
#: the user unblocking the agent, not a new task.
_REPORT_VERBS = frozenset(
    """signed logged switched merged restarted installed ran did added
    approved fixed updated pushed deleted created made set changed
    reset rebooted connected started stopped closed opened moved got
    am have""".split()
)

#: The revive sweep's continue prompt (core.revive.CONTINUE_PROMPT) and the
#: usage-limit reset nudge, matched by their stable heads.
_RESUME_HEADS = (
    "your previous run was cut off",
    "your claude.ai usage limit has reset",
    "your usage limit has reset",
)

# --------------------------------------------------------------------------- #
# Verb inflections (built once from the tables above)
# --------------------------------------------------------------------------- #

#: Verbs whose final consonant doubles before -ing/-ed.
_DOUBLING = frozenset(
    """ship split stop drop run set get put cut plan pin unpin tag wrap
    unwrap debug begin swap map chop trim skip step scan log dig hit sit
    shut rerun refer commit submit admit permit occur prefer control flip
    strip grab spin win slip trap zip jam chat clip prop shop plug unplug
    pop drag star scrub snap rub sum tap top cap rig overlap""".split()
)

#: Irregular past participles / past tenses → base.
_IRREGULAR_PAST = {
    "built": "build",
    "rebuilt": "rebuild",
    "made": "make",
    "written": "write",
    "wrote": "write",
    "rewritten": "rewrite",
    "done": "do",
    "gone": "go",
    "ran": "run",
    "set": "set",
    "put": "put",
    "cut": "cut",
    "split": "split",
    "shown": "show",
    "given": "give",
    "taken": "take",
    "got": "get",
    "gotten": "get",
    "found": "find",
    "broken": "break",
    "kept": "keep",
    "left": "leave",
    "sent": "send",
    "held": "hold",
    "brought": "bring",
    "spun": "spin",
    "drawn": "draw",
    "torn": "tear",
    "hidden": "hide",
    "chosen": "choose",
    "frozen": "freeze",
    "begun": "begin",
    "undone": "undo",
    "redone": "redo",
    "seen": "see",
    "told": "tell",
    "led": "lead",
    "fed": "feed",
    "shut": "shut",
}

#: Verbs that are not in the action tables but show up as the predicate of
#: a subject ("the pin bar DRAWS below …"), for noun-phrase boundaries and
#: the "-s" → "-ing" rewrite.
_STATE_VERBS = frozenset(
    """draw show appear render overlap jump flicker crash hang fail break
    disappear stick freeze load open scroll wrap say keep get go reach take
    run look seem stay sit start stop end land come leave happen need
    have make work use cause return point match pass hit send post call
    lose miss hold reset overflow cover duplicate double repeat block
    eat cut drop grow shrink spin lag display print emit read write
    contain include sag ask spam swallow timeout stutter truncate stall request
    vanish clip drift leak ignore shift""".split()
)


def _gerund(base: str) -> str:
    """``draw`` → ``drawing``, ``make`` → ``making``, ``ship`` → ``shipping``."""
    if base.endswith("ie"):
        return base[:-2] + "ying"
    if base.endswith("e") and not base.endswith(("ee", "ye", "oe")) and len(base) > 2:
        return base[:-1] + "ing"
    if base in _DOUBLING:
        return base + base[-1] + "ing"
    return base + "ing"


def _forms(base: str) -> List[Tuple[str, str]]:
    """Inflections of a base verb → ``(form, kind)``, kind ``s``/``ed``/``ing``.
    For MATCHING only, so over-generation is harmless."""
    out = [(base + "s", "s"), (base + "es", "s"), (_gerund(base), "ing")]
    if base.endswith("y") and len(base) > 2 and base[-2] not in "aeiou":
        out += [(base[:-1] + "ies", "s"), (base[:-1] + "ied", "ed")]
    if base.endswith("e"):
        out.append((base + "d", "ed"))
    else:
        out.append((base + "ed", "ed"))
    if base in _DOUBLING:
        out += [(base + base[-1] + "ed", "ed"), (base + base[-1] + "ing", "ing")]
    return out


#: inflected form → (base, kind), across every verb above.
_INFLECTED: Dict[str, Tuple[str, str]] = {}
for _base in sorted(_VERBS) + sorted(_STATE_VERBS):
    for _form, _kind in _forms(_base):
        _INFLECTED.setdefault(_form, (_base, _kind))
for _form, _base in _IRREGULAR_PAST.items():
    _INFLECTED.setdefault(_form, (_base, "ed"))


def _display(base: str) -> str:
    return _CANONICAL.get(base, base[:1].upper() + base[1:])


# --------------------------------------------------------------------------- #
# Tokens
# --------------------------------------------------------------------------- #


class _Tok(NamedTuple):
    text: str  # as the user wrote it (edge punctuation stripped)
    key: str  # lower-case, apostrophes removed — what the tables match


_EDGE = "\"'`“”‘’()[]{}<>,.;:!?*_~"


def _key(word: str) -> str:
    return word.lower().replace("'", "").replace("’", "").strip(_EDGE)


def _tokens(text: str) -> List[_Tok]:
    """Words of one clause, shorthand respelled (``pls`` → ``please``);
    bare punctuation and ``--flags`` dropped."""
    out: List[_Tok] = []
    for raw in text.split():
        word = raw.strip(_EDGE)
        if not word or word.startswith("--") or not any(c.isalnum() for c in word):
            continue
        k = _key(word)
        repl = _RESPELL.get(k)
        if repl and repl != k:
            out.extend(_Tok(p, p) for p in repl.split())
        else:
            out.append(_Tok(word, k))
    return out


def _keys(toks: Sequence[_Tok]) -> Tuple[str, ...]:
    return tuple(t.key for t in toks)


def _starts(keys: Sequence[str], phrase: Sequence[str], at: int = 0) -> bool:
    return tuple(keys[at : at + len(phrase)]) == tuple(phrase)


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #

_ENVELOPE = re.compile(
    r"^\s*\[MindFlock\s+(PEER\s+message|message|result|note)\b([^\]]*)\]\s*"
)
_REPLY_HINT = re.compile(
    r"\s*(\(Reply only if asked.*$|\(reply: [^)]*\)\s*$|…\s*\(full text: call [^)]*\)\s*$)",
    re.S,
)
_WORKER = re.compile(r'from worker "([^"]+)"')
_BLOCK_TAGS = re.compile(
    r"<(pasted_content|system-reminder|command-[\w-]+|local-command-[\w-]+|"
    r"bash-[\w-]+|task-notification|user-prompt-submit-hook)\b[^>]*>.*?"
    r"(</\1>|\Z)",
    re.S,
)
_FENCE = re.compile(r"```.*?(```|\Z)", re.S)
_PLACEHOLDER = re.compile(r"\[(Image|Pasted text|Pasted image)[^\]]*\]", re.I)
_PAREN = re.compile(r"\([^()]*\)")
_URL = re.compile(r"\bhttps?://[^\s<>()\"']+", re.I)
_GH_PULL = re.compile(r"github\.com/[^/\s]+/[^/\s]+/pull/(\d+)", re.I)
_GH_ISSUE = re.compile(r"github\.com/[^/\s]+/[^/\s]+/issues/(\d+)", re.I)
_SHORTCUT = re.compile(r"shortcut\.com/[^\s]*/story/(\d+)", re.I)
_PATHISH = re.compile(r"(?<![\w@])(?:~/|\.{1,2}/|/)?(?:[\w.@+-]+/){1,}[\w.@+:-]*")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_MD_MARK = re.compile(r"(\*\*|__|`)")
_RULE_LINE = re.compile(r"={3,}|-{4,}|_{4,}|\*{4,}")
_WS = re.compile(r"[ \t ]+")


def _url_label(m: "re.Match[str]") -> str:
    url = m.group(0)
    for pat, fmt in (
        (_GH_PULL, "PR #%s"),
        (_GH_ISSUE, "issue #%s"),
        (_SHORTCUT, "sc-%s"),
    ):
        hit = pat.search(url)
        if hit:
            return fmt % hit.group(1)
    host = re.sub(r"^https?://(www\.)?", "", url, flags=re.I).split("/")[0]
    return "artifact" if host.endswith("claude.ai") else host


def _path_label(m: "re.Match[str]") -> str:
    """``~/a/b/c.py`` → ``c.py``; but ``full/url`` or ``and/or`` (two short
    words) stays as typed."""
    path = m.group(0)
    if path.count("/") == 1 and not path.startswith(("~", ".", "/")):
        return path
    base = path.rstrip("/:").rsplit("/", 1)[-1].split("::")[0]
    return base or path


def _strip_symbols(text: str) -> str:
    """Emoji, pictographs and control characters go; letters of every script
    stay."""
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat in ("So", "Sk", "Cs", "Co", "Cn") or "︀" <= ch <= "️":
            out.append(" ")
        elif cat == "Cc" and ch not in "\n\t":
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


#: Line heads of pasted terminal output (compared case-sensitively).
_OUTPUT_HEADS = (
    "$ ",
    "% ",
    "➜",
    "Traceback",
    'File "',
    "FAILED",
    "PASSED",
    "ERROR",
    "E  ",
    "WARNING",
    "INFO",
    "DEBUG",
    "zsh:",
    "bash:",
    "fatal:",
    "remote:",
    "diff --git",
    "+++",
    "---",
    "@@",
)
_CODE_SIGNS = re.compile(r"\w\(|\)\.\w|[{}]|(?<!=)==(?!=)|!=|=>|->|;\s*$")
_CODE_HEAD = re.compile(r"^\s*(def|class|import|from \S+ import|return)\s")
_PROSE_PUNCT = set(" .,;:!?'\"-/()&+#’“”‘@")


def _is_prose(line: str) -> bool:
    """Whether a line reads as something a person typed, not pasted output."""
    s = line.strip()
    if not s or s.startswith(_OUTPUT_HEADS):
        return False
    if "::" in s and " " not in s.split("::")[0][-40:]:
        return False  # pytest node ids
    if re.match(r"^\d+ (failed|passed|errors?|warnings?|skipped)\b", s):
        return False  # pytest summary
    if not re.search(r"[^\W\d_]{2,}", s):
        return False
    odd = sum(1 for c in s if not (c.isalnum() or c in _PROSE_PUNCT))
    return odd / len(s) < 0.15


def _is_code(line: str) -> bool:
    """Pasted source: two code-ish marks (``x(``, ``).``, braces, ``==`` …)
    or a ``def``/``import`` head. One mark is prose ("the {name} pod")."""
    line = re.sub(r"\{[\w-]+\}", "", line)  # a "{name}" placeholder is prose
    return len(_CODE_SIGNS.findall(line)) >= 2 or bool(_CODE_HEAD.match(line))


def _normalise(text: str) -> str:
    """Prose only: blocks, fences, output, links, paths and symbols reduced."""
    t = text[:MAX_INPUT_CHARS]
    t = _BLOCK_TAGS.sub("\n", t)
    t = _FENCE.sub("\n", t)
    t = "\n".join("" if _is_code(line) else line for line in t.splitlines())
    t = _PLACEHOLDER.sub(" ", t)
    t = _MD_LINK.sub(r"\1", t)
    t = _URL.sub(_url_label, t)
    t = _PATHISH.sub(_path_label, t)
    t = _strip_symbols(t)
    t = _MD_MARK.sub("", t)
    # Parentheticals are asides: "(task ws5crl7a0)", "(one PR, both)".
    for _ in range(3):
        t2 = _PAREN.sub(" ", t)
        if t2 == t:
            break
        t = t2
    lines = []
    for line in t.splitlines():
        s = _RULE_LINE.split(line)[0].strip()
        if s.startswith((">", "|")):
            lines.append("")  # quoted text, table rows
            continue
        s = re.sub(r"^\s*(?:[-*+•]|\d+[.)])\s+", "", s)  # list bullets
        lines.append(_WS.sub(" ", s) if _is_prose(s) else "")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Sentences and clauses
# --------------------------------------------------------------------------- #


class _Sentence(NamedTuple):
    text: str
    question: bool
    boost: float  # + under a "Task:" label, − in a background section


_SENT_SPLIT = re.compile(r"(?<=[.!?;])\s+|(?<=\?)(?=\S)")
_LABEL = re.compile(r"^\s*#*\s*([A-Za-z][A-Za-z ]{0,30}?)\s*:\s*(.*)$")
_TICKET_ID = re.compile(r"^(?:[A-Za-z]{1,8}-\d+|#\d+)$")


def _sentences(text: str) -> List[_Sentence]:
    out: List[_Sentence] = []
    section = 0.0  # boost carried by the lines under a bare label
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            if section < 0:
                section = 0.0
            continue
        heading = line.startswith("#")
        boost = section
        m = _LABEL.match(line)
        labelled = False
        if m:
            label = m.group(1).strip().lower()
            rest = m.group(2).strip()
            if label in _TASK_LABELS:
                boost, line, labelled = 1.5, rest, True
                if not line:
                    section = 1.0
                    continue
            elif label in _CONTEXT_LABELS:
                boost, line, labelled = -2.0, rest, True
                if not line:
                    section = -2.0
                    continue
        if heading:
            line = line.lstrip("#").strip()
            if len(line.split()) < 2:
                continue  # "## Description"
            boost = max(boost, 0.5)
        if not labelled:
            line = re.sub(r"\s*:\s+", ". ", line)
        words = line.split()
        while words and _TICKET_ID.match(words[0].strip("\"'")) and boost > 0:
            words = words[1:]  # "Your story: sc-123 "Title"" → the title
        line = " ".join(words)
        for part in _SENT_SPLIT.split(line):
            part = part.strip()
            if not part:
                continue
            q = part.endswith("?")
            out.append(_Sentence(part.rstrip(".!?;: ").strip(), q, boost))
            if len(out) >= MAX_SENTENCES:
                return out
    return out


#: Cut a run-on sentence before these ("I merged a pr can you tell me …").
_REQUEST_MARKERS = _phrases("""
    can you|could you|would you|i want you to|i need you to|you need to|
    you should|please|lets|then|so i can|so we can|so that|because|since|
    but|although|though|once you|after that|when done|if so|if you|if need|
    if needed|if possible|why|is it possible|what are|what is|
    how do|how does|how can|where is|where are|i want|i need|make sure|
    when i|instead of|is there|is that|does that|did we|are we|how are|
    how is|
    i get the weird|i get a weird|i get weird|i get an error|i miss|i dont like|i dont love|i hate|we need to|i would like
    """)
#: Clauses that begin like this are side remarks, not the ask.
_SIDE_CLAUSES = _phrases("""
    so i can|so we can|so that|because|since|although|though|if you|
    if need|if needed|if possible|when done|when i|instead of|once you|
    is that possible|does that already exist|is that
    """)


def _strip_filler(toks: Sequence[_Tok]) -> List[_Tok]:
    out = [t for t in toks if t.key not in _NOISE_WORDS]
    changed = True
    while changed and out:
        changed = False
        keys = _keys(out)
        for phrase in _FILLER:
            if _starts(keys, phrase):
                out = out[len(phrase) :]
                changed = True
                break
        # "Instead of doing that, …" / "although it was good, …"
        if out and out[0].key == "instead" and len(out) > 2 and out[1].key == "of":
            j = 2
            while j < len(out) and j < 5 and out[j].key not in _VAGUE_WORDS:
                j += 1
            out = out[j + 1 :]
            changed = True
    return out


def _verb_at(toks: Sequence[_Tok], i: int = 0) -> Optional[Tuple[str, str, int]]:
    """``(display, class, words used)`` when an action verb starts at ``i``."""
    keys = _keys(toks)
    for phrase, disp, cls in _PHRASAL:
        if _starts(keys, phrase, i):
            return disp, cls, len(phrase)
    if i >= len(keys):
        return None
    k = keys[i]
    cls = _VERBS.get(k)
    if not cls:
        return None
    nxt = keys[i + 1] if i + 1 < len(keys) else ""
    if nxt in _NOUN_AFTER.get(k, ()):
        return None  # "merge conflicts", "test suite", "release notes"
    # "set this up", "figure it out" — particle after a pronoun object.
    if i + 2 < len(keys) and keys[i + 1] in _PARTICLE_OBJECTS:
        disp = _SEPARABLE.get((k, keys[i + 2]))
        if disp:
            return disp, cls, 1
    return _display(k), cls, 1


def _separable_object(toks: List[_Tok]) -> List[_Tok]:
    """``this up like v1`` → ``this like v1`` (the particle of "set this up")."""
    if len(toks) >= 2 and toks[0].key in _PARTICLE_OBJECTS:
        if toks[1].key in ("up", "out", "back", "down", "off", "on", "forward"):
            # "set this up like the staging one" → "like staging one"
            return list(toks[2:]) or [toks[0]]
    return toks


def _starts_action(toks: Sequence[_Tok]) -> bool:
    s = _strip_filler(toks)
    if not s:
        return False
    if _verb_at(s):
        return True
    keys = _keys(s)
    return any(_starts(keys, m) for m in _REQUEST_MARKERS)


def _clauses(sent: _Sentence) -> List[Tuple[List[_Tok], bool]]:
    """``(tokens, side_remark)`` per clause. Cuts at ``,``/``and``/``&`` only
    when an action follows, and before mid-sentence request markers."""
    text = re.sub(r"\s+as well as\s+", " and ", sent.text, flags=re.I)
    pieces = re.split(r"(,\s*|\s+and\s+|\s+&\s+)", text)
    clauses: List[List[_Tok]] = []
    cur: List[_Tok] = []
    sep = ""
    for piece in pieces:
        if re.fullmatch(r",\s*|\s+and\s+|\s+&\s+", piece or ""):
            sep = "," if piece.strip() == "," else "and"
            continue
        toks = [t for t in _tokens(piece) if t.key not in _NOISE_WORDS]
        if sep and cur and toks and _starts_action(toks):
            clauses.append(cur)
            cur = []
        elif sep and cur and toks:
            # Not a new action: the separator joins a noun list ("articles
            # and posts", "billing, search").
            cur.append(_Tok(sep, sep))
        sep = ""
        cur.extend(toks)
    if cur:
        clauses.append(cur)
    out: List[Tuple[List[_Tok], bool]] = []
    for c in clauses:
        keys = _keys(c)
        start, i = 0, 1
        while i < len(keys):
            hit = next((m for m in _REQUEST_MARKERS if _starts(keys, m, i)), None)
            # "please" right after a verb is politeness, not a new clause;
            # "what do we …" is one question.
            if (
                hit == ("please",)
                and i == start + 1
                or _WH & set(keys[max(start, i - 2) : i])
                # "do i need more pods" is one question, not "do | i need …"
                or (
                    keys[i - 1] in ("do", "does", "did")
                    and keys[i] in _SUBJECT_PRONOUNS
                )
            ):
                hit = None
            if not hit and i > start and _run_on_at(c, i, start):
                hit = (keys[i],)
            if hit:
                out.append((c[start:i], _is_side(c[start:i])))
                start = i
                i += len(hit)
                continue
            i += 1
        if c[start:]:
            out.append((c[start:], _is_side(c[start:])))
    return out


def _run_on_at(toks: Sequence[_Tok], i: int, start: int = 0) -> bool:
    """Whether a new action starts at ``i`` in a run-on clause: a verb right
    after a pronoun object ("run all of these follow the manual"), or a
    shipping step ("… commit make a PR of it", "… working on merge and cut")."""
    keys = _keys(toks)
    prev = keys[i - 1]
    nxt = keys[i + 1] if i + 1 < len(keys) else ""
    v = _verb_at(toks, i)
    if not v:
        return False
    for j in range(max(start, i - 3), i):  # inside "cut a new release"
        inner = _verb_at(toks, j)
        if inner and j + inner[2] > i:
            return False
    if prev in ("it", "them", "everything") and v[1] in (WORK, LOOK, SHIP, META):
        return True
    if (
        nxt in ("it", "them", "everything")
        and v[1] in (WORK, LOOK, SHIP)
        and prev not in _STOP
    ):
        return True  # "enable the scan run it"
    if prev in ("these", "those") and v[1] in (WORK, LOOK, SHIP, META):
        # "all of these follow the manual" — but "these merge conflicts"
        return nxt in _ARTICLES | _POSSESSIVES | _DEMONSTRATIVES or not nxt
    if (
        v[1] == SHIP
        and prev not in _PREPOSITIONS | _ARTICLES | _POSSESSIVES | _DEMONSTRATIVES
    ):
        return (
            v[2] > 1
            or not nxt
            or nxt in _SHIP_STOP
            or nxt in ("and", "to", "it", "them")
        )
    return False


def _is_side(toks: Sequence[_Tok]) -> bool:
    keys = _keys(toks)
    return any(_starts(keys, m) for m in _SIDE_CLAUSES)


# --------------------------------------------------------------------------- #
# Objects
# --------------------------------------------------------------------------- #

#: An object ends at these (a new clause the splitter could not see).
_OBJECT_STOPS = frozenset(
    """so because since if when while once before after until unless but
    though although instead which whereas cuz bc then whether""".split()
)
#: Verbs whose "to" names a destination, not a purpose: "rename X to Y".
_TO_VERBS = frozenset(
    """rename move change convert migrate port upgrade downgrade switch set
    bump add push merge deploy send point route redirect copy pin revert
    reset restore roll back""".split()
)
_LEADING_STOPS = frozenset(
    "before after because since unless until though although whereas".split()
)
_SHIP_STOP = frozenset("commit push pr merge release deploy rebase tag".split())


def _trim_tails(toks: List[_Tok]) -> List[_Tok]:
    changed = True
    while changed and toks:
        changed = False
        keys = _keys(toks)
        for phrase in _TAILS:
            n = len(phrase)
            if n < len(keys) and tuple(keys[-n:]) == phrase:
                toks = toks[:-n]
                changed = True
                break
    return toks


def _drop_determiners(toks: Sequence[_Tok]) -> List[_Tok]:
    """Articles and possessives anywhere; quantifiers and demonstratives at
    the start of a noun phrase — unless that leaves nothing (then the vague
    word IS the object)."""
    out: List[_Tok] = []
    keys = _keys(toks)
    i = 0
    while i < len(toks):
        k = keys[i]
        at_np = not out or out[-1].key in _PREPOSITIONS or out[-1].key in (",", "and")
        if at_np:
            q = next((p for p in _QUANTIFIERS if _starts(keys, p, i)), None)
            if q and i + len(q) < len(toks):
                i += len(q)
                continue
        if (k in _ARTICLES or k in _POSSESSIVES) and i + 1 < len(toks):
            i += 1
            continue
        if at_np:
            if k in _DEMONSTRATIVES and i + 1 < len(toks) and keys[i + 1] not in _STOP:
                i += 1
                continue
        out.append(toks[i])
        i += 1
    return out


def _object(toks: Sequence[_Tok], purpose_stop: bool = True) -> List[_Tok]:
    """The words after a verb, up to where its clause really ends."""
    toks = [t for t in toks if t.key not in _NOISE_WORDS]
    keys = _keys(toks)
    toks = [  # "as well" is never part of the object
        t
        for i, t in enumerate(toks)
        if not (keys[i] == "as" and keys[i + 1 : i + 2] == ("well",))
        and not (keys[i] == "well" and i and keys[i - 1] == "as")
    ]
    keys = _keys(toks)
    out: List[_Tok] = []
    content = 0
    for i, t in enumerate(toks):
        k = keys[i]
        prev = keys[i - 1] if i else ""
        nxt = keys[i + 1] if i + 1 < len(keys) else ""
        if k in _LEADING_STOPS:
            break  # "… as well before making the PR"
        if i > 0:
            if k in _OBJECT_STOPS:
                break
            if (
                k == "that"
                and prev not in _PREPOSITIONS
                and nxt not in ("one", "thing")
            ):
                break
            if k == "where" and prev not in _INTRO_NOUNS:
                break
            if k in ("what", "like") and nxt in _SUBJECT_PRONOUNS | {"how"}:
                break
            if k in ("i", "we", "you", "they", "it") and (
                nxt in _AUX
                or nxt in _VERBS
                or nxt in _INFLECTED
                or nxt in ("think", "feel", "want", "need")
            ):
                break  # a new subject: "skip hinge, i dont think …"
            if (
                k in _SHIP_STOP
                and prev
                not in _PREPOSITIONS | _ARTICLES | _POSSESSIVES | _DEMONSTRATIVES
                and (not nxt or nxt in _SHIP_STOP or nxt in ("and", "to", "it", "them"))
            ):
                break
            v = _verb_at(toks, i)
            if v and v[1] == SHIP and v[2] > 1:
                break  # "… make a PR"
            if purpose_stop and k == "to" and content >= 2 and nxt in _VERBS:
                break  # "write the SQL query to get rows" → "SQL query"
            if k in ("for", "with") and nxt in ("me", "us") and i + 2 >= len(keys):
                break
        if k not in _STOP:
            content += 1
        out.append(t)
    out = _trim_tails(out)
    return _intro_noun(out)


def _intro_noun(toks: List[_Tok]) -> List[_Tok]:
    """ "the bug where the pin bar draws below X" → "pin bar drawing below X"."""
    keys = _keys(toks)
    for i, k in enumerate(keys[:4]):
        if k in _INTRO_NOUNS and i + 1 < len(keys) and keys[i + 1] in _INTRO_LINKS:
            j = i + 2
            if keys[i + 1] == "in" and j < len(keys) and keys[j] == "which":
                j += 1
            rest = list(toks[j:])
            if len([t for t in rest if t.key not in _STOP]) < 2:
                return toks
            return _gerundize(rest)
    return toks


def _gerundize(toks: List[_Tok]) -> List[_Tok]:
    """The first predicate after a subject becomes ``-ing`` and loses its
    auxiliary: "pin bar draws below" → "pin bar drawing below", "X is slow"
    → "X slow", "X doesnt work" → "X not working"."""
    keys = _keys(toks)
    out = list(toks)
    for i in range(1, len(keys)):
        k, prev = keys[i], keys[i - 1]
        if prev in _ARTICLES or prev in _DEMONSTRATIVES or prev in _POSSESSIVES:
            continue
        if k in ("doesnt", "dont", "isnt", "arent", "cant", "wont"):
            nxt = keys[i + 1] if i + 1 < len(keys) else ""
            base = _INFLECTED.get(nxt, (nxt, ""))[0] if nxt else ""
            if base in _VERBS or base in _STATE_VERBS:
                g = _gerund(base)
                return out[:i] + [_Tok("not", "not"), _Tok(g, g)] + out[i + 2 :]
            return out[:i] + [_Tok("not", "not")] + out[i + 1 :]
        if k in ("is", "are", "was", "were"):
            return out[:i] + out[i + 1 :]
        inf = _INFLECTED.get(k)
        if inf and inf[1] == "s" and (inf[0] in _STATE_VERBS or inf[0] in _VERBS):
            g = _gerund(inf[0])
            return out[:i] + [_Tok(g, g)] + out[i + 1 :]
    return out


def _is_verbish(keys: Sequence[str], i: int) -> bool:
    """Whether ``keys[i]`` is a predicate (ends a leading noun phrase). A
    word right after a determiner is a noun ("the tests"), and an ``-s``
    word with nothing after it is a plural ("scenario profiles")."""
    if i == 0:
        return False
    k, prev = keys[i], keys[i - 1]
    if k in _AUX and prev not in _ARTICLES:
        return True
    if prev in _ARTICLES or prev in _POSSESSIVES or prev in _DEMONSTRATIVES:
        return False
    if prev in ("few", "many", "some", "all", "of", "more", "other", "new", "and"):
        return False
    if k in _AUX:
        return True
    if k in ("seem", "seems", "looks", "keeps", "gets", "goes", "says", "needs"):
        return True
    inf = _INFLECTED.get(k)
    if not inf:
        return False
    if inf[1] == "ed":
        return True
    if inf[1] == "s":
        nxt = keys[i + 1] if i + 1 < len(keys) else ""
        return bool(nxt) and nxt not in _AUX
    return False


def _leading_np(toks: Sequence[_Tok], stop_at_prep: bool = False) -> List[_Tok]:
    """The noun phrase a clause opens with; ``stop_at_prep`` also ends it at a
    preposition and lets ``and`` join ("193 and 192 merged")."""
    keys = _keys(toks)
    for i in range(len(keys)):
        k = keys[i]
        if (
            k in _OBJECT_STOPS
            or (i and k == "to")
            or (i and k == "and" and not stop_at_prep)
        ):
            return list(toks[:i])
        if stop_at_prep and i and k in _PREPOSITIONS and k != "of":
            return list(toks[:i])
        if _is_verbish(keys, i):
            return list(toks[:i])
    return list(toks)


def _content(toks: Sequence[_Tok]) -> List[_Tok]:
    return [t for t in toks if t.key not in _STOP and t.key not in _VAGUE_WORDS]


def _specific(t: _Tok) -> bool:
    """A name rather than a word: digits, ``#``, ``-``/``_``/``.`` inside,
    CamelCase or an unusual acronym (not PR/CI/UI …)."""
    w = t.text
    if t.key in _ACRONYMS:
        return False
    if any(c.isdigit() for c in w) or "#" in w:
        return True
    if re.search(r"\w[-_.]\w", w):
        return True
    return len(w) > 1 and w[1:] != w[1:].lower() and t.key not in _STOP


def _is_vague(obj: Sequence[_Tok]) -> bool:
    content = _content(obj)
    return not content or all(t.key in _GENERIC_NOUNS for t in content)


# --------------------------------------------------------------------------- #
# Clause readings
# --------------------------------------------------------------------------- #


class _Cand(NamedTuple):
    verb: str
    obj: Tuple[_Tok, ...]
    cls: str
    score: float
    order: int
    #: Whether the reading is trustworthy as a constructed phrase: True (the
    #: rule vouches for its grammar), False (a loose reading — show the
    #: person's own words instead), None (judge the object, :func:`_tidy`).
    clean: Optional[bool] = None


def _cand(
    verb: str,
    obj: Sequence[_Tok],
    cls: str,
    order: int,
    boost: float,
    clean: Optional[bool] = None,
) -> _Cand:
    obj = _trim_tails(_drop_determiners(obj))
    while obj and obj[0].key in (",", "and"):
        obj = obj[1:]
    if obj and obj[0].key not in _EMBEDDED:
        # "option to confirm I'm happy with session" → "option to confirm":
        # a new subject inside an object starts the sentence's next clause.
        cut = next((i for i, t in enumerate(obj) if t.key in _LEFTOVER), len(obj))
        if cut >= 2:
            obj = _trim_tails(list(obj[:cut]))
    score = _score(obj, cls, order, boost) - (0.3 if clean is False else 0.0)
    return _Cand(verb, tuple(obj), cls, score, order, clean)


#: Words that, left inside an object, mean the reading kept a fragment of
#: the sentence around it ("Make it give me count", "Fix stuff I'm confused").
_LEFTOVER = frozenset("i im ive id we you youre they me us he she".split())


def _tidy(c: _Cand) -> bool:
    """Whether a candidate renders as a clean phrase (judged on the words
    :func:`_render` will actually show)."""
    if c.clean is not None:
        return c.clean
    keys = [t.key for t in _shown(c.verb, c.obj)]
    if not keys:
        # "Open argo PR", "Roll forward": the verb phrase is the whole task.
        return c.verb.startswith("Open ") or c.verb.split()[0] in _INTRANSITIVE_OK
    if keys[0] in _EMBEDDED or c.verb in ("Explain how", "Explain what"):
        return True  # "what keys I'm missing", "if there was a cache clear"
    if any(k in _LEFTOVER for k in keys):
        return False
    if c.verb != "Ensure" and any(k in _AUX for k in keys):
        return False  # "is", "doesnt" left in: a clause, not an object
    first = _INFLECTED.get(keys[0])
    if keys[0] == "going" or (first and first[1] == "ed"):
        return False  # "Pull requested", "Investigate going to take"
    if c.cls == LOOK and keys[0] in _VERB_STARTS and c.verb.split()[-1] in _LOOK_HEADS:
        return False  # "Explain keeps file up to date": the subject is lost
    if len(keys) > 1 and keys[0] in _POINTERS:
        # "it in the VPC" is an object; "it give me count" is a clause.
        return keys[1] in _PREPOSITIONS or keys[1] in _PARTICLES
    return True


#: Object-initial words that point instead of naming.
_POINTERS = frozenset("it this that them these those there here one ones".split())
#: Verbs that, opening a question's object, show its subject was lost.
_VERB_STARTS = frozenset(
    """see need get make do go know have want use take keep say run keeps
    makes does says means needs has goes gets saying seeing""".split()
)
#: LOOK verbs whose object must name a thing, not start with a verb.
_LOOK_HEADS = frozenset("Explain Investigate Check Find Identify Review".split())


#: Words that open an embedded question — the object is a clause on purpose.
_EMBEDDED = frozenset("what how which why where if whether who when".split())
_PARTICLES = frozenset("up out back down off on forward".split())


def _score(obj: Sequence[_Tok], cls: str, order: int, boost: float) -> float:
    s = _CLASS_WEIGHT[cls] + boost - 0.22 * min(order, 24)
    content = _content(obj)
    if content and not _is_vague(obj):
        s += 0.6
        if any(_specific(t) for t in content):
            s += 0.4 if cls != SHIP else 1.2
    elif cls != SHIP:
        s -= 2.5
    return s


def _read_imperative(toks: List[_Tok], order: int, boost: float) -> Optional[_Cand]:
    keys = _keys(toks)
    v = _verb_at(toks)
    if not v:
        # "You are analyzing staged changes" → "Analyze staged changes".
        if len(keys) > 2 and keys[0] == "you" and keys[1] in ("are", "were"):
            inf = _INFLECTED.get(keys[2])
            if inf and inf[1] == "ing" and inf[0] in _VERBS:
                return _cand(
                    _display(inf[0]), _object(toks[3:]), _VERBS[inf[0]], order, boost
                )
        return None
    disp, cls, n = v
    rest = toks[n:]
    rkeys = _keys(rest)
    low = disp.lower()
    if low == "make" and rkeys:
        return _read_make(rest, order, boost)
    if low in ("get", "keep") and len(rkeys) > 1:
        verb = {"working": "Fix", "fixed": "Fix", "merged": "Merge"}.get(rkeys[-1])
        if verb:
            return _cand(verb, _object(rest[:-1]), WORK, order, boost)
    if low in ("do", "take", "give") and rkeys:
        # "do a thorough scan of X" → "Scan X"; "take a look at X" → "Review X".
        nominal = _action_noun(rest)
        if nominal:
            return _cand(*nominal, order, boost)
    if low in ("create", "file", "open", "write", "ticket"):
        t = _read_ticket(rest if low != "ticket" else toks, order, boost)
        if t:
            return t
    separated = _separable(toks)
    if separated is not None:
        rest = separated
    obj = _object(rest, purpose_stop=low not in _TO_VERBS)
    # A meta verb aimed at a brief ("read X.md first and follow it") is a
    # preamble, not the task.
    if low in ("read", "follow", "show") and (
        any(re.search(r"\.(md|txt|rst)$", t.key) for t in obj) or "first" in rkeys
    ):
        cls = META
    if low == "do" and _is_vague(obj):
        cls = VAGUE
    if low == "use":
        cls = META
    return _cand(disp, obj, cls, order, boost)


def _separable(toks: List[_Tok]) -> Optional[List[_Tok]]:
    keys = _keys(toks)
    if (
        len(keys) > 2
        and keys[1] in _PARTICLE_OBJECTS
        and (keys[0], keys[2]) in _SEPARABLE
    ):
        return _separable_object(toks[1:])
    return None


def _action_noun(rest: Sequence[_Tok]) -> Optional[Tuple[str, List[_Tok], str]]:
    """``a thorough scan of X`` → ``("Scan", X, WORK)``."""
    keys = _keys(rest)
    # an article, then at most two adjectives: "a (quick|thorough) scan"
    if not keys or keys[0] not in _ARTICLES:
        return None
    i = 1
    while i < len(keys) and i < 4 and keys[i] not in _ACTION_NOUNS:
        if keys[i] in _VERBS or keys[i] in _AUX or keys[i] in _PREPOSITIONS:
            return None
        i += 1
    if i < len(keys) and keys[i] in _ACTION_NOUNS:
        verb, cls = _ACTION_NOUNS[keys[i]]
        tail = list(rest[i + 1 :])
        if tail and tail[0].key in ("of", "at", "on", "for"):
            tail = tail[1:]
        return verb, _object(tail), cls
    return None


def _read_ticket(rest: Sequence[_Tok], order: int, boost: float) -> Optional[_Cand]:
    """ "make a ticket so that X happens" → "Ticket: X"; "ticket this" →
    "File ticket"."""
    keys = _keys(rest)
    at = next((i for i, k in enumerate(keys[:4]) if k in ("ticket", "tickets")), -1)
    if at < 0:
        return None
    inner = list(rest[at + 1 :])
    while inner and inner[0].key in (
        "for",
        "to",
        "about",
        "so",
        "that",
        "saying",
        "where",
        "which",
        "this",
        "it",
        "we",
        "need",
        "and",
        "me",
        "set",
        "in",
        "the",
    ):
        inner = inner[1:]
    c = _read_clause(_strip_filler(inner), False, order, boost) if inner else None
    if c and not _is_vague(c.obj) and c.cls in (WORK, LOOK):
        verb = c.verb[:1].lower() + c.verb[1:]
        return _Cand("Ticket: " + verb, c.obj, WORK, c.score + 0.2, order)
    obj = _object(inner)
    if not _is_vague(obj):
        return _cand("Ticket", obj, WORK, order, boost)
    return _cand("File ticket", (), VAGUE, order, boost)


def _read_make(rest: List[_Tok], order: int, boost: float) -> Optional[_Cand]:
    """ "make X faster" → "Speed up X"; "make a copy of X" → "Copy X";
    "make X a PR" → "Open PR"; "make sure …" → :func:`_read_make_sure`."""
    keys = _keys(rest)
    if keys[:1] in (("sure",), ("certain",)):
        return _read_make_sure(rest[1:], order, boost)
    if keys[:2] in (("it", "happen"), ("it", "so"), ("it", "work"), ("that", "happen")):
        return _cand("Make it happen", (), VAGUE, order, boost)
    t = _read_ticket(rest, order, boost)
    if t:
        return t
    pr_at = next((i for i, k in enumerate(keys[:5]) if k in ("pr", "prs")), -1)
    if pr_at > 0 and keys[pr_at + 1 : pr_at + 2] not in (
        (),
        ("for",),
        ("to",),
        ("into",),
        ("of",),
        ("if",),
        ("and",),
        ("so",),
        ("on",),
        ("with",),
        ("that",),
    ):
        pr_at = -1  # "make a single PR stack" — PR is an adjective there
    obj = list(rest[: pr_at + 1]) if pr_at > 0 else _object(rest)
    okeys = _keys(obj)
    if okeys and okeys[-1] in ("pr", "prs") and len(okeys) <= 4:
        # "make this a PR", "make an argo pr" → "Open (argo) PR"
        mid = [
            t for t in obj[:-1] if t.key not in _STOP and t.key not in _DEMONSTRATIVES
        ]
        verb = "Open " + " ".join([t.text for t in mid] + ["PR"])
        # "Open terraform PR" names the work; a bare "Open PR" is a step.
        return _cand(
            verb, (), WORK if mid else SHIP, order, boost + (0.6 if mid else 0.5)
        )
    nominal = _action_noun(rest)
    if nominal and keys[:1] in (("a",), ("an",)):
        return _cand(*nominal, order, boost)
    for i, k in enumerate(okeys):
        if i > 0 and k in _COMPARATIVES:
            target = list(obj[:i])
            if okeys[i - 1] in ("more", "less"):
                target = target[:-1]
            if not _is_vague(target):
                return _cand(_COMPARATIVES[k], target, WORK, order, boost)
            break
        if i > 0 and k == "up" and okeys[i : i + 3] == ("up", "to", "date"):
            return _cand("Update", obj[:i], WORK, order, boost)
    return _cand("Make", obj, WORK, order, boost)


def _read_make_sure(toks: List[_Tok], order: int, boost: float) -> Optional[_Cand]:
    """ "make sure X is fixed" → "Fix X"; "… X gets to main" → "Get X to
    main"; "… X is up to date" → "Update X"; else "Ensure X …"."""
    toks = list(toks)
    if toks and toks[0].key == "that":
        toks = toks[1:]
    if not toks:
        return None
    v = _verb_at(toks)
    if v and v[1] != VAGUE:
        return _read_imperative(toks, order, boost)
    keys = _keys(toks)
    if (
        keys[:1] == ("there",)
        and len(keys) > 2
        and keys[1] in ("is", "are", "was", "were")
    ):
        # "make sure there is a ticket for this" → "File ticket" / "Ensure …"
        return _read_ticket(toks[2:], order, boost) or _cand(
            "Ensure", _object(toks[2:]), WORK, order, boost - 0.3
        )
    if (
        any(k in ("going", "running", "alive") for k in keys[1:5])
        and "still" in keys[:5]
    ):
        return _cand("Check progress", (), VAGUE, order, boost)  # "… still going"
    np = _leading_np(toks)
    j = len(np)
    if not np or j >= len(keys):
        return _cand("Ensure", _object(toks), WORK, order, boost - 0.3)
    if keys[j] in ("is", "are") and j + 1 < len(keys):
        inf = _INFLECTED.get(keys[j + 1])
        if (
            inf
            and inf[1] == "ing"
            and inf[0] in _VERBS
            and np[-1].key in _SUBJECT_PRONOUNS | {"youre", "your"}
        ):
            # "make sure you're updating the PRs" → "Update PRs"
            return _cand(
                _display(inf[0]), _object(toks[j + 2 :]), _VERBS[inf[0]], order, boost
            )
    k = keys[j]
    nxt = keys[j + 1] if j + 1 < len(keys) else ""
    if k in ("is", "are", "gets", "get", "was", "were", "has", "have") and nxt:
        if k in ("has", "have") and nxt == "been" and j + 2 < len(keys):
            j += 1
            nxt = keys[j + 1]
        inf = _INFLECTED.get(nxt)
        if inf and inf[1] == "ed" and inf[0] in _VERBS:
            return _cand(
                _display(inf[0]),
                np + _object(toks[j + 2 :]),
                _VERBS[inf[0]],
                order,
                boost,
            )
        if k in ("gets", "get") and nxt in ("to", "into", "onto", "in", "on"):
            return _cand("Get", np + _object(toks[j + 1 :]), WORK, order, boost)
        if keys[j + 1 : j + 4] == ("up", "to", "date"):
            return _cand("Update", np, WORK, order, boost)
        if k in ("is", "are"):
            return _cand("Make", np + _object(toks[j + 1 :]), WORK, order, boost)
    if k in ("goes", "lands", "makes") and nxt in (
        "to",
        "into",
        "onto",
        "in",
        "on",
        "it",
    ):
        return _cand("Get", np + _object(toks[j + 1 :]), WORK, order, boost)
    return _cand("Ensure", _object(toks), WORK, order, boost - 0.3)


_WISH_HEADS = _phrases("""
    i want|i need|i would like|id like|we want|we need|i also want|
    i really want|i just want|i want it so|i would love|id love
    """)


def _read_wish(toks: List[_Tok], order: int, boost: float) -> Optional[_Cand]:
    """ "I want X dropped from Y" → "Drop X from Y"; "I want X to be
    optional" → "Make X optional"; "I want X on the left" → "Put X on
    left"; "I want X to go to Y" → "Make X go to Y"."""
    keys = _keys(toks)
    head = next((h for h in _WISH_HEADS if _starts(keys, h)), None)
    if not head:
        return None
    rest = toks[len(head) :]
    rkeys = _keys(rest)
    if not rkeys:
        return None
    if rkeys[0] == "to":
        return _read_clause(_strip_filler(rest[1:]), False, order, boost)
    if rkeys[0] == "it" and len(rkeys) > 1 and rkeys[1] in ("so", "to"):
        return None
    np = _leading_np(rest, stop_at_prep=True)
    j = len(np)
    if not np:
        return None
    nxt = rkeys[j] if j < len(rkeys) else ""
    if not nxt or nxt in _OBJECT_STOPS:
        verb = "Use" if head[-1] in ("like", "love") else "Add"
        return _cand(verb, np, WORK, order, boost - 0.4)
    inf = _INFLECTED.get(nxt)
    if inf and inf[1] == "ed" and inf[0] in _VERBS:
        return _cand(
            _display(inf[0]), np + _object(rest[j + 1 :]), _VERBS[inf[0]], order, boost
        )
    if nxt == "to":
        if rkeys[j + 1 : j + 2] == ("be",):
            tail = _object(rest[j + 2 :])
            inf = _INFLECTED.get(tail[0].key) if tail else None
            if inf and inf[1] == "ed" and inf[0] in _VERBS:  # "this to be committed"
                return _cand(
                    _display(inf[0]), np + tail[1:], _VERBS[inf[0]], order, boost
                )
            if tail and tail[0].key in _PREPOSITIONS:
                return _cand("Put", np + tail, WORK, order, boost)
            return _cand("Make", np + tail, WORK, order, boost)
        return _cand("Make", np + _object(rest[j + 1 :]), WORK, order, boost - 0.2)
    if rkeys[j : j + 3] in (("as", "a", "pr"), ("in", "a", "pr")) or rkeys[
        j : j + 2
    ] == ("as", "prs"):
        return _cand(
            "Open PR", _object(rest[j + 3 :]), SHIP, order, boost
        )  # "I want them as a PR"
    if nxt in _PREPOSITIONS:
        return _cand("Put", np + _object(rest[j:]), WORK, order, boost - 0.2)
    return None


def _read_requirement(toks: List[_Tok], order: int, boost: float) -> Optional[_Cand]:
    """ "Peer sandbox needs to work on mac" → "Make peer sandbox work on mac";
    "it should be on the review page" → "Move to review page"; "it has to
    work for all frameworks" → "Support all frameworks"."""
    keys = _keys(toks)
    for i in range(1, min(len(keys), 10)):
        modal = next((m for m in _MODALS if _starts(keys, m, i)), None)
        if not modal:
            continue
        subject = _np_before(toks[:i])
        if not subject or subject[0].key in ("i", "we", "you", "im"):
            continue
        rest = list(toks[i + len(modal) :])
        rkeys = _keys(rest)
        if not rkeys or rkeys[0] in ("not", "never"):
            return None
        vague = _is_vague(subject)
        if modal[-1] == "be":
            if rkeys[0] in _PREPOSITIONS:
                where = _object(rest)
                if vague and where and where[0].key in ("on", "in", "at", "into"):
                    where = [_Tok("to", "to")] + where[1:]  # "Move to review page"
                return _cand(
                    "Move", ([] if vague else subject) + where, WORK, order, boost
                )
            inf = _INFLECTED.get(rkeys[0])
            if inf and inf[1] == "ed" and inf[0] in _VERBS:
                return _cand(
                    _display(inf[0]), subject + _object(rest[1:]), WORK, order, boost
                )
            return _cand("Make", subject + _object(rest), WORK, order, boost - 0.3)
        if rkeys[0] == "have" and len(rkeys) > 1:
            # "mindflock should have the ability to create folders" → "Add …"
            obj = _object(rest[1:])
            return _cand("Add", obj, WORK, order, boost)
        if (
            rkeys[0] == "work"
            and vague
            and len(rkeys) > 2
            and rkeys[1] in ("for", "with", "on", "in")
        ):
            return _cand("Support", _object(rest[2:]), WORK, order, boost)
        if (rkeys[0] in _VERBS or rkeys[0] in _STATE_VERBS) and rkeys[0] not in (
            "need",
            "have",
            "be",
        ):
            return _cand("Make", subject + _object(rest), WORK, order, boost - 0.3)
        return None
    return None


def _np_before(toks: Sequence[_Tok]) -> List[_Tok]:
    """The noun phrase that ends right before a predicate: "… for claude it"
    → "it", "the team settings" → "team settings"."""
    keys = _keys(toks)
    if keys and keys[-1] in _SUBJECT_PRONOUNS | _DEMONSTRATIVES:
        return [toks[-1]]
    j = len(keys)
    while j > 0 and j > len(keys) - 5:
        k = keys[j - 1]
        if (
            k in _AUX
            or k in _PREPOSITIONS
            or k in _SUBJECT_PRONOUNS
            or k in ("and", ",")
        ):
            break
        if _is_verbish(keys, j - 1) or (k in _VERBS and j - 1 > 0):
            break
        j -= 1
    return _drop_determiners(_strip_filler(toks[j:]))


_WH = frozenset("why how what where which who when whats wheres hows whys".split())


def _squeeze(ts: Sequence[_Tok]) -> List[_Tok]:
    """Question words → a noun-ish object: no aux, pronouns or adverbs."""
    drop = (
        _AUX
        | _SUBJECT_PRONOUNS
        | _INTENSIFIERS
        | frozenset("even already yet ever right much".split())
    )
    return [t for t in _object(ts, purpose_stop=False) if t.key not in drop]


def _read_question(toks: List[_Tok], order: int, boost: float) -> Optional[_Cand]:
    keys = _keys(toks)
    if not keys:
        return None
    w = keys[0]
    rest = list(toks[1:])
    rkeys = _keys(rest)

    def no_aux(ts: List[_Tok]) -> List[_Tok]:
        while ts and (ts[0].key in _AUX or ts[0].key in _SUBJECT_PRONOUNS):
            ts = ts[1:]
        return ts

    if w in ("why", "whys"):
        r = no_aux(rest)
        rk = _keys(r)
        # "why is the server using so much memory" → "server memory use"
        for i, k in enumerate(rk):
            if k in ("using", "uses", "eating", "eats") and i > 0:
                tail = [
                    t
                    for t in r[i + 1 :]
                    if t.key not in _STOP and t.key not in _INTENSIFIERS
                ]
                if tail:
                    return _cand(
                        "Investigate",
                        r[:i] + tail[:2] + [_Tok("use", "use")],
                        LOOK,
                        order,
                        boost,
                    )
        if rk[:1] == ("not",):
            return _cand("Consider", _squeeze(r[1:]), LOOK, order, boost - 0.5)
        return _cand("Investigate", _squeeze(r), LOOK, order, boost)
    if w in ("how", "hows"):
        if rkeys[:1] in (("much",), ("long",)) and any(
            k in ("time", "longer", "left", "long") for k in rkeys[:5]
        ):
            return _cand("Check progress", (), VAGUE, order, boost)
        if rkeys[:1] == ("many",):
            return _cand("Count", _squeeze(rest[1:]), LOOK, order, boost)
        if rkeys[1:3] in (
            ("we", "doing"),
            ("you", "doing"),
            ("it", "going"),
            ("things", "going"),
        ):
            on = list(rest[3:])
            if on and on[0].key in ("on", "with"):
                return _cand("Check progress on", _object(on[1:]), LOOK, order, boost)
            return _cand("Check progress", (), VAGUE, order, boost)
        if rkeys[:1] == ("come",):
            return _cand("Investigate", _squeeze(no_aux(rest[1:])), LOOK, order, boost)
        if rkeys[:1] and rkeys[0] in _AUX | {"to"}:
            aux = rkeys[0]
            r = rest[1:]
            rk = _keys(r)
            if aux == "to" or (rk[:1] and rk[0] in ("i", "we", "you", "one")):
                r2 = r if aux == "to" else r[1:]
                v = _verb_at(r2)
                if v:
                    return _cand(
                        "Explain how to " + v[0].lower(),
                        _object(r2[v[2] :]),
                        LOOK,
                        order,
                        boost,
                    )
                return _cand("Explain how to", _squeeze(r2), LOOK, order, boost)
            # "how does the revive sweep work" → "Explain revive sweep"
            np = _leading_np(r) or r
            while len(np) > 1 and np[-1].key in (
                "work",
                "works",
                "function",
                "behave",
                "run",
            ):
                np = np[:-1]
            return _cand("Explain", _object(np), LOOK, order, boost)
        return _cand("Explain", _squeeze(rest), LOOK, order, boost)
    if w in ("what", "whats", "which"):
        if w == "what" and rkeys[:1] == ("about",):
            return _cand("Check", _squeeze(rest[1:]), LOOK, order, boost)
        if w == "what" and rkeys[:1] == ("if",) and len(rkeys) > 2:
            v = _verb_at(rest, 2)
            if v:
                g = _gerund(v[0].split()[0].lower())
                return _cand(
                    "Explore " + g, _object(rest[2 + v[2] :]), LOOK, order, boost
                )
        r = rest
        if w == "what" and rkeys[:1] in (("is",), ("are",), ("was",), ("were",)):
            r = rest[1:]
        rk = _keys(r)
        if rk[:2] == ("going", "on"):
            tail = [t for t in r[2:] if t.key not in ("here", "there", "with")]
            return _cand("Investigate", _squeeze(tail), LOOK, order, boost)
        if rk[:2] == ("wrong", "with"):
            return _cand("Fix", _squeeze(r[2:]), WORK, order, boost)
        if rk[:1] in (("causing",), ("making",), ("breaking",)):
            return _cand("Investigate", _squeeze(r[1:]), LOOK, order, boost)
        if rk[:1] in (("does",), ("do",)) and len(rk) > 2 and rk[-1] in ("do", "mean"):
            # "what does panw do in v1" → "Explain panw" — only the subject
            return _cand("Explain", _squeeze(r[1:-1]), LOOK, order, boost)
        if rk[:1] in (("does",), ("do",)):
            inner = [t for t in r[1:] if t.key not in ("do", "mean")]
            return _cand("Explain", _squeeze(inner), LOOK, order, boost)
        if w == "which":
            return _cand("Identify", _squeeze(r), LOOK, order, boost)
        return _cand("Explain", _squeeze(r), LOOK, order, boost - 0.2)
    if w in ("where", "wheres"):
        r = no_aux(rest)
        while r and r[0].key in ("see", "find", "view", "get", "look", "access"):
            r = r[1:]  # "where can i see X" → "Find X"
        found = _squeeze(r)
        if len(found) > 1 and _INFLECTED.get(found[-1].key, ("", ""))[1] == "ed":
            found = found[:-1]  # "where is X configured" → "Find X"
        return _cand("Find", found, LOOK, order, boost)
    if w == "who":
        return _cand("Find who", _squeeze(rest), LOOK, order, boost - 0.2)
    if w == "when":
        return _cand("Check when", _squeeze(rest), LOOK, order, boost - 0.3)
    if w in _AUX:
        r = list(rest)
        rk = _keys(r)
        # "do you think it makes sense to X" → "Consider X"
        if rk[:2] == ("you", "think"):
            r, rk = r[2:], rk[2:]
            if rk[:3] in (("it", "makes", "sense"), ("that", "makes", "sense")):
                tail = r[3:]
                tk = _keys(tail)
                if tk[:1] in (("for",), ("to",)):
                    tail = tail[1:]
                return _cand("Consider", _squeeze(tail), LOOK, order, boost)
        # "is it possible to X" / "is there a way to X" → "Explore Xing …"
        if rk[:3] == ("it", "possible", "to") or rk[:2] == ("there", "a"):
            r = r[3:] if rk[0] == "it" else r[2:]
            if r and r[0].key in ("way", "thing"):
                r = r[1:]
                if r and r[0].key in ("to", "for"):
                    r = r[1:]
            v = _verb_at(r)
            if v:
                g = _gerund(v[0].split()[0].lower())
                return _cand("Explore " + g, _object(r[v[2] :]), LOOK, order, boost)
            return _cand("Explore", _squeeze(r), LOOK, order, boost)
        if rk[:1] and rk[0] in ("we", "i", "you") and w in ("can", "could", "should"):
            v = _verb_at(r[1:])
            if v:
                g = _gerund(v[0].split()[0].lower())
                return _cand(
                    "Consider " + g, _object(r[1 + v[2] :]), LOOK, order, boost
                )
        # "do we still need the reaper ticket" → "Check need for reaper ticket"
        nd = next((i for i, k in enumerate(rk[:4]) if k == "need"), -1)
        if nd >= 0 and rk[0] in ("we", "i", "you"):
            return _cand(
                "Check need for", _squeeze(r[nd + 1 :]), LOOK, order, boost - 0.2
            )
        stated = _statement_of(w, r)
        if stated is not None:
            return _cand("Check", stated, LOOK, order, boost - 0.3, clean=True)
        return _cand("Check", _squeeze(r), LOOK, order, boost - 0.3, clean=False)
    return None


def _third_person(base: str) -> str:
    """``work`` → ``works``, ``fix`` → ``fixes``, ``apply`` → ``applies``."""
    if base.endswith("y") and len(base) > 2 and base[-2] not in "aeiou":
        return base[:-1] + "ies"
    if base.endswith(("s", "sh", "ch", "x", "z", "o")):
        return base + "es"
    return base + "s"


def _statement_of(aux: str, rest: Sequence[_Tok]) -> Optional[List[_Tok]]:
    """A yes/no question turned back into the statement to check, or None
    when its subject can't be found: "does X work on mac" → "X works on
    mac", "is the cache invalidated" → "cache is invalidated"."""
    keys = _keys(rest)
    if not keys or keys[0] in _SUBJECT_PRONOUNS | {"there"}:
        return None
    if aux in ("does", "do"):
        for i in range(1, min(len(keys), 6)):
            k = keys[i]
            if k in _VERBS or k in _STATE_VERBS:
                subject = _drop_determiners(rest[:i])
                if _is_vague(subject):
                    return None
                verb = _third_person(k) if aux == "does" else k
                tail = [t for t in _object(rest[i + 1 :]) if t.key not in _INTENSIFIERS]
                tail = [t for t in tail if t.key not in ("yet", "already", "ever")]
                return subject + [_Tok(verb, verb)] + tail
        return None
    if aux in ("is", "are", "was", "were"):
        np = _leading_np(rest)
        if len(np) == len(keys):
            # An unknown participle ends the subject too: "the cache
            # invalidated on deploy" → "the cache".
            at = next(
                (
                    i
                    for i in range(1, len(keys))
                    if keys[i].endswith("ed") and len(keys[i]) > 4
                ),
                len(keys),
            )
            np = list(rest[:at])
        if not np or len(np) == len(keys) or _is_vague(np):
            return None
        if any(_INFLECTED.get(t.key, ("", ""))[1] == "ing" for t in np):
            return None  # "the cause prod getting wiped": no clean subject
        tail = [t for t in _object(rest[len(np) :]) if t.key not in _INTENSIFIERS]
        tail = [t for t in tail if t.key not in ("yet", "already", "ever")]
        if not tail:
            return None
        return _drop_determiners(np) + [_Tok(aux, aux)] + tail
    return None


def _read_complaint(toks: List[_Tok], order: int, boost: float) -> Optional[_Cand]:
    keys = _keys(toks)
    # "I don't like X" — the phrase may come after a short lead-in.
    for start in range(0, min(4, len(keys))):
        for phrase, verb in _DISLIKE:
            if not _starts(keys, phrase, start):
                continue
            rest = list(toks[start + len(phrase) :])
            if rest and rest[0].key == "about":
                rest = rest[1:]
            rk0 = _keys(rest)
            for lead in (
                ("what", "you", "did", "with"),
                ("what", "you", "did", "to"),
                ("what", "you", "did", "in"),
            ):
                if _starts(rk0, lead):  # "I don't like what you did with X"
                    rest = rest[len(lead) :]
                    break
            rkeys = _keys(rest)
            # "… that it doesn't have the option to confirm" → "Add option …"
            for i in range(len(rkeys)):
                lack = next((p for p in _LACKS if _starts(rkeys, p, i)), None)
                if lack and verb == "Rework":
                    return _cand(
                        "Add", _object(rest[i + len(lack) :]), WORK, order, boost
                    )
            if rest and rest[0].key in ("how", "what", "why") and verb == "Explain":
                return _cand(
                    "Explain " + rest[0].key, _object(rest[1:]), LOOK, order, boost
                )
            if rest and rest[0].key == "how" and len(rest) > 1:
                # "I don't like how slow it is" → "Speed up"
                adj = next((v for p, v in _COMPLAINTS if p == (rest[1].key,)), None)
                if adj:  # vague ("it"), but it still says what is wrong
                    return _cand(adj, _object(rest[2:]), WORK, order, boost + 1.2)
            how_or_that = bool(rest) and rest[0].key in ("how", "that")
            if how_or_that:
                rest = rest[1:]
                rk = _keys(rest)
                if len(rk) > 3 and rk[0] in _SUBJECT_PRONOUNS and rk[1] in _AUX:
                    inf = _INFLECTED.get(rk[2])
                    if inf and inf[1] == "ing":  # "how we are doing the imports"
                        return _cand(verb, _object(rest[3:]), WORK, order, boost)
                inner = _read_complaint(rest, order, boost)
                if inner and not _is_vague(inner.obj):
                    return inner
            obj = _object(rest)
            np = _leading_np(obj) or obj
            target = np if not _is_vague(np) else _gerundize(obj)
            # After "how"/"that" nothing above read the clause: a guess.
            loose = False if how_or_that else None
            return _cand(
                verb, target, LOOK if verb == "Explain" else WORK, order, boost, loose
            )
    # "the wrapping looks lazy" → "Polish wrapping": earliest predicate wins.
    skeys = [k if k not in _INTENSIFIERS else "" for k in keys]
    best_at, best = len(keys), None
    for phrase, verb in _COMPLAINTS:
        flat = [k for k in skeys]
        at = _find_skipping(flat, phrase)
        if at < 0 or at > best_at or (at == best_at and best):
            continue
        if at == 0 and phrase[0] not in _STANDALONE_COMPLAINTS:
            continue
        best_at, best = at, (phrase, verb)
    if best:
        phrase, verb = best
        at, end = best_at, _span_end(skeys, best_at, phrase)
        before = _strip_filler(toks[:at])
        while before and (
            before[-1].key in _AUX
            or before[-1].key in _INTENSIFIERS
            or before[-1].key in ("seem", "seems", "to")
        ):
            before = before[:-1]
        subject = [
            t for t in (_leading_np(before) or before) if t.key not in _INTENSIFIERS
        ]
        if _is_vague(subject):
            # "it is not working for session create" → "session create"
            after = list(toks[end:])
            if after and after[0].key in ("for", "in", "on", "with", "when"):
                subject = _object(after[1:])
        if _is_vague(subject):
            boost += 1.2  # "it's slow" still says what is wrong
        if verb == "Fix failing" and not (
            subject
            and subject[-1].key
            in (
                "test",
                "tests",
                "check",
                "checks",
                "build",
                "builds",
                "job",
                "jobs",
                "hook",
                "hooks",
                "ci",
                "step",
                "steps",
                "precommit",
                "pipeline",
            )
        ):
            verb = "Fix"
        return _cand(verb, subject, WORK, order, boost)
    # "the header overlaps the toolbar" → "Fix header overlapping toolbar"
    np = _leading_np(toks)
    if (
        np
        and len(np) < len(keys)
        and not _is_vague(np)
        and keys[0] not in _SUBJECT_PRONOUNS
    ):
        k = keys[len(np)]
        inf = _INFLECTED.get(k)
        if inf and inf[1] == "s" and inf[0] in _MISBEHAVIOURS:
            return _cand("Fix", _gerundize(_object(toks)), WORK, order, boost)
    # "there are a bunch of merge conflicts in the 3 PRs" → "Fix merge conflicts in 3 PRs"
    if keys[:2] in (("there", "are"), ("there", "is")) or keys[:1] == ("theres",):
        rest = list(toks[1:] if keys[0] == "theres" else toks[2:])
        obj = _drop_determiners(_object(rest))
        if any(t.key in _PROBLEM_NOUNS for t in obj[:4]):
            return _cand("Fix", obj, WORK, order, boost)
    return None


#: Verbs that describe something misbehaving when a UI thing or a job does
#: them on its own ("the header OVERLAPS the toolbar", "the queue STALLS").
_MISBEHAVIOUR_WORDS = """overlap flicker jump draw cover clip stall leak spam
    freeze hang crash duplicate overflow disappear vanish reset shift drift
    stutter lag timeout ignore swallow drop truncate"""
_MISBEHAVIOURS = frozenset(_MISBEHAVIOUR_WORDS.split())


def _find_skipping(keys: Sequence[str], phrase: Sequence[str]) -> int:
    """Where ``phrase`` first occurs in ``keys`` (-1 if nowhere). Blank
    entries (dropped intensifiers) inside the match are skipped: "is super
    annoying" matches "is annoying"."""
    for i in range(len(keys)):
        if keys[i] != phrase[0]:
            continue
        j, p = i + 1, 1
        while p < len(phrase) and j < len(keys):
            if keys[j] == "":
                j += 1
                continue
            if keys[j] != phrase[p]:
                break
            j, p = j + 1, p + 1
        if p == len(phrase):
            return i
    return -1


def _span_end(keys: Sequence[str], at: int, phrase: Sequence[str]) -> int:
    j, p = at, 0
    while p < len(phrase) and j < len(keys):
        if keys[j] == phrase[p]:
            p += 1
        j += 1
    return j


_NEGATIONS = frozenset("dont never cant shouldnt wont".split())


def _read_clause(
    toks: List[_Tok], question: bool, order: int, boost: float
) -> Optional[_Cand]:
    if not toks:
        return None
    keys = _keys(toks)
    if keys[:2] in (("make", "sure"), ("making", "sure")) or keys[:1] == ("ensure",):
        return _read_make_sure(
            toks[1:] if keys[0] == "ensure" else toks[2:], order, boost
        )
    v = _verb_at(toks)
    if v and v[0] == "Skip":  # "don't worry about X"
        return _cand("Skip", _object(toks[v[2] :]), WORK, order, boost - 0.8)
    if keys[0] in ("i", "there", "theres", "im", "we", "ok", "a", "few"):
        c = _read_complaint(toks, order, boost)
        if c:
            return c
    if (
        keys[0] in _NEGATIONS
        or keys[:2] == ("do", "not")
        or (
            len(keys) > 1
            and keys[0] in ("you", "we", "i", "it")
            and keys[1] in _NEGATED_AUX
        )
    ):
        # A constraint ("don't restart it") is not the task — but a
        # requirement may follow ("it can't just work for X, it has to …").
        return _read_requirement(toks, order, boost)
    if keys[0] in _AUX and _asks(keys, question):
        c = _read_question(toks, order, boost)
        if c:
            return c
    c = _read_wish(toks, order, boost)
    if c:
        return c
    c = _read_imperative(toks, order, boost)
    if c:
        return c
    if keys[0] in _WH and (
        question
        or keys[0] in ("why", "how", "whats", "hows")
        or (len(keys) > 1 and keys[1] in _AUX | {"about", "if"})
    ):
        c = _read_question(toks, order, boost)
        if c:
            return c
    if keys[:2] in (("any", "ideas"), ("ideas", "for")) or keys[:1] == ("ideas",):
        tail = [
            t for t in toks[2:] if t.key not in ("you", "have", "for", "on", "about")
        ]
        return _cand("Suggest", _object(tail), LOOK, order, boost)
    c = _read_complaint(toks, order, boost)
    if c:
        return c
    c = _read_requirement(toks, order, boost)
    if c:
        return c
    # "Confirming that the top bar updates" → "Confirm top bar updates"
    inf = _INFLECTED.get(keys[0])
    if inf and inf[1] == "ing" and inf[0] in _VERBS and len(keys) > 1:
        rest = toks[2:] if keys[1] == "that" else toks[1:]
        return _cand(
            _display(inf[0]),
            _object(rest),
            _VERBS[inf[0]],
            order,
            boost - 0.3,
            clean=False,
        )
    # "merge conflicts please fix" → "Fix merge conflicts"
    last = _verb_at(toks, len(toks) - 1) if len(toks) > 1 else None
    if (
        last
        and keys[-1] in _OBJECT_FIRST_VERBS
        and all(not _is_verbish(keys, i) for i in range(len(keys) - 1))
    ):
        return _cand(last[0], toks[:-1], last[1], order, boost)
    if question and keys[0] not in ("i", "we", "im"):
        return _cand("Check", _squeeze(toks), LOOK, order, boost - 0.8, clean=False)
    return None


#: Verbs that may come AFTER their object: "merge conflicts please fix".
_OBJECT_FIRST_VERBS = frozenset(
    "fix check review update handle address resolve investigate debug".split()
)
_NEGATED_AUX = frozenset("dont arent isnt wont cant shouldnt doesnt didnt not".split())


def _asks(keys: Sequence[str], question: bool) -> bool:
    """Whether an aux-first clause is a yes/no question ("is it …", "do we
    …") rather than an imperative "do a scan" / request "can you fix"."""
    nxt = keys[1] if len(keys) > 1 else ""
    if keys[0] in ("can", "could", "would", "will") and nxt == "you":
        return False  # a request — the filler strip handles it
    if keys[0] == "do":
        # "do a scan of X" is an order; "do the retries fire?" asks.
        return question or nxt in _SUBJECT_PRONOUNS | {"this", "that", "these", "those"}
    return question or nxt in _SUBJECT_PRONOUNS | _ARTICLES | {
        "there",
        "all",
        "this",
        "that",
    }


# --------------------------------------------------------------------------- #
# Canned headlines for machine text and one-word replies
# --------------------------------------------------------------------------- #


def _canned(clean: str) -> Optional[Headline]:
    """A fixed, NOT-meaningful headline for prompts that carry no task."""
    low = clean.strip().lower()
    if not low or not re.search(r"[^\W_]", low):
        return Headline("", False)
    if low.startswith(_RESUME_HEADS):
        return Headline("Continue", False)
    words: List[str] = []
    for w in low.split():
        k = _key(w)
        if k:
            words.extend(_RESPELL.get(k, k).split())
    if not words:
        return Headline("", False)
    if all(w in _GREETING_WORDS for w in words):
        return Headline("", False)
    if words[0].startswith("continue") and len(words) <= 3:
        return Headline("Continue", False)
    if (
        len(words) <= 14
        and all(w in _CONTINUE_WORDS for w in words)
        and any(w.startswith(_CONTINUE_TRIGGERS) or w == "left" for w in words)
    ):
        return Headline("Continue", False)
    if (
        len(words) <= 10
        and all(w in _STATUS_WORDS for w in words)
        and any(w in _STATUS_TRIGGERS for w in words)
    ):
        return Headline("Check progress", False)
    if len(words) <= 12 and all(w in _CONFIRM_WORDS for w in words):
        return Headline("Go ahead", False)
    first = clean.strip().split()[0]
    if first.startswith("/") and len(words) <= 3 and re.fullmatch(r"/[\w:-]+", first):
        return Headline("Run " + first, False)
    return None


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _render_word(t: _Tok) -> str:
    if t.key in _ACRONYMS and t.text.lower() == t.key:
        return _ACRONYMS[t.key]
    if t.key in ("i", "im", "ive", "id", "ill") and t.text.lower() == t.key:
        return {"i": "I", "im": "I'm", "ive": "I've", "id": "I'd", "ill": "I'll"}[t.key]
    return t.text


def _shown(verb: str, obj: Sequence[_Tok]) -> List[_Tok]:
    """The object words :func:`_render` shows after ``verb``."""
    toks = [t for t in obj if t.key not in (",",)]
    # Never repeat the verb's own words ("Open PR" + "PR").
    vwords = set(verb.lower().split())
    while toks and toks[0].key in vwords:
        toks = toks[1:]
    n_verb = len(verb.split())
    toks = toks[
        : MAX_OBJECT_WORDS if n_verb <= 2 else max(3, MAX_OBJECT_WORDS + 2 - n_verb)
    ]
    while toks and (
        toks[-1].key in _TRAILING_STOP or toks[-1].key in _AUX or toks[-1].key == "and"
    ):
        toks.pop()
    return toks


def _render(verb: str, obj: Sequence[_Tok]) -> str:
    words = [_render_word(t) for t in _shown(verb, obj)]
    text = " ".join([verb] + words).strip()
    text = re.sub(r"[\s\-–—:;,.!?/]+$", "", text)
    return text[:1].upper() + text[1:] if text else text


_SHIP_ORDER = (
    "Commit",
    "Push",
    "Rebase",
    "Open PR",
    "Open PRs",
    "Merge",
    "Release",
    "Cut release",
    "Tag",
    "Deploy",
    "Ship",
)


_NAMING_SHIP_VERBS = frozenset("Ship Release Deploy Publish Land Backport".split())


def _chain(cands: Sequence[_Cand]) -> Tuple[str, bool]:
    """Shipping steps as one phrase: "Commit, push and open PR". Meaningful
    only when a step names something specific ("Merge PR #161")."""
    steps: List[str] = []
    for c in cands:
        if c.verb not in steps:
            steps.append(c.verb)
    steps.sort(
        key=lambda v: _SHIP_ORDER.index(v) if v in _SHIP_ORDER else len(_SHIP_ORDER)
    )
    specific = [c for c in cands if any(_specific(t) for t in _content(c.obj))]
    specific += [  # "ship the new onboarding flow" names the work too
        c for c in cands if c.verb in _NAMING_SHIP_VERBS and not _is_vague(c.obj)
    ]
    if specific:  # "Merge PR #161" says more than "Merge and release"
        return _render(specific[0].verb, specific[0].obj), True
    if len(steps) == 1:
        target = (
            specific[0]
            if specific
            else next((c for c in cands if not _is_vague(c.obj)), None)
        )
        return (_render(steps[0], target.obj) if target else steps[0]), bool(specific)
    parts = [steps[0]] + [s[:1].lower() + s[1:] for s in steps[1:]]
    return ", ".join(parts[:-1]) + " and " + parts[-1], bool(specific)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def headline(text: Optional[str]) -> Headline:
    """``text`` (a prompt) as a short verb phrase, plus whether it is about
    the task at all. ``("", False)`` for empty input. Never raises."""
    try:
        return _headline(text)
    except Exception:  # noqa: BLE001 — a label must never fail a poll
        return Headline("", False)


def task_headline(text: Optional[str]) -> str:
    """``text`` (a prompt) as a ~2–6 word verb phrase, sentence case, no
    trailing punctuation; "" when there is nothing to say."""
    return headline(text).text


def _headline(text: Optional[str]) -> Headline:
    if not text or not isinstance(text, str):
        return Headline("", False)
    raw = text.strip()
    meaningful = True
    m = _ENVELOPE.match(raw)
    if m:
        kind = m.group(1).lower()
        if kind == "result":
            worker = _WORKER.search(m.group(0))
            name = worker.group(1) if worker else "worker"
            return Headline("Review %s result" % name, False)
        if kind == "note":
            meaningful = False  # MindFlock's own bookkeeping
        raw = _REPLY_HINT.sub("", raw[m.end() :]).strip()
    if raw.lower().startswith(_RESUME_HEADS):
        return Headline("Continue", False)
    has_image = bool(_PLACEHOLDER.search(raw))
    clean = _normalise(raw)
    canned = _canned(clean)
    if canned is not None:
        return canned
    best = _best(clean)
    if best is None:
        words = _own_words(clean)
        return Headline(words, meaningful and _fallback_meaningful(clean, words), False)
    out, weak, confident = best
    if (
        weak
        and has_image
        and len(out.split()) == 1
        and out not in ("Continue", "Go ahead")
    ):
        return Headline(out + " UI in screenshot", meaningful)
    if not confident:
        # A loose reading: the person's own words beat a constructed phrase
        # that might be salad. Weak stays weak (the caller keeps the task).
        words = _own_words(clean)
        if words:
            return Headline(words, meaningful and not weak, False)
    return Headline(out, meaningful and not weak, confident)


#: Verbs that are a whole task without an object.
_INTRANSITIVE_OK = frozenset(
    "Restart Retry Debug Deploy Release Rebase Merge Revert Investigate Redeploy".split()
)


def _best(clean: str) -> Optional[Tuple[str, bool]]:
    cands: List[_Cand] = []
    for s_idx, sent in enumerate(_sentences(clean)):
        prev: List[_Tok] = []
        for c_idx, (toks, side) in enumerate(_clauses(sent)):
            toks = _strip_filler(toks)
            if not toks:
                continue
            boost = sent.boost - (1.2 if side else 0.0)
            order = s_idx * 2 + c_idx
            if _keys(toks) == ("why",) and prev:
                # "the artifact has nothing in the tables why" — a trailing
                # "why" turns the statement before it into the question.
                cands.append(
                    _cand("Investigate", _gerundize(_object(prev)), LOOK, order, boost)
                )
                continue
            prev = toks
            c = _read_clause(toks, sent.question, order, boost)
            if c:
                cands.append(c)
    if not cands:
        return None
    ships = [c for c in cands if c.cls == SHIP]
    best = max(cands, key=lambda c: (c.score, -c.order))
    if best.cls == SHIP or (best.score < 1.0 and ships):
        text, specific = _chain(ships)
        return text, not specific, True
    vague = _is_vague(best.obj)
    if best.verb == "Check progress":
        return best.verb, True, True
    if best.verb in ("Make it happen", "File ticket"):
        return best.verb, True, False
    intransitive = best.verb.split()[0] in _INTRANSITIVE_OK
    # "Open argo PR": a verb phrase that names the work needs no object.
    named = (
        not best.obj and best.verb.startswith("Open ") and len(best.verb.split()) > 2
    )
    weak = vague and not intransitive and not named
    if weak and best.cls in (VAGUE, META):
        # A vague ask ("do both", "use it") with nothing better: the plain
        # words of the prompt say more.
        words = _fallback_words(clean)
        if len(words.split()) >= 3 and _fallback_meaningful(clean, words):
            return words, False, False
    confident = _tidy(best) and (not vague or named or (intransitive and not best.obj))
    return _render(best.verb, best.obj), weak, confident


def _fallback_meaningful(clean: str, words: str) -> bool:
    """Plain words count as a task unless the prompt is the user reporting
    on themselves ("I signed in", "I switched accounts")."""
    if len(words.split()) < 2:
        return False
    first = next((ln for ln in clean.split("\n") if ln.strip()), "") if clean else ""
    toks = _strip_filler(_tokens(first))
    keys = _keys(toks)
    if keys[:1] in (("i",), ("im",), ("we",), ("ive",)):
        return not (len(keys) > 1 and (keys[1] in _REPORT_VERBS or keys[0] == "im"))
    # A statement ("it says X", "0.7.2 is merged", "you can leave v1 on") is
    # news, not a task; a title ("V2 launch-gate fixes #2861") is a task.
    return not any(k in _AUX or k in ("says", "said") for k in keys[1:8])


#: Own-words fallback length: enough to recognise the ask, short for a rail.
MAX_OWN_WORDS = 8


def _own_words(clean: str) -> str:
    """The person's own words, cleaned: the first sentence of the ask (a
    "Task:" line if there is one; background sections skipped), politeness
    and preambles peeled off, cut to :data:`MAX_OWN_WORDS` words with "…",
    sentence case, no trailing punctuation. "" when nothing readable."""
    sents = [s for s in _sentences(clean) if s.boost >= 0]
    sents.sort(key=lambda s: -s.boost)  # stable: a labelled task line first
    for sent in sents:
        toks = _strip_filler(_tokens(sent.text))
        toks = [t for t in toks if t.key not in _NOISE_WORDS]
        if not any(t.key not in _STOP for t in toks):
            continue
        cut = len(toks) > MAX_OWN_WORDS
        toks = toks[:MAX_OWN_WORDS]
        while cut and len(toks) > 2 and toks[-1].key in _TRAILING_STOP:
            toks.pop()
        words = [_render_word(t) for t in toks]
        text = re.sub(r"[\s\-–—:;,.!?/]+$", "", " ".join(words))
        if not text:
            continue
        text = text[:1].upper() + text[1:]
        return text + "…" if cut else text
    return ""


def _fallback_words(text: str) -> str:
    """The first few content words of the first prose sentence, sentence
    case — the plain label when no rule reads the prompt."""
    for sent in _sentences(text):
        toks = _strip_filler(_tokens(sent.text))
        words = [t for t in toks if t.key not in _STOP]
        if words:
            return _render(_render_word(words[0]), words[1:MAX_OBJECT_WORDS])
    return ""
