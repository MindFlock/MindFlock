"""Route groups split out of ``backend.web.server``.

One module per group, ``routes/<name>.py``, each exposing a module-level
``router = APIRouter()`` whose routes are declared with their full paths (no
``prefix``, so a route reads the same here as in docs/web-api.md). The routes
only translate HTTP to calls into ``backend.web.core`` (or ``backend.peer``);
they hold no logic of their own.

``server.py`` includes each router with ``app.include_router(...)`` AT THE
POSITION the routes used to occupy, because route order decides path matching
(a static ``/api/x/foo`` must stay ahead of ``/api/x/{title}``). Anything a
test monkeypatches on the server module is read through :func:`_server` at
call time, and ``server.py`` re-imports every name moved here, so
``monkeypatch.setattr(server, "_x", …)`` keeps reaching the route.

Auth needs no per-route wiring: the access-token gate is ASGI middleware
(``core.auth``) and covers every included router. Routes that change settings
take ``Depends(_auth.configure_allowed)`` exactly as they would in server.py.
"""


def _server():
    """The ``backend.web.server`` module, imported lazily (circular import)."""
    from backend.web import server

    return server
