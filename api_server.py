"""LARR-E local HTTP API, plus a dashboard page small enough for the Nintendo 3DS browser.

Every request needs the shared token, as `?k=<token>` or an `X-Larre-Token` header.

    GET  /                      dashboard page (open /?k=<token> on the 3DS browser or a phone)
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
    "stop": {
        "label": "Stop stir + heat",
        "mode": "exec",
        "prompt": "Stop the stirring job and the temperature automation on the default unit.",
    },
}


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
        values = s.values
        if len(values) > points:  # bucket averages keep the shape without sending every point
            size = len(values) / points  # > 1, so every bucket is non-empty
            buckets = [values[int(i * size): int((i + 1) * size)] for i in range(points)]
            values = [sum(b) / len(b) for b in buckets]
        return {"unit": unit, "metric": metric, "minutes": minutes, "window_end": anchor,
                "values": [round(v, 4) for v in values], "summary": s.summary()}


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
            elif method == "GET" and parts == ["api", "status"]:
                self._json(200, svc.tools.get_live_status())
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
