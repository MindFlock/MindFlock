"""The server's half of frontend/src/__tests__/fixtures/verify_shared.json.

verify.ts repeats the store's rules about which run is current and which
steps a person still owes (``latestRun`` / ``openHumanSteps`` against
``_current_run`` / ``open_human_steps``). Any plan the two answer differently
about is a defect, so both evaluate the same fixture
(``verifySharedFixture.test.ts`` is the other half).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.web.core import test_plans as tp

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "frontend"
    / "src"
    / "__tests__"
    / "fixtures"
    / "verify_shared.json"
)
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_the_server_agrees_with_the_fixture(case):
    plan = tp._normalize(case["plan"]["id"], case["plan"])
    current = tp._current_run(plan)
    index = None
    for i, run in enumerate(plan["runs"]):
        if run is current:
            index = i
    assert index == case["current"]
    assert [s["id"] for s in tp.open_human_steps(plan)] == case["open_human"]
