# Your devices without Tailscale ("Reach my devices from anywhere")

Your own computers (the fleet, [devices.md](devices.md)) find each other
through Tailscale today. This feature adds a second path that needs nothing
installed by hand, no sign-up and **no central MindFlock server**:

- **Carrier:** a Cloudflare quick tunnel on each device, carrying the same
  pinned-key TLS 1.3 inside a WebSocket that peer links already use
  ([peer-link.md → Connecting across networks](peer-link.md#connecting-across-networks)).
  Cloudflare carries bytes it cannot read.
- **Address book:** a quick tunnel gets a new hostname every time it starts.
  Each device writes its current address, **encrypted and authenticated with
  a key derived from the fleet key**, into a private repo in the user's own
  GitHub account. The other devices read it from there.
- **Path choice is automatic.** Discovery tries the paths it already knows
  first: the tailnet address, then MagicDNS. The tunnel is tried only for a
  member those paths can't reach.

It is **off by default**: one switch, Settings → Your devices → **Reach my
devices from anywhere (no Tailscale needed)** (`general.devices_anywhere`).
Tailscale stays fully supported. It is the sturdier option, with no third
party in the path and a stable address.

Code: `backend/peer/fleet_record.py` (record crypto),
`backend/peer/fleet_tunnel.py` (carrier, handshake, local proxies),
`backend/web/core/github_records.py` (the GitHub store),
`backend/web/core/devices_anywhere.py` (lifecycle, reader, status).

## 1. Where in GitHub the record lives

| | Secret gist | Private repo (`<login>/mindflock-devices`) |
|---|---|---|
| Access control | **None.** "Secret" only means unlisted; anyone with the URL reads it. Acceptable only because the content is encrypted. | Only the owner (and collaborators they add). |
| Classic-token scope | `gist` | `repo` |
| `gh auth login` default token | has it (`repo`, `read:org`, `gist`) | has it |
| **MindFlock's own sign-in** (device flow, `SCOPES = "repo read:org"`; the pre-filled PAT page asks for `repo,read:org`) | **lacks it.** Every user who connected GitHub inside MindFlock would hit "missing permission" and have to reconnect. | has it |
| Fine-grained token | "Gists: read and write" | "Administration: write" (to create the repo once) + "Contents: read and write" on it. Or create the repo by hand and grant only Contents. |
| History | gist revisions | commits |

**Decision: a private repo**, `mindflock-devices`, owned by the signed-in
GitHub user, written through the Contents API. It works with every token
MindFlock already resolves (`github_auth.resolve_full_chain`: Settings
token → `GH_TOKEN`/`GITHUB_TOKEN` → `gh auth token`), so nobody has to sign
in again. It is also access-controlled, where a gist is only unlisted.

- The repo is created on first publish (`POST /user/repos`, `private: true`,
  `auto_init: true`, an opaque description). An existing repo with that
  name is used only if GitHub reports it **private** and it is exactly
  `<signed-in login>/mindflock-devices`, **owned by that login**
  (`full_name` and `owner.login`). A public one, or one owned by someone
  else, is refused with a plain message.
- **Redirects are never followed.** A renamed or transferred repo answers
  `301` to wherever it went (possibly another account); the store treats
  that as "moved" and stops, so neither a record nor the token goes
  anywhere it didn't choose.
- **Privacy is checked again before every write**, not once per process: a
  repo made public (or transferred) since the last write gets no new record.
- **Missing permission is never silent.** A classic token whose
  `X-OAuth-Scopes` lacks `repo` is refused before any call, and a 403/404 on
  the create/read/write calls (fine-grained tokens report no scopes) gets the
  same answer. The status line then says: *"GitHub sign-in is missing the
  repo permission — reconnect GitHub (Settings → GitHub), or give your
  fine-grained token access to a private repo named mindflock-devices
  (Contents: read and write)."* No token at all: *"Not signed in to GitHub —
  connect GitHub (Settings → GitHub) so your devices can find each other."*
- Every device must be signed in to the **same GitHub account**. Settings
  sync already shares the GitHub token across the fleet, so this is the
  normal case. A device on another account sees no records, and its status
  says how many of your devices it found.

## 2. Record format

One file per device at the repo root, `<slot>.rec`, where
`slot = hex(HMAC-SHA256(K_slot, device_key))[:32]`. The filename reveals
nothing without the fleet key: it is not a hash of anything public.

Keys come from the fleet key with **HKDF-SHA256** (`cryptography`, already a
dependency for peer identities; no new dependency):

```
ikm  = fleet key (utf-8)
salt = b"mindflock-devices-anywhere-v1"
info = b"<purpose>|" + fleet_id + b"|" + str(epoch)     purpose ∈ record, slot, auth
```

The record is **AES-256-GCM** (`cryptography.hazmat.primitives.ciphers.aead.AESGCM`):

```
file  = "mfd1." + base64url( ver(1)=0x01 | nonce(12, random) | ciphertext+tag )
aad   = b"mindflock-devices-anywhere-record|" + ver + b"|" + slot
plain = {"v":1, "fleet":<id>, "epoch":<n>, "device":<key>,
         "url":"wss://<label>.trycloudflare.com/<token>",
         "fp":<hex sha256(identity pub)[:16]>, "ctr":<int>, "ts":<epoch s>}
```

A reader accepts a record only if **all** of these hold, and otherwise
ignores it (the reason is counted, never logged with content):

- the version byte is known, and GCM authenticates under *this* fleet's
  current-epoch key. A tampered byte, another fleet's record or an old-epoch
  record fails here.
- `fleet` and `epoch` are ours, and `device` is a live roster member other
  than this device.
- the file's slot equals `slot(device)`, so a valid record copied into
  another device's file is rejected.
- `ts` is not more than 5 minutes ahead and not older than **3 days**. A
  publishing device refreshes it every 12 h.
- `ctr` is at least the highest counter this reader has accepted for that
  device. The counter is persisted, so old repo history replayed after a
  restart is rejected. Writers use `ctr = max(last + 1, now_ms)`, which stays
  monotonic across a reinstall.
- `url` is a `wss://` address on `*.trycloudflare.com` with a valid relay
  path, and `fp` is 32 hex characters.

Nothing readable is stored: no hostname, device name, IP or tunnel address
in clear. The commit message is the constant `"update"`. Plaintext and keys
are never logged.

## 3. Authentication of the connection

**Finding an address never grants trust.** The record says only *where to
dial*. The connection authenticates on its own:

1. **Outer TLS** to Cloudflare's edge (WebPKI). Defense in depth only.
2. **Inner TLS 1.3, pinned.** The device's server presents its peer
   identity cert (`backend/peer/identity.py`, Ed25519). Before sending any
   byte, the client checks `sha256(pub)[:16]` against the record's `fp`
   (constant time).
3. **Mutual fleet-key proof** inside that TLS (`fleet_tunnel.py`, frames
   from `backend/peer/wire.py`):

   ```
   S→C {"t":"hello","v":1,"nonce":sn}
   C→S {"t":"dev","v":1,"nonce":cn,"from":<client key>,"to":<server key>,
        "proof":HMAC(K_auth, "mfdev-client-v1" | server_pub | sn | cn | from | to)}
   S→C {"t":"ok","proof":HMAC(K_auth, "mfdev-server-v1" | server_pub | sn | cn | from | to)}
       or {"t":"denied"} and close
   ```

   The server checks that `to` is itself, `from` is a live member and the
   proof is valid before it sends anything. The client checks the server's
   proof before it sends a single application byte. The proofs bind the
   server key the client actually saw in TLS, so a man in the middle that
   relays to the real device fails (its own key is in the client's
   transcript). The fleet key itself never travels.
4. Only after that does the stream reach this device's HTTP server, and
   **the access gate still applies**: requests that came through the tunnel
   are marked (by their loopback source port, which the tunnel registers)
   and are treated as a tailnet caller with the gate **on**. The mark is a
   **counted hold** per port (`backend/peer/tunnel_peers.py`): a closed
   stream gives its hold back 5 s later (the server may still be finishing
   its last request), and a new stream handed the same ephemeral port in
   the meantime holds its own, so the late release never unmarks a live
   stream. Being marked a moment too long only ever makes a plain local
   caller look remote. So:
   - public paths only, unless they carry a valid token or the current fleet
     key;
   - never "from this machine", even with the gate off in local mode;
   - `X-Forwarded-For` is ignored for them.

   The client's requests carry the fleet key exactly as they do to a
   tailnet member today, inside the end-to-end TLS.

The tunnel speaks the **current key epoch only**. A device removed from the
group holds only an old key, so it can neither read new records nor pass the
handshake. Streams that are **already open** are ended too: every save of
the group's state (a removal rotates the key) and every 10 s tick ends each
incoming stream whose member left the roster or whose keys are no longer
the current ones (`TunnelServer.sweep`), and on a key change every stream
through this device's own proxies (`DeviceProxy.drop_streams`). A member
that was offline during a removal must be reached once over Tailscale or
the same network, or re-added.

**Someone who can write to the user's GitHub** (a stolen token, a
compromised device signed in to it) can delete or corrupt records, or put
back old ones. The worst outcome is a **failed connection**: without the
fleet key they can't produce a record that authenticates, and an old record
is rejected by `ctr`/`ts`. Even with a record (a hypothetical), the address
it names must still pass the pin and the mutual proof. Tests cover forged
addresses, forged pins and replays.

## 4. What the tunnel exposes

- `cloudflared` forwards only to the **relay ingress** (`RelayIngress`,
  loopback). It never forwards to the HTTP API.
- The ingress answers exactly one path, `/<token>`, where the token is
  **128 random bits** (base32, 26 characters), minted fresh each time the
  tunnel starts and carried only inside the encrypted record. Every other
  request gets the same empty `404` (no banner, no handshake slot used).
- Behind the path: the pinned TLS server and the proof above. A visitor
  without the fleet key gets `{"t":"denied"}` (or a timeout) and never a
  byte from the HTTP server.
- Pre-auth limits: at most 16 unauthenticated handshakes at once, a 10 s
  handshake deadline, and the ingress's own caps (32 pending heads, 64 open
  WebSockets).
