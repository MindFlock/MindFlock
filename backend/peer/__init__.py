"""Peer links — two MindFlock users' instances, connected for pair coding.

See ``docs/peer-link.md`` for the design, the threat model and the wire
protocol. Module map:

* :mod:`backend.peer.paths`       — where peer state lives (0700 dirs)
* :mod:`backend.peer.identity`    — this instance's Ed25519 identity + TLS cert
* :mod:`backend.peer.invite`      — one-time pairing codes (in memory only)
* :mod:`backend.peer.store`       — persisted links (``links.json``, 0600)
* :mod:`backend.peer.wire`        — frame codec + strict per-op validators
* :mod:`backend.peer.transport`   — TLS listener/dialer, pair/auth handshakes
* :mod:`backend.peer.share`       — the one shared folder per link
* :mod:`backend.peer.sandbox`     — bubblewrap argv for the shared session
* :mod:`backend.peer.egress`      — allow-listed CONNECT proxy (unix socket)
* :mod:`backend.peer.bridge`      — in-sandbox TCP→unix-socket forwarder
* :mod:`backend.peer.sandbox_exec`— host-side launcher that execs bwrap
* :mod:`backend.peer.agent_api`   — unix-socket API the sandboxed MCP talks to
* :mod:`backend.peer.service`     — glue owned by the web server
"""
