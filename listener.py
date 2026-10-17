"""LARR-E MQTT listener: watches Pioreactor telemetry and raises anomaly alerts.

On every alert it:
  1. prints it to the console,
  2. appends it to runtime/alerts.jsonl,
  3. publishes it to larre/alerts/<unit>,
  4. posts it to Slack (if LARRE_SLACK_WEBHOOK_URL is set),
  5. optionally asks the agent for a read-only triage note (in a worker thread) and
     publishes it to larre/triage/<unit>, where slack_bot.py threads it under the alert.

It also keeps runtime/live_state.json current so the agent can read live values.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import sqlite3
import statistics
import threading
import time
import urllib.request
import uuid
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt
from rich.console import Console
from rich.panel import Panel

from config import Settings, settings as default_settings
from linkwatch import LinkWatch
from schema import SCHEMA
from tools import parse_timestamp, utc_timestamp

console = Console()
log = logging.getLogger("larre.listener")

OD_CHANNEL = re.compile(r"^od\d$")


@dataclass
class Alert:
    unit: str
    experiment: str
    metric: str
    severity: str  # "warning" | "critical"
    message: str
    value: float
    baseline: float | None
    reading_timestamp: str
    detected_at: str
    topic: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])


@dataclass
class Reading:
    unit: str
    experiment: str
    metric: str  # od1, od2, temperature, growth_rate, stirring
    value: float
    timestamp: str
    topic: str


def metric_for_topic(topic: str) -> tuple[str, str, str, str] | None:
    """(unit, experiment, metric, payload key) for a telemetry topic, or None."""
    parts = topic.split("/")
    if len(parts) != 5 or parts[0] != "pioreactor":
        return None
    _, unit, experiment, job, attr = parts
    if job == "od_reading" and OD_CHANNEL.match(attr):
        return unit, experiment, attr, "od"
    if job == "growth_rate_calculating" and attr == "growth_rate":
        return unit, experiment, "growth_rate", "growth_rate"
    if job in {"temperature_automation", "temperature_control"} and attr == "temperature":
        return unit, experiment, "temperature", "temperature"
    if job == "stirring" and attr == "measured_rpm":
        return unit, experiment, "stirring", "measured_rpm"
    return None


def parse_message(topic: str, payload: bytes) -> Reading | None:
    """Turn a Pioreactor telemetry message into a Reading, or None to ignore it."""
    found = metric_for_topic(topic)
    if found is None:
        return None
    unit, experiment, metric, key = found

    if not payload:
        return None  # retained-message clears
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if isinstance(data, dict):
        if key not in data or data[key] is None:
            return None
        value, ts = data[key], data.get("timestamp") or utc_timestamp()
    elif isinstance(data, (int, float)):  # older firmware published bare numbers
        value, ts = data, utc_timestamp()
    else:
        return None
    return Reading(unit, experiment, metric, float(value), ts, topic)


STOPPED_STATES = {"disconnected", "sleeping", "lost"}


def parse_job_state(topic: str, payload: bytes) -> tuple[str, str, str] | None:
    """pioreactor/<unit>/<exp>/<job>/$state -> (unit, job, state), or None."""
    parts = topic.split("/")
    if len(parts) != 5 or parts[0] != "pioreactor" or parts[4] != "$state" or not payload:
        return None
    try:
        return parts[1], parts[3], payload.decode().strip()
    except UnicodeDecodeError:
        return None


class JobStates:
    """Latest $state per (unit, job), as each Pioreactor job publishes it."""

    def __init__(self) -> None:
        self._states: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()

    def update(self, unit: str, job: str, state: str) -> None:
        with self._lock:
            self._states[(unit, job)] = state

    def get(self, unit: str, job: str) -> str | None:
        with self._lock:
            return self._states.get((unit, job))


class StopAwareGate:
    """Holds stirring-stall alerts for a few seconds before passing them on.

    Stopping stirring on purpose publishes measured_rpm=0 and, a moment later, $state
    "disconnected". A real stall publishes 0 while the job stays "ready". Other alerts pass
    straight through.
    """

    def __init__(self, jobs: JobStates, emit, grace_s: float, schedule=None):
        self.jobs, self.emit, self.grace_s = jobs, emit, grace_s
        self.schedule = schedule or self._timer

    @staticmethod
    def _timer(delay: float, fn) -> None:
        t = threading.Timer(delay, fn)
        t.daemon = True
        t.start()

    def submit(self, alert: "Alert") -> None:
        if alert.metric != "stirring":
            self.emit(alert)
            return
        self.schedule(self.grace_s, lambda: self._release(alert))

    def _release(self, alert: "Alert") -> None:
        if self.jobs.get(alert.unit, "stirring") in STOPPED_STATES:
            log.info("No stall alert on %s: stirring was stopped on purpose.", alert.unit)
            return
        self.emit(alert)


class AnomalyDetector:
    """Rolling-window rules per (unit, metric). Thread-safe enough for paho's single network thread."""

    def __init__(self, cfg: Settings = default_settings, clock=time.monotonic):
        self.cfg = cfg.anomaly
        self.clock = clock
        # Heater warm-up per unit: when the first reading came in, and whether it has reached the band.
        self.warm_since: dict[str, float] = {}
        self.warmed: set[str] = set()
        self.windows: dict[tuple[str, str], deque[float]] = defaultdict(
            lambda: deque(maxlen=self.cfg.window_size)
        )
        self.last_alert: dict[tuple[str, str], float] = {}

    def observe(self, r: Reading) -> Alert | None:
        key = (r.unit, r.metric)
        window = self.windows[key]
        baseline = statistics.median(window) if len(window) >= self.cfg.min_samples else None
        verdict = self._evaluate(r, baseline)
        window.append(r.value)
        if verdict is None:
            return None

        now = self.clock()
        if now - self.last_alert.get(key, float("-inf")) < self.cfg.cooldown_s:
            return None
        self.last_alert[key] = now
        severity, message = verdict
        return Alert(
            unit=r.unit,
            experiment=r.experiment,
            metric=r.metric,
            severity=severity,
            message=message,
            value=r.value,
            baseline=baseline,
            reading_timestamp=r.timestamp,
            detected_at=utc_timestamp(),
            topic=r.topic,
        )

    def job_started(self, unit: str, job: str) -> None:
        """A job reported "ready". A (re)started thermostat gets a fresh warm-up allowance."""
        if job == "temperature_automation":
            self.warm_since.pop(unit, None)
            self.warmed.discard(unit)

    def _evaluate(self, r: Reading, baseline: float | None) -> tuple[str, str] | None:
        c, v = self.cfg, r.value

        if r.metric.startswith("od"):
            if baseline is None:
                return None
            delta = v - baseline
            if delta > max(c.od_spike_abs, c.od_spike_rel * baseline):
                return "critical", (
                    f"OD spike on {r.metric}: {v:.3f} vs rolling median {baseline:.3f}"
                    f"{f' (+{delta / baseline:.0%})' if baseline else ''}. "
                    "Possible bubble, foam, clump or contamination."
                )
            if -delta > max(c.od_spike_abs, c.od_drop_rel * baseline):
                return "warning", (
                    f"OD drop on {r.metric}: {v:.3f} vs rolling median {baseline:.3f}"
                    f"{f' ({delta / baseline:.0%})' if baseline else ''}. "
                    "Possible washout, vial moved, or sensor fault."
                )

        elif r.metric == "temperature":
            now = self.clock()
            started = self.warm_since.setdefault(r.unit, now)
            if v >= c.temp_min_c:
                self.warmed.add(r.unit)
            warming_up = r.unit not in self.warmed and now - started < c.temp_warmup_s
            if v > c.temp_max_c or (v < c.temp_min_c and not warming_up):
                return "critical", f"Temperature {v:.2f} °C outside safe band {c.temp_min_c}-{c.temp_max_c} °C."
            if baseline is not None and baseline - v > c.temp_drop_c:
                return "critical", (
                    f"Temperature drop: {v:.2f} °C vs rolling median {baseline:.2f} °C "
                    f"(−{baseline - v:.2f} °C). Heater fault or vial removed?"
                )

        elif r.metric == "growth_rate":
            if v < c.growth_rate_crash:
                return "warning", f"Growth rate {v:.3f} h⁻¹ below {c.growth_rate_crash} h⁻¹. Culture may be crashing."

        elif r.metric == "stirring":
            if baseline is not None and baseline >= c.stir_min_baseline_rpm and v < c.stir_stall_fraction * baseline:
                return "critical", (
                    f"Stirring stall: {v:.0f} RPM vs rolling median {baseline:.0f} RPM. "
                    "Stir bar decoupled or fan failure?"
                )
        return None


