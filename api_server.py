"""LARR-E local HTTP API, plus a dashboard page small enough for the Nintendo 3DS browser.

Every request needs the shared token, as `?k=<token>` or an `X-Larre-Token` header.

    GET  /                      dashboard page (open /?k=<token> on the 3DS browser or a phone)
    GET  /plan                  plan editor: drag the numbers, see the profile and the ghost line
    GET  /api/units             every unit LARR-E knows (configured leaders and streaming units)
    GET  /api/plan?unit=pio01   the saved plan for a unit (or all units)
    POST /api/plan/preview      {"name", "duration_h", "temperature_c", "rpm", "unit"} -> plan + YAML + limits
    POST /api/plan/start        same body; waits for approval, no LLM -> {"job_id": ...}
    GET  /api/status            live telemetry from listener.py
    GET  /api/alerts?n=5        recent alerts
    GET  /api/series?metric=od&unit=worker1&minutes=30&points=40
    GET  /api/presets           canned requests for screens without a keyboard
    POST /api/presets/<name>    start a preset             -> {"job_id": ...}
    POST /api/ask               {"prompt", "mode": "query"|"exec", "style": "3ds"|"web"} -> {"job_id": ...}
    GET  /api/jobs/<id>         {"state": "running"|"done"|"error", "answer", "approval"}
    GET  /api/approvals         pending hardware actions
    POST /api/approvals/<id>    {"decision": "approve"|"deny", "by": "3ds"}

Agent answers take longer than a single PocketJS offload request may wait, so asking is
"start a job, then poll it". Hardware actions block their job until someone approves
from any client, or the request expires with nothing changed.
"""

from __future__ import annotations

import hmac
import json
import logging
import secrets
import textwrap
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import anthropic

from agent import LarreAgent
from openai_compat import LLMError
from config import Settings, settings as default_settings
from tools import METRIC_TABLES, PioreactorTools, ToolError, parse_timestamp, utc_timestamp

log = logging.getLogger("larre.api")

DASHBOARD_PATH = Path(__file__).resolve().parent / "web" / "dashboard.html"
PLAN_PAGE_PATH = Path(__file__).resolve().parent / "web" / "plan.html"

STYLES = {
    "3ds": "Your answer is shown on a Nintendo 3DS screen: plain text only, no Markdown, "
    "at most 5 short lines (about 300 characters).",
    "web": "Your answer is shown on a small phone-sized web page: plain text, at most 8 short lines.",
}

PRESET_LIBRARY: dict[str, dict[str, str]] = {
    "summary": {
        "label": "Health check",
        "mode": "query",
        "prompt": "Give a one-screen health summary of every unit: OD, temperature, growth rate, "
        "stirring, and anything abnormal.",
    },
    "triage": {
        "label": "Explain last alert",
        "mode": "query",
        "prompt": "Look at the most recent alert, check the data around it, and give the likely "
        "cause and the one recommended next action.",
    },
    "trend": {
        "label": "OD trend 30 min",
        "mode": "query",
        "prompt": "How has OD changed over the last 30 minutes on the default unit? Is growth healthy?",
    },
    "experiment": {
        "label": "Experiment progress",
        "mode": "query",
        "prompt": "Report on the running experiment plan: elapsed and remaining time, whether temperature "
        "and stirring match the plan, and how OD and growth rate are developing.",
    },
    "dilute": {
        "label": "Dilute 0.5 ml",
        "mode": "exec",
        "prompt": "Dilute the default unit: add 0.5 ml of fresh media, then remove 0.5 ml of waste.",
    },
    "stir": {
        "label": "Start stirring",
        "mode": "exec",
        "prompt": "Start stirring on the default unit at 500 RPM. If stirring is already running, "
        "set its target to 500 RPM instead.",
    },
    "heat": {
        "label": "Heat to 32 C",
        "mode": "exec",
        "prompt": "Heat the default unit to 32 C: start the temperature automation (thermostat) with "
        "target_temperature 32. If it is already running, set its target temperature to 32 instead.",
    },
    "pump": {
        "label": "Pump media 2 s",
        "mode": "exec",
        "prompt": "Run the media pump on the default unit for 2 seconds (dose with pump=media, duration_s=2).",
    },
    "exchange": {
        "label": "Swap media 2 s",
        "mode": "exec",
        "prompt": "Exchange media on the default unit: run the media pump for 2 seconds (dose pump=media, "
        "duration_s=2), then the waste pump for 2 seconds (dose pump=waste, duration_s=2).",
    },
    "stop": {
        "label": "Stop stir + heat",
        "mode": "exec",
        "prompt": "Stop the stirring job and the temperature automation on the default unit.",
    },
}


