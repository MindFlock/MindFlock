"""The refusals a person can meet in a stop's dialog say it in the UI's
words (docs/vocabulary.md: a "stop", its kind's name), not the wire's:
a Wait stop's "Or until", its length, a Split's prompts, a Command's
command. (Sky and routes clarity, round 4.)"""

import pytest

from backend.web.core import flights as fl


@pytest.mark.parametrize(
    "kind, raw, words",
    [
        ("wait", {"until": "teatime"}, "Until is a time of day, like 18:00"),
        ("wait", {"seconds": 0}, "a Wait stop waits at least one second"),
        ("split", {"prompts": []}, "a Split stop needs at least one prompt"),
        ("command", {"command": "  "}, "a Command stop needs a command"),
    ],
)
def test_a_refusal_in_the_dialog_says_stop(kind, raw, words):
    with pytest.raises(fl.PlanError) as err:
        fl._step_config(kind, raw, True, "step.config")
    assert err.value.message == words
    assert " step " not in " " + err.value.message + " "


ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]


def test_the_phone_says_a_halt_point_is_before_its_stop():
    js = (ROOT / "backend/web/static/mobile.js").read_text()
    assert (
        'return at ? "Paused before " + at + " — Carry on" : "Paused at its halt point — Carry on";'
        in js
    )
    assert '” reached its halt point before "' in js
    assert (
        '"\'s route is paused " + (hpAt ? "before " + hpAt : "at its halt point")' in js
    )


def test_the_cli_and_mcp_say_run_again_and_finished():
    cli = (ROOT / "backend/cli.py").read_text()
    mcp = (ROOT / "backend/mcp/flights.py").read_text()
    assert (
        'help="run an ended (stopped, halted or finished) route again, from the "'
        in cli
    )
    assert "fly an ended" not in cli and "fly it on this session" not in cli
    assert "again (run an ENDED route — stopped, halted or finished — again" in mcp