class LiveState:
    """Latest reading per unit/metric plus a short history, flushed atomically to disk at most
    once a second."""

    def __init__(self, cfg: Settings = default_settings):
        self.path = cfg.output.state_path
        self.state: dict[str, dict[str, Any]] = {}
        self.points, self.step = cfg.output.history_points, cfg.output.history_step_s
        self.history: dict[tuple[str, str], deque[float]] = {}
        self.history_t: dict[tuple[str, str], deque[float]] = {}
        self._sampled: dict[tuple[str, str], float] = {}
        self._last_flush = 0.0
        self._lock = threading.Lock()

    def update(self, r: Reading) -> None:
        with self._lock:
            unit = self.state.setdefault(r.unit, {})
            unit["experiment"] = r.experiment
            unit["last_seen"] = r.timestamp
            entry = {"value": r.value, "timestamp": r.timestamp}
            if r.metric.startswith("od"):
                unit.setdefault("od", {})[r.metric] = entry
            else:
                unit[r.metric] = entry
            key, now = (r.unit, r.metric), time.monotonic()
            if now - self._sampled.get(key, float("-inf")) >= self.step:
                self._sampled[key] = now
                self.history.setdefault(key, deque(maxlen=self.points)).append(round(r.value, 4))
                unit.setdefault("history", {})[r.metric] = list(self.history[key])
                try:
                    stamp = parse_timestamp(r.timestamp).timestamp()
                except ValueError:
                    stamp = time.time()
                self.history_t.setdefault(key, deque(maxlen=self.points)).append(round(stamp, 1))
                unit.setdefault("history_t", {})[r.metric] = list(self.history_t[key])
            if now - self._last_flush >= 1.0:
                self._flush()

    def clear(self, unit: str, metric: str) -> None:
        """A stopped job cleared this reading: drop it rather than keep showing the last value."""
        with self._lock:
            entry = self.state.get(unit, {})
            if metric.startswith("od"):
                entry.get("od", {}).pop(metric, None)
            else:
                entry.pop(metric, None)
            self._flush()

    def _flush(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2))
        os.replace(tmp, self.path)
        self._last_flush = time.monotonic()

    def flush(self) -> None:
        with self._lock:
            self._flush()


