"""Tests for /api/screen: the plain-text view the native 3DS app draws on its 40-column console.

Each line is a one-letter tag, a space, then ASCII text. The C side only splits lines, so every
rule about what to show lives here.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api_server import screen_lines  # noqa: E402

STATUS = {
    "available": True,
    "pio01": {"od": {"od1": {"value": 0.1234}}, "temperature": {"value": 37.04},
              "growth_rate": {"value": 0.121}, "stirring": {"value": 498.6},
              "seconds_since_last_reading": 5},
    "twin1": {"temperature": {"value": 30.0}, "stirring": {"value": 300.0},
              "seconds_since_last_reading": 900, "stale": True},
}


def test_one_row_per_unit_sorted_with_stale_marked():
    lines = screen_lines(STATUS, {"alerts": []}, [], cols=40)
    assert lines == [
        "U pio01  OD 0.123 37.0C 0.12/h  499rpm",
        "U twin1  OD     - 30.0C    -/h  300rpm old",
    ]
    assert all(len(line) <= 2 + 40 for line in lines)


def test_no_telemetry_gives_a_note():
    lines = screen_lines({"available": False, "note": "listener.py is not running"}, {"alerts": []}, [])
    assert lines == ["N listener.py is not running"]


def test_recent_alert_is_shown_and_old_one_is_not():
    alert = {"severity": "critical", "unit": "pio01", "metric": "stirring",
             "message": "Stirring RPM dropped to 0 while the job is running.", "age_s": 30}
    lines = screen_lines({"available": False, "note": "x"}, {"alerts": [alert]}, [], cols=40)
    assert lines[1:] == [
        "C CRITICAL pio01 stirring",
        "a Stirring RPM dropped to 0 while the job",
        "a is running.",
    ]
    old = dict(alert, age_s=3600)
    assert screen_lines({"available": False, "note": "x"}, {"alerts": [old]}, []) == ["N x"]


def test_pending_approval_comes_with_its_id_and_arguments():
    approval = {"id": "abc123", "tool": "set_target_temperature",
                "args": {"unit": "pio01", "celsius": 30.0, "note": None}}
    lines = screen_lines({"available": False, "note": "x"}, {"alerts": []}, [approval])
    assert lines[1:] == ["P abc123 Change temperature", "p unit = pio01", "p celsius = 30.0"]


def test_non_ascii_is_replaced():
    alert = {"severity": "warning", "unit": "pio01", "metric": "temperature",
             "message": "Temperature 45 °C → too hot", "age_s": 1}
    lines = screen_lines({"available": False, "note": "x"}, {"alerts": [alert]}, [])
    assert "a Temperature 45 C -> too hot" in lines
    assert all(line.isascii() for line in lines)
