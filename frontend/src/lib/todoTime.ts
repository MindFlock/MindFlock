/** Timed todos (the Assistant's todo list): `due` is a LOCAL minute,
 * "YYYY-MM-DDTHH:MM" — the same string the server and the assistant write
 * (backend/web/addons/assistant.py `_DUE_FMT`) and an <input type=
 * "datetime-local"> reads. Pure helpers: parsing "… tue 9am" off the end of a
 * new todo, and saying a due time in words. */

const DAYS = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"];
const DAY_ALIASES: Record<string, number> = {
  sun: 0, sunday: 0,
  mon: 1, monday: 1,
  tue: 2, tues: 2, tuesday: 2,
  wed: 3, weds: 3, wednesday: 3,
  thu: 4, thur: 4, thurs: 4, thursday: 4,
  fri: 5, friday: 5,
  sat: 6, saturday: 6,
};
/** A day named without a time: the morning. */
const DEFAULT_HOUR = 9;

const pad = (n: number) => String(n).padStart(2, "0");

/** A Date → "YYYY-MM-DDTHH:MM" in local time. */
export function toDue(d: Date): string {
  return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + "T" + pad(d.getHours()) + ":" + pad(d.getMinutes());
}

/** "YYYY-MM-DDTHH:MM" → a local Date, or null. */
export function fromDue(due: string | undefined | null): Date | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(String(due || ""));
  if (!m) return null;
  const d = new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], 0, 0);
  return isNaN(d.getTime()) ? null : d;
}

function clock(hRaw: string, mRaw: string | undefined, ampm: string | undefined): [number, number] | null {
  let h = +hRaw;
  const m = mRaw ? +mRaw : 0;
  if (m > 59) return null;
  const ap = (ampm || "").toLowerCase().replace(/\./g, "");
  if (ap === "pm" && h < 12) h += 12;
  else if (ap === "am" && h === 12) h = 0;
  else if (!ap && h >= 1 && h <= 6) h += 12; // "at 3" means the afternoon
  if (h > 23) return null;
  return [h, m];
}

const TIME = String.raw`(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?`;
const DAY = String.raw`(today|tonight|tomorrow|tmrw|tmr|` + Object.keys(DAY_ALIASES).join("|") + ")";
const RE_IN = new RegExp(String.raw`\s+in\s+(\d+)\s*(m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)\s*$`, "i");
const RE_DAY_TIME = new RegExp(String.raw`\s+(?:on\s+|next\s+)?` + DAY + String.raw`(?:\s+` + TIME + String.raw`)?\s*$`, "i");
const RE_TIME_DAY = new RegExp(String.raw`\s+` + TIME + String.raw`\s+(?:on\s+)?` + DAY + String.raw`\s*$`, "i");
const RE_TIME = new RegExp(String.raw`\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\s*$`, "i");

function onDay(day: string, now: Date): Date {
  const d = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const key = day.toLowerCase();
  if (key === "today" || key === "tonight") return d;
  if (key === "tomorrow" || key === "tmrw" || key === "tmr") {
    d.setDate(d.getDate() + 1);
    return d;
  }
  const want = DAY_ALIASES[key];
  let ahead = (want - d.getDay() + 7) % 7;
  if (ahead === 0) ahead = 7; // "tuesday" said on a Tuesday means next week…
  d.setDate(d.getDate() + ahead);
  return d;
}

/** "Call the bank tue 9am" → `{text: "Call the bank", due: "…T09:00"}`; null
 * when the end of the text names no time. Understands "in 20 min / 2 hours /
 * 3 days", "<day> [at] <time>", "<time> <day>", "at <time>" (today, or
 * tomorrow once it has passed), and a day alone (9 AM). "tonight" alone is
 * 8 PM. A weekday said on that weekday with a time still to come is today. */
