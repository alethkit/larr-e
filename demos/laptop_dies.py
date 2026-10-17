"""Edge case: "run it for N minutes", and then LARR-E goes away before the N minutes are up.

The laptop sleeps, the hotspot drops, or the bot crashes. Both versions get the same request on the
digital twin, one unit each, and the LARR-E process exits straight after acting:

* this branch: start_experiment(duration_h=...) uploads a Pioreactor experiment profile, and the
  leader's own scheduler stops the jobs at the end;
* upstream (mrgarbett/larr-e main): no tool takes a duration (only `dose` has duration_s), so the
  most its agent can do is start_job for stirring and the thermostat. The agent loop ends when it
  answers, and nothing is left to stop the jobs.

We then wait past the planned end and ask the twin which jobs are still running. No hardware, no LLM.

    git archive <upstream-ref> | tar -x -C /tmp/upstream-larr-e
    uv run --group dev python demos/laptop_dies.py --upstream /tmp/upstream-larr-e
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

OURS = """
import json
from tools import PioreactorTools
r = PioreactorTools().start_experiment(name="edge case", unit="twin1", duration_h={duration_h},
                                       temperature_c=30, rpm=300, od_reading=False)
print(json.dumps({{"ok": r["ok"], "profile": r["plan"]["profile_filename"]}}))
"""

UPSTREAM = """
import json
from tools import PioreactorTools
t = PioreactorTools()
a = t.start_job("stirring", unit="twin2", options={"target_rpm": 300})
b = t.start_job("temperature_automation", unit="twin2", options={"target_temperature": 30})
print(json.dumps({"ok": a["ok"] and b["ok"]}))
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(cond, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if cond():
                return
        except OSError:
            pass
        time.sleep(0.2)
    sys.exit(f"timed out waiting for {what}")


def running(url: str) -> dict[str, list[str]]:
    with urllib.request.urlopen(f"{url}/twin/state", timeout=5) as r:
        state = json.loads(r.read())
    return {u: sorted(s["jobs"]) for u, s in state["units"].items()}


def larre(code: str, cwd: Path, env: dict[str, str]) -> str:
    """Run one LARR-E 'session' to completion: it acts, prints, and exits like a closed laptop."""
    out = subprocess.run([sys.executable, "-c", code], cwd=cwd, env=os.environ | env,
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        sys.exit(f"LARR-E in {cwd} failed:\n{out.stderr[-1500:]}")
    return out.stdout.strip().splitlines()[-1]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--upstream", type=Path, required=True, help="a checkout of mrgarbett/larr-e main")
    p.add_argument("--minutes", type=float, default=0.75, help="planned run length (real time)")
    a = p.parse_args()

    amqtt = shutil.which("amqtt") or str(Path(sys.executable).parent / "amqtt")
    tmp = Path(tempfile.mkdtemp(prefix="larre-edge-"))
    mqtt_port, http_port = free_port(), free_port()
    url = f"http://127.0.0.1:{http_port}"
    (tmp / "broker.yaml").write_text(
        f"listeners:\n  default:\n    type: tcp\n    bind: 127.0.0.1:{mqtt_port}\n"
        "plugins:\n  amqtt.plugins.authentication.AnonymousAuthPlugin:\n    allow_anonymous: true\n"
    )
    common = {"LARRE_ENV_FILE": "/nonexistent", "LARRE_MQTT_HOST": "127.0.0.1", "LARRE_MQTT_PORT": str(mqtt_port),
              "LARRE_DB_PATH": str(tmp / "none.sqlite")}
    procs = [subprocess.Popen([amqtt, "-c", str(tmp / "broker.yaml")],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)]
    try:
        wait_for(lambda: socket.socket().connect_ex(("127.0.0.1", mqtt_port)) == 0, 15, "MQTT broker")
        procs.append(subprocess.Popen(
            [sys.executable, str(REPO / "twin.py"), "--units", "twin1,twin2", "--experiment", "demo",
             "--mqtt-host", "127.0.0.1", "--mqtt-port", str(mqtt_port), "--host", "127.0.0.1",
             "--port", str(http_port)],
            env=os.environ | common, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        wait_for(lambda: urllib.request.urlopen(f"{url}/api/health", timeout=2).status == 200, 15, "twin")

        print(f'Request: "run at 30 °C and 300 rpm for {a.minutes:g} minutes", then LARR-E exits.\n')
        print("this branch, twin1:", larre(OURS.format(duration_h=a.minutes / 60), REPO,
                                          common | {"LARRE_LEADERS": f"twin1={url}",
                                                    "LARRE_RUNTIME_DIR": str(tmp / "ours")}))
        print("upstream,    twin2:", larre(UPSTREAM, a.upstream,
                                          common | {"LARRE_JOB_TRANSPORT": "http", "LARRE_LEADER_URL": url,
                                                    "LARRE_EXPERIMENT": "demo",
                                                    "LARRE_RUNTIME_DIR": str(tmp / "upstream")}))
        time.sleep(3)
        print("\nRunning straight after (both LARR-E processes have exited):", running(url))

        wait_s = a.minutes * 60 + 10
        print(f"Waiting {wait_s:.0f} s, past the planned end…")
        time.sleep(wait_s)
        after = running(url)
        print("Running after the planned end:", after)

        ours_ok, upstream_ok = not after["twin1"], not after["twin2"]
        print(f"\nthis branch: {'stopped on schedule' if ours_ok else 'STILL RUNNING'}")
        print(f"upstream:    {'stopped on schedule' if upstream_ok else 'STILL RUNNING: heater and stirrer on, nobody will stop them'}")
        sys.exit(0 if ours_ok and not upstream_ok else 1)
    finally:
        for proc in reversed(procs):
            proc.terminate()


if __name__ == "__main__":
    main()
