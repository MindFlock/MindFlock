/** A session name that ellipsizes in the MIDDLE (lib/midName): drawn inside
 * the caller's own title span, so its font, tooltip and click stay theirs. */

import { splitName } from "../../lib/midName";

export function MidName({ text }: { text: string }) {
  const [head, tail] = splitName(text);
  if (!tail) return <>{text}</>;
  return (
    <span className="mid-name">
      <span className="mid-head">{head}</span>
      <span className="mid-tail">{tail}</span>
    </span>
  );
}