class Recorder:
    """Writes every reading into a local Pioreactor-schema SQLite (LARRE_RECORD_DB=1).

    The leader's own database lives on the Pi, and digital-twin units have none, so a LARR-E
    running on a laptop records what it hears. Writes are committed at most once a second.
    """

    TABLES = {"temperature": "temperature_readings", "growth_rate": "growth_rates", "stirring": "stirring_rates"}

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.executescript(SCHEMA)
        self.experiments: set[str] = set()
        self._last_commit = 0.0

    def record(self, r: "Reading") -> None:
        if r.experiment not in self.experiments:
            self.conn.execute(
                "INSERT OR IGNORE INTO experiments (experiment, created_at, description) VALUES (?, ?, ?)",
                (r.experiment, r.timestamp, "recorded by LARR-E"),
            )
            self.experiments.add(r.experiment)
        if r.metric.startswith("od"):
            self.conn.execute(
                "INSERT INTO od_readings VALUES (?, ?, ?, ?, ?, ?)",
                (r.experiment, r.unit, r.timestamp, r.value, 90, int(r.metric[2:] or 1)),
            )
        elif r.metric in self.TABLES:
            self.conn.execute(
                f"INSERT INTO {self.TABLES[r.metric]} VALUES (?, ?, ?, ?)", (r.experiment, r.unit, r.timestamp, r.value)
            )
        if time.monotonic() - self._last_commit >= 1.0:
            self.flush()

    def flush(self) -> None:
        self.conn.commit()
        self._last_commit = time.monotonic()


