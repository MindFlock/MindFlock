"""The UI preview: a scratch MindFlock with sample data, isolated from the
machine it runs on, plus headless screenshots of it.

Entry point: ``scripts/preview.py`` (``uv run --group dev --group web python
scripts/preview.py --help``). Modules:

* :mod:`mfpreview.sandbox` — the isolation: scratch home, allowlisted env,
  a ``tmux`` wrapper that pins every call to the preview's own socket, the
  failing stubs for machine-global tools, the one-at-a-time lock, boot and a
  teardown that also runs after a crash.
* :mod:`mfpreview.stub_agent` — the stub ``claude`` each session runs: it
  plays a scripted screen (working / idle / a question / a crash) so the
  sessions look alive without a login or a token spent.
* :mod:`mfpreview.scenarios` — named sample-data sets, seeded through the
  server's own HTTP API.
* :mod:`mfpreview.shots` — Playwright + the system Chrome.
* :mod:`mfpreview.cli` — ``up`` / ``down`` / ``status`` / ``shot`` /
  ``scenarios``.

See docs/development.md "Seeing a UI change".
"""
