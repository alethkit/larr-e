"""The /plan page: preview a plan as you drag its numbers, then start it through the approval gate.

No hardware, Slack or Claude API needed. Starting a plan from the page makes no LLM call.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from api_server import Handler, LarreService  # noqa: E402
from test_experiment_plans import FakeLeader, make_settings  # noqa: E402
from tools import ToolError  # noqa: E402

PLAN = dict(name="Overnight", duration_h=18, temperature_c=37, rpm=180)


@pytest.fixture
def leader():
    fake = FakeLeader()
    yield fake
    fake.server.shutdown()


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


def test_preview_gives_yaml_and_limits_and_touches_nothing(tmp_path, leader):
    svc = LarreService(make_settings(tmp_path, leader.url))
    out = svc.preview_plan(PLAN)
    assert "target_temperature: 37" in out["profile_yaml"]
    assert "target_rpm: 180" in out["profile_yaml"]
    assert out["plan"]["status"] == "draft"
    lim = out["limits"]
    assert lim["temperature_c"][1] == svc.cfg.safety.max_temperature_c
    assert lim["rpm"][1] == svc.cfg.safety.max_stirring_rpm
    assert lim["duration_h"][1] == svc.cfg.plans.max_duration_h
    assert leader.posts() == []


def test_preview_rejects_unsafe_values_and_unknown_fields(tmp_path, leader):
    svc = LarreService(make_settings(tmp_path, leader.url))
    with pytest.raises(ToolError):
        svc.preview_plan({**PLAN, "temperature_c": 90})
    with pytest.raises(ValueError, match="unknown"):
        svc.preview_plan({**PLAN, "check_only": False})


def test_start_waits_for_approval_then_uploads_the_profile(tmp_path, leader):
    svc = LarreService(make_settings(tmp_path, leader.url))
    job = svc.start_plan(PLAN, source="web")
    wait_for(lambda: svc.pending_approvals())
    (approval,) = svc.pending_approvals()
    assert approval["tool"] == "start_experiment" and approval["args"]["temperature_c"] == 37
    assert leader.posts() == []  # nothing moves before a human says yes

    svc.decide(approval["id"], "approve", "test")
    wait_for(lambda: svc.get_job(job.id).state != "running")
    assert svc.get_job(job.id).state == "done"
    assert len(leader.profiles) == 1
    assert svc.tools.plans.get("pio1").status == "running"


def test_denied_start_changes_nothing(tmp_path, leader):
    svc = LarreService(make_settings(tmp_path, leader.url))
    job = svc.start_plan(PLAN, source="web")
    wait_for(lambda: svc.pending_approvals())
    svc.decide(svc.pending_approvals()[0]["id"], "deny", "test")
    wait_for(lambda: svc.get_job(job.id).state != "running")
    assert "not started" in svc.get_job(job.id).answer.lower()
    assert leader.posts() == [] and svc.tools.plans.get("pio1") is None


def test_invalid_start_is_refused_before_asking_anyone(tmp_path, leader):
    svc = LarreService(make_settings(tmp_path, leader.url))
    with pytest.raises(ToolError):
        svc.start_plan({**PLAN, "rpm": 99999}, source="web")
    assert svc.pending_approvals() == []


def test_http_routes(tmp_path, leader):
    Handler.service = LarreService(make_settings(tmp_path, leader.url))
    Handler.token = "tok"
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(f"{base}/plan?k=tok") as r:
            assert r.status == 200 and b"<title>" in r.read()
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(f"{base}/plan")
        assert err.value.code == 401

        req = urllib.request.Request(f"{base}/api/plan/preview?k=tok", data=json.dumps(PLAN).encode(), method="POST")
        with urllib.request.urlopen(req) as r:
            assert "target_rpm: 180" in json.loads(r.read())["profile_yaml"]

        bad = urllib.request.Request(f"{base}/api/plan/preview?k=tok",
                                     data=json.dumps({**PLAN, "rpm": 99999}).encode(), method="POST")
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(bad)
        assert err.value.code == 400

        with urllib.request.urlopen(f"{base}/api/plan?k=tok&unit=pio1") as r:
            assert json.loads(r.read()) == {"plan": None}
    finally:
        server.shutdown()
