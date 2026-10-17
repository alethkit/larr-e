"""Hardware and data toolset that the LLM routing layer calls.

Actuators reach the Pioreactor cluster through one of three transports (LARRE_JOB_TRANSPORT):

* "http" (Pioreactor 25+), the leader's web API, as the web UI uses it:
    start   POST /api/workers/<unit>/jobs/run/job_name/<job>/experiments/<exp>     {"options": {...}}
    change  POST /api/workers/<unit>/jobs/update/job_name/<job>/experiments/<exp>  {"settings": {...}}
    stop    POST /api/workers/<unit>/jobs/stop/job_name/<job>/experiments/<exp>
* "mqtt" (older software):
    change  pioreactor/<unit>/<exp>/<job>/<attr>/set
    stop    pioreactor/<unit>/<exp>/<job>/$state/set  -> "disconnected"
    start   pioreactor/<unit>/<exp>/run/<job>  -> {"options": {...}, "args": []}
* "pios": `pios run ...` on the leader.

Reads come from the leader's SQLite database (opened read-only) and from the
live state snapshot that listener.py keeps up to date.
"""

from __future__ import annotations

import json
import shlex
import sqlite3
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import paho.mqtt.publish as mqtt_publish

from config import Settings, settings as default_settings


class ToolError(Exception):
    """A tool failed in a way the LLM should see and can recover from."""


