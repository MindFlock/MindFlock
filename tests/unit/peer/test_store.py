import glob
import json
import os
import stat
import threading

import pytest

from backend.peer import store
from backend.peer.store import Link, LinkStore


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def _link(i: int = 1, **kw) -> Link:
    d = dict(
        link_id=f"{i:032x}",
        peer_name="Bob",
        peer_pub="ab" * 32,
        role="dialer",
        peer_addr="127.0.0.1:8799",
        sas="123-456-789-0",
    )
    d.update(kw)
    return Link(**d)


def test_crud_roundtrip(tmp_path):
    s = LinkStore(str(tmp_path / "links.json"))
    assert s.list() == []
    a = s.add(_link(1))
    s.add(_link(2, role="listener", peer_addr=None))
    assert [l.link_id for l in s.list()] == [a.link_id, f"{2:032x}"]
    got = s.get(a.link_id)
    assert got == a and got.perms == store.DEFAULT_PERMS
    up = s.update(
        a.link_id, perms={"diff": False}, share_id="cd" * 16, session_title="peer-x"
    )
    assert up.perms == {"messages": True, "diff": False, "read_file": True}
    assert LinkStore(s.path).get(a.link_id).share_id == "cd" * 16
    assert s.remove(a.link_id)
    assert not s.remove(a.link_id)
    assert s.get(a.link_id) is None
    assert s.update(a.link_id, sas="") is None


def test_default_path_and_permissions(peer_home):
    old = os.umask(0)
    try:
        s = LinkStore()
        s.add(_link())
    finally:
        os.umask(old)
    assert s.path == str(peer_home / "links.json")
    assert _mode(s.path) == 0o600
    assert _mode(peer_home) == 0o700
    assert _mode(s.path + ".lock") == 0o600
    assert not glob.glob(s.path + ".tmp-*")


def test_loose_mode_is_tightened(tmp_path):
    s = LinkStore(str(tmp_path / "links.json"))
    s.add(_link())
    os.chmod(s.path, 0o644)
    s.list()
    assert _mode(s.path) == 0o600


@pytest.mark.parametrize(
    "content",
    [
        b"{not json",
        b"[]",
        b'{"version": 2, "links": {}}',
        b'{"version": 1, "links": []}',
        b"\xff\xfe",
        json.dumps({"version": 1, "links": {"0" * 32: {"link_id": "1" * 32}}}).encode(),
        json.dumps(
            {"version": 1, "links": {"0" * 32: {**_link(0).to_dict(), "evil": 1}}}
        ).encode(),
        json.dumps(
            {
                "version": 1,
                "links": {"0" * 32: {**_link(0).to_dict(), "peer_pub": "zz"}},
            }
        ).encode(),
        json.dumps(
            {
                "version": 1,
                "links": {"0" * 32: {**_link(0).to_dict(), "perms": {"messages": 1}}},
            }
        ).encode(),
        json.dumps(
            {
                "version": 1,
                "links": {"0" * 32: {**_link(0).to_dict(), "share_id": "../../x"}},
            }
        ).encode(),
        b"[" * 100000,
    ],
)
def test_corrupt_file_loads_empty_and_is_backed_up(tmp_path, content):
    path = tmp_path / "links.json"
    path.write_bytes(content)
    s = LinkStore(str(path))
    assert s.list() == []
    backups = glob.glob(str(path) + ".corrupt-*")
    assert len(backups) == 1
    assert open(backups[0], "rb").read() == content
    assert _mode(backups[0]) == 0o600
    s.add(_link())
    assert len(s.list()) == 1


def test_symlinked_store_is_not_followed(tmp_path):
    target = tmp_path / "elsewhere.json"
    target.write_text("precious")
    path = tmp_path / "links.json"
    os.symlink(target, path)
    s = LinkStore(str(path))
    assert s.list() == []
    s.add(_link())
    assert target.read_text() == "precious"
    assert not os.path.islink(path)


@pytest.mark.parametrize(
    "raw,clean",
    [
        ("Bob", "Bob"),
        ("  Bob   Smith ", "Bob Smith"),
        ("\x1b[31mred\x1b[0m", "31mred0m"),
        ("a" * 50, "a" * 32),
        ("😀", "peer"),
        ("", "peer"),
        ("../../etc", "....etc"),
        (None, "peer"),
        ("line\nbreak", "linebreak"),
    ],
)
def test_peer_name_sanitized(raw, clean):
    assert _link(peer_name=raw).peer_name == clean
    assert store.sanitize_name(raw) == clean


def test_update_rejects_immutable_and_unknown_fields(tmp_path):
    s = LinkStore(str(tmp_path / "links.json"))
    s.add(_link())
    for bad in (
        {"link_id": "f" * 32},
        {"peer_pub": "cd" * 32},
        {"role": "listener"},
        {"evil": 1},
    ):
        with pytest.raises(ValueError):
            s.update(_link().link_id, **bad)
    with pytest.raises(ValueError):
        s.update(_link().link_id, perms={"shell": True})
    with pytest.raises(ValueError):
        s.update(_link().link_id, perms={"diff": "yes"})
    with pytest.raises(ValueError):
        s.update(_link().link_id, share_id="not-hex")
    assert s.get(_link().link_id).peer_pub == "ab" * 32


def test_update_sanitizes_name(tmp_path):
    s = LinkStore(str(tmp_path / "links.json"))
    s.add(_link())
    assert s.update(_link().link_id, peer_name="\x07evil\x1b").peer_name == "evil"


@pytest.mark.parametrize(
    "kw",
    [
        {"link_id": "short"},
        {"link_id": "G" * 32},
        {"peer_pub": "AB" * 32},
        {"role": "admin"},
        {"peer_addr": None},
        {"peer_addr": "host"},
        {"peer_addr": "host:99999"},
        {"peer_addr": "a b:1"},
        {"role": "listener"},  # listener with an addr
        {"sas": "1234"},
        {"created": "yesterday"},
        {"last_seen": True},
        {"session_title": ""},
    ],
)
def test_add_validates(tmp_path, kw):
    with pytest.raises(ValueError):
        LinkStore(str(tmp_path / "links.json")).add(_link(**kw))


def test_ipv6_addr_ok(tmp_path):
    s = LinkStore(str(tmp_path / "links.json"))
    assert s.add(_link(peer_addr="[::1]:8799")).peer_addr == "[::1]:8799"


def test_duplicate_add_rejected(tmp_path):
    s = LinkStore(str(tmp_path / "links.json"))
    s.add(_link())
    with pytest.raises(ValueError):
        s.add(_link(peer_name="Other"))


def test_get_bad_ids(tmp_path):
    s = LinkStore(str(tmp_path / "links.json"))
    s.add(_link())
    for bad in (None, 1, "../x", "A" * 32, ""):
        assert s.get(bad) is None


def test_concurrent_writers_lose_nothing(tmp_path):
    path = str(tmp_path / "links.json")
    errors = []

    def worker(i):
        try:
            LinkStore(path).add(
                _link(i)
            )  # separate instances: only flock protects them
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(1, 41)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(LinkStore(path).list()) == 40
    assert not glob.glob(path + ".corrupt-*")


def test_returned_links_are_copies(tmp_path):
    s = LinkStore(str(tmp_path / "links.json"))
    s.add(_link())
    got = s.get(_link().link_id)
    got.perms["diff"] = False
    assert s.get(_link().link_id).perms["diff"] is True
