"""Tests for experiment plans, the leader REST client, and the Slack progress loop.

No hardware, Slack or Claude API needed:  pytest tests/

The profile-schema test uses Pioreactor's own validator when the Pioreactor source is
importable, e.g.:
    git clone https://github.com/Pioreactor/pioreactor && \\
    PYTHONPATH=pioreactor/core GLOBAL_CONFIG=pioreactor/config.dev.ini TESTING=1 pytest tests/
"""

from __future__ import annotations

import dataclasses
import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Settings  # noqa: E402
from experiment import ExperimentPlan, PlanStore  # noqa: E402
from leader_api import LeaderAPI, LeaderAPIError  # noqa: E402
from tools import PioreactorTools, ToolError  # noqa: E402


# ------------------------------------------------------------------ fixtures


class FakeLeader:
    """Pretends to be the Pioreactor leader's web API, including 202 task polling."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, object]] = []
        self.profiles: dict[str, str] = {}
        self.reject_upload: str | None = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _reply(self, status: int, payload: object) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                outer.requests.append(("GET", self.path, None))
                if self.path.startswith("/unit_api/task_results/"):
                    self._reply(200, {"task_id": "t1", "status": "succeeded", "result": {"ran": True}})
                elif self.path.endswith("/experiment_profiles/running"):
                    runs = [{"job_name": "experiment_profile", "job_id": 41, "settings": {"profile_filename": "someone-elses.yaml"}}]
                    runs += [{"job_name": "experiment_profile", "job_id": 42 + i, "settings": {"profile_filename": f}}
                             for i, f in enumerate(outer.profiles)]
                    self._reply(200, runs)
                elif self.path.endswith("/experiment"):
                    self._reply(404, {"error": "not assigned"})  # fall back to configured experiment
                else:
                    self._reply(200, [])

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"null")
                outer.requests.append(("POST", self.path, body))
                if self.path == "/api/experiment_profiles":
                    if outer.reject_upload:
                        self._reply(400, {"error": outer.reject_upload})
                        return
                    outer.profiles[body["filename"]] = body["body"]
                    self._reply(200, {"status": "success"})
                else:
                    self._reply(202, {"task_id": "t1", "result_url_path": "/unit_api/task_results/t1", "status": "accepted"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def posts(self) -> list[tuple[str, object]]:
        return [(p, b) for m, p, b in self.requests if m == "POST"]


@pytest.fixture
def leader():
    fake = FakeLeader()
    yield fake
    fake.server.shutdown()


def make_settings(tmp_path: Path, leader_url: str = "http://127.0.0.1:9", transport: str = "rest") -> Settings:
    base = Settings()
    pio = dataclasses.replace(
        base.pioreactor, default_unit="pio1", experiment="demo", leader_url=leader_url, pump_transport=transport, dry_run=False
    )
    out = dataclasses.replace(
        base.output,
        runtime_dir=tmp_path,
        state_path=tmp_path / "live_state.json",
        alerts_path=tmp_path / "alerts.jsonl",
        audit_path=tmp_path / "audit.jsonl",
        plans_path=tmp_path / "plans.json",
        plots_dir=tmp_path / "plots",
    )
    return dataclasses.replace(base, pioreactor=pio, output=out)


OVERNIGHT = dict(name="E. coli overnight", duration_h=18, temperature_c=37, rpm=180, update_every_h=2)


# ------------------------------------------------------------------ plan + profile


def test_profile_yaml_shape(tmp_path):
    tools = PioreactorTools(make_settings(tmp_path))
    preview = tools.plan_experiment(**OVERNIGHT)
    y = preview["profile_yaml"]
    assert y.startswith('version: "1.0"\n')
    assert "  pio1:\n    jobs:\n" in y
    assert "target_rpm: 180" in y and "target_temperature: 37" in y
    assert y.count("type: stop") == 4 and "t: 18h" in y
    assert "growth_rate_calculating" in y and "t: 30s" in y
    # preview writes nothing
    assert not (tmp_path / "plans.json").exists()


def test_profile_passes_pioreactor_validator(tmp_path):
    msgspec_yaml = pytest.importorskip("msgspec.yaml")
    try:
        from pioreactor.experiment_profiles.profile_struct import Profile
        from pioreactor.experiment_profiles.validate import validate_profile
    except Exception as exc:  # pragma: no cover - depends on local checkout
        pytest.skip(f"Pioreactor source not importable: {exc}")
    tools = PioreactorTools(make_settings(tmp_path))
    for kwargs in (OVERNIGHT, dict(name="od only", duration_h=0.5), dict(name="warm", duration_h=2, temperature_c=30, od_reading=False)):
        y = tools.plan_experiment(**kwargs)["profile_yaml"]
        profile = msgspec_yaml.decode(y, type=Profile)
        result = validate_profile(profile)
        assert result.ok, (y, result.diagnostics)


@pytest.mark.parametrize(
    "bad",
    [
        dict(name="x", duration_h=0),
        dict(name="x", duration_h=500),
        dict(name="x", duration_h=1, temperature_c=90),
        dict(name="x", duration_h=1, rpm=5000),
        dict(name="x", duration_h=1, od_reading=False, growth_rate=True),
        dict(name="x", duration_h=1, od_reading=False),
    ],
)
def test_plan_rejects_unsafe_or_empty(tmp_path, bad):
    with pytest.raises(ToolError):
        PioreactorTools(make_settings(tmp_path)).plan_experiment(**bad)


def test_name_is_quoted_safely(tmp_path):
    y = PioreactorTools(make_settings(tmp_path)).plan_experiment(name='a: "b"\nc', duration_h=1)["profile_yaml"]
    assert 'experiment_profile_name: "larre-a-b-c"' in y
    assert len(y.splitlines()) == len(y.strip().split("\n"))


# ------------------------------------------------------------------ start / stop through the leader API


def test_start_and_stop_experiment(tmp_path, leader):
    tools = PioreactorTools(make_settings(tmp_path, leader.url))
    out = tools.start_experiment(**OVERNIGHT)
    assert out["ok"] and out["plan"]["status"] == "running"

    (fname, body), = leader.profiles.items()
    assert fname.startswith("larre-e-coli-overnight-") and fname.endswith(".yaml")
    assert body == out["profile_yaml"]

    path, payload = leader.posts()[1]
    assert path == "/api/workers/pio1/jobs/run/job_name/experiment_profile/experiments/demo"
    assert payload["args"] == ["execute", f"/home/pioreactor/.pioreactor/experiment_profiles/{fname}", "demo"]
    assert out["leader_response"] == {"ran": True}  # 202 was polled to completion

    # persisted, survives a new tools instance (restart)
    plan = PioreactorTools(make_settings(tmp_path, leader.url)).plans.get("pio1")
    assert plan and plan.status == "running" and plan.temperature_c == 37

    with pytest.raises(ToolError, match="already runs"):
        tools.start_experiment(**OVERNIGHT)

    stopped = tools.stop_experiment()
    assert stopped["ok"], stopped
    # only this plan's profile is stopped (by job id), never every profile in the experiment
    assert ("/unit_api/jobs/stop", {"job_id": 42}) in leader.posts()
    assert not any("/jobs/stop/job_name/experiment_profile/" in p for p, _ in leader.posts())
    assert any("/jobs/stop/job_name/stirring/" in p for p, _ in leader.posts())
    assert tools.plans.get("pio1").status == "stopped"


def test_rejected_profile_starts_nothing(tmp_path, leader):
    leader.reject_upload = "Validation error."
    tools = PioreactorTools(make_settings(tmp_path, leader.url))
    with pytest.raises(ToolError, match="rejected"):
        tools.start_experiment(**OVERNIGHT)
    assert len(leader.posts()) == 1
    assert tools.plans.get("pio1") is None


def test_unreachable_leader_is_a_tool_error():
    with pytest.raises(LeaderAPIError, match="Cannot reach"):
        LeaderAPI("http://127.0.0.1:9", timeout_s=1).run_job("pio1", "stirring", "demo")


def test_rest_transport_for_single_jobs(tmp_path, leader):
    tools = PioreactorTools(make_settings(tmp_path, leader.url, transport="rest"))
    tools.start_job("stirring", options={"target_rpm": 400})
    path, payload = leader.posts()[0]
    assert path == "/api/workers/pio1/jobs/run/job_name/stirring/experiments/demo"
    assert payload["options"] == {"target_rpm": 400}


def test_dry_run_touches_nothing(tmp_path, leader):
    cfg = make_settings(tmp_path, leader.url)
    tools = PioreactorTools(cfg, dry_run=True)
    tools.start_experiment(**OVERNIGHT)
    tools.stop_experiment()
    assert leader.requests == []


# ------------------------------------------------------------------ timing, expected state


def test_expected_state_and_finish():
    t0 = datetime(2026, 10, 17, 9, 0, tzinfo=timezone.utc)
    plan = ExperimentPlan(name="x", unit="pio1", experiment="demo", duration_h=18, rpm=180, update_every_h=2,
                          status="running", started_at=t0.isoformat())
    mid = plan.expected_state(t0 + timedelta(hours=5))
    assert "stirring" in mid["jobs_should_be_running"] and mid["target_rpm"] == 180
    end = plan.expected_state(t0 + timedelta(hours=19))
    assert end["jobs_should_be_running"] == [] and end["target_rpm"] is None
    assert not plan.update_due(t0 + timedelta(hours=1))
    assert plan.update_due(t0 + timedelta(hours=2, minutes=1))
    assert plan.refresh_status(t0 + timedelta(hours=18, seconds=1)) and plan.status == "finished"


def test_store_marks_finished_on_read(tmp_path):
    store = PlanStore(tmp_path / "p.json")
    start = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    store.put(ExperimentPlan(name="x", unit="pio1", experiment="e", duration_h=2, status="running", started_at=start))
    assert store.get("pio1").status == "finished"
    assert json.loads((tmp_path / "p.json").read_text())["pio1"]["status"] == "finished"


# ------------------------------------------------------------------ Slack progress loop (fake client)


class FakeSlack:
    def __init__(self):
        self.posts, self.files = [], []

    def chat_postMessage(self, **kw):
        self.posts.append(kw)
        return {"ts": str(len(self.posts))}

    def files_upload_v2(self, **kw):
        self.files.append(kw)


def test_progress_loop_posts_updates_and_final(tmp_path):
    pytest.importorskip("slack_bolt")
    from slack_bot import LarreBot

    cfg = make_settings(tmp_path)

    class App:
        client = FakeSlack()

        def event(self, *_):
            return lambda f: f

        command = action = event

    bot = LarreBot(cfg, app=App(), bot_user_id="UBOT")
    started = (datetime.now(timezone.utc) - timedelta(hours=2, minutes=5)).isoformat()
    bot.tools.plans.put(ExperimentPlan(name="overnight", unit="pio1", experiment="demo", duration_h=18, rpm=180,
                                       update_every_h=2, status="running", started_at=started,
                                       slack_channel="C1", slack_thread_ts="111.1"))
    bot.check_plans()
    assert len(bot.client.posts) == 1 and bot.client.posts[0]["thread_ts"] == "111.1"
    assert "2.1 h of 18 h" in bot.client.posts[0]["text"]
    bot.check_plans()  # not due again yet
    assert len(bot.client.posts) == 1

    plan = bot.tools.plans.get("pio1")
    plan.started_at = (datetime.now(timezone.utc) - timedelta(hours=19)).isoformat()
    bot.tools.plans.put(plan)
    bot.check_plans()
    bot.check_plans()
    finals = [p for p in bot.client.posts if "finished" in p["text"]]
    assert len(finals) == 1


# ------------------------------------------------------------------ agent wiring (no API call)


def test_agent_exposes_and_gates_experiment_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    from agent import LarreAgent

    asked = []
    agent = LarreAgent("exec", cfg=make_settings(tmp_path), dry_run=True,
                       confirm=lambda n, a: asked.append(n) or False, verbose=False)
    names = {t["name"] for t in agent.tool_specs}
    assert {"plan_experiment", "start_experiment", "stop_experiment", "get_experiment_plan"} <= names
    out = agent._dispatch("start_experiment", dict(OVERNIGHT))
    assert out["declined"] and asked == ["start_experiment"]
    assert agent._dispatch("plan_experiment", dict(OVERNIGHT))["plan"]["status"] == "draft"
    assert asked == ["start_experiment"]  # preview needs no approval

    q = LarreAgent("query", cfg=make_settings(tmp_path), verbose=False)
    assert "start_experiment" not in {t["name"] for t in q.tool_specs}


# ------------------------------------------------------------------ .env loading


def test_env_file_loader(tmp_path, monkeypatch):
    from config import _load_env_file

    f = tmp_path / ".env"
    f.write_text(
        "# comment\n\nLARRE_T_A=plain\nexport LARRE_T_B='quoted # not a comment'\n"
        'LARRE_T_C="x=y"\nLARRE_T_D=value # trailing\nLARRE_T_SET=from-file\nnot a line\n'
    )
    monkeypatch.setenv("LARRE_T_SET", "from-env")
    for k in "ABCD":
        monkeypatch.delenv(f"LARRE_T_{k}", raising=False)
    _load_env_file(f)
    import os

    assert os.environ["LARRE_T_A"] == "plain"
    assert os.environ["LARRE_T_B"] == "quoted # not a comment"
    assert os.environ["LARRE_T_C"] == "x=y"
    assert os.environ["LARRE_T_D"] == "value"
    assert os.environ["LARRE_T_SET"] == "from-env"  # the environment wins
    _load_env_file(tmp_path / "missing.env")  # no file: no error
    for k in "ABCD":
        monkeypatch.delenv(f"LARRE_T_{k}", raising=False)
