/** Shaping text for a voice — the browser twin of
 * `backend/web/core/spoken_text.py` (its module doc is the specification).
 * `shapeForSpeech(text, overrides)` is what a client calls right before
 * `speechSynthesis.speak` (and ONLY there — a caption shows the text as
 * written): the person's own pronunciations first (`voice.pronounce`,
 * `GET /api/voice` → `pronounce`), then markup stripped, paths and files
 * spoken, versions / PR numbers / durations / units said the engineers'
 * way, identifiers split into words, the tech table ("SQL" → "sequel",
 * "JSON" → "jayson", "k8s" → "kubernetes"), and the heteronyms by context
 * ("everything goes live" → "lyve", "I read it" → "red"). Pure, idempotent,
 * bounded (MAX_OUT), no dependencies — `backend/web/static/spoken-text.js`
 * is this file bundled as a classic script (window.MindFlockSpokenText.shape)
 * for the phone page, generated from here (in frontend/) with vite's own
 * bundler, then given its banner and window line:
 *
 *   node -e "const {rolldown}=require('rolldown');rolldown({input:'src/lib/spokenText.ts'})
 *     .then(b=>b.write({format:'iife',name:'MindFlockSpokenText',file:'../backend/web/static/spoken-text.js'}))"
 *
 * The vitest and pytest suites run the fixture through the twin too, so a
 * stale twin fails CI.
 *
 * TABLES is the one rule table: the identical JSON literal sits in the Python
 * module and in the static twin between `@@tables` / `@@end` markers, and
 * tests/unit/test_spoken_text.py diffs the three; the fixture
 * tests/unit/data/spoken_text.json runs through all three implementations.
 * No regex lookbehind anywhere: the twin runs on older iOS Safaris too. */

export interface PronounceRule {
  say: string;
  as: string;
}

/** The longest input shaped; the rest is cut first. */
export const MAX_IN = 4000;
/** The longest output. */
export const MAX_OUT = 2000;
/** `voice.pronounce` bounds: rows, and characters per side. */
export const PRONOUNCE_MAX = 40;
export const PRONOUNCE_MAX_CHARS = 40;

interface Heteronym {
  word: string;
  as: string;
  default: "respell" | "keep";
  keep_prev?: string[];
  keep_next?: string[];
  respell_prev?: string[];
  respell_next?: string[];
  respell_next_re?: string;
  keep_next_re?: string;
}

interface Tables {
  tech: Record<string, string>;
  ext: Record<string, string>;
  units: Record<string, [string, string]>;
  durations: Record<string, [string, string]>;
  heteronyms: Heteronym[];
}