class AlertSink:
    def __init__(self, cfg: Settings, client: mqtt.Client, auto_triage: bool):
        self.cfg = cfg
        self.client = client
        self.auto_triage = auto_triage
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="larre-sink")

    def emit(self, alert: Alert) -> None:
        payload = asdict(alert)
        color = "red" if alert.severity == "critical" else "yellow"
        console.print(
            Panel(
                f"[bold]{alert.message}[/bold]\n"
                f"unit={alert.unit}  experiment={alert.experiment}  value={alert.value:.3f}  "
                f"at={alert.reading_timestamp}",
                title=f"🚨 {alert.severity.upper()} · {alert.metric}",
                border_style=color,
            )
        )
        with self.cfg.output.alerts_path.open("a") as f:
            f.write(json.dumps(payload) + "\n")
        self.client.publish(f"{self.cfg.mqtt.alert_topic_prefix}/{alert.unit}", json.dumps(payload), qos=1)

        # Network calls run off the paho thread so telemetry keeps flowing.
        self.pool.submit(self._slack, f":rotating_light: *{alert.severity.upper()}* `{alert.unit}` {alert.message}")
        if self.auto_triage:
            self.pool.submit(self._triage, payload)

    def _slack(self, text: str) -> None:
        url = self.cfg.output.slack_webhook_url
        if not url:
            return
        req = urllib.request.Request(
            url, data=json.dumps({"text": text}).encode(), headers={"Content-Type": "application/json"}
        )
        try:
            urllib.request.urlopen(req, timeout=10).close()
        except OSError as exc:
            log.warning("Slack webhook failed: %s", exc)

    def _triage(self, alert: dict[str, Any]) -> None:
        from agent import triage_alert  # imported lazily: the listener runs without an API key too

        console.print(f"[dim]🧠 triaging {alert['metric']} alert on {alert['unit']}…[/dim]")
        try:
            note = triage_alert(alert, self.cfg)
        except Exception as exc:  # the listener must survive any triage failure
            log.exception("Triage failed")
            note = f"(triage failed: {exc})"
        console.print(Panel(note, title=f"🧠 LARR-E triage · {alert['unit']}", border_style="cyan"))
        self.client.publish(
            f"{self.cfg.mqtt.triage_topic_prefix}/{alert['unit']}",
            json.dumps({"alert_id": alert["id"], "unit": alert["unit"], "note": note}),
            qos=1,
        )
        self._slack(f":brain: *Triage for `{alert['unit']}`*\n{note}")


def handle_message(
    topic: str,
    payload: bytes,
    *,
    state: Any,
    detector: AnomalyDetector,
    emit,
    recorder: Any = None,
    jobs: JobStates | None = None,
) -> None:
    """Process one MQTT message. Never raises: paho stops its loop on an exception from on_message."""
    try:
        job_state = parse_job_state(topic, payload)
        if job_state:
            unit, job, value = job_state
            if jobs is not None:
                jobs.update(unit, job, value)
            if value == "ready":
                detector.job_started(unit, job)
            return
        if not payload:  # a stopping job clears its retained readings
            found = metric_for_topic(topic)
            if found:
                state.clear(found[0], found[2])
            return
        reading = parse_message(topic, payload)
        if reading is None:
            return
        state.update(reading)
        if recorder:
            try:
                recorder.record(reading)
            except Exception:
                log.exception("Could not record a reading from %s; carrying on.", topic)
        alert = detector.observe(reading)
        if alert:
            emit(alert)
    except Exception:
        log.exception("Skipped a message on %s; the listener keeps running.", topic)


