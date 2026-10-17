"""Tests for the listener's anomaly rules and message handling. No broker, hardware or API key.

The Pioreactor behaviour these encode was checked in the 26.11.0 source installed on pio01:
stopping stirring publishes measured_rpm=0 (stirring.py:455-459, from on_disconnected) and only
then sets the job's $state to "disconnected" (background_jobs/base.py:880-894).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Settings  # noqa: E402
from listener import AnomalyDetector, JobStates, StopAwareGate, handle_message  # noqa: E402

UNIT, EXP = "pio01", "demo"


def topic(job: str, attr: str) -> str:
    return f"pioreactor/{UNIT}/{EXP}/{job}/{attr}"


def payload(key: str, value: float) -> bytes:
    return json.dumps({key: value, "timestamp": "2026-10-17T20:00:00Z"}).encode()


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class FakeState:
    def __init__(self) -> None:
        self.readings = []

    def update(self, reading) -> None:
        self.readings.append(reading)


def feed(detector, metric_topic, key, values, clock=None, step_s=30.0):
    """Feed readings through handle_message; return the alerts the detector raised."""
    alerts = []
    for v in values:
        handle_message(metric_topic, payload(key, v), state=FakeState(), detector=detector, emit=alerts.append)
        if clock:
            clock.t += step_s
    return alerts


# ------------------------------------------------------------------ the listener must not die


def test_od_rules_survive_a_zero_baseline():
    # A blank vial reads 0. delta / baseline used to raise ZeroDivisionError and kill the listener.
    alerts = feed(AnomalyDetector(Settings()), topic("od_reading", "od1"), "od", [0.0] * 6 + [0.5])
    assert [a.severity for a in alerts] == ["critical"]


def test_a_failing_step_does_not_stop_message_handling():
    class BrokenRecorder:
        def record(self, reading):
            raise sqlite3.OperationalError("database is locked")

    detector, state, alerts = AnomalyDetector(Settings()), FakeState(), []
    for _ in range(2):  # the second message still gets through
        handle_message(topic("stirring", "measured_rpm"), payload("measured_rpm", 500), state=state,
                       detector=detector, emit=alerts.append, recorder=BrokenRecorder())
    assert len(state.readings) == 2


# ------------------------------------------------------------------ stopping stirring on purpose


def stall_scenario(final_state: str | None):
    jobs, scheduled, emitted = JobStates(), [], []
    gate = StopAwareGate(jobs, emitted.append, grace_s=5, schedule=lambda delay, fn: scheduled.append(fn))
    detector = AnomalyDetector(Settings())
    stirring = topic("stirring", "measured_rpm")
    for v in [500] * 6 + [0]:
        handle_message(stirring, payload("measured_rpm", v), state=FakeState(), detector=detector,
                       emit=gate.submit, jobs=jobs)
    if final_state:
        handle_message(topic("stirring", "$state"), final_state.encode(), state=FakeState(),
                       detector=detector, emit=gate.submit, jobs=jobs)
    for fn in scheduled:
        fn()
    return emitted


def test_stopping_stirring_is_not_a_stall():
    assert stall_scenario("disconnected") == []


def test_a_real_stall_is_still_reported():
    # On 17 Oct pio01's stirrer read 0 while its job stayed "ready": that one must still alert.
    emitted = stall_scenario(None)
    assert [a.metric for a in emitted] == ["stirring"] and emitted[0].severity == "critical"


# ------------------------------------------------------------------ heater warm-up


def test_heater_warm_up_is_not_an_alert_but_a_heater_that_never_warms_is():
    clock = Clock()
    detector = AnomalyDetector(Settings(), clock=clock)
    temp = topic("temperature_automation", "temperature")
    # Room is 20.5 °C, below the 22 °C alert band; the thermostat is warming the vial.
    assert feed(detector, temp, "temperature", [20.5, 21.0, 21.6], clock=clock, step_s=60) == []
    clock.t += detector.cfg.temp_warmup_s  # still cold after the warm-up allowance
    alerts = feed(detector, temp, "temperature", [21.0])
    assert [a.severity for a in alerts] == ["critical"]


def test_warm_up_starts_again_when_the_thermostat_restarts():
    clock = Clock()
    detector = AnomalyDetector(Settings(), clock=clock)
    temp = topic("temperature_automation", "temperature")
    feed(detector, temp, "temperature", [30.0], clock=clock)  # in band: warm-up over
    clock.t += 3600
    handle_message(topic("temperature_automation", "$state"), b"ready", state=FakeState(),
                   detector=detector, emit=lambda a: None, jobs=JobStates())
    assert feed(detector, temp, "temperature", [20.5], clock=clock) == []
