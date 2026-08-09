import json
from pathlib import Path


PLAN_PATH = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "research"
    / "historical-cohort-recovery-plan-2026-07-14.json"
)


def test_historical_recovery_plan_is_bounded_fail_closed_and_privacy_safe():
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    assert plan["schema"] == "mempalace-historical-cohort-recovery-plan/v1"
    assert plan["status"] == "no-go-awaiting-gates-and-explicit-operator-approval"
    assert plan["scope"]["automaticAdvanceAllowed"] is False
    assert plan["scope"]["oneSourcePerRun"] is True
    assert plan["scope"]["railwayAllowed"] is False

    candidates = plan["candidates"]
    assert len(candidates) == plan["scope"]["candidateCount"] == 18
    assert [item["sequence"] for item in candidates] == list(range(1, 19))
    assert len({item["id"] for item in candidates}) == 18
    assert sum(item["projectedRows"] for item in candidates) == 22220
    chatgpt = [item for item in candidates if item["provider"] == "ChatGPT"]
    assert len(chatgpt) == 3
    assert sum(item["projectedRows"] for item in chatgpt) == 15057
    assert candidates[0]["id"] == "H02-61d6f2a4"
    assert candidates[0]["lane"] == "live-canary"
    assert all(item["lane"] == "high-output-last" for item in candidates[-3:])

    gates = plan["gates"]
    assert len(gates) == 8
    assert all(gate["status"] == "pending" and gate["require"] for gate in gates)
    assert {gate["id"] for gate in gates} == {
        "bounded-cohort-closeout",
        "clone-canary-rehearsal",
        "equivalent-content-dedup-review",
        "exclusive-full-directory-backup",
        "explicit-live-approval",
        "native-restore-proof",
        "per-source-attended-checkpoint",
        "refresh-source-evidence",
    }
    serialized = PLAN_PATH.read_text(encoding="utf-8").lower()
    assert '"sourcepath"' not in serialized
    assert '"content"' not in serialized
