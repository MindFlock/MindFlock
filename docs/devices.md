# Your devices, your phone, and people: where trust lives

MindFlock has **two ways to trust another machine**, and each control is on
exactly one Settings screen (the **Devices and people** group). This page is
the map; the API behind it is in [web-api.md](web-api.md) (Your devices,
Remote devices, Peer links, Auth), the invite/join flow in
[peer-link.md](peer-link.md).

| Screen (key) | What is there | What is NOT there |
|---|---|---|
| **Your devices** (`devices`) | Tailscale on this device · **Make reachable** (the one Tailscale-mode switch; **Restart server to apply** when needed) · the roster · **Add a device** (pair by code) / **Ask to join** → **Approve** · Paste a code · **Run here** (`github.automation_device`) · **Settings sync** · **One link for all devices** (the shared phone link + its setup checklist) · **Advanced**: Trusted Tailscale accounts, Allow remote control, Pair by pasted token | the access token |
| **Your phone** (`mobile`) | the three steps in order (Tailscale on this computer — with the doctor's diagnosis when it's missing; Tailscale on the phone; scan) · **one** QR · the `/m` links · a pointer to Your devices → Make reachable when the server is local-only | the token, the Tailscale-mode switch, the shared-link setup |
| **People** (`peer`) | **Invite someone** / **Join someone**, links, the shared folder, messages; Advanced: the peer-links switch, relay, listen address | anything about your own devices |
| **Security** (`security`) | **Require access token** (auto / on / off) · this device's token (Show / Copy / **Regenerate**) · "Trusting your own devices lives in Your devices" | trusted accounts, remote control, token pairing, settings sync |

## The two ways

1. **Your own computers → Your devices.** They join one group by code (or
   Ask → Approve) and share a fleet key: settings sync, sign-in, ticket
   claims, "Run here", the phone link. No token to paste. A computer the
   others can't reach (bound to 127.0.0.1) turns on **Make reachable** —
   Tailscale mode *with* the access gate, one save, MindFlock restarts once.
   Off again = this computer only.
2. **Other people → People.** A one-time invite (`mfp1:`/`mfp2:`), a
   safety number to compare, one shared folder per link, sandboxed. Nothing
   on your machine beyond that folder is exposed.

## Advanced (Your devices → Advanced): when you'd still use the older routes

- **Trusted Tailscale accounts** — a phone or laptop that doesn't run
  MindFlock at all: signed in to Tailscale as a ticked account, its browser
  opens this device without the access token. Tagged and shared-in devices
  still need it. (`general.tailnet_trusted_logins`; was on Security.)
- **Allow remote control** — a MindFlock too old to join your devices, or one
  you don't own: with it on, a MindFlock holding this device's access token
  lists and drives its sessions. (`general.remote_control`; was on Security.)
- **Pair by pasted token** — the other half: paste that device's token
  (Security there, or `mindflock token`) to see and drive its sessions. The
  sidebar's **Connect…** on such a device lands here with it preselected.
  (`POST /api/devices/{device}/connect`; was the sidebar's Connect dialog and
  Security's *Pair a device you don't own*.)

## Keys and deep links

The screen keys never changed: `devices`, `mobile`, `peer`, `security`. The
new names and the moved controls' names route too
(`frontend/src/components/settings/screenKeys.ts`): `your-devices`,
`your-phone` / `phone`, `people` / `peer-links`, and `remote-control`,
`tailnet-trust`, `trusted-accounts`, `pair-by-token`, `settings-sync`,
`make-reachable`, `shared-link` → Your devices. Palette: **Your devices**,
**Your phone**, **People…**, **Paste a code…**.

`tests/settingsScreens.test.ts` (vitest) pins the once-only rule: the token
display, the Tailscale-mode switch and the trusted-accounts list are each
rendered by one screen, found by their `data-control` attribute.
