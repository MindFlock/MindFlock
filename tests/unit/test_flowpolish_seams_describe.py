"""Flow polish seams: the Sky's sentence box lints the described route with
the kinds' own option names, so an option a kind doesn't have is flagged
there too (as the MCP's describe_flight already does). No model is run."""

from __future__ import annotations

from backend.web.core import commit_message as cm
from backend.web.core import flight_cookbook as C
from backend.web.core import flight_describe as D
from backend.web.core import flights as F


def _answer(monkeypatch, tmp_path, raw: str) -> dict:
    monkeypatch.setattr(cm, "pick_argv", lambda *a, **kw: ["claude"])
    monkeypatch.setattr(cm, "neutral_cwd", lambda: str(tmp_path))
    monkeypatch.setattr(cm, "_run", lambda *a: raw)
    return D.describe("x", "plan", program="claude")


def test_the_lint_gets_the_kinds_option_names(monkeypatch, tmp_path):
    seen = {}
    real = C.lint

    def spy(steps, **kw):
        seen.update(kw)
        return real(steps, **kw)

    monkeypatch.setattr(C, "lint", spy)
    _answer(
        monkeypatch,
        tmp_path,
        '<flight>[{"kind": "ship", "config": {"lane": "pr"}}]</flight>',
    )
    assert seen["fields"] == C.kind_fields(F.kinds_dto())
    assert "lane" in seen["fields"]["ship"]


def test_an_unknown_option_is_refused_in_plain_words(monkeypatch, tmp_path):
    import pytest

    with pytest.raises(D.DescribeError) as err:
        _answer(
            monkeypatch,
            tmp_path,
            '<flight>[{"kind": "ship", "config": {"lane": "pr", "colour": "red"}}]</flight>',
        )
    assert "unknown option 'colour' on a Ship stop" in err.value.detail


def test_the_kinds_unreadable_skips_only_the_option_check(monkeypatch, tmp_path):
    def boom():
        raise RuntimeError("no kinds")

    seen = {}
    real = C.lint
    monkeypatch.setattr(
        C, "lint", lambda steps, **kw: seen.update(kw) or real(steps, **kw)
    )
    monkeypatch.setattr(F, "kinds_dto", boom)
    out = _answer(
        monkeypatch,
        tmp_path,
        '<flight>[{"kind": "ship", "config": {"lane": "pr"}}]</flight>',
    )
    assert out["sentence"] == "Open PR"
    assert "warnings" not in out
    assert seen["fields"] is None
