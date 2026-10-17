"""Hackathon demo helper: push fake Pioreactor telemetry onto MQTT to trigger alerts on cue.

Payloads match what Pioreactor jobs publish, so listener.py cannot tell them apart.

Examples
--------
Rehearsal with no hardware (local Mosquitto + local SQLite):
    python inject_anomaly.py --unit worker1 --experiment demo --seed-history 30 --write-db ./runtime/demo.sqlite
    python inject_anomaly.py --unit worker1 --experiment demo --scenario od_spike

Live, on a unit that is already streaming real telemetry:
    python inject_anomaly.py --unit worker1 --scenario temp_drop --live-baseline

WARNING: on a real cluster the leader's mqtt_to_db_streaming job also stores injected
readings in the experiment you name. Use a dedicated demo experiment if that matters.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import paho.mqtt.client as mqtt

from config import settings
from tools import utc_timestamp


@dataclass(frozen=True)
class Metric:
    job: str
    attr: str
    key: str
    db_table: str
    db_column: str
    baseline: float
    noise: float


METRICS = {
    "od": Metric("od_reading", "od1", "od", "od_readings", "od_reading", 0.80, 0.004),
    "temperature": Metric("temperature_automation", "temperature", "temperature", "temperature_readings", "temperature_c", 30.0, 0.04),
    "growth_rate": Metric("growth_rate_calculating", "growth_rate", "growth_rate", "growth_rates", "rate", 0.35, 0.01),
    "stirring": Metric("stirring", "measured_rpm", "measured_rpm", "stirring_rates", "measured_rpm", 500.0, 4.0),
}

# scenario -> (metric, function of baseline -> anomalous value, story for the presenter)
SCENARIOS = {
    "od_spike": ("od", lambda b: b * 3.0, "OD triples in seconds: bubble / clump / contamination"),
    "od_drop": ("od", lambda b: b * 0.2, "OD collapses: vial pulled or washout"),
    "temp_drop": ("temperature", lambda b: b - 6.0, "Temperature falls 6 °C: heater failure"),
    "growth_crash": ("growth_rate", lambda b: -0.4, "Growth rate turns sharply negative"),
    "stir_stall": ("stirring", lambda b: 0.0, "Stir bar decouples: RPM to zero"),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    experiment TEXT PRIMARY KEY, created_at TEXT NOT NULL, description TEXT,
    media_used TEXT, organism_used TEXT);
CREATE TABLE IF NOT EXISTS od_readings (
    experiment TEXT NOT NULL, pioreactor_unit TEXT NOT NULL, timestamp TEXT NOT NULL,
    od_reading REAL NOT NULL, angle INTEGER NOT NULL, channel INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS temperature_readings (
    experiment TEXT NOT NULL, pioreactor_unit TEXT NOT NULL, timestamp TEXT NOT NULL,
    temperature_c REAL NOT NULL);
CREATE TABLE IF NOT EXISTS growth_rates (
    experiment TEXT NOT NULL, pioreactor_unit TEXT NOT NULL, timestamp TEXT NOT NULL,
    rate REAL NOT NULL);
CREATE TABLE IF NOT EXISTS stirring_rates (
    experiment TEXT NOT NULL, pioreactor_unit TEXT NOT NULL, timestamp TEXT NOT NULL,
    measured_rpm REAL NOT NULL);
CREATE TABLE IF NOT EXISTS dosing_events (
    experiment TEXT NOT NULL, pioreactor_unit TEXT NOT NULL, timestamp TEXT NOT NULL,
    event TEXT NOT NULL, volume_change_ml REAL NOT NULL, source_of_event TEXT);
CREATE INDEX IF NOT EXISTS od_readings_ix ON od_readings (experiment, pioreactor_unit, timestamp);
CREATE INDEX IF NOT EXISTS temperature_readings_ix ON temperature_readings (experiment, pioreactor_unit, timestamp);
"""


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def payload_for(metric: Metric, value: float, ts: str) -> str:
    if metric.key == "od":
        return json.dumps({"od": round(value, 6), "angle": "90", "channel": "1", "timestamp": ts})
    return json.dumps({metric.key: round(value, 6), "timestamp": ts})


