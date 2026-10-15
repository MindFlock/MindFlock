/** The contract, rendered: the little Markdown a contract uses — headings,
 * bullet and numbered lists, code fences, paragraphs, `code`, **bold** and
 * *emphasis*. Built as React elements (never innerHTML), so the other side's
 * text can't inject markup. Anything else shows as plain text. */

import type { ReactNode } from "react";

type Block =
  | { kind: "h"; level: number; text: string }
  | { kind: "ul" | "ol"; items: string[] }
  | { kind: "code"; text: string }
  | { kind: "p"; text: string };

/** Split Markdown into blocks (pure — tested). */
export function contractBlocks(md: string): Block[] {
  const lines = String(md || "").replace(/\r\n?/g, "\n").split("\n");
  const out: Block[] = [];
  let para: string[] = [];
  const flush = () => {
    if (para.length) out.push({ kind: "p", text: para.join(" ") });
    para = [];
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (/^\s*```/.test(line)) {
      flush();
      const body: string[] = [];
      for (i++; i < lines.length && !/^\s*```/.test(lines[i]); i++) body.push(lines[i]);
      out.push({ kind: "code", text: body.join("\n") });
      continue;
    }
    const h = /^(#{1,6})\s+(.*)$/.exec(line);
    if (h) {
      flush();
      out.push({ kind: "h", level: h[1].length, text: h[2].trim() });
      continue;
    }
    const li = /^\s*([-*+]|\d+[.)])\s+(.*)$/.exec(line);
    if (li) {
      flush();
      const kind = /\d/.test(li[1]) ? "ol" : "ul";
      const last = out[out.length - 1];
      if (last && last.kind === kind) last.items.push(li[2]);
      else out.push({ kind, items: [li[2]] });
      continue;
    }
    if (!line.trim()) {
      flush();
      continue;
    }
    para.push(line.trim());
  }
  flush();
  return out;
}

/** `code`, **bold**, *em* — everything else stays text. */
function inline(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /(`[^`]+`|\*\*[^*]+\*\*|\*[^*\s][^*]*\*)/g;
  let at = 0;
  let m: RegExpExecArray | null;
  let k = 0;
  while ((m = re.exec(text))) {
    if (m.index > at) out.push(text.slice(at, m.index));
    const t = m[0];
    if (t.startsWith("`")) out.push(<code key={k++}>{t.slice(1, -1)}</code>);
    else if (t.startsWith("**")) out.push(<strong key={k++}>{t.slice(2, -2)}</strong>);
    else out.push(<em key={k++}>{t.slice(1, -1)}</em>);
    at = m.index + t.length;
  }
  if (at < text.length) out.push(text.slice(at));
  return out;
}

export function ContractMarkdown({ text, id, empty = "No contract yet." }: { text: string; id?: string; empty?: string }) {
  const blocks = contractBlocks(text);
  if (!blocks.length) return <p className="set-hint peer-md-empty">{empty}</p>;
  return (
    <div className="peer-md" id={id}>
      {blocks.map((b, i) => {
        switch (b.kind) {
          case "h": {
            // Contract headings sit under the card's own: h5 for ##, h6 below.
            const Tag = b.level <= 2 ? "h5" : "h6";
            return <Tag key={i}>{inline(b.text)}</Tag>;
          }
          case "ul":
          case "ol": {
            const Tag = b.kind;
            return (
              <Tag key={i}>
                {b.items.map((it, j) => (
                  <li key={j}>{inline(it)}</li>
                ))}
              </Tag>
            );
          }
          case "code":
            return <pre key={i}>{b.text}</pre>;
          default:
            return <p key={i}>{inline(b.text)}</p>;
        }
      })}
    </div>
  );
}
