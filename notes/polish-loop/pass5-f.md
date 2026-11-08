# Pass 5 — (f) peer links

Walked as a new user on two loopback sandboxes (A :9510 / peer 9512 "Ana",
B :9511 / peer 9513 "Bob", relay off, listen 127.0.0.1, tours marked seen):
Settings → Show advanced settings → People; wrong / nonsense / reused codes;
Invite someone (warning → I understand → Which sessions? → Make an invite
without sessions); Join with the link; the joined cards and safety number on
both sides; Watch their session / Their changes before It matches; a message
B → A; It matches; Remove connection… and what B sees after. Compared
docs/peer-link.md "How to use" with the screen.

What a wrong code says (all fine): a reused or expired invite → "That invite
was already used or has expired — ask for a new one (each invite works once,
for 10 minutes)."; text with no code → "That doesn't contain an invite code —
paste the whole message you were sent (the code starts with mfp1: or
mfp2:)."; `mfp1:garbage` → "This invite is cut off — copy the whole message
again." (see #6).

| # | Sev | Finding | Repro/where | Status |
|---|-----|---------|-------------|--------|
| 1 | high | Every invite made without a public tunnel failed: "AttributeError: 'tuple' object has no attribute 'rstrip'" in the Which sessions? dialog. Pass 4 (0fc501a7) made `guests._local_base` answer `(address, live)`; `routes/peer._invite_base` still handed the pair to `invite_url`. The route's error log also used Go's `%v` (printed raw) and carried no traceback. `tests/unit/peer/test_invite_link.py::test_invite_answers_its_url` was already failing on polish-pass4. | People → Invite someone → I understand → Make an invite (no relay) | FIXED 77e676da |
| 2 | med | The invite message said "open Settings → People (or press Ctrl+P…)". People is an advanced screen, so a fresh install's Settings nav doesn't list it; the /join page already dropped the phrase for this reason (join trial 2026-10-28). | `service.invite_message` | FIXED bde2671f (now: press Ctrl+P, type "people", press Join someone…) |
| 3 | low | Remove connection… on a link where nothing was shared said "Their shared session is closed, not deleted; yours too unless you tick delete" and offered "also delete the sandbox copy of the shared folder" (lower-case). | pair, never share, Remove connection… | FIXED 40e709ef (names only the folders that exist; the delete tick shows only with a folder of yours) |
| 4 | low | The invite's red warning and its "I understand…" label had no font-size and inherited the page's 16px, larger than every other line on the screen (13px). The guest-link warning uses the same class. | People → Invite someone | FIXED 40e709ef |
| 5 | low | The docs disagreed with the screen: "unlink now" (the UI has It doesn't match), *Unshare* / *Unlink* (Stop sharing… / Remove connection…), the permission names (*send messages*, *see my diff* vs message me / see my changes), and no word that People is behind Show advanced settings. | docs/peer-link.md How to use, steps 1, 6, 9, 11 | FIXED 314c875a |
| 6 | low | `mfp1:` + junk is called "cut off — copy the whole message again", which misleads a wrong (not truncated) code. The check runs in the browser before the server sees it. | Join someone, paste `mfp1:garbage` | DEFERRED (outside fence: `frontend/src/lib/peer.ts:657` `isCutOffInvite`) |
| 7 | low | Local-only server, no tunnel: the invite LINK is `http://127.0.0.1:<port>/join#…`. That works only when the joiner runs MindFlock on the same port, because it opens their own /join page. The message under it still carries the code. | Make an invite with relay off | DEFERRED (design call: the e2e join trial relies on the loopback link; guest links hide theirs) |
| 8 | low | Watch their session is offered before the other person has shared anything. It opens "Ana's shared session · connected" with the safety-number gate; after It matches it says "Ana hasn't started a shared session." | link card → Talk → Watch their session | DEFERRED (minor) |

Not walked (budget): sharing a folder with a sandboxed agent, Share a
session…, joint tasks and the checklist, Mirror, reconnecting with a fresh
invite, peer links off/on.

Tests: `pytest tests/unit/test_peer_sessions.py tests/unit/peer/test_invite_shares.py
tests/unit/peer/test_service_safety.py tests/unit/peer/test_wire.py
tests/unit/peer/test_invite_link.py tests/unit/peer/test_integration_routes.py`
→ 305 passed; `vitest run peerUnlinkConfirm peopleCard peerSessions` → pass;
`tsc --noEmit` clean.