# ---------------------------------------------------------------- plain-text screen (native 3DS app)

ALERT_MAX_AGE_S = 600  # older alerts are history, not news
TOOL_TITLES = {
    "dose": "Run a pump",
    "set_target_temperature": "Change temperature",
    "set_stirring_rpm": "Change stirring",
    "start_job": "Start a job",
    "stop_job": "Stop a job",
    "start_experiment": "Start experiment",
    "stop_experiment": "Stop experiment",
}
_ASCII = {"°": "", "→": "->", "←": "<-", "µ": "u", "–": "-", "—": "-", "’": "'", "‘": "'", "“": '"', "”": '"', "…": "..."}


def _ascii(text: str) -> str:
    text = "".join(_ASCII.get(c, c) for c in str(text))
    return text.encode("ascii", "replace").decode()


def _wrap(text: str, cols: int, max_lines: int) -> list[str]:
    lines = [w for para in _ascii(text).splitlines() for w in (textwrap.wrap(para, cols) or [""])]
    return lines[:max_lines]


def screen_lines(status: dict[str, Any], alerts: dict[str, Any], approvals: list[dict[str, Any]],
                 cols: int = 40) -> list[str]:
    """What the native 3DS app shows, as tagged ASCII lines it can print without parsing JSON.

    U unit row, N note, C/A alert title (critical/other), a alert text, P "<id> <title>" of the
    first pending approval, p its arguments. The app only splits lines on the tag.
    """
    def num(entry: Any, fmt: str) -> str:
        return format(entry["value"], fmt) if isinstance(entry, dict) and isinstance(entry.get("value"), (int, float)) else "-"

    lines: list[str] = []
    units = {k: v for k, v in status.items() if k not in ("available", "note") and isinstance(v, dict)}
    if status.get("available") and units:
        for name in sorted(units):
            u = units[name]
            od = (u.get("od") or {}).get("od1") or (u.get("od") or {}).get("od2")
            row = (f"{name[:6]:<6} OD {num(od, '.3f'):>5} {num(u.get('temperature'), '.1f'):>4}C "
                   f"{num(u.get('growth_rate'), '.2f'):>4}/h {num(u.get('stirring'), '.0f'):>4}rpm")
            if u.get("stale"):
                row += " old"
            lines.append("U " + _ascii(row)[:cols])
    else:
        lines.append("N " + _ascii(status.get("note") or "No telemetry yet")[:cols])

    last = (alerts.get("alerts") or [None])[-1]
    if last and last.get("age_s") is not None and last["age_s"] <= ALERT_MAX_AGE_S:
        tag = "C" if last.get("severity") == "critical" else "A"
        title = f"{str(last.get('severity', '')).upper()} {last.get('unit', '')} {last.get('metric', '')}"
        lines.append(f"{tag} " + _ascii(title)[:cols])
        lines += ["a " + line for line in _wrap(last.get("message", ""), cols, 2)]

    if approvals:
        a = approvals[0]
        lines.append(f"P {a['id']} " + _ascii(TOOL_TITLES.get(a["tool"], a["tool"]))[:cols])
        args = "\n".join(f"{k} = {v}" for k, v in a.get("args", {}).items() if v is not None)
        lines += ["p " + line for line in _wrap(args, cols, 4)]
    return lines


@dataclass
class Approval:
    id: str
    job_id: str
    tool: str
    args: dict[str, Any]
    requested_at: str
    decision: str | None = None  # approved | denied | expired
    decided_by: str | None = None
    event: threading.Event = field(default_factory=threading.Event)

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "job_id": self.job_id, "tool": self.tool, "args": self.args,
                "requested_at": self.requested_at}


@dataclass
class Job:
    id: str
    prompt: str
    mode: str
    source: str
    state: str = "running"  # running | done | error
    answer: str = ""
    approval_id: str | None = None
    created: float = field(default_factory=time.time)

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "state": self.state, "mode": self.mode, "prompt": self.prompt,
                "answer": self.answer, "approval": self.approval_id}