/* @@tables */
export const TABLES: Tables = {
  "tech": {
    "sql": "sequel", "json": "jayson", "nginx": "engine x", "tmux": "tee mux",
    "uv": "you vee", "npm": "N P M", "npx": "N P X", "api": "A P I", "apis": "A P I's",
    "cli": "C L I", "url": "U R L", "urls": "U R L's", "ssh": "S S H", "ci": "C I",
    "pr": "P R", "prs": "P R's", "id": "I D", "ids": "I D's", "uuid": "U U I D",
    "eta": "E T A", "fyi": "F Y I", "lgtm": "looks good to me", "pytest": "pie test",
    "regex": "rej ex", "regexes": "rej exes", "github": "git hub", "sudo": "soo doo",
    "chmod": "C H mod", "chown": "C H own", "venv": "vee env", "k8s": "kubernetes",
    "i18n": "i 18 n", "a11y": "a 11 y", "e2e": "end to end", "oauth": "O auth",
    "websocket": "web socket", "websockets": "web sockets", "enum": "ee num",
    "enums": "ee nums", "src": "source", "kwargs": "kay wargs", "tmp": "temp",
    "usr": "user", "msg": "message", "msgs": "messages", "pkg": "package", "pkgs": "packages",
    "ctx": "context", "db": "dee bee", "dbs": "dee bees", "zsh": "zee shell", "vitest": "vee test",
    "tsc": "T S C", "eslint": "E S lint", "stdout": "standard out", "stderr": "standard error",
    "stdin": "standard in", "localhost": "local host", "e.g.": "for example",
    "i.e.": "that is", "etc.": "etcetera", "etc": "etcetera", "vs": "versus", "vs.": "versus",
    "ttl": "T T L", "tls": "T L S", "ssl": "S S L", "http": "H T T P", "https": "H T T P S",
    "html": "H T M L", "css": "C S S", "js": "J S", "ts": "T S", "tsx": "T S X", "jsx": "J S X",
    "yaml": "yammel", "yml": "yammel", "toml": "tommel", "csv": "C S V", "ui": "U I", "ux": "U X",
    "cpu": "C P U", "gpu": "G P U", "ram": "ram", "os": "O S", "io": "I O", "sdk": "S D K",
    "cdn": "C D N", "dns": "D N S", "ip": "I P", "tcp": "T C P", "udp": "U D P", "pty": "P T Y",
    "mcp": "M C P", "llm": "L L M", "llms": "L L M's", "stt": "S T T", "tts": "T T S",
    "wsl": "W S L", "vm": "V M", "vms": "V M's", "qa": "Q A", "ok": "okay", "readme": "read me",
    "dto": "D T O", "orm": "O R M", "jwt": "J W T", "sha": "shah", "utf": "U T F", "ascii": "askey",
    "iso": "I S O", "lib": "lib", "libs": "libs", "cfg": "config", "ptt": "push to talk"
  },
  "ext": {
    "py": "pie", "ts": "T S", "tsx": "T S X", "js": "J S", "jsx": "J S X", "mjs": "M J S",
    "cjs": "C J S", "md": "M D", "json": "jayson", "toml": "tommel", "yml": "yammel",
    "yaml": "yammel", "css": "C S S", "html": "H T M L", "txt": "text", "sh": "S H",
    "zsh": "zee shell", "cfg": "config", "ini": "I N I", "csv": "C S V", "svg": "S V G",
    "png": "P N G", "jpg": "J P G", "jpeg": "J P G", "gif": "gif", "pdf": "P D F", "env": "env",
    "log": "log", "lock": "lock", "sql": "sequel", "xml": "X M L", "rs": "R S", "go": "go",
    "rb": "R B", "java": "java", "kt": "K T", "swift": "swift", "c": "C", "h": "H", "cpp": "C plus plus",
    "wav": "wave", "mp3": "M P 3", "zip": "zip", "tar": "tar", "gz": "G Z", "exe": "E X E",
    "dmg": "D M G", "app": "app", "plist": "P list", "ipynb": "notebook", "pyc": "pie C",
    "egg": "egg", "whl": "wheel", "bak": "backup", "tmp": "temp"
  },
  "units": {
    "ms": ["millisecond", "milliseconds"], "px": ["pixel", "pixels"],
    "kb": ["kilobyte", "kilobytes"], "mb": ["megabyte", "megabytes"],
    "gb": ["gigabyte", "gigabytes"], "tb": ["terabyte", "terabytes"],
    "kib": ["kibibyte", "kibibytes"], "mib": ["mebibyte", "mebibytes"],
    "gib": ["gibibyte", "gibibytes"], "fps": ["frame per second", "frames per second"]
  },
  "durations": {
    "h": ["hour", "hours"], "hr": ["hour", "hours"], "hrs": ["hour", "hours"],
    "m": ["minute", "minutes"], "min": ["minute", "minutes"], "mins": ["minute", "minutes"],
    "s": ["second", "seconds"], "sec": ["second", "seconds"], "secs": ["second", "seconds"]
  },
  "heteronyms": [
    {
      "word": "live", "as": "lyve", "default": "respell",
      "respell_prev": ["go", "goes", "went", "going", "gone", "is", "are", "was", "were", "be",
        "been", "being", "it's", "its", "now", "still", "already", "stay", "stays", "stayed",
        "remain", "remains", "became", "become", "becomes", "got", "get", "gets", "back",
        "fully", "finally", "officially", "currently"],
      "keep_prev": ["i", "we", "you", "they", "people", "who", "to", "can't", "cannot",
        "couldn't", "won't", "wouldn't", "don't", "didn't", "doesn't", "must", "may", "might",
        "will", "would", "could", "should", "shall", "i'll", "we'll", "you'll", "they'll",
        "i'd", "we'd", "you'd", "they'd", "users", "developers", "teams", "long", "let", "lets"],
      "keep_next": ["with", "here", "there", "alone", "together", "near", "among", "off",
        "like", "without", "within", "nearby", "abroad", "downtown", "upstairs", "downstairs",
        "forever", "happily", "comfortably", "longer", "anywhere", "somewhere", "elsewhere"],
      "respell_next_re": "in (?:\\d|one|two|three|four|five|ten|a |an |about |under |less )",
      "keep_next_re": "in (?![\\d]|one|two|three|four|five|ten|a |an |about |under |less )"
    },
    {
      "word": "read", "as": "red", "default": "keep",
      "respell_prev": ["i", "we", "you", "they", "he", "she", "it", "who", "had", "have", "has",
        "having", "already", "just", "once", "been", "was", "were", "got", "haven't", "hasn't",
        "hadn't", "never", "i've", "we've", "you've", "they've", "he's", "she's", "it's",
        "all", "both", "everyone", "everybody", "somebody", "someone", "nobody"]
    },
    {
      "word": "record", "as": "recorde", "default": "keep",
      "respell_prev": ["to", "will", "can", "could", "should", "would", "must", "may", "might",
        "please", "let's", "lets", "don't", "doesn't", "didn't", "won't", "can't", "cannot",
        "and", "or", "then", "we", "i", "you", "they", "also", "now", "just", "always", "never",
        "still", "not", "shall"]
    },
    {
      "word": "present", "as": "prezent", "default": "keep",
      "respell_prev": ["to", "will", "can", "could", "should", "would", "must", "may", "might",
        "please", "let's", "lets", "don't", "doesn't", "didn't", "won't", "can't", "cannot",
        "we", "i", "you", "they", "also", "now", "then", "and", "or", "shall"],
      "respell_next": ["it", "them", "this", "that", "these", "those", "the", "a", "an", "your",
        "our", "my", "their", "its", "results", "data", "options", "findings", "each", "every",
        "both", "all", "some", "two", "three"]
    },
    {
      "word": "use", "as": "yooz", "default": "respell",
      "keep_prev": ["the", "a", "an", "its", "in", "of", "no", "any", "this", "that", "for",
        "their", "our", "your", "my", "each", "every", "general", "heavy", "memory", "cpu",
        "disk", "token", "tokens", "api", "resource", "resources", "intended", "actual",
        "single", "one", "multiple", "make", "makes", "made", "making", "little", "much", "more",
        "less", "some", "into", "under", "without", "daily", "personal", "internal", "external",
        "fair", "common", "wide", "widespread", "first", "second", "last", "future", "later", "later"],
      "keep_next": ["of", "case", "cases", "by"]
    },
    {
      "word": "separate", "as": "seperate", "default": "keep",
      "respell_prev": ["to", "will", "can", "could", "should", "would", "must", "please",
        "let's", "lets", "we", "i", "you", "they", "and", "then", "also", "don't", "doesn't",
        "didn't", "won't", "can't", "cannot", "shall"],
      "respell_next": ["the", "them", "it", "these", "those", "this", "that", "out", "your",
        "our", "my", "each", "both", "all"]
    },
    {
      "word": "estimate", "as": "estimayt", "default": "keep",
      "respell_prev": ["to", "will", "can", "could", "should", "would", "must", "please",
        "let's", "lets", "we", "i", "you", "they", "and", "then", "also", "don't", "doesn't",
        "didn't", "won't", "can't", "cannot", "shall"],
      "respell_next": ["the", "them", "it", "these", "those", "this", "that", "how", "when",
        "what", "your", "our", "my", "each", "both", "all"]
    },
    {
      "word": "wind", "as": "wined", "default": "keep",
      "respell_next": ["down", "up", "back", "through", "around", "forward"]
    },
    {
      "word": "tear", "as": "tare", "default": "respell",
      "keep_prev": ["a", "the", "this", "that", "one", "every", "no", "any", "another", "single"]
    },
    {
      "word": "resume", "as": "rezoomay", "default": "keep",
      "respell_prev": ["my", "your", "his", "her", "their", "our", "attached", "updated"],
      "keep_next": ["the", "a", "an", "it", "them", "this", "that", "where", "from", "at",
        "on", "in", "after", "work", "working", "session", "sessions", "run", "runs", "playback",
        "listening", "recording", "feature", "button", "command", "option", "later", "now",
        "when", "once", "and", "or", "with", "without", "speaking", "reading"]
    },
    {
      "word": "alternate", "as": "alternayt", "default": "keep",
      "respell_prev": ["to", "will", "can", "could", "should", "would", "must", "let's", "lets",
        "we", "i", "you", "they", "and", "then", "also", "don't", "doesn't", "didn't", "shall"],
      "respell_next": ["between", "them", "it", "these", "those"]
    },
    {
      "word": "permit", "as": "permitt", "default": "keep",
      "respell_prev": ["to", "will", "can", "could", "should", "would", "must", "may", "might",
        "please", "let's", "lets", "don't", "doesn't", "didn't", "won't", "can't", "cannot",
        "we", "i", "you", "they", "and", "then", "not", "also", "shall"],
      "respell_next": ["it", "them", "this", "that", "the", "a", "an", "us", "me", "you", "any"]
    }
  ]
};
/* @@end */

