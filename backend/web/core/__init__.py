"""Core of the web platform.

Cohesive modules the monolithic ``server.py`` was split into: the session engine
singleton (:mod:`engine`), pure git helpers (:mod:`git_ops`), the GitHub forge
(:mod:`forge_github`), the per-session wire shape (:mod:`session_dto`), the
shared PTY/terminal plumbing (:mod:`terminal`), and the app factory + lifespan
(:mod:`app`). Addons (``backend/web/addons``) and providers
(``backend/providers``) build on top of this.
"""
