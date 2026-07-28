"""Per-provider default launch flags + the per-session explicit override.

The New Session dialog pre-fills the launch-flags field with the selected
provider's default (``coding_cli.default_launch_args`` is a provider-name ->
flags map) and always sends the list explicitly, so a default the user toggles
off for one session must be honored. Other creators omit the field and inherit
the provider default (resolved from ``opts.program``). This pins that contract
at the ``new_instance`` boundary."""

from __future__ import annotations

import pytest

from backend.config import settings as S
from backend.session import instance as I


@pytest.fixture(autouse=True)
def _isolate_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    S.invalidate()
    yield
    S.invalidate()


def _launch_args(**opts):
    return I.new_instance(I.InstanceOptions(title="t", path=".", **opts)).LaunchArgs


def test_unspecified_inherits_provider_default():
    # program="" resolves to the default provider (claude).
    S.update_settings(
        coding_cli={"default_launch_args": {"claude": "--dangerously-skip-permissions"}}
    )
    assert _launch_args() == ("--dangerously-skip-permissions",)


def test_default_is_scoped_to_the_session_provider():
    # A default set for codex must NOT leak onto a claude session.
    S.update_settings(coding_cli={"default_launch_args": {"codex": "--search"}})
    assert _launch_args() == ()  # claude session, no claude default
    assert _launch_args(program="codex") == ("--search",)


def test_explicit_empty_disables_default_for_this_session():
    S.update_settings(
        coding_cli={"default_launch_args": {"claude": "--dangerously-skip-permissions"}}
    )
    # Explicit empty list = "toggled every default off" -> no flags, and the
    # provider default is NOT re-applied.
    assert _launch_args(launch_args=[]) == ()


def test_explicit_list_is_verbatim_not_merged_with_default():
    S.update_settings(
        coding_cli={"default_launch_args": {"claude": "--dangerously-skip-permissions"}}
    )
    # An explicit curated list wins outright — the provider default is not added.
    assert _launch_args(launch_args=["--verbose"]) == ("--verbose",)


def test_no_default_and_unspecified_is_empty():
    assert _launch_args() == ()


def test_explicit_list_is_deduped():
    assert _launch_args(launch_args=["--x", "--x", "--y"]) == ("--x", "--y")