def utc_timestamp() -> str:
    """Timestamp in the format Pioreactor uses in payloads and SQLite rows."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# metric name -> (table, value column, extra WHERE clause)
METRIC_TABLES: dict[str, tuple[str, str, str]] = {
    "od": ("od_readings", "od_reading", ""),
    "od_filtered": ("od_readings_filtered", "normalized_od_reading", ""),
    "temperature": ("temperature_readings", "temperature_c", ""),
    "growth_rate": ("growth_rates", "rate", ""),
    "stirring": ("stirring_rates", "measured_rpm", ""),
}

# The leader's live time-series API (/api/experiments/<exp>/time_series/<source>) covers these
# metrics; stirring isn't served there, so it comes from the listener's recent history instead.
LEADER_SERIES = {
    "od": "od_readings",
    "od_filtered": "od_readings_filtered",
    "temperature": "temperature_readings",
    "growth_rate": "growth_rates",
}

METRIC_LABELS = {
    "od": "OD (raw)",
    "od_filtered": "OD (normalized)",
    "temperature": "Temperature (°C)",
    "growth_rate": "Growth rate (h⁻¹)",
    "stirring": "Stirring (RPM)",
}

PUMP_JOBS = {"media": "add_media", "waste": "remove_waste", "alt_media": "add_alt_media"}

STARTABLE_JOBS = {"stirring", "od_reading", "growth_rate_calculating", "temperature_automation"}
STOPPABLE_JOBS = STARTABLE_JOBS | {"dosing_automation", "led_automation", *PUMP_JOBS.values()}


@dataclass
class Series:
    metric: str
    unit: str
    experiment: str
    timestamps: list[datetime]
    values: list[float]

    def summary(self) -> dict[str, Any]:
        if not self.values:
            return {"metric": self.metric, "points": 0}
        first, last = self.timestamps[0], self.timestamps[-1]
        return {
            "metric": self.metric,
            "points": len(self.values),
            "start": first.isoformat(),
            "end": last.isoformat(),
            "min": round(min(self.values), 4),
            "max": round(max(self.values), 4),
            "mean": round(sum(self.values) / len(self.values), 4),
            "first": round(self.values[0], 4),
            "last": round(self.values[-1], 4),
        }


class PioreactorTools:
    def __init__(
        self, cfg: Settings = default_settings, dry_run: bool | None = None, render_terminal: bool = True
    ):
        self.cfg = cfg
        self.dry_run = cfg.pioreactor.dry_run if dry_run is None else dry_run
        self.render_terminal = render_terminal
        cfg.ensure_dirs()

    # ------------------------------------------------------------------ context

    def resolve_unit(self, unit: str | None) -> str:
        unit = unit or self.cfg.pioreactor.default_unit
        if unit:
            return unit
        units = sorted(self._live_state().keys())
        if len(units) == 1:
            return units[0]
        raise ToolError(
            f"No unit given and no single default available (live units: {units or 'none'}). "
            "Specify a unit name."
        )

    def resolve_experiment(self, unit: str | None = None) -> str:
        if self.cfg.pioreactor.experiment:
            return self.cfg.pioreactor.experiment
        state = self._live_state()
        if unit and unit in state and state[unit].get("experiment"):
            return state[unit]["experiment"]
        try:
            with self._connect_ro() as conn:
                row = conn.execute(
                    "SELECT experiment FROM experiments ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
        except ToolError:
            row = None
        if row:
            return row[0]
        raise ToolError("Could not determine the active experiment; set LARRE_EXPERIMENT.")

    # ------------------------------------------------------------------ MQTT

    def _publish(self, topic: str, payload: str, qos: int = 2) -> dict[str, Any]:
        record = {"topic": topic, "payload": payload, "dry_run": self.dry_run}
        if self.dry_run:
            return record
        m = self.cfg.mqtt
        auth = {"username": m.username, "password": m.password} if m.username else None
        try:
            mqtt_publish.single(
                topic,
                payload=payload,
                qos=qos,
                hostname=m.host,
                port=m.port,
                auth=auth,
                keepalive=m.keepalive,
                client_id=f"{m.client_id_prefix}-cmd-{int(time.time() * 1000)}",
            )
        except OSError as exc:
            raise ToolError(f"MQTT publish to {m.host}:{m.port} failed: {exc}") from exc
        return record

    def _set_attribute(self, unit: str, job: str, attr: str, value: Any) -> dict[str, Any]:
        exp = self.resolve_experiment(unit)
        if self.cfg.pioreactor.pump_transport == "http":
            if attr == "$state" and value == "disconnected":
                return self._job_http("stop", unit, job, exp, None)
            return self._job_http("update", unit, job, exp, {"settings": {attr: value}})
        return self._publish(f"pioreactor/{unit}/{exp}/{job}/{attr}/set", str(value))

    def _run_job(self, unit: str, job: str, options: dict[str, Any]) -> dict[str, Any]:
        if self.cfg.pioreactor.pump_transport == "pios":
            return self._run_job_pios(unit, job, options)
        if self.cfg.pioreactor.pump_transport == "http":
            return self._job_http("run", unit, job, self.resolve_experiment(unit), {"options": options})
        exp = self.resolve_experiment(unit)
        payload = json.dumps({"options": options, "args": []})
        return self._publish(f"pioreactor/{unit}/{exp}/run/{job}", payload)

    # ------------------------------------------------------------------ leader web API

    def _http_json(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        url = self.cfg.pioreactor.leader_url.rstrip("/") + path
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise ToolError(f"Pioreactor API {exc.code} on {path}: {exc.read()[:200].decode(errors='replace')}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ToolError(f"Pioreactor API unreachable at {url}: {exc}") from exc

    def _await_task(self, task: dict[str, Any], seconds: float) -> dict[str, Any]:
        """The leader queues work and returns a task; poll it so we report what actually happened."""
        result_path, deadline = task.get("result_url_path"), time.monotonic() + seconds
        while result_path and task.get("status") not in ("succeeded", "failed") and time.monotonic() < deadline:
            time.sleep(0.5)
            task = self._http_json("GET", result_path)
        return task

    def running_jobs(self, unit: str | None = None) -> set[str] | None:
        """Names of jobs running on a unit, from the leader's web API (read-only). None if unknown."""
        if self.cfg.pioreactor.pump_transport != "http":
            return None
        unit = self.resolve_unit(unit)
        try:
            task = self._await_task(self._http_json("GET", f"/api/workers/{urllib.parse.quote(unit)}/jobs/running"), 5)
            jobs = task["result"][unit]["value"]
            return {j["job_name"] for j in jobs if j.get("is_running")}
        except (ToolError, KeyError, TypeError):
            return None

    def _job_http(self, action: str, unit: str, job: str, exp: str, body: dict[str, Any] | None) -> dict[str, Any]:
        path = (f"/api/workers/{urllib.parse.quote(unit)}/jobs/{action}/job_name/{urllib.parse.quote(job)}"
                f"/experiments/{urllib.parse.quote(exp)}")
        record: dict[str, Any] = {"http": f"POST {path}", "body": body, "dry_run": self.dry_run}
        if self.dry_run:
            return record
        task = self._await_task(self._http_json("POST", path, body), 8)
        status = task.get("status")
        per_unit = task.get("result") if isinstance(task.get("result"), dict) else {}
        failures = [f"{u}: {r.get('error') or r}" for u, r in per_unit.items() if isinstance(r, dict) and not r.get("ok")]
        if status == "failed" or failures:
            raise ToolError(f"Pioreactor {action} {job} failed: {'; '.join(failures) or task}"[:300])
        record["status"] = status or "accepted"
        if status not in ("succeeded", "failed"):
            record["note"] = "Accepted by the leader; still running after 8 s, check the Pioreactor UI."
        return record

    def _run_job_pios(self, unit: str, job: str, options: dict[str, Any]) -> dict[str, Any]:
        cmd = ["pios", "run", job, "--units", unit, "-y"]
        for key, value in options.items():
            cmd.append(f"--{key.replace('_', '-')}={value}")
        record = {"command": shlex.join(cmd), "dry_run": self.dry_run}
        if self.dry_run:
            return record
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise ToolError(f"`{record['command']}` failed: {proc.stderr.strip() or proc.stdout.strip()}")
        return record

    # ------------------------------------------------------------------ actuators
    # Each actuator validates against SafetyLimits first. check_only=True stops after
    # validation, so an out-of-limit request is rejected before anyone is asked to confirm it.

    def dose(
        self,
        pump: str,
        unit: str | None = None,
        ml: float | None = None,
        duration_s: float | None = None,
        check_only: bool = False,
    ) -> dict[str, Any]:
        if pump not in PUMP_JOBS:
            raise ToolError(f"pump must be one of {sorted(PUMP_JOBS)}")
        if (ml is None) == (duration_s is None):
            raise ToolError("Give exactly one of ml or duration_s.")
        limits = self.cfg.safety
        if ml is not None:
            if not 0 < ml <= limits.max_dose_ml:
                raise ToolError(f"ml must be in (0, {limits.max_dose_ml}] (safety limit).")
            options = {"ml": round(float(ml), 3)}
        else:
            if not 0 < duration_s <= limits.max_dose_duration_s:
                raise ToolError(f"duration_s must be in (0, {limits.max_dose_duration_s}] (safety limit).")
            options = {"duration": round(float(duration_s), 2)}
        unit = self.resolve_unit(unit)
        if check_only:  # validated; the agent asks for confirmation before the real call
            return {"ok": True}
        result = self._run_job(unit, PUMP_JOBS[pump], options)
        return {"ok": True, "action": PUMP_JOBS[pump], "unit": unit, **options, **result}

    def set_target_temperature(self, celsius: float, unit: str | None = None, check_only: bool = False) -> dict[str, Any]:
        limits = self.cfg.safety
        if not limits.min_temperature_c <= celsius <= limits.max_temperature_c:
            raise ToolError(
                f"Target must be within {limits.min_temperature_c}-{limits.max_temperature_c} °C."
            )
        unit = self.resolve_unit(unit)
        if check_only:  # validated; the agent asks for confirmation before the real call
            return {"ok": True}
        result = self._set_attribute(unit, "temperature_automation", "target_temperature", round(celsius, 2))
        return {
            "ok": True,
            "unit": unit,
            "target_temperature": celsius,
            "note": "Takes effect only if the thermostat temperature automation is running.",
            **result,
        }

    def set_stirring_rpm(self, rpm: float, unit: str | None = None, check_only: bool = False) -> dict[str, Any]:
        if not 0 <= rpm <= self.cfg.safety.max_stirring_rpm:
            raise ToolError(f"rpm must be within 0-{self.cfg.safety.max_stirring_rpm}.")
        unit = self.resolve_unit(unit)
        if check_only:  # validated; the agent asks for confirmation before the real call
            return {"ok": True}
        result = self._set_attribute(unit, "stirring", "target_rpm", round(rpm))
        return {"ok": True, "unit": unit, "target_rpm": round(rpm), **result}

    def start_job(
        self, job: str, unit: str | None = None, options: dict[str, Any] | None = None, check_only: bool = False
    ) -> dict[str, Any]:
        if job not in STARTABLE_JOBS:
            raise ToolError(f"job must be one of {sorted(STARTABLE_JOBS)}")
        options = dict(options or {})
        if job == "temperature_automation":
            options.setdefault("automation_name", "thermostat")
            if "target_temperature" in options:
                t = float(options["target_temperature"])
                lim = self.cfg.safety
                if not lim.min_temperature_c <= t <= lim.max_temperature_c:
                    raise ToolError(f"target_temperature must be within {lim.min_temperature_c}-{lim.max_temperature_c} °C.")
        if job == "stirring" and "target_rpm" in options:
            if not 0 <= float(options["target_rpm"]) <= self.cfg.safety.max_stirring_rpm:
                raise ToolError(f"target_rpm must be within 0-{self.cfg.safety.max_stirring_rpm}.")
        unit = self.resolve_unit(unit)
        if check_only:  # validated; the agent asks for confirmation before the real call
            return {"ok": True}
        result = self._run_job(unit, job, options)
        return {"ok": True, "unit": unit, "job": job, "options": options, **result}

    def stop_job(self, job: str, unit: str | None = None, check_only: bool = False) -> dict[str, Any]:
        if job not in STOPPABLE_JOBS:
            raise ToolError(f"job must be one of {sorted(STOPPABLE_JOBS)}")
        unit = self.resolve_unit(unit)
        if check_only:  # validated; the agent asks for confirmation before the real call
            return {"ok": True}
        result = self._set_attribute(unit, job, "$state", "disconnected")
        return {"ok": True, "unit": unit, "job": job, "state": "disconnected", **result}

    # ------------------------------------------------------------------ SQLite

    def _connect_ro(self) -> sqlite3.Connection:
        path = Path(self.cfg.pioreactor.db_path)
        if not path.exists():
            raise ToolError(f"Database not found at {path}. Set LARRE_DB_PATH.")
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row

        # Defense in depth on top of mode=ro: refuse anything but reads.
        allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}

        def authorizer(action, *_):
            return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY

        conn.set_authorizer(authorizer)
        deadline = time.monotonic() + self.cfg.safety.query_timeout_s
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
        return conn

    def query_database(self, sql: str) -> dict[str, Any]:
        stripped = sql.strip().rstrip(";")
        if ";" in stripped:
            raise ToolError("Only a single statement is allowed.")
        if not stripped.lower().startswith(("select", "with")):
            raise ToolError("Only SELECT / WITH queries are allowed.")
        max_rows = self.cfg.safety.max_query_rows
        try:
            with self._connect_ro() as conn:
                cur = conn.execute(stripped)
                rows = cur.fetchmany(max_rows + 1)
                columns = [d[0] for d in cur.description or []]
        except sqlite3.DatabaseError as exc:
            raise ToolError(f"SQL error: {exc}") from exc
        truncated = len(rows) > max_rows
        return {
            "columns": columns,
            "rows": [list(r) for r in rows[:max_rows]],
            "row_count": min(len(rows), max_rows),
            "truncated": truncated,
        }

    def get_series(
        self, metric: str, unit: str, minutes: float, experiment: str | None = None
    ) -> tuple[Series, str]:
        """Fetch a time series covering the most recent `minutes` of data, from the first source
        that has it: the leader's live API, then a local copy of its database, then the
        listener's recent history.

        For the database, the window is anchored on the newest stored reading rather than the
        wall clock: Raspberry Pis have no RTC, so clocks can disagree across machines.
        """
        if metric not in METRIC_TABLES:
            raise ToolError(f"metric must be one of {sorted(METRIC_TABLES)}")
        experiment = experiment or self.resolve_experiment(unit)
        attempts: list[str] = []
        if self.cfg.pioreactor.pump_transport == "http" and metric in LEADER_SERIES:
            try:
                series = self._leader_series(metric, unit, minutes, experiment)
                if series.values:
                    return series, series.timestamps[-1].isoformat()
                attempts.append("leader API: no readings in that window")
            except ToolError as exc:
                attempts.append(str(exc))
        if Path(self.cfg.pioreactor.db_path).exists():
            series, latest = self._db_series(metric, unit, minutes, experiment)
            if series.values:
                return series, latest
            attempts.append("database copy: no readings")
        series = self._live_series(metric, unit, minutes, experiment)
        if series.values:
            return series, series.timestamps[-1].isoformat()
        attempts.append("listener: no recent readings (is the job running and `larr-e listen` up?)")
        return Series(metric, unit, experiment, [], []), "no data (" + "; ".join(attempts) + ")"

    def _leader_series(self, metric: str, unit: str, minutes: float, experiment: str) -> Series:
        path = (f"/api/experiments/{urllib.parse.quote(experiment)}/time_series/{LEADER_SERIES[metric]}"
                f"?lookback={minutes / 60:.4f}")  # lookback is in hours
        data = self._http_json("GET", path)
        names, points = data.get("series", []), data.get("data", [])
        rows = points[names.index(unit)] if unit in names and names.index(unit) < len(points) else []
        rows = sorted(rows, key=lambda p: p["x"])
        return Series(metric, unit, experiment, [parse_timestamp(p["x"]) for p in rows], [float(p["y"]) for p in rows])

    def _live_series(self, metric: str, unit: str, minutes: float, experiment: str) -> Series:
        s = self._live_state().get(unit, {})
        values, stamps = s.get("history", {}).get(metric, []), s.get("history_t", {}).get(metric, [])
        pairs = list(zip(stamps, values))
        if pairs:
            cutoff = pairs[-1][0] - minutes * 60  # anchored on the newest reading, like the database path
            pairs = [(t, v) for t, v in pairs if t >= cutoff]
        return Series(metric, unit, experiment,
                      [datetime.fromtimestamp(t, timezone.utc) for t, _ in pairs], [float(v) for _, v in pairs])

    def _db_series(self, metric: str, unit: str, minutes: float, experiment: str) -> tuple[Series, str]:
        table, column, extra = METRIC_TABLES[metric]
        try:
            with self._connect_ro() as conn:
                latest = conn.execute(
                    f"SELECT MAX(timestamp) FROM {table} WHERE experiment = ? AND pioreactor_unit = ? {extra}",
                    (experiment, unit),
                ).fetchone()[0]
                if latest is None:
                    return Series(metric, unit, experiment, [], []), "no data"
                cutoff = parse_timestamp(latest) - timedelta(minutes=minutes)
                rows = conn.execute(
                    f"""SELECT timestamp, {column} FROM {table}
                        WHERE experiment = ? AND pioreactor_unit = ? AND timestamp >= ? {extra}
                        ORDER BY timestamp""",
                    (experiment, unit, cutoff.strftime("%Y-%m-%dT%H:%M:%S")),
                ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise ToolError(f"SQL error reading {table}: {exc}") from exc
        series = Series(
            metric,
            unit,
            experiment,
            [parse_timestamp(r[0]) for r in rows],
            [float(r[1]) for r in rows],
        )
        return series, latest

    def get_recent_timeseries(
        self, metric: str, unit: str | None = None, minutes: float = 10, max_points: int = 60
    ) -> dict[str, Any]:
        unit = self.resolve_unit(unit)
        series, anchor = self.get_series(metric, unit, minutes)
        step = max(1, len(series.values) // max_points)
        return {
            "unit": unit,
            "experiment": series.experiment,
            "window_end": anchor,
            "summary": series.summary(),
            "points": [
                [t.isoformat(timespec="seconds"), round(v, 4)]
                for t, v in list(zip(series.timestamps, series.values))[::step]
            ],
        }

    def plot_timeseries(
        self, metrics: list[str], unit: str | None = None, minutes: float = 10
    ) -> dict[str, Any]:
        """Draw 1-2 metrics in the terminal and save a PNG with twin y-axes."""
        # The Figure API (no pyplot) is safe to use from the Slack bot's worker threads.
        import numpy as np
        import plotext as tplt
        from matplotlib.figure import Figure

        if not 1 <= len(metrics) <= 2:
            raise ToolError("Plot one or two metrics at a time.")
        unit = self.resolve_unit(unit)
        series = [self.get_series(m, unit, minutes)[0] for m in metrics]
        if any(not s.values for s in series):
            empty = [s.metric for s in series if not s.values]
            raise ToolError(f"No data for {empty} on {unit}.")

        t0 = min(s.timestamps[0] for s in series)
        xs = [[(t - t0).total_seconds() / 60 for t in s.timestamps] for s in series]

        if self.render_terminal:
            # Terminal plot, so the judges see it on the shared screen immediately.
            tplt.clear_figure()
            tplt.theme("pro")
            tplt.plotsize(100, 25)
            tplt.title(f"{unit} · last {minutes:g} min")
            tplt.xlabel("minutes")
            for i, (s, x) in enumerate(zip(series, xs)):
                tplt.plot(x, s.values, label=METRIC_LABELS[s.metric], yside="left" if i == 0 else "right")
            tplt.show()

        # PNG for Slack / the README.
        fig = Figure(figsize=(10, 4.5), dpi=120)
        ax = fig.subplots()
        colors = ["#2563eb", "#dc2626"]
        ax.plot(xs[0], series[0].values, color=colors[0], label=METRIC_LABELS[series[0].metric])
        ax.set_ylabel(METRIC_LABELS[series[0].metric], color=colors[0])
        ax.set_xlabel(f"minutes since {t0.strftime('%H:%M:%S')} UTC")
        if len(series) == 2:
            ax2 = ax.twinx()
            ax2.plot(xs[1], series[1].values, color=colors[1], label=METRIC_LABELS[series[1].metric])
            ax2.set_ylabel(METRIC_LABELS[series[1].metric], color=colors[1])
        ax.set_title(f"LARR-E · {unit} · {series[0].experiment}")
        ax.grid(alpha=0.3)
        fig.tight_layout()
        out = self.cfg.output.plots_dir / f"{unit}_{'_vs_'.join(metrics)}_{int(time.time())}.png"
        fig.savefig(out)

        result: dict[str, Any] = {
            "unit": unit,
            "png_path": str(out),
            "rendered_in_terminal": self.render_terminal,
            "summaries": [s.summary() for s in series],
        }
        if len(series) == 2:
            # Align the second series onto the first's timestamps before correlating.
            x0 = np.array([t.timestamp() for t in series[0].timestamps])
            x1 = np.array([t.timestamp() for t in series[1].timestamps])
            y1_on_x0 = np.interp(x0, x1, series[1].values)
            y0 = np.array(series[0].values)
            if len(y0) > 2 and y0.std() > 0 and y1_on_x0.std() > 0:
                result["pearson_r"] = round(float(np.corrcoef(y0, y1_on_x0)[0, 1]), 3)
        return result

    # ------------------------------------------------------------------ live state

    def _live_state(self) -> dict[str, Any]:
        path = self.cfg.output.state_path
        try:
            return json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def get_live_status(self, unit: str | None = None, include_history: bool = False) -> dict[str, Any]:
        state = self._live_state()
        if not include_history:  # charts only; keep it out of the agent's context
            state = {u: {k: v for k, v in s.items() if k not in ("history", "history_t")} for u, s in state.items()}
        now = datetime.now(timezone.utc)
        for s in state.values():  # say how old the data is, so a stopped job isn't reported as live
            try:
                age = round((now - parse_timestamp(s["last_seen"])).total_seconds())
            except (KeyError, ValueError):
                continue
            s["seconds_since_last_reading"] = age
            if age > 120:  # spelled out in the data: weaker models skip instructions more than facts
                s["stale"] = True
                s["note"] = (f"STALE: the newest reading is {age // 60} min old. The jobs that publish these "
                             "readings are probably stopped; these values are NOT current.")
        if not state:
            return {"available": False, "note": "listener.py is not running or has seen no telemetry yet."}
        if unit:
            if unit not in state:
                raise ToolError(f"No live telemetry for {unit}. Known units: {sorted(state)}")
            return {"available": True, unit: state[unit]}
        return {"available": True, **state}

    def list_recent_alerts(self, limit: int = 10) -> dict[str, Any]:
        path = self.cfg.output.alerts_path
        if not path.exists():
            return {"alerts": []}
        lines = path.read_text().strip().splitlines()[-limit:]
        return {"alerts": [json.loads(line) for line in lines if line]}
