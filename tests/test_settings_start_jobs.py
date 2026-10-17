"""Changing a setting of a job that isn't running must start the job, not silently do nothing.

On 17 Oct, "stir at 200 rpm" and "heat to 37 C" were approved in Slack and reported ok, but
stirring and the temperature automation weren't running on pio01: the MQTT .../set went to no one.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from test_experiment_plans import FakeLeader, make_settings  # noqa: E402
from tools import PioreactorTools  # noqa: E402


@pytest.fixture
def leader():
    fake = FakeLeader()
    yield fake
    fake.server.shutdown()


def capture_mqtt(monkeypatch) -> list[tuple[str, str]]:
    published: list[tuple[str, str]] = []
    monkeypatch.setattr(PioreactorTools, "_publish",
                        lambda self, topic, payload, qos=2: published.append((topic, payload)) or {"topic": topic})
    return published


def test_setting_rpm_on_stopped_stirring_starts_it(tmp_path, leader, monkeypatch):
    leader.running = ["monitor", "mqtt_to_db_streaming"]
    published = capture_mqtt(monkeypatch)
    result = PioreactorTools(make_settings(tmp_path, leader.url)).set_stirring_rpm(200)
    [(path, payload)] = leader.posts()
    assert path == "/api/workers/pio1/jobs/run/job_name/stirring/experiments/demo"
    assert payload["options"] == {"target_rpm": 200}
    assert published == []
    assert "started" in result["note"]


def test_setting_temperature_on_stopped_automation_starts_thermostat(tmp_path, leader, monkeypatch):
    capture_mqtt(monkeypatch)
    PioreactorTools(make_settings(tmp_path, leader.url)).set_target_temperature(37)
    [(path, payload)] = leader.posts()
    assert path == "/api/workers/pio1/jobs/run/job_name/temperature_automation/experiments/demo"
    assert payload["options"] == {"automation_name": "thermostat", "target_temperature": 37}


def test_setting_rpm_on_running_stirring_changes_it(tmp_path, leader, monkeypatch):
    leader.running = ["stirring"]
    published = capture_mqtt(monkeypatch)
    PioreactorTools(make_settings(tmp_path, leader.url)).set_stirring_rpm(300)
    assert leader.posts() == []
    assert published == [("pioreactor/pio1/demo/stirring/target_rpm/set", "300")]


def test_unknown_running_state_changes_and_says_so(tmp_path, monkeypatch):
    published = capture_mqtt(monkeypatch)  # leader at 127.0.0.1:9 is unreachable
    result = PioreactorTools(make_settings(tmp_path)).set_stirring_rpm(300)
    assert published == [("pioreactor/pio1/demo/stirring/target_rpm/set", "300")]
    assert "couldn't check" in result["note"]


def test_dry_run_still_touches_nothing(tmp_path, leader, monkeypatch):
    capture_mqtt(monkeypatch)
    PioreactorTools(make_settings(tmp_path, leader.url), dry_run=True).set_stirring_rpm(300)
    assert leader.requests == []
