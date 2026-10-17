"""Slack shows the plan the way /plan does: a plan-vs-measured chart in progress updates, and an
'Adjust in LARR-E' button that opens the /plan page. No Slack, hardware or Claude API needed."""

from __future__ import annotations

import dataclasses
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from experiment import ExperimentPlan  # noqa: E402
from test_experiment_plans import FakeSlack, make_settings  # noqa: E402
from tools import PioreactorTools  # noqa: E402

MIN = 60


def write_history(tmp_path: Path, start: float, minutes: int = 20) -> None:
    """Stirring at 300 rpm with a 2-minute stall at minutes 10-12; temperature warming 22 -> 30 °C over 8 min."""
    ts = [start + 10 * i for i in range(minutes * 6)]
    rpm = [0.0 if 10 * MIN <= t - start < 12 * MIN else 300.0 for t in ts]
    temp = [min(30.0, 22.0 + (t - start) / MIN) for t in ts]
    (tmp_path / "live_state.json").write_text(json.dumps({"pio1": {
        "experiment": "demo",
        "history": {"stirring": rpm, "temperature": temp},
        "history_t": {"stirring": ts, "temperature": ts},
    }}))


def running_plan(start: float, **kw) -> ExperimentPlan:
    started = datetime.fromtimestamp(start, timezone.utc).isoformat()
    return ExperimentPlan(name="overnight", unit="pio1", experiment="demo", duration_h=18, temperature_c=30,
                          rpm=300, status="running", started_at=started, **kw)


def test_off_target_rule_forgives_warm_up_and_spin_up():
    plan = running_plan(time.time())
    assert not plan.off_target("temperature", 5 * MIN, 25.0)  # heater still warming
    assert plan.off_target("temperature", 15 * MIN, 25.0)
    assert not plan.off_target("temperature", 15 * MIN, 30.5)
    assert not plan.off_target("stirring", 10, 0.0)  # stirrer spinning up
    assert plan.off_target("stirring", 5 * MIN, 0.0)
    assert not plan.off_target("stirring", 5 * MIN, 290.0)
    assert not plan.off_target("od", 5 * MIN, 0.0)  # no target, never off


def test_plan_chart_draws_targets_and_counts_time_off_target(tmp_path):
    start = time.time() - 20 * MIN
    write_history(tmp_path, start)
    tools = PioreactorTools(make_settings(tmp_path), render_terminal=False)
    out = tools.plot_plan(running_plan(start))
    assert Path(out["png_path"]).stat().st_size > 1000
    assert out["off_target_min"]["temperature"] == 0
    assert 1.7 <= out["off_target_min"]["stirring"] <= 2.3


def bot_with(cfg):
    pytest.importorskip("slack_bolt")
    from slack_bot import LarreBot

    class App:
        client = FakeSlack()

        def event(self, *_):
            return lambda f: f

        command = action = event

    return LarreBot(cfg, app=App(), bot_user_id="UBOT")


def test_progress_update_posts_the_plan_chart_first(tmp_path):
    start = time.time() - 20 * MIN
    write_history(tmp_path, start)
    bot = bot_with(make_settings(tmp_path))
    bot.post_progress(running_plan(start, slack_channel="C1", slack_thread_ts="1.1"))
    first = bot.client.files[0]
    assert "plan" in first["title"].lower()
    assert "stirring off target 2" in first["initial_comment"]  # 2.0 min, roughly


def with_public_url(tmp_path, url="http://10.0.0.5:8787", token="tok"):
    cfg = make_settings(tmp_path)
    return dataclasses.replace(cfg, api=dataclasses.replace(cfg.api, public_url=url, token=token))


def buttons(post):
    return [e for b in post.get("blocks") or [] if b["type"] == "actions" for e in b["elements"]]


def test_progress_update_has_an_adjust_button_when_the_page_is_reachable(tmp_path):
    start = time.time() - 20 * MIN
    write_history(tmp_path, start)
    bot = bot_with(with_public_url(tmp_path))
    bot.post_progress(running_plan(start, slack_channel="C1", slack_thread_ts="1.1"))
    (adjust,) = [b for b in buttons(bot.client.posts[0]) if b.get("url")]
    assert adjust["url"] == "http://10.0.0.5:8787/plan?k=tok&unit=pio1"
    assert "Adjust" in adjust["text"]["text"]
    assert bot.client.posts[0]["text"]  # plain-text fallback for notifications


def test_no_adjust_button_without_a_public_url_or_token(tmp_path):
    start = time.time() - 20 * MIN
    write_history(tmp_path, start)
    for cfg in (make_settings(tmp_path), with_public_url(tmp_path, token=None)):
        bot = bot_with(cfg)
        bot.post_progress(running_plan(start, slack_channel="C1", slack_thread_ts="1.1"))
        assert not [b for b in buttons(bot.client.posts[0]) if b.get("url")]


def test_plan_approval_card_has_an_adjust_button(tmp_path):
    cfg = with_public_url(tmp_path)
    cfg = dataclasses.replace(cfg, slack=dataclasses.replace(cfg.slack, approval_timeout_s=0.05))
    bot = bot_with(cfg)
    bot.client.chat_update = lambda **kw: None
    from slack_bot import Conversation

    conv = Conversation(channel="C1", thread_ts="1.1", requester="U1")
    assert bot.request_approval(conv, "start_experiment", dict(name="x", duration_h=1, temperature_c=30, rpm=300)) is False
    urls = [b["url"] for b in buttons(bot.client.posts[0]) if b.get("url")]
    assert urls == ["http://10.0.0.5:8787/plan?k=tok&unit=pio1"]
    # other hardware actions don't get one
    bot.request_approval(conv, "set_stirring_rpm", dict(rpm=300))
    assert not [b for b in buttons(bot.client.posts[1]) if b.get("url")]