class Injector:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"larre-injector-{int(time.time())}",
        )
        m = settings.mqtt
        if m.username:
            self.client.username_pw_set(m.username, m.password)
        self.db = self._open_db(args.write_db) if args.write_db else None

    def _open_db(self, path: str) -> sqlite3.Connection:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO experiments (experiment, created_at, description) VALUES (?, ?, ?)",
            (self.args.experiment, utc_timestamp(), "LARR-E demo experiment"),
        )
        conn.commit()
        return conn

    def topic(self, metric: Metric) -> str:
        return f"pioreactor/{self.args.unit}/{self.args.experiment}/{metric.job}/{metric.attr}"

    def connect(self) -> None:
        self.client.connect(self.args.host, self.args.port, 30)
        self.client.loop_start()

    def close(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()
        if self.db:
            self.db.close()

    def store(self, metric: Metric, value: float, ts: str) -> None:
        if not self.db:
            return
        a = self.args
        if metric.db_table == "od_readings":
            self.db.execute(
                "INSERT INTO od_readings VALUES (?, ?, ?, ?, ?, ?)", (a.experiment, a.unit, ts, value, 90, 1)
            )
        else:
            self.db.execute(
                f"INSERT INTO {metric.db_table} VALUES (?, ?, ?, ?)", (a.experiment, a.unit, ts, value)
            )
        self.db.commit()

    def send(self, metric: Metric, value: float, label: str) -> None:
        if metric.key in {"od", "measured_rpm"}:
            value = max(value, 0.0)  # noise must not produce physically impossible readings
        ts = utc_timestamp()
        self.client.publish(self.topic(metric), payload_for(metric, value, ts), qos=1).wait_for_publish(5)
        self.store(metric, value, ts)
        print(f"  [{label:>8}] {self.topic(metric)} = {value:.4f}")

    def live_baseline(self, metric: Metric, timeout: float) -> float | None:
        """Wait for one real reading on the target topic and use it as the baseline."""
        received: list[float] = []

        def on_message(client, userdata, msg):
            try:
                data = json.loads(msg.payload)
                received.append(float(data[metric.key] if isinstance(data, dict) else data))
            except (ValueError, KeyError, TypeError):
                pass

        self.client.on_message = on_message
        self.client.subscribe(self.topic(metric))
        deadline = time.monotonic() + timeout
        while not received and time.monotonic() < deadline:
            time.sleep(0.2)
        self.client.unsubscribe(self.topic(metric))
        return received[0] if received else None

    def seed_history(self, minutes: float) -> None:
        """Backfill realistic history (logistic OD growth, steady temp) into the local DB only."""
        if not self.db:
            sys.exit("--seed-history needs --write-db (it never touches a real cluster's database).")
        a = self.args
        end = datetime.now(timezone.utc)
        n = int(minutes * 60 / 5)  # Pioreactor's default OD interval is 5 s
        for i in range(n):
            t = end - timedelta(seconds=5 * (n - i))
            frac = i / max(n - 1, 1)
            od = 0.05 + 0.75 / (1 + math.exp(-10 * (frac - 0.5))) + random.gauss(0, 0.003)
            temp = 30.0 + 0.15 * math.sin(frac * 12) + random.gauss(0, 0.03)
            gr = 0.35 * (1 - frac) + random.gauss(0, 0.01)
            ts = iso(t)
            self.db.execute("INSERT INTO od_readings VALUES (?, ?, ?, ?, ?, ?)", (a.experiment, a.unit, ts, od, 90, 1))
            self.db.execute("INSERT INTO temperature_readings VALUES (?, ?, ?, ?)", (a.experiment, a.unit, ts, temp))
            self.db.execute("INSERT INTO growth_rates VALUES (?, ?, ?, ?)", (a.experiment, a.unit, ts, gr))
            self.db.execute("INSERT INTO stirring_rates VALUES (?, ?, ?, ?)", (a.experiment, a.unit, ts, 500 + random.gauss(0, 4)))
        self.db.commit()
        print(f"Seeded {n} rows per table covering {minutes:g} min into {a.write_db}")

    def run_scenario(self, name: str) -> None:
        a = self.args
        metric_name, anomalous, story = SCENARIOS[name]
        metric = METRICS[metric_name]
        print(f"\n▶ {name}: {story}")

        base = metric.baseline
        warm = a.baseline_samples
        if a.live_baseline:
            live = self.live_baseline(metric, a.live_timeout)
            if live is None:
                print(f"  no live reading within {a.live_timeout:g}s; falling back to synthetic baseline")
            else:
                base, warm = live, 0  # real telemetry has already warmed up the detector
                print(f"  live baseline {base:.4f}")

        for _ in range(warm):
            self.send(metric, base + random.gauss(0, metric.noise), "baseline")
            time.sleep(a.interval)
        for _ in range(a.anomaly_samples):
            self.send(metric, anomalous(base) + random.gauss(0, metric.noise), "ANOMALY")
            time.sleep(a.interval)
        if a.recover:
            for _ in range(a.baseline_samples):
                self.send(metric, base + random.gauss(0, metric.noise), "recover")
                time.sleep(a.interval)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default=settings.mqtt.host)
    parser.add_argument("--port", type=int, default=settings.mqtt.port)
    parser.add_argument("--unit", default=settings.pioreactor.default_unit, required=settings.pioreactor.default_unit is None)
    parser.add_argument("--experiment", default=settings.pioreactor.experiment or "larre-demo")
    parser.add_argument("--scenario", choices=[*SCENARIOS, "all", "none"], default="od_spike")
    parser.add_argument("--baseline-samples", type=int, default=8, help="normal readings sent first, to warm up the detector")
    parser.add_argument("--anomaly-samples", type=int, default=3)
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between messages")
    parser.add_argument("--recover", action="store_true", help="send normal readings again afterwards")
    parser.add_argument("--live-baseline", action="store_true", help="read the current real value first and spike relative to it")
    parser.add_argument("--live-timeout", type=float, default=20.0)
    parser.add_argument("--write-db", metavar="PATH", help="also write rows to a local Pioreactor-schema SQLite (rehearsal only)")
    parser.add_argument("--seed-history", type=float, metavar="MINUTES", help="backfill MINUTES of history into --write-db")
    args = parser.parse_args()

    inj = Injector(args)
    try:
        if args.seed_history:
            inj.seed_history(args.seed_history)
        if args.scenario != "none":
            inj.connect()
            for name in (SCENARIOS if args.scenario == "all" else [args.scenario]):
                inj.run_scenario(name)
    finally:
        inj.close()


if __name__ == "__main__":
    main()