class LarreService:
    def __init__(self, cfg: Settings = default_settings):
        self.cfg = cfg
        cfg.ensure_dirs()
        self.tools = PioreactorTools(cfg, render_terminal=False)
        self.pool = ThreadPoolExecutor(max_workers=cfg.api.workers, thread_name_prefix="larre-api")
        self.lock = threading.Lock()
        self.jobs: OrderedDict[str, Job] = OrderedDict()
        self.approvals: dict[str, Approval] = {}
        names = [n.strip() for n in cfg.api.presets.split(",") if n.strip()]
        unknown = [n for n in names if n not in PRESET_LIBRARY]
        if unknown:
            raise ValueError(f"Unknown LARRE_PRESETS entries {unknown}; choose from {sorted(PRESET_LIBRARY)}")
        self.presets = {n: PRESET_LIBRARY[n] for n in names}

    # ---------------------------------------------------------------- jobs

    def start_job(self, prompt: str, mode: str, style: str, source: str) -> Job:
        if mode not in {"query", "exec"}:
            raise ValueError("mode must be 'query' or 'exec'")
        prompt = prompt.strip()
        if not prompt or len(prompt) > 2000:
            raise ValueError("prompt must be 1-2000 characters")
        job = Job(uuid.uuid4().hex[:10], prompt, mode, source)
        with self.lock:
            self.jobs[job.id] = job
            while len(self.jobs) > self.cfg.api.max_jobs:
                self.jobs.popitem(last=False)
        self.pool.submit(self._run_job, job, STYLES.get(style, STYLES["web"]))
        return job

    def _run_job(self, job: Job, style_note: str) -> None:
        agent = LarreAgent(
            job.mode,
            cfg=self.cfg,
            confirm=lambda tool, args: self.request_approval(job, tool, args),
            verbose=False,
            extra_system=style_note,
            render_terminal=False,
        )
        try:
            job.answer = agent.ask(job.prompt)
            job.state = "done"
        except anthropic.APIStatusError as exc:
            job.answer, job.state = f"Claude API error {exc.status_code}: {exc.message}", "error"
        except anthropic.APIConnectionError:
            job.answer, job.state = "Cannot reach the Claude API.", "error"
        except LLMError as exc:
            job.answer, job.state = str(exc), "error"
        except Exception as exc:  # the API server must survive any job failure
            log.exception("Job %s failed", job.id)
            job.answer, job.state = f"Internal error: {exc}", "error"

    def get_job(self, job_id: str) -> Job | None:
        with self.lock:
            return self.jobs.get(job_id)

    # ---------------------------------------------------------------- approvals

    def request_approval(self, job: Job, tool: str, args: dict[str, Any]) -> bool:
        approval = Approval(uuid.uuid4().hex[:10], job.id, tool, dict(args), utc_timestamp())
        with self.lock:
            self.approvals[approval.id] = approval
        job.approval_id = approval.id
        decided = approval.event.wait(self.cfg.api.approval_timeout_s)
        with self.lock:
            self.approvals.pop(approval.id, None)
            if not decided:
                approval.decision = "expired"
        job.approval_id = None
        self._audit(approval, job)
        return approval.decision == "approved"

    def decide(self, approval_id: str, decision: str, by: str) -> None:
        if decision not in {"approve", "deny"}:
            raise ValueError("decision must be 'approve' or 'deny'")
        with self.lock:
            approval = self.approvals.get(approval_id)
            if approval is None or approval.event.is_set():
                raise LookupError("No such pending approval (already decided or expired).")
            approval.decision = "approved" if decision == "approve" else "denied"
            approval.decided_by = by[:40] or "unknown"
            approval.event.set()

    def pending_approvals(self) -> list[dict[str, Any]]:
        with self.lock:
            return [a.public() for a in self.approvals.values() if not a.event.is_set()]

    def _audit(self, approval: Approval, job: Job) -> None:
        record = {
            "at": utc_timestamp(), "source": f"api:{job.source}", "tool": approval.tool,
            "args": approval.args, "requester": job.source, "decision": approval.decision,
            "decided_by": approval.decided_by, "prompt": job.prompt, "dry_run": self.tools.dry_run,
        }
        with self.cfg.output.audit_path.open("a") as f:
            f.write(json.dumps(record) + "\n")

    # ---------------------------------------------------------------- plan page (no LLM)

    PLAN_FIELDS = {"name", "duration_h", "temperature_c", "rpm", "unit", "update_every_h", "notes"}

    def _plan_args(self, body: dict[str, Any]) -> dict[str, Any]:
        unknown = set(body) - self.PLAN_FIELDS
        if unknown:
            raise ValueError(f"unknown plan fields {sorted(unknown)}")
        return {k: v for k, v in body.items() if v is not None}

    def preview_plan(self, body: dict[str, Any]) -> dict[str, Any]:
        """The plan, its profile YAML, and the safety limits the page's draggable numbers stay within."""
        out = self.tools.plan_experiment(**self._plan_args(body))
        lim = self.cfg.safety
        out["limits"] = {
            "temperature_c": [lim.min_temperature_c, lim.max_temperature_c],
            "rpm": [1, lim.max_stirring_rpm],
            "duration_h": [0.05, self.cfg.plans.max_duration_h],
        }
        return out

    def start_plan(self, body: dict[str, Any], source: str) -> Job:
        """Start a plan straight from the page: validate now, then wait for the same approval gate."""
        args = self._plan_args(body)
        self.tools.start_experiment(check_only=True, **args)  # refuse bad plans before asking anyone
        job = Job(uuid.uuid4().hex[:10], f"Start plan: {args.get('name', '')}", "plan", source)
        with self.lock:
            self.jobs[job.id] = job
        self.pool.submit(self._run_plan, job, args)
        return job

    def _run_plan(self, job: Job, args: dict[str, Any]) -> None:
        try:
            if not self.request_approval(job, "start_experiment", args):
                job.answer, job.state = "Not started: the request was denied or expired.", "done"
                return
            out = self.tools.start_experiment(**args)
            job.answer, job.state = f"Started {out['plan']['summary']}", "done"
        except ToolError as exc:
            job.answer, job.state = str(exc), "error"
        except Exception as exc:  # the API server must survive any job failure
            log.exception("Plan job %s failed", job.id)
            job.answer, job.state = f"Internal error: {exc}", "error"

    # ---------------------------------------------------------------- data

    def recent_alerts(self, limit: int) -> dict[str, Any]:
        # age_s is computed here because old handheld browsers can't parse microsecond ISO timestamps.
        alerts = self.tools.list_recent_alerts(limit)["alerts"]
        now = time.time()
        for a in alerts:
            try:
                a["age_s"] = round(now - parse_timestamp(a["detected_at"]).timestamp())
            except (KeyError, ValueError):
                a["age_s"] = None
        return {"alerts": alerts}

    def series(self, metric: str, unit: str | None, minutes: float, points: int) -> dict[str, Any]:
        if metric not in METRIC_TABLES:
            raise ValueError(f"metric must be one of {sorted(METRIC_TABLES)}")
        unit = self.tools.resolve_unit(unit)
        s, anchor = self.tools.get_series(metric, unit, minutes)
        values, times = s.values, [t.timestamp() for t in s.timestamps]
        if len(values) > points:  # bucket averages keep the shape without sending every point
            size = len(values) / points  # > 1, so every bucket is non-empty
            spans = [(int(i * size), int((i + 1) * size)) for i in range(points)]
            values = [sum(values[a:b]) / (b - a) for a, b in spans]
            times = [sum(times[a:b]) / (b - a) for a, b in spans] if times else []
        return {"unit": unit, "metric": metric, "minutes": minutes, "window_end": anchor,
                "values": [round(v, 4) for v in values], "t": [round(t, 1) for t in times],
                "summary": s.summary()}


