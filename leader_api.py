"""Minimal client for the Pioreactor leader's REST API (the same API its web UI and MCP server use).

Recent Pioreactor releases start jobs through this API rather than through an MQTT
`run/<job>` topic. Long-running requests return 202 with a `result_url_path`, which
we poll until the task finishes.

Endpoints used (checked against Pioreactor 26.11 source, core/pioreactor/web/api.py):
    POST /api/workers/<unit>/jobs/run/job_name/<job>/experiments/<exp>
    POST /api/workers/<unit>/jobs/stop/job_name/<job>/experiments/<exp>
    POST /api/experiment_profiles                      {"filename", "body"}
    GET  /api/experiments/<exp>/experiment_profiles/running
    GET  /api/workers/<unit>/experiment                 the experiment the unit is assigned to
    POST /unit_api/jobs/stop                           {"job_id": ...}  (core/pioreactor/web/unit_api.py)
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import quote


class LeaderAPIError(Exception):
    pass


class LeaderAPI:
    def __init__(self, base_url: str, timeout_s: float = 10.0, poll_timeout_s: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.poll_timeout_s = poll_timeout_s

    # ------------------------------------------------------------------ transport

    def _request(self, method: str, path: str, body: Any = None, timeout_s: float | None = None) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s or self.timeout_s) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise LeaderAPIError(f"{method} {path} failed (HTTP {exc.code}): {detail}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise LeaderAPIError(f"Cannot reach the Pioreactor leader at {self.base_url}: {exc}") from exc

    def call(self, method: str, path: str, body: Any = None) -> Any:
        """Make a request; if the leader answers 202 with a task, poll it to completion."""
        status, content = self._request(method, path, body)
        deadline = time.monotonic() + self.poll_timeout_s
        while status == 202 and isinstance(content, dict) and "result_url_path" in content:
            result_path = content["result_url_path"]
            if not result_path.startswith("/"):
                result_path = "/" + result_path
            while True:
                if time.monotonic() > deadline:
                    raise LeaderAPIError(
                        f"Task {content.get('task_id')} still pending at {result_path}. "
                        "It may still complete: check running jobs before retrying."
                    )
                time.sleep(0.25)
                status, polled = self._request("GET", result_path)
                if isinstance(polled, dict) and polled.get("status") in {"pending", "accepted", "running"}:
                    continue
                break
            if isinstance(polled, dict) and polled.get("status") == "failed":
                raise LeaderAPIError(polled.get("error") or f"Task at {result_path} failed.")
            if isinstance(polled, dict) and "result" in polled:
                return polled["result"]
            return polled
        return content

    # ------------------------------------------------------------------ jobs

    def run_job(
        self,
        unit: str,
        job: str,
        experiment: str,
        options: dict[str, Any] | None = None,
        args: list[str] | None = None,
    ) -> Any:
        path = f"/api/workers/{quote(unit, safe='')}/jobs/run/job_name/{quote(job, safe='')}/experiments/{quote(experiment, safe='')}"
        body = {"options": options or {}, "args": args or [], "env": {"JOB_SOURCE": "larr-e"}, "config_overrides": []}
        return self.call("POST", path, body)

    def stop_job(self, unit: str, job: str, experiment: str) -> Any:
        path = f"/api/workers/{quote(unit, safe='')}/jobs/stop/job_name/{quote(job, safe='')}/experiments/{quote(experiment, safe='')}"
        return self.call("POST", path, {})

    def assigned_experiment(self, unit: str) -> str | None:
        """The experiment the leader has this worker assigned to; None if unknown or unreachable.

        Jobs can only run in the assigned experiment, so this beats any guess from config or data.
        """
        try:
            status, content = self._request("GET", f"/api/workers/{quote(unit, safe='')}/experiment", timeout_s=3)
        except LeaderAPIError:
            return None
        return content.get("experiment") if isinstance(content, dict) else None

    # ------------------------------------------------------------------ experiment profiles

    def upload_profile(self, filename: str, yaml_body: str) -> Any:
        """Save a profile on the leader. The leader validates it and returns 400 with diagnostics if invalid."""
        return self.call("POST", "/api/experiment_profiles", {"filename": filename, "body": yaml_body})

    def run_profile(self, leader_unit: str, filename: str, experiment: str, dry_run: bool = False) -> Any:
        """Equivalent to `pio run experiment_profile execute <filename> <experiment> [--dry-run]` on the leader."""
        options: dict[str, Any] = {"dry-run": None} if dry_run else {}
        return self.run_job(leader_unit, "experiment_profile", experiment, options, ["execute", filename, experiment])

    def stop_profiles(self, leader_unit: str, experiment: str) -> Any:
        """Stops EVERY profile running in the experiment, on any unit. Prefer stop_profile_run."""
        return self.stop_job(leader_unit, "experiment_profile", experiment)

    def find_profile_run(self, experiment: str, profile_filename: str) -> int | None:
        """job_id of the running profile started from this file, if any."""
        for run in self.running_profiles(experiment) or []:
            settings = run.get("settings") or {}
            if settings.get("profile_filename") == profile_filename:
                return int(run["job_id"])
        return None

    def stop_job_id(self, job_id: int) -> Any:
        """Stop one job by id on the leader (unit API), leaving other profiles alone."""
        return self.call("POST", "/unit_api/jobs/stop", {"job_id": int(job_id)})

    def running_profiles(self, experiment: str) -> Any:
        return self.call("GET", f"/api/experiments/{quote(experiment, safe='')}/experiment_profiles/running")