// --- the private-use placeholders a protected span hides behind ----------- //
const HOLD_OPEN = "";
const HOLD_CLOSE = "";
const HOLD_DIGITS = "";
const HOLD_RE = /([-]+)/g;

class Shaper {
  held: string[] = [];
  hold(text: string): string {
    this.held.push(text);
    const idx = String(this.held.length - 1);
    let out = HOLD_OPEN;
    for (const c of idx) out += HOLD_DIGITS[Number(c)];
    return out + HOLD_CLOSE;
  }
  release(text: string): string {
    const back = (_m: string, digits: string) => {
      let idx = "";
      for (const c of digits) idx += String(HOLD_DIGITS.indexOf(c));
      const i = Number(idx);
      return i < this.held.length ? this.held[i] : "";
    };
    for (let round = 0; round < 4 && text.includes(HOLD_OPEN); round++) text = text.replace(HOLD_RE, back);
    return text;
  }
}

// --- helpers ----------------------------------------------------------------- //
const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\\/]/g, "\\$&");
const bounded = (key: string) => "(^|[^A-Za-z0-9])(" + esc(key) + ")(?![A-Za-z0-9])";
const isUpperWord = (w: string) => !/[a-z]/.test(w) && /[A-Z]/.test(w);

function matchCase(original: string, spoken: string): string {
  const rest = original.slice(1);
  if (/^[A-Z]/.test(original) && (original.length === 1 || !isUpperWord(rest))) {
    return spoken.slice(0, 1).toUpperCase() + spoken.slice(1);
  }
  return spoken;
}