def build_client(cfg: Settings, role: str = "listener") -> mqtt.Client:
    m = cfg.mqtt
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"{m.client_id_prefix}-{role}-{os.getpid()}",
    )
    if m.username:
        client.username_pw_set(m.username, m.password)
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    return client


def run(cfg: Settings = default_settings, auto_triage: bool | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg.ensure_dirs()
    auto_triage = cfg.output.auto_triage if auto_triage is None else auto_triage

    detector = AnomalyDetector(cfg)
    state = LiveState(cfg)
    client = build_client(cfg)
    sink = AlertSink(cfg, client, auto_triage)
    jobs = JobStates()
    gate = StopAwareGate(jobs, sink.emit, cfg.anomaly.stop_grace_s)
    recorder = Recorder(cfg.pioreactor.db_path) if cfg.pioreactor.record_db else None
    if recorder:
        console.print(f"[cyan]Recording telemetry into {cfg.pioreactor.db_path}[/cyan]")

    # The broker is on the Pi: when it goes, our own alerts can't travel through it, so say it here
    # (and over the webhook, if one is set). The Slack bot posts the same news to its channel.
    def link_down(seconds: float) -> None:
        console.print(Panel(f"[bold]Lost contact with the Pioreactor at {cfg.mqtt.host} for {round(seconds)} s.[/bold]\n"
                            "LARR-E can't see or control it. If the Pi still has power, its jobs carry on.",
                            title="🔌 LOST CONTACT", border_style="red"))
        sink._slack(f":electric_plug: *Lost contact with the Pioreactor* ({cfg.mqtt.host}) for {round(seconds)} s.")

    def link_up(seconds: float) -> None:
        console.print(Panel(f"Back in contact after {round(seconds)} s. Readings from the gap weren't recorded.",
                            title="✅ BACK ONLINE", border_style="green"))
        sink._slack(f":white_check_mark: *Back in contact with the Pioreactor* after {round(seconds)} s.")

    link = LinkWatch(cfg.mqtt.link_grace_s, on_down=link_down, on_up=link_up)
    link.start()

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            console.print(f"[red]MQTT connect failed: {reason_code}[/red]")
            return
        link.connected()
        client.subscribe([(t, 0) for t in cfg.topics.all()])
        console.print(
            f"[green]Connected to {cfg.mqtt.host}:{cfg.mqtt.port}[/green]; watching "
            f"{len(cfg.topics.all())} topic patterns (auto-triage {'on' if auto_triage else 'off'})"
        )

    def on_disconnect(client, userdata, flags, reason_code, properties):
        console.print(f"[yellow]MQTT disconnected ({reason_code}); reconnecting…[/yellow]")
        link.disconnected()

    def on_message(client, userdata, msg):
        handle_message(msg.topic, msg.payload, state=state, detector=detector, emit=gate.submit,
                       recorder=recorder, jobs=jobs)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    def shutdown(*_):
        client.disconnect()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, shutdown)
    console.print(f"[cyan]LARR-E listener starting → {cfg.mqtt.host}:{cfg.mqtt.port}[/cyan]")
    client.connect_async(cfg.mqtt.host, cfg.mqtt.port, cfg.mqtt.keepalive)
    try:
        client.loop_forever(retry_first_connection=True)
    except KeyboardInterrupt:
        pass
    finally:
        state.flush()
        if recorder:
            recorder.flush()
        sink.pool.shutdown(wait=False, cancel_futures=True)
        console.print("[cyan]Listener stopped.[/cyan]")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="LARR-E MQTT anomaly listener")
    parser.add_argument("--no-triage", action="store_true", help="Disable LLM triage of alerts")
    args = parser.parse_args()
    run(auto_triage=False if args.no_triage else None)
