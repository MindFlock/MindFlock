"""Join by link: an invite travels as ``<base>/join#<code>``; whatever the
joiner pastes (code, message, either link) reaches the same join; the page the
link opens needs no token."""

from __future__ import annotations

import urllib.parse

import pytest
from fastapi.testclient import TestClient

from backend.peer import service as svc_mod
from backend.peer.invite import code_from_text, encode_code, invite_url

CODE = encode_code("100.64.0.1", 8799, b"\x01" * 8, b"\x02" * 20, b"\x03" * 16)


# -- code_from_text ----------------------------------------------------------


def test_bare_code():
    assert code_from_text(CODE) == CODE
    assert code_from_text("  " + CODE.upper() + "\n") == CODE


def test_whole_invite_message():
    msg = "Open this link to join me in MindFlock: https://x/join#%s\n\n%s" % (
        CODE,
        svc_mod.invite_message(CODE, 600),
    )
    assert code_from_text(msg) == CODE


@pytest.mark.parametrize(
    "url",
    [
        "https://abc.trycloudflare.com/join#" + CODE,
        "http://100.64.0.1:8765/join#" + CODE,
        "https://abc.trycloudflare.com/join#" + urllib.parse.quote(CODE, safe=""),
    ],
)
def test_invite_link(url):
    assert code_from_text(url) == CODE


@pytest.mark.parametrize(
    "url",
    [
        "mindflock://join?code=" + CODE,
        "mindflock://join?code=" + urllib.parse.quote(CODE, safe=""),
    ],
)
def test_app_link(url):
    assert code_from_text(url) == CODE


@pytest.mark.parametrize(
    "junk",
    [
        "",
        "hello there",
        "https://abc.trycloudflare.com/join#",
        "mfp1:",
        "mfp2:abc",  # cut off: no checksum
        None,
        42,
        "x" * 5000,
    ],
)
def test_junk_is_empty(junk):
    assert code_from_text(junk) == ""


def test_invite_url_puts_the_code_in_the_fragment():
    url = invite_url("https://abc.trycloudflare.com/", CODE)
    assert url == "https://abc.trycloudflare.com/join#" + CODE
    parts = urllib.parse.urlsplit(url)
    assert not parts.query and parts.fragment == CODE
    assert invite_url("http://h:1", CODE) == "http://h:1/join#" + CODE


# -- routes ------------------------------------------------------------------

from tests.unit.peer.test_integration_routes import (  # noqa: E402
    CODE as FAKE_CODE,
    client,
    svc,
)

__all__ = ["client", "svc"]  # fixtures, re-used


def test_invite_answers_its_url(svc, client, monkeypatch):
    from backend.web.core import guest_tunnel

    monkeypatch.setattr(guest_tunnel, "url", lambda: "")
    body = client.post("/api/peer/invites", json={}).json()
    assert body["url"].endswith("/join#" + FAKE_CODE)
    assert "?" not in body["url"]  # never in the query: no server log sees it


def test_invite_url_prefers_the_public_tunnel(svc, client, monkeypatch):
    from backend.web.core import guest_tunnel

    monkeypatch.setattr(guest_tunnel, "url", lambda: "https://abc.trycloudflare.com/")
    body = client.post("/api/peer/invites", json={}).json()
    assert body["url"] == "https://abc.trycloudflare.com/join#" + FAKE_CODE


def test_status_still_never_shows_the_code(svc, client):
    client.post("/api/peer/invites", json={})
    text = client.get("/api/peer").text
    assert FAKE_CODE not in text and "/join#" not in text


@pytest.mark.parametrize(
    "pasted",
    [
        "https://abc.trycloudflare.com/join#" + FAKE_CODE,
        "https://abc.trycloudflare.com/join#" + urllib.parse.quote(FAKE_CODE, safe=""),
        "mindflock://join?code=" + urllib.parse.quote(FAKE_CODE, safe=""),
    ],
)
def test_join_takes_a_link(svc, client, pasted):
    r = client.post("/api/peer/join", json={"code": pasted})
    assert r.status_code == 201
    assert ("pair", FAKE_CODE) in svc.transport.calls


def test_join_page_is_public(client, monkeypatch):
    from backend.web.core import auth

    monkeypatch.setattr(auth, "auth_enabled", lambda: True)
    r = client.get("/join")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "mindflock://join?code=" in r.text
    assert "Open in MindFlock" in r.text and "Copy code" in r.text
    # Only /join itself opens up, not the API next to it.
    assert client.get("/api/peer").status_code == 401