class Handler(BaseHTTPRequestHandler):
    service: LarreService
    token: str
    server_version = "LARR-E"

    # Old handheld browsers send HTTP/1.0 and dislike keep-alive surprises.
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        pass  # the default log line includes the query string, and with it the token

    def _log(self, status: int) -> None:
        log.info("%s %s %s %d", self.client_address[0], self.command, urlparse(self.path).path, status)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self._log(status)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, json.dumps(payload, default=str).encode(), "application/json; charset=utf-8")

    def _authorized(self, query: dict[str, list[str]]) -> bool:
        supplied = self.headers.get("X-Larre-Token") or (query.get("k") or [""])[0]
        return hmac.compare_digest(supplied.encode(), self.token.encode())

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 16_384:
            raise ValueError("request body too large")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if not self._authorized(query):
            if url.path == "/":
                self._send(401, b"<p>LARR-E: open this page with ?k=&lt;token&gt;</p>", "text/html")
            else:
                self._json(401, {"error": "missing or wrong token"})
            return
        parts = [p for p in url.path.split("/") if p]
        q = lambda name, default=None: (query.get(name) or [default])[0]  # noqa: E731
        svc = self.service
        try:
            if method == "GET" and not parts:
                self._send(200, DASHBOARD_PATH.read_bytes(), "text/html; charset=utf-8")
            elif method == "GET" and parts == ["plan"]:
                self._send(200, PLAN_PAGE_PATH.read_bytes(), "text/html; charset=utf-8")
            elif method == "GET" and parts == ["api", "units"]:
                self._json(200, svc.tools.list_units())
            elif method == "GET" and parts == ["api", "plan"]:
                self._json(200, svc.tools.get_experiment_plan(q("unit")))
            elif method == "POST" and parts == ["api", "plan", "preview"]:
                self._json(200, svc.preview_plan(self._body()))
            elif method == "POST" and parts == ["api", "plan", "start"]:
                job = svc.start_plan(self._body(), source="plan-page")
                self._json(202, {"job_id": job.id})
            elif method == "GET" and parts == ["api", "status"]:
                self._json(200, svc.tools.get_live_status(include_history=q("history") == "1"))
            elif method == "GET" and parts == ["api", "screen"]:
                cols = max(20, min(int(q("cols", 40)), 80))
                text = "\n".join(screen_lines(svc.tools.get_live_status(), svc.recent_alerts(1),
                                               svc.pending_approvals(), cols))
                self._send(200, (text + "\n").encode("ascii"), "text/plain; charset=us-ascii")
            elif method == "GET" and parts == ["api", "alerts"]:
                self._json(200, svc.recent_alerts(min(int(q("n", 5)), 50)))
            elif method == "GET" and parts == ["api", "series"]:
                self._json(200, svc.series(q("metric", "od"), q("unit"), min(float(q("minutes", 30)), 1440),
                                           max(2, min(int(q("points", 40)), 200))))
            elif method == "GET" and parts == ["api", "presets"]:
                self._json(200, {"presets": [{"name": k, "label": v["label"], "mode": v["mode"]}
                                             for k, v in svc.presets.items()], "dry_run": svc.tools.dry_run})
            elif method == "POST" and len(parts) == 3 and parts[:2] == ["api", "presets"]:
                preset = svc.presets.get(parts[2])
                if not preset:
                    raise LookupError(f"unknown preset {parts[2]}")
                body = self._body()
                job = svc.start_job(preset["prompt"], preset["mode"], body.get("style", "web"),
                                    str(body.get("source", "web")))
                self._json(202, {"job_id": job.id})
            elif method == "POST" and parts == ["api", "ask"]:
                body = self._body()
                job = svc.start_job(str(body.get("prompt", "")), body.get("mode", "query"),
                                    body.get("style", "web"), str(body.get("source", "web")))
                self._json(202, {"job_id": job.id})
            elif method == "GET" and len(parts) == 3 and parts[:2] == ["api", "jobs"]:
                job = svc.get_job(parts[2])
                if not job:
                    raise LookupError("unknown job")
                payload = job.public()
                if job.approval_id:
                    payload["approval"] = next(
                        (a for a in svc.pending_approvals() if a["id"] == job.approval_id), None
                    )
                self._json(200, payload)
            elif method == "GET" and parts == ["api", "approvals"]:
                self._json(200, {"approvals": svc.pending_approvals()})
            elif method == "POST" and len(parts) == 3 and parts[:2] == ["api", "approvals"]:
                body = self._body()
                svc.decide(parts[2], str(body.get("decision", "")), str(body.get("by", "api")))
                self._json(200, {"ok": True})
            else:
                self._json(404, {"error": "not found"})
        except LookupError as exc:
            self._json(404, {"error": str(exc)})
        except (ValueError, ToolError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})


def run(cfg: Settings = default_settings, host: str | None = None, port: int | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    token = cfg.api.token or secrets.token_urlsafe(9)
    Handler.service = LarreService(cfg)
    Handler.token = token
    host, port = host or cfg.api.host, port or cfg.api.port
    server = ThreadingHTTPServer((host, port), Handler)
    shown = "<this-computer's-LAN-IP>" if host == "0.0.0.0" else host
    print(f"LARR-E API on http://{shown}:{port}/?k={token}")
    if not cfg.api.token:
        print("(token generated for this run; set LARRE_API_TOKEN to keep it stable)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run()