- The process gets the same sandboxed environment as peer relays
  (`QuickTunnel`: private `HOME`, our own config, `--no-autoupdate`).
- **The origin is this server's own port, known for certain.** The server
  passes the port it was started on (`UVICORN_PORT`, which `mindflock
  serve` always exports, `PORT` or `--port`; never the 8765 guess) and the
  tunnel checks that this very process listens on it. If not, it refuses
  to start (*"this MindFlock server couldn't tell which port it listens
  on"*), so another device's requests are never piped to whatever else
  owns a guessed port.
- **cloudflared never outlives the server.** On Linux the child asks the
  kernel to SIGTERM it when the server dies (`PR_SET_PDEATHSIG`).
  Everywhere, each running cloudflared is registered
  (`<peer home>/cloudflared-pids/`: its pid, start time and exact command
  line, and the server's pid and start time), and at every server start
  (before any loopback listener opens) and every tunnel start a stale one
  is ended — matched on the recorded pid **and** start time **and** command
  line, never a pattern; a tunnel whose server still runs is left alone
  (two servers may share one state folder), and so is one whose pid now
  belongs to something else. Without this, a SIGKILLed server could leave
  a public hostname forwarding to a freed loopback port that something
  else binds later.
- **The local proxies take only this server's own client.** A connection
  to a `DeviceProxy` must open with a request addressed to that very
  listener (`Host: 127.0.0.1:<its port>`) carrying none of the headers a
  relay adds (`Cf-*`, `CDN-Loop`, `X-Forwarded-*`, `Forwarded`, …);
  anything else is closed before a byte is dialed. So even a stale tunnel
  that ended up pointing at a reused port gets nothing.

## 5. Lifecycle

- **Switch on** (this device only; the setting is per device and never
  synced; only `POST /api/devices/anywhere` changes it — the generic
  settings save drops the field, and that route refuses another MindFlock
  relaying and anything that came through the tunnel): start the ingress, start the quick tunnel, wait for its name, then
  **publish** the record. A tunnel that dies is restarted with backoff
  (5 s → 5 min). Each restart gets a new token and hostname and is published
  again. Publishing happens again on a key epoch change, and as a refresh
  every 12 h.
- **Readers** (every device with the switch on): read the records at start,
  then **on failure**. A member that answers on no path triggers a re-read,
  at most once a minute, doubling to 15 minutes while nothing changes. There
  is also a slow refresh every 10 minutes while some member is unreachable,
  and none while every member answers. File contents are only fetched when
  the file's git SHA changed.
- **Connecting** (discovery, every 20 s, `remote._discover_once`): a member
  the tailnet found and that answered uses the tailnet. Otherwise, if a
  record names an address, a local proxy (`127.0.0.1:<port>`, one per
  device) dials it, and `base_url` points there. Gossip, settings sync,
  session lists and `…/api/devices/<device>/fwd/…` all use `base_url`
  unchanged.
- **GitHub unreachable or rate-limiting** (403/429 with
  `X-RateLimit-Remaining: 0` or `Retry-After`): back off until the reset and
  keep the last addresses that were read. The status line says *"Couldn't
  reach GitHub — using the addresses found earlier; retrying in N min"*. The
  tailnet path is unaffected.
- **Cloudflare unreachable or rate-limiting:** the tunnel start fails with
  the peer tunnel's classified reason (`tunnel.FAILURE_TEXT`). The status
  line says *"Couldn't start the tunnel — <why> · Retry"*. Tailscale/direct
  keep working.
- **cloudflared missing:** the switch puts `cloudflared` in the doctor's
  install plan (the same one-click install People and guest links use,
  `doctor.check_cloudflared`), and the status line offers it.

## 6. Turning it off

Switching off stops `cloudflared`, the ingress and every local proxy, then
**deletes this device's record** from the repo (best effort; a failure is
shown). The repo itself is left in place, because other devices may still use
it. Leaving the fleet does the same. Removing a device rotates the fleet key,
which makes every record under the old key unreadable, and the remaining
devices republish under the new one.

## Threat model summary

| Threat | Defense |
|---|---|
| Cloudflare (or whoever answers the hostname) reads/alters/replays traffic | Inner pinned TLS 1.3 + mutual fleet-key proof; Cloudflare sees only TLS records. |
| A forged or stale address in GitHub | Records are AEAD under a fleet-derived key, slot-bound, counter- and age-checked. An address that still gets through only says where to dial: pin + proof fail → failed connection. |
| An internet scanner on the tunnel URL | One 128-bit path; everything else a constant 404; nothing reaches the HTTP API before the proof. |
| A tunneled request posing as "this machine" | Tunnel connections are registered by source port (a counted hold, so a closed stream's late release can't unmark a live one on the same port) and treated as remote, gate on; forwarded headers ignored. |
| Another device turning this device's tunnel on | Only `POST /api/devices/anywhere` sets the switch, and only from this device (never relayed, never tunneled); the generic settings save drops the field; settings sync never carries it. |
| A crash leaving cloudflared running toward a freed port | Linux PDEATHSIG; a pid registry and an exact-match reaper (pid + start time + command line) at server and tunnel start; the local proxies refuse anything not sent by this server's own client. |
| A renamed / transferred / re-published GitHub repo | No redirects followed; the repo must be `<login>/mindflock-devices`, owned by the login, private — re-checked before every write. |
| Piping to the wrong local port | The origin is the port the server was started on and that this process listens on; unknown → the tunnel doesn't start. |
| Another local user on the dialing computer | The local proxy (`127.0.0.1:<port>`) only pipes to the far device, which still requires a token or the fleet key, exactly like a tailnet caller. A proxy's port is never released while the switch is on, so the fleet-key bearer can't be sent to a process that took over a freed port. Turning the switch off first drops those devices from discovery. |
| A removed device | Holds only the old key: can't decrypt new records or pass the current-epoch handshake. Its open streams are ended when the key changes. |
| A malicious member | Same as today: the fleet key is a full credential on every member. Over the tunnel one member could also claim another's name (Tailscale binds names, the fleet key does not); it gains nothing it doesn't already have. |
| Metadata | GitHub sees when each device updates its record (and that a `mindflock-devices` repo exists). Cloudflare sees IPs, timing and volume, as for peer relays. |

## Manual check (about ten minutes)

Real GitHub and a real Cloudflare tunnel are not exercised by the test
suite (it uses a folder store and a loopback carrier). With two of your
devices, A and B, already in one group and both signed in to the same
GitHub account:

1. On A and on B: Settings → Your devices → turn on **Reach my devices from
   anywhere**. Each status line reaches *"On · reachable through a private
   tunnel"*.
2. On GitHub, open your private `mindflock-devices` repo: two `.rec` files,
   one per device, with opaque names and `mfd1.` ciphertext only (no host
   name, IP or address in clear), commit messages all `update`.
3. On A: `tailscale down`. Within a minute or two B's Your devices shows A
   reachable with `via: "tunnel"` (`GET /api/devices` / `GET /api/fleet`);
   A's sessions appear in B's list, a session started on A from B (device
   forwarding) works, and a settings change on one arrives on the other.
   `tailscale up` again afterwards: B goes back to `via: "tailnet"`.
4. On A: turn the switch off. A's `.rec` file disappears from the repo, and
   B stops listing A as reached through the tunnel.
5. Hardening spot checks: `mindflock doctor` on A shows the
   devices-anywhere row; after `kill -9` of A's server, no `cloudflared` of
   A's is left running (Linux at once; elsewhere at the next start of A's
   server); `POST /api/settings` with `{"general":{"devices_anywhere":true}}`
   leaves the switch off.

## Not covered in this round

- **Joining the group without Tailscale.** Pairing by code (`redeem`,
  `requests`) still needs the two devices to reach each other directly. The
  feature connects devices that are already in one group.
- **Same-network (LAN) discovery without Tailscale.** "Direct first" means
  the paths discovery already probes.
- A stable name (a named tunnel) and an in-app `cloudflared` download that
  needs no package manager.
