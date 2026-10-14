/** Middle truncation for a session name: the end of a name is often the only
 * part that tells two sessions apart ("add-readme-section-about-setup" vs
 * "…-setup-2"), so a long name keeps its tail and gives way in the middle
 * ("add-readme…setup-2"). The split is plain text; CSS ellipsizes the head
 * and never the tail (components/sidebar/MidName). */

/** Names this short are never split — there is no middle worth cutting. */
const MIN_SPLIT = 14;

/** [head, tail]: the tail is always drawn whole. A numeric suffix ("-2",
 * " (3)", "#4") is always inside it, with a few characters of context. */
export function splitName(name: string): [string, string] {
  if (name.length < MIN_SPLIT) return [name, ""];
  const m = /(?:[-_ .#]\d+|\s?\(\d+\))$/.exec(name);
  const n = Math.min(name.length - 4, Math.max(m ? m[0].length + 5 : 0, Math.min(8, Math.floor(name.length / 3))));
  return [name.slice(0, name.length - n), name.slice(name.length - n)];
}
