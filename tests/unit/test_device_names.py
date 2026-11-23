"""A device is found by the name a person or an agent types.

The run that found this: ``mindflock send tripwire-send ML-Rig-2023`` was
refused as "not one of your devices" — the linked device's key is
``ml-rig-2023-1`` (an older tailnet node already held the plain MagicDNS
label) and its host name ``ML-Rig-2023``; the key match on the OTHER,
unlinked node won. Now the key, the host name and the tailnet name all
match (case, punctuation, spaces vs hyphens and curly quotes ignored), a
linked device beats an unlinked one, and a real ambiguity is a "did you
mean" naming both.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout

import pytest

from backend.web.core import remote
from backend.web.core import send_session
from tests.unit._mcp_fakes import FakeCtx, make_box


def _dev(key, host, dns="", linked=True, reachable=True):
    d = remote._device_state(key)
    d.update(
        host=host,
        dns=dns or key + ".tail0.ts.net",
        reachable=reachable,
        remote_control=True,
        auth=True,
        base_url="http://%s.invalid:8765" % key,
    )
    if linked:
        _LINKED.add(key)
    return d


_LINKED: set = set()


@pytest.fixture(autouse=True)
def devices(monkeypatch):
    monkeypatch.setattr(remote, "_DEVICES", {})
    monkeypatch.setattr(
        remote,
        "_SELF",
        {"key": "ethanlaptop2026", "host": "EthanLaptop2026", "os": "linux"},
    )
    _LINKED.clear()
    monkeypatch.setattr(remote, "token_for", lambda key: "")
    monkeypatch.setattr(remote, "is_linked", lambda dev: dev.get("key") in _LINKED)
    monkeypatch.setattr(
        remote, "_fleet_key_for", lambda key: "fk" if key in _LINKED else ""
    )
    yield


def test_name_key_folds_case_punctuation_and_quotes():
    assert remote.name_key("ML-Rig-2023") == remote.name_key("ml rig 2023")
    assert remote.name_key("ML-Rig-2023") == remote.name_key("ml_rig_2023")
    assert remote.name_key("Ethan’s Mac mini") == remote.name_key("ethans-mac-mini")
    assert remote.name_key("Ethan's Mac mini") == remote.name_key("Ethan’s Mac mini")
    assert remote.name_key("ml-rig-2023") != remote.name_key("ml-rig-2023-1")


def test_the_ml_rig_case_host_name_of_the_linked_device_wins():
    _dev("ml-rig-2023", "ml-rig-2023", linked=False)  # the old, unlinked node
    _dev("ml-rig-2023-1", "ML-Rig-2023")
    for typed in ("ML-Rig-2023", "ml-rig-2023", "ml rig 2023", "ML_RIG_2023"):
        assert remote.find_device(typed)["key"] == "ml-rig-2023-1", typed
    # The key itself and the tailnet name work too.
    assert remote.find_device("ml-rig-2023-1")["key"] == "ml-rig-2023-1"
    assert remote.find_device("ml-rig-2023-1.tail0.ts.net")["key"] == "ml-rig-2023-1"


def test_curly_apostrophe_and_spaces():
    _dev("ethans-mac-mini", "Ethan’s Mac mini")
    assert remote.find_device("Ethan's Mac mini")["key"] == "ethans-mac-mini"
    assert remote.find_device("ethans mac mini")["key"] == "ethans-mac-mini"
    assert remote.find_device("ETHAN’S-MAC-MINI")["key"] == "ethans-mac-mini"


def test_two_linked_devices_of_one_name_are_ambiguous():
    _dev("box", "Box")
    _dev("box-1", "box")
    hits = remote.match_devices("BOX ")
    # "box" is box's key exactly: that one is meant.
    assert [d["key"] for d in hits] == ["box"]
    _dev("studio", "Studio")
    _dev("studio-1", "Studio")
    hits = remote.match_devices("studio")
    assert len(hits) == 1 and hits[0]["key"] == "studio"
    _dev("lab-a", "Lab PC")
    _dev("lab-b", "Lab-PC")
    assert {d["key"] for d in remote.match_devices("lab_pc")} == {"lab-a", "lab-b"}
    assert remote.find_device("LAB_PC") is None


def test_resolve_device_accepts_the_host_name(monkeypatch):
    _dev("ml-rig-2023", "ml-rig-2023", linked=False)
    _dev("ml-rig-2023-1", "ML-Rig-2023")
    monkeypatch.setattr(remote, "is_connected", lambda dev: True)
    assert send_session.resolve_device("ML-Rig-2023")["key"] == "ml-rig-2023-1"


def test_resolve_device_ambiguity_is_a_did_you_mean():
    _dev("lab-a", "Lab PC")
    _dev("lab-b", "Lab-PC")
    with pytest.raises(send_session.Refusal) as err:
        send_session.resolve_device("lab_pc")
    r = err.value
    assert r.code == "ambiguous_device" and r.status == 409
    assert "did you mean" in r.message
    assert "Lab PC (lab-a)" in r.message and "Lab-PC (lab-b)" in r.message
    assert set(r.extra["candidates"]) == {"lab-a", "lab-b"}


def test_resolve_device_an_unlinked_device_alone_is_still_refused():
    _dev("stranger", "Stranger-PC", linked=False)
    with pytest.raises(send_session.Refusal) as err:
        send_session.resolve_device("stranger pc")
    assert err.value.code == "not_linked"
    assert "Stranger-PC (stranger)" in err.value.message


def test_this_device_by_its_host_name_is_same_device():
    with pytest.raises(send_session.Refusal) as err:
        send_session.resolve_device("ethan laptop 2026")
    assert err.value.code == "same_device"


def test_unknown_name_suggests_by_folded_name():
    _dev("ml-rig-2023-1", "ML-Rig-2023")
    with pytest.raises(send_session.Refusal) as err:
        send_session.resolve_device("ML-Rig-2024")
    assert err.value.code == "no_device"
    assert "ML-Rig-2023" in err.value.message


def test_forwarding_resolves_a_host_name_to_the_key():
    _dev("ml-rig-2023", "ml-rig-2023", linked=False)
    _dev("ml-rig-2023-1", "ML-Rig-2023")
    assert remote._device_by_name("ML-Rig-2023")["key"] == "ml-rig-2023-1"
    # The key itself is the live state dict, not a copy.
    assert remote._device_by_name("ml-rig-2023") is remote._DEVICES["ml-rig-2023"]
    assert remote._device_by_name("nope") is None


def test_list_devices_shows_the_name_to_type_and_the_key():
    from tests.unit.test_send_session import _tree

    box, api, clock = make_box(_tree(), me="orch")
    api.top_routes[("GET", "/api/devices")] = {
        "self": {"device": "ethanlaptop2026", "host": "EthanLaptop2026"},
        "devices": [
            {
                "device": "ml-rig-2023",
                "host": "ml-rig-2023",
                "reachable": True,
                "connected": True,
                "member": False,
            },
            {
                "device": "ml-rig-2023-1",
                "host": "ML-Rig-2023",
                "reachable": True,
                "connected": True,
                "member": True,
                "same_fleet": True,
            },
        ],
    }
    out = box.list_devices({}, FakeCtx(clock))
    first = out["devices"][0]
    assert first["name"] == "ML-Rig-2023" and first["device"] == "ml-rig-2023-1"
    assert first["linked"] is True and out["devices"][1]["linked"] is False
    assert out["this_device_name"] == "EthanLaptop2026"


def test_cli_devices_list_prints_the_key_beside_the_host():
    from backend import cli

    buf = io.StringIO()
    with redirect_stdout(buf):
        cli._print_devices(
            {
                "in_fleet": True,
                "members": [
                    {"key": "ethanlaptop2026", "host": "EthanLaptop2026", "self": True},
                    {
                        "key": "ml-rig-2023-1",
                        "host": "ML-Rig-2023",
                        "reachable": True,
                    },
                    {"key": "mac", "host": "mac", "reachable": True},
                ],
            }
        )
    text = buf.getvalue()
    assert "ML-Rig-2023" in text and "key ml-rig-2023-1" in text
    assert "key mac" not in text
