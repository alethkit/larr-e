"""'Audit the pump actuations over the last hour' must be answerable from LARR-E's own records.

On 17 Oct the agent tried the Pi's database (not on the laptop) and gave up. LARR-E's audit log
already holds every hardware decision (who asked, who approved); the agent also logs each
action's result, and audit_log reads them back.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_experiment_plans import make_settings  # noqa: E402
from tools import PioreactorTools  # noqa: E402


def at(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")


def write_audit(path: Path, records: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def test_audit_log_filters_by_kind_and_window(tmp_path):
    cfg = make_settings(tmp_path)
    write_audit(cfg.output.audit_path, [
        {"at": at(90), "source": "slack", "tool": "dose", "args": {"pump": "waste", "duration_s": 30},
         "requester": "U1", "decision": "approved", "decided_by": "U1"},
        {"at": at(30), "source": "slack", "tool": "dose", "args": {"pump": "media", "duration_s": 2},
         "requester": "U1", "decision": "approved", "decided_by": "U2"},
        {"at": at(20), "source": "slack", "tool": "set_stirring_rpm", "args": {"rpm": 200},
         "requester": "U1", "decision": "approved", "decided_by": "U1"},
        {"at": at(10), "source": "agent", "tool": "dose", "args": {"pump": "media", "duration_s": 2},
         "result": "error", "detail": "leader unreachable"},
    ])
    out = PioreactorTools(cfg).audit_log(hours=1, kind="pump")
    assert [(e["tool"], e["args"]["pump"]) for e in out["entries"]] == [("dose", "media"), ("dose", "media")]
    assert out["entries"][0]["decided_by"] == "U2"
    assert out["entries"][1]["result"] == "error"
    assert out["count"] == 2
    assert len(PioreactorTools(cfg).audit_log(hours=2, kind="all")["entries"]) == 4


def test_audit_log_counts_stirring_jobs_as_stirring(tmp_path):
    cfg = make_settings(tmp_path)
    write_audit(cfg.output.audit_path, [
        {"at": at(5), "source": "slack", "tool": "start_job", "args": {"job": "stirring"}, "decision": "approved"},
        {"at": at(4), "source": "slack", "tool": "stop_job", "args": {"job": "temperature_automation"}, "decision": "approved"},
    ])
    out = PioreactorTools(cfg).audit_log(hours=1, kind="stirring")
    assert [e["args"]["job"] for e in out["entries"]] == ["stirring"]


def test_audit_log_without_a_file_is_empty(tmp_path):
    out = PioreactorTools(make_settings(tmp_path)).audit_log(hours=1)
    assert out["entries"] == [] and out["count"] == 0


def test_agent_records_the_result_of_each_hardware_action(tmp_path, monkeypatch):
    import agent

    cfg = make_settings(tmp_path)
    monkeypatch.setattr(agent.anthropic, "Anthropic", lambda *a, **k: None)
    a = agent.LarreAgent("exec", cfg=cfg, dry_run=True, verbose=False, confirm=lambda n, x: True)
    a._execute("dose", {"pump": "media", "duration_s": 2})
    a._execute("dose", {"pump": "media", "duration_s": 999})  # over the limit: an error, still recorded
    a._execute("get_live_status", {})  # reads are not audited
    records = [json.loads(line) for line in cfg.output.audit_path.read_text().splitlines()]
    assert [(r["tool"], r["result"]) for r in records] == [("dose", "ok"), ("dose", "error")]
    assert all(r["source"] == "agent" for r in records)


def test_malformed_unit_name_is_refused_before_approval(tmp_path):
    """The audit log showed an approved waste-pump run for unit 'p i o 01' (a model mangling pio01)."""
    import pytest
    from tools import ToolError

    asked = []
    tools = PioreactorTools(make_settings(tmp_path), dry_run=True)
    with pytest.raises(ToolError, match="isn't a valid unit name"):
        tools.dose("waste", unit="p i o 01", duration_s=2, check_only=True)
    with pytest.raises(ToolError, match="isn't a valid unit name"):
        tools.set_stirring_rpm(200, unit="pio01; rm -rf", check_only=True)
    assert tools.dose("waste", unit="pio01", duration_s=2, check_only=True) == {"ok": True}
    assert asked == []
