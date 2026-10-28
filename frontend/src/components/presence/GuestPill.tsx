/** A guest's pill on the rail row they are looking at (lib/guestFocus):
 * their name, moving from row to row as they click; press it to follow them
 * — your selection then moves with theirs — press again to stop. Nothing
 * while no guest is on the row. Styles: `.guest-pill` in SidebarRow.css. */

import { guestName, toggleFollow, useGuestFocus } from "../../lib/guestFocus";
import { openChat } from "../../lib/chat";

export function GuestPills({ title }: { title: string }) {
  const { focus, following } = useGuestFocus();
  const here = Object.values(focus).filter((f) => f.title === title);
  if (!here.length) return null;
  return (
    <span className="guest-pills">
      {here.map((f) => {
        const name = guestName(f.label);
        const on = following === f.link;
        return (
          <button
            key={f.link}
            type="button"
            className={"guest-pill" + (on ? " following" : "")}
            data-guest={f.link}
            title={on ? `Following ${name} — click to stop` : `${name} is on this session — click to follow them`}
            aria-pressed={on}
            onClick={(e) => {
              e.stopPropagation();
              toggleFollow(f.link);
            }}
            onDoubleClick={(e) => e.stopPropagation()}
          >
            {on ? "▶ " : ""}
            {name}
          </button>
        );
      })}
      <button
        type="button"
        className="guest-pill guest-pill-chat"
        title="Open the chat for this session"
        aria-label="Open the chat for this session"
        onClick={(e) => {
          e.stopPropagation();
          openChat(title);
        }}
        onDoubleClick={(e) => e.stopPropagation()}
      >
        💬
      </button>
    </span>
  );
}