const PREV_RE = /([A-Za-z0-9']+)[ \t-]*$/;
const NEXT_RE = /^[ \t-]*([A-Za-z0-9']+)/;
const prevWord = (text: string, start: number) => {
  const m = PREV_RE.exec(text.slice(Math.max(0, start - 48), start));
  return m ? m[1].toLowerCase() : "";
};
const nextWord = (text: string, end: number) => {
  const m = NEXT_RE.exec(text.slice(end, end + 48));
  return m ? m[1].toLowerCase() : "";
};
const byLength = (keys: string[]) => keys.slice().sort((a, b) => b.length - a.length);

// --- 2. markup ----------------------------------------------------------------- //
const FENCE = /^[ \t]*(```|~~~)[^\n]*$/gm;
const INLINE_CODE = /`([^`\n]*)`/g;
const IMAGE = /!\[([^\]]*)\]\([^)]*\)/g;
const LINK = /\[([^\]]+)\]\([^)]*\)/g;
const URL_RE = /(^|[^A-Za-z0-9])((?:https?|ftp|wss?):\/\/[^\s)\]>"']+|www\.[^\s)\]>"']+)/g;
const HTML = /<\/?[A-Za-z][^<>]*>/g;
const HEADING = /^[ \t]{0,3}#{1,6}[ \t]+/gm;
const LIST = /^[ \t]*(?:[-*+]|\d{1,3}[.)])[ \t]+/gm;
const QUOTE = /^[ \t]*>[ \t]?/gm;
const STRONG = /(^|[\s(])(\*\*|__)(\S(?:.*?\S)?)\2(?=[\s).,!?;:]|$)/g;
const EM = /(^|[\s(])([*_])([^\s*_](?:.*?[^\s*_])?)\2(?=[\s).,!?;:]|$)/g;
const STRIKE = /~~(\S(?:.*?\S)?)~~/g;
const EMOJI = /[\u{1F000}-\u{1FAFF}\u{1F900}-\u{1F9FF}☀-➿⬀-⯿️‍←-⇿⌀-⏿■-◿\u{1F1E6}-\u{1F1FF}]/gu;

function stripMarkup(text: string): string {
  text = text.replace(FENCE, "");
  text = text.replace(INLINE_CODE, "$1");
  text = text.replace(IMAGE, "$1");
  text = text.replace(LINK, "$1");
  text = text.replace(URL_RE, "$1a link");
  text = text.replace(HTML, "");
  text = text.replace(HEADING, "");
  text = text.replace(LIST, "");
  text = text.replace(QUOTE, "");
  text = text.replace(STRONG, "$1$3");
  text = text.replace(EM, "$1$3");
  text = text.replace(STRIKE, "$1");
  text = text.replace(EMOJI, "");
  return text;
}

// --- 3. paths, files, SHAs ---------------------------------------------------- //
const SEG = "[A-Za-z0-9_.~@+-]+";
const PATH = new RegExp(
  "(^|[^A-Za-z0-9/.~])((?:~|\\.{1,2})?/" + SEG + "(?:/" + SEG + ")*/?|" + SEG + "(?:/" + SEG + ")+/?)(?::(\\d+))?(?![A-Za-z0-9/])",
  "g"
);
const FILE = new RegExp(
  "(^|[^A-Za-z0-9/.])([A-Za-z0-9_][A-Za-z0-9_-]*\\.(" + byLength(Object.keys(TABLES.ext)).join("|") + "))(?::(\\d+))?(?![A-Za-z0-9.])",
  "g"
);
const SHA = /(^|[^A-Za-z0-9])([0-9a-f]{7,40})(?![A-Za-z0-9])/g;

function spokenName(name: string): string {
  const hidden = name.startsWith(".");
  const words = name.replace(/^\.+/, "").replace(/_/g, " ").replace(/-/g, " ").trim();
  return hidden ? ("dot " + words).trim() : words;
}

function spokenSegment(seg: string, last: boolean): string {
  if (seg === "~") return "home";
  if (seg === "..") return "dot dot";
  if (last && seg.replace(/^\.+/, "").includes(".")) {
    const i = seg.lastIndexOf(".");
    const stem = seg.slice(0, i);
    const ext = seg.slice(i + 1).toLowerCase();
    const spokenExt = TABLES.ext[ext];
    if (spokenExt !== undefined) return spokenName(stem) + " dot " + spokenExt;
  }
  return spokenName(seg);
}

function pathWords(path: string, line: string | undefined, s: Shaper): string {
  const segs = path.split("/").filter((seg) => seg && seg !== ".");
  let words = segs.map((seg, i) => spokenSegment(seg, i === segs.length - 1)).join(", ");
  if (line) words += " line " + line;
  return tech(words, s);
}

function isPath(token: string): boolean {
  if (/^(\/|~\/|\.\/|\.\.\/)/.test(token)) return true;
  if ((token.match(/\//g) || []).length >= 2) return true;
  const parts = token.replace(/\/+$/, "").split("/");
  const tail = parts[parts.length - 1];
  const ext = tail.includes(".") ? tail.slice(tail.lastIndexOf(".") + 1).toLowerCase() : "";
  return !!ext && ext in TABLES.ext;
}

function paths(text: string, s: Shaper): string {
  text = text.replace(PATH, (whole: string, pre: string, tokenIn: string, line: string | undefined) => {
    let token = tokenIn;
    let tail = "";
    while (token.endsWith(".") || token.endsWith(",")) {
      tail = token.slice(-1) + tail;
      token = token.slice(0, -1);
    }
    if (!isPath(token) || !/[A-Za-z]/.test(token)) return whole;
    return pre + s.hold(pathWords(token, line, s)) + tail;
  });
  text = text.replace(FILE, (_whole: string, pre: string, name: string, ext: string, line: string | undefined) => {
    const stem = name.slice(0, name.length - ext.length - 1);
    let spoken = spokenName(stem) + " dot " + TABLES.ext[ext.toLowerCase()];
    if (line) spoken += " line " + line;
    return pre + s.hold(tech(spoken, s));
  });
  return text.replace(SHA, (whole: string, pre: string, h: string) => {
    if (!/\d/.test(h) || !/[a-f]/.test(h)) return whole;
    return pre + s.hold(h.slice(0, 7).split("").join(" "));
  });
}

// --- 4. numbers ---------------------------------------------------------------- //
const VERSION = /(^|[^A-Za-z0-9])[vV](\d+(?:\.\d+)+)(?![A-Za-z0-9]|\.\d)/g;
const DOTTED = /(^|[^A-Za-z0-9.])(\d+\.\d+(?:\.\d+)+)(?![A-Za-z0-9]|\.\d)/g;
const PR = /(^|[^A-Za-z0-9])(?:PR|pr|Pr)[ \t]?#?(\d+)(?![A-Za-z0-9])/g;
const ISSUE = /(^|[^A-Za-z0-9])(issue|Issue|ticket|Ticket)[ \t]?#(\d+)(?![A-Za-z0-9])/g;
const HASH = /(^|[^A-Za-z0-9&#])#(\d+)(?![A-Za-z0-9])/g;
const DURATION =
  /(^|[^A-Za-z0-9.])(\d+)(h|hr|hrs)(?:[ \t]?(\d+)(m|min|mins))?(?:[ \t]?(\d+)(s|sec|secs))?(?![A-Za-z0-9])|(^|[^A-Za-z0-9.])(\d+)(m|min|mins)[ \t]?(\d+)(s|sec|secs)(?![A-Za-z0-9])/g;
const DURATION_ONE = /(^|[^A-Za-z0-9.])(\d+(?:\.\d+)?)(ms|h|hr|hrs|m|min|mins|s|sec|secs)(?![A-Za-z0-9])/g;
const UNIT = new RegExp(
  "(^|[^A-Za-z0-9.])(\\d+(?:\\.\\d+)?)[ \\t]?(" + byLength(Object.keys(TABLES.units)).join("|") + ")(?![A-Za-z0-9])",
  "gi"
);
const THOUSANDS = /(^|[^A-Za-z0-9.,])(\d{1,3}(?:,\d{3})+)(?![A-Za-z0-9,])/g;

const plural = (n: string, pair: [string, string]) => (n === "1" || n === "1.0" ? pair[0] : pair[1]);

function numbers(text: string): string {
  text = text.replace(VERSION, (_w, pre: string, v: string) => pre + "version " + v.split(".").join(" point "));
  text = text.replace(DOTTED, (_w, pre: string, v: string) => pre + v.split(".").join(" point "));
  text = text.replace(PR, (_w, pre: string, n: string) => pre + "P R " + n);
  text = text.replace(ISSUE, (_w, pre: string, kind: string, n: string) => pre + kind + " " + n);
  text = text.replace(HASH, (_w, pre: string, n: string) => pre + "number " + n);
  text = text.replace(DURATION, (...g: unknown[]) => {
    const d = TABLES.durations;
    const a = g as string[];
    if (a[2] !== undefined) {
      let out = a[1] + a[2] + " " + plural(a[2], d[a[3]]);
      if (a[4]) out += " " + a[4] + " " + plural(a[4], d[a[5]]);
      if (a[6]) out += " " + a[6] + " " + plural(a[6], d[a[7]]);
      return out;
    }
    return a[8] + a[9] + " " + plural(a[9], d[a[10]]) + " " + a[11] + " " + plural(a[11], d[a[12]]);
  });
  text = text.replace(DURATION_ONE, (_w, pre: string, n: string, unit: string) => {
    const pair = unit === "ms" ? TABLES.units.ms : TABLES.durations[unit];
    return pre + n + " " + plural(n, pair);
  });
  text = text.replace(UNIT, (_w, pre: string, n: string, unit: string) => pre + n + " " + plural(n, TABLES.units[unit.toLowerCase()]));
  return text;
}

// --- 5. identifiers ------------------------------------------------------------ //
const IDENT = /(^|[^A-Za-z0-9_'-])([A-Za-z0-9]+(?:[_-][A-Za-z0-9]+)*)(?![A-Za-z0-9_])/g;
const CAMEL_LOWER_UPPER = /([a-z0-9])([A-Z])/g;
const CAMEL_ACRONYM = /([A-Z]+)([A-Z][a-z])/g;
const LETTER_DIGIT = /([A-Za-z])(\d)/g;
const ALL_DIGITS = /^[\d_-]+$/;

function splitPart(part: string): string {
  let split = part.replace(CAMEL_ACRONYM, "$1 $2");
  split = split.replace(CAMEL_LOWER_UPPER, "$1 $2");
  if (split !== part) {
    split = split
      .split(" ")
      .map((w) => (isUpperWord(w) ? w : w.toLowerCase()))
      .join(" ");
  }
  return split.replace(LETTER_DIGIT, "$1 $2");
}

function identifiers(text: string, s: Shaper): string {
  const t = TABLES.tech;
  return text.replace(IDENT, (whole: string, pre: string, token: string) => {
    const low = token.toLowerCase();
    if (low in t) return pre + s.hold(matchCase(token, t[low]));
    if (ALL_DIGITS.test(token)) return whole;
    const parts = token.split(/[_-]/);
    if (parts.length === 1 && splitPart(token) === token) return whole;
    const spoken: string[] = [];
    for (const p of parts) {
      if (!p) continue;
      const pl = p.toLowerCase();
      spoken.push(pl in t ? s.hold(matchCase(p, t[pl])) : splitPart(p));
    }
    return pre + spoken.join(" ");
  });
}

// --- 6. tech words and 1. overrides ------------------------------------------- //
function tableRegex(keys: string[]): RegExp {
  return new RegExp("(^|[^A-Za-z0-9])(" + byLength(keys).map(esc).join("|") + ")(?![A-Za-z0-9])", "gi");
}

const TECH_RE = tableRegex(Object.keys(TABLES.tech));

function tech(text: string, s: Shaper): string {
  const t = TABLES.tech;
  return text.replace(TECH_RE, (whole: string, pre: string, token: string) => {
    const spoken = t[token.toLowerCase()];
    if (spoken === undefined) return whole;
    return pre + s.hold(matchCase(token, spoken));
  });
}

function applyOverrides(text: string, rules: PronounceRule[], s: Shaper): string {
  if (!rules.length) return text;
  const table: Record<string, string> = {};
  for (const r of rules) table[r.say] = r.as;
  const alts = byLength(Object.keys(table))
    .map((k) => esc(k).replace(/ /g, "[ \\t]+"))
    .join("|");
  const pattern = new RegExp("(^|[^A-Za-z0-9])(" + alts + ")(?![A-Za-z0-9])", "gi");
  return text.replace(pattern, (whole: string, pre: string, token: string) => {
    const spoken = table[token.toLowerCase().replace(/[ \t]+/g, " ")];
    if (spoken === undefined) return whole;
    return pre + s.hold(matchCase(token, spoken));
  });
}

// --- 7. heteronyms ------------------------------------------------------------- //
interface CompiledHeteronym {
  re: RegExp;
  as: string;
  respell: boolean;
  keepPrev: Set<string>;
  keepNext: Set<string>;
  respellPrev: Set<string>;
  respellNext: Set<string>;
  respellNextRe: RegExp | null;
  keepNextRe: RegExp | null;
}

const HETERONYMS: CompiledHeteronym[] = TABLES.heteronyms.map((rule) => ({
  re: new RegExp(bounded(rule.word), "gi"),
  as: rule.as,
  respell: rule.default === "respell",
  keepPrev: new Set(rule.keep_prev || []),
  keepNext: new Set(rule.keep_next || []),
  respellPrev: new Set(rule.respell_prev || []),
  respellNext: new Set(rule.respell_next || []),
  respellNextRe: rule.respell_next_re ? new RegExp("^[ \\t]*" + rule.respell_next_re, "i") : null,
  keepNextRe: rule.keep_next_re ? new RegExp("^[ \\t]*" + rule.keep_next_re, "i") : null,
}));

function decide(rule: CompiledHeteronym, prev: string, next: string, after: string): boolean {
  if (rule.respell) {
    if (rule.keepPrev.has(prev)) return false;
    // "goes live in production": a respell-context before the word beats a keep-context after it.
    if (rule.respellPrev.has(prev)) return true;
    if (rule.respellNextRe && rule.respellNextRe.test(after)) return true;
    if (rule.keepNext.has(next)) return false;
    if (rule.keepNextRe && rule.keepNextRe.test(after)) return false;
    return true;
  }
  if (rule.keepPrev.has(prev) || rule.keepNext.has(next)) return false;
  if (rule.respellPrev.has(prev) || rule.respellNext.has(next)) return true;
  if (rule.respellNextRe && rule.respellNextRe.test(after)) return true;
  return false;
}

function heteronyms(text: string): string {
  for (const rule of HETERONYMS) {
    const now = text;
    text = now.replace(rule.re, (whole: string, pre: string, word: string, offset: number) => {
      const start = offset + pre.length;
      const end = start + word.length;
      if (decide(rule, prevWord(now, start), nextWord(now, end), now.slice(end, end + 48))) {
        return pre + matchCase(word, rule.as);
      }
      return whole;
    });
  }
  return text;
}

// --- the entry points ----------------------------------------------------------- //
const SAY_OK = /^[a-z0-9][a-z0-9 '.+#_-]*$/;
const AS_BAD = /[\x00-\x1f\x7f<>`]/;

/** The saved form of `voice.pronounce` (spoken_text.normalize_pronounce):
 * `say` lower case, both sides trimmed and 1–40 characters, duplicates and
 * junk dropped, at most 40 rows. */
export function normalizePronounce(raw: unknown): PronounceRule[] {
  if (!Array.isArray(raw)) return [];
  const out: PronounceRule[] = [];
  const seen = new Set<string>();
  for (const row of raw) {
    if (!row || typeof row !== "object") continue;
    const r = row as Record<string, unknown>;
    if (typeof r.say !== "string" || typeof r.as !== "string") continue;
    const say = r.say.replace(/’/g, "'").replace(/\s+/g, " ").trim().toLowerCase();
    const spoken = r.as.replace(/’/g, "'").replace(/\s+/g, " ").trim();
    if (!say || !spoken || say.length > PRONOUNCE_MAX_CHARS || spoken.length > PRONOUNCE_MAX_CHARS) continue;
    if (!SAY_OK.test(say) || AS_BAD.test(spoken) || seen.has(say)) continue;
    seen.add(say);
    out.push({ say, as: spoken });
    if (out.length === PRONOUNCE_MAX) break;
  }
  return out;
}

/** The person's pronunciations as the server last served them
 * (`GET /api/voice` → `pronounce`; lib/voiceDesktop's loadVoiceCfg sets
 * them): what the browser's own voice shapes with. */
let served: PronounceRule[] = [];

/** Keep `raw` (`pronounce` from GET /api/voice, any state) as the
 * pronunciations {@link pronounce} returns; a non-list clears them. */
export function setPronounce(raw: unknown): void {
  served = normalizePronounce(raw);
}

/** The pronunciations {@link setPronounce} kept. */
export function pronounce(): PronounceRule[] {
  return served;
}

/** `text` as a voice should read it. `overrides` is `voice.pronounce` in
 * any state (raw, normalised, or nothing). Pure; a non-string is "". */
export function shapeForSpeech(text: unknown, overrides?: unknown): string {
  if (typeof text !== "string" || !text) return "";
  const s = new Shaper();
  let t = text.slice(0, MAX_IN);
  t = applyOverrides(t, normalizePronounce(overrides), s);
  t = stripMarkup(t);
  t = paths(t, s);
  t = numbers(t);
  t = identifiers(t, s);
  t = tech(t, s);
  t = heteronyms(t);
  t = t.replace(THOUSANDS, (_w, pre: string, n: string) => pre + n.replace(/,/g, ""));
  t = t.replace(/[ \t]+/g, " ").replace(/[ \t]*\n[ \t\n]*/g, "\n").trim();
  t = s.release(t);
  if (t.length > MAX_OUT) {
    const cut = t.lastIndexOf(" ", MAX_OUT - 1);
    t = t.slice(0, cut > MAX_OUT / 2 ? cut : MAX_OUT).replace(/\s+$/, "");
  }
  return t;
}


/** `window.MindFlockSpokenText.shape` in the static twin. */
export const shape = shapeForSpeech;