export function parseWhen(input: string, now: Date = new Date()): { text: string; due: string } | null {
  const raw = " " + String(input || "").trim();
  let m = RE_IN.exec(raw);
  if (m) {
    const n = +m[1];
    const unit = m[2].toLowerCase();
    const ms = unit.startsWith("d") ? 86400e3 : unit.startsWith("h") ? 3600e3 : 60e3;
    const text = raw.slice(0, m.index).trim();
    if (!text || !n) return null;
    return { text, due: toDue(new Date(now.getTime() + n * ms)) };
  }
  // "<time> <day>" before "<day> [time]": the latter also matches a bare
  // day at the end, which would leave "3pm" of "3pm friday" in the text.
  m = RE_TIME_DAY.exec(raw);
  let day: string | undefined;
  let hm: [number, number] | null = null;
  if (m) {
    hm = clock(m[1], m[2], m[3]);
    day = m[4];
  } else if ((m = RE_DAY_TIME.exec(raw))) {
    day = m[1];
    if (m[2]) hm = clock(m[2], m[3], m[4]);
    else hm = day.toLowerCase() === "tonight" ? [20, 0] : [DEFAULT_HOUR, 0];
  } else if ((m = RE_TIME.exec(raw))) {
    hm = clock(m[1], m[2], m[3]);
  }
  if (!m || !hm) return null;
  const text = raw.slice(0, m.index).trim();
  if (!text) return null;
  let d: Date;
  if (day) {
    d = onDay(day, now);
    const key = day.toLowerCase();
    // A weekday named on that very weekday, with the time still ahead: today.
    if (key in DAY_ALIASES && DAY_ALIASES[key] === now.getDay()) {
      const today = new Date(now.getFullYear(), now.getMonth(), now.getDate(), hm[0], hm[1]);
      if (today > now) d = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    }
  } else {
    d = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  }
  d.setHours(hm[0], hm[1], 0, 0);
  if (!day && d <= now) d.setDate(d.getDate() + 1); // "at 9am" once 9 has passed
  return { text, due: toDue(d) };
}

function clockText(d: Date): string {
  let h = d.getHours();
  const ap = h >= 12 ? "PM" : "AM";
  h = h % 12 || 12;
  return h + ":" + pad(d.getMinutes()) + " " + ap;
}

/** A due time in words: "Today 9:00 AM", "Tomorrow 9:00 AM", "Tue 9:00 AM"
 * (within the week ahead), else "Nov 18 9:00 AM" ("…, 2027" another year). */
export function formatDue(due: string | undefined | null, now: Date = new Date()): string {
  const d = fromDue(due);
  if (!d) return "";
  const day0 = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const dd = new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((dd - day0) / 86400e3);
  let lead: string;
  if (days === 0) lead = "Today";
  else if (days === 1) lead = "Tomorrow";
  else if (days === -1) lead = "Yesterday";
  else if (days > 1 && days < 7) lead = DAYS[d.getDay()].slice(0, 1).toUpperCase() + DAYS[d.getDay()].slice(1, 3);
  else {
    lead = d.toLocaleString("en-US", { month: "short", day: "numeric" });
    if (d.getFullYear() !== now.getFullYear()) lead += ", " + d.getFullYear();
  }
  return lead + " " + clockText(d);
}

/** Whether the due time has come (at it or after). */
export function isOverdue(due: string | undefined | null, now: Date = new Date()): boolean {
  const d = fromDue(due);
  return !!d && d.getTime() <= now.getTime();
}

/** The snooze choices the reminder dialog offers → `{label, until}` (a due
 * string): 10 minutes, 1 hour, tomorrow 9 AM. */
export function snoozeChoices(now: Date = new Date()): { label: string; until: string }[] {
  const t9 = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1, DEFAULT_HOUR, 0);
  return [
    { label: "10 minutes", until: toDue(new Date(now.getTime() + 10 * 60e3)) },
    { label: "1 hour", until: toDue(new Date(now.getTime() + 60 * 60e3)) },
    { label: "Tomorrow 9 AM", until: toDue(t9) },
  ];
}
