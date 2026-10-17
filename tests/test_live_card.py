"""Option A: one live plan card per plan in Slack, with stepper buttons that propose a new setpoint,
draw it on the chart, and change it only after Apply (the approval). No Slack, hardware or Claude API needed."""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from experiment import ExperimentPlan, PlanStore  # noqa: E402
from test_experiment_plans import make_settings  # noqa: E402
from test_slack_plan import MIN, running_plan, write_history  # noqa: E402

T0 = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)


def plan_at_t0(**kw) -> ExperimentPlan:
    return ExperimentPlan(name="overnight", unit="pio1", experiment="demo", duration_h=18, temperature_c=30, rpm=300,
                          status="running", started_at=T0.isoformat(), **kw)


# ------------------------------------------------------------------ piece 1: setpoint history on the plan


def test_a_setpoint_change_is_recorded_and_becomes_the_current_target():
    plan = plan_at_t0()
    plan.set_target("temperature", 31, by="U2", now=T0 + timedelta(hours=1))
    assert plan.setpoints == [{"at": "2026-10-17T13:00:00Z", "metric": "temperature", "value": 31.0, "by": "U2"}]
    assert plan.target("temperature") == 31
    assert plan.target("stirring") == 300  # untouched
    assert plan.temperature_c == 30  # what was asked for at the start stays on the plan (and in the profile)


def test_target_at_a_time_follows_the_history():
    plan = plan_at_t0()
    plan.set_target("stirring", 350, now=T0 + timedelta(hours=1))
    plan.set_target("stirring", 400, now=T0 + timedelta(hours=2))
    assert plan.target("stirring", seconds_in=30 * MIN) == 300
    assert plan.target("stirring", seconds_in=90 * MIN) == 350
    assert plan.target("stirring", seconds_in=150 * MIN) == 400
    assert plan.target_steps("stirring") == [(0.0, 300.0), (60.0, 350.0), (120.0, 400.0)]
    assert plan.target_steps("temperature") == [(0.0, 30.0)]


def test_alert_triage_expects_the_current_setpoint():
    plan = plan_at_t0()
    plan.set_target("temperature", 33, now=T0 + timedelta(hours=1))
    plan.set_target("stirring", 250, now=T0 + timedelta(hours=1))
    state = plan.expected_state(T0 + timedelta(hours=2))
    assert state["target_temperature_c"] == 33 and state["target_rpm"] == 250


def test_off_target_uses_the_setpoint_in_force_and_forgives_warm_up_after_a_change():
    plan = plan_at_t0()
    plan.set_target("temperature", 33, now=T0 + timedelta(hours=1))
    assert plan.off_target("temperature", 50 * MIN, 33.0)  # before the change, 30 was the target
    assert not plan.off_target("temperature", 50 * MIN, 30.2)
    assert not plan.off_target("temperature", 65 * MIN, 30.2)  # heater still warming to the new target
    assert plan.off_target("temperature", 75 * MIN, 30.2)
    assert not plan.off_target("temperature", 75 * MIN, 32.8)


def test_setpoint_history_survives_the_store(tmp_path):
    store = PlanStore(tmp_path / "plans.json")
    plan = plan_at_t0()
    plan.duration_h = 100_000  # still running whenever this test runs
    plan.set_target("stirring", 350, by="U2", now=T0 + timedelta(hours=1))
    store.put(plan)
    again = store.get("pio1")
    assert again.target("stirring") == 350 and again.setpoints == plan.setpoints


# ------------------------------------------------------------------ piece 2: the chart draws steps and a proposal


def drawn(tmp_path, plan, proposed=None):
    """Draw the plan chart and return {metric: {gid: line}} for the lines tagged 'target' and 'proposed'."""
    from matplotlib.figure import Figure

    from tools import PioreactorTools

    tools = PioreactorTools(make_settings(tmp_path), render_terminal=False)
    start, series = tools._plan_series(plan)
    fig = Figure(figsize=(10, 6))
    tools._draw_plan(fig, plan, series, (time.time() - start) / 60, proposed=proposed)
    out = {}
    for ax, metric in zip(fig.axes, series):
        out[metric] = {line.get_gid(): line for line in ax.get_lines() if line.get_gid()}
    return out


def test_target_line_steps_at_each_approved_change(tmp_path):
    start = time.time() - 20 * MIN
    write_history(tmp_path, start)
    plan = running_plan(start)
    plan.set_target("stirring", 350, now=datetime.fromtimestamp(start + 15 * MIN, timezone.utc))
    lines = drawn(tmp_path, plan)
    stir = lines["stirring"]["target"]
    assert list(stir.get_ydata())[0] == 300 and list(stir.get_ydata())[-1] == 350
    assert stir.get_drawstyle() == "steps-post"
    assert "proposed" not in lines["stirring"]


def test_a_proposal_is_a_second_dashed_line_from_now_to_the_end(tmp_path):
    start = time.time() - 20 * MIN
    write_history(tmp_path, start)
    plan = running_plan(start)
    lines = drawn(tmp_path, plan, proposed={"temperature": 31})
    prop = lines["temperature"]["proposed"]
    assert set(prop.get_ydata()) == {31}
    xs = list(prop.get_xdata())
    assert 19 <= xs[0] <= 21 and xs[-1] == plan.duration_h * 60  # from now to the planned end
    assert prop.get_linestyle() == "--"
    assert prop.get_color() != lines["temperature"]["target"].get_color()
    assert "proposed" not in lines["stirring"]


def test_plot_plan_passes_the_proposal_through(tmp_path):
    from tools import PioreactorTools

    start = time.time() - 20 * MIN
    write_history(tmp_path, start)
    tools = PioreactorTools(make_settings(tmp_path), render_terminal=False)
    out = tools.plot_plan(running_plan(start), proposed={"stirring": 350})
    assert Path(out["png_path"]).stat().st_size > 1000
