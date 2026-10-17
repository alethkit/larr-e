"""Film-ready scene: two digital-twin Pioreactors, a run on each, then a stirrer fault LARR-E catches.

One command, no hardware and no Pioreactor. It starts a local MQTT broker, two twin units, and the
LARR-E listener (its alerts print in this terminal). Then it:

1. starts a short run on each twin (37 °C, 300 rpm) from an experiment plan, as `larr-e exec` would;
2. waits for a baseline of readings;
3. stalls twin1's stirrer, the way a decoupled stir bar would;
4. shows the alert and the live status table.

    uv run --group dev python demos/twin_scene.py            # alerts only, no API key needed
    uv run --group dev python demos/twin_scene.py --triage   # also Claude's triage (needs ANTHROPIC_API_KEY)

Leave it running to keep filming; Ctrl-C stops everything. The twins are simulated: say so on screen.
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
UNITS = ["twin1", "twin2"]

START_RUN = """
import json, sys
from tools import PioreactorTools
r = PioreactorTools(render_terminal=False).start_experiment(
    name="overnight culture", unit=sys.argv[1], duration_h=float(sys.argv[2]),
    temperature_c=37, rpm=300, od_reading=True, update_every_h=None)
print(json.dumps({"ok": r["ok"], "profile": r["plan"]["profile_filename"]}))
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


def say(text: str) -> None:
    print(f"\n\033[1;36m▶ {text}\033[0m", flush=True)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--triage", action="store_true", help="let Claude triage the alert (needs ANTHROPIC_API_KEY)")
    p.add_argument("--baseline-s", type=float, default=60, help="seconds of normal readings before the fault")
    p.add_argument("--run-minutes", type=float, default=30, help="length of each twin's run")
    p.add_argument("--bio-speed", type=float, default=1.0,
                   help="twin growth speed-up; above 1, growth rates on screen are inflated by the same factor")
    a = p.parse_args()

    amqtt = shutil.which("amqtt") or str(Path(sys.executable).parent / "amqtt")
    if not Path(amqtt).exists():
        sys.exit("No amqtt broker found: run `uv sync --group dev` first.")
    tmp = Path(tempfile.mkdtemp(prefix="larre-scene-"))
    mqtt_port, http_port = free_port(), free_port()
    url = f"http://127.0.0.1:{http_port}"
    (tmp / "broker.yaml").write_text(
        f"listeners:\n  default:\n    type: tcp\n    bind: 127.0.0.1:{mqtt_port}\n"
        "plugins:\n  amqtt.plugins.authentication.AnonymousAuthPlugin:\n    allow_anonymous: true\n"
    )
    env = os.environ | {
        "LARRE_ENV_FILE": "/nonexistent",  # keep this scene independent of the real Pi's .env
        "LARRE_MQTT_HOST": "127.0.0.1", "LARRE_MQTT_PORT": str(mqtt_port), "LARRE_MQTT_USERNAME": "",
        "LARRE_LEADERS": ",".join(f"{u}={url}" for u in UNITS), "LARRE_PUMP_TRANSPORT": "rest",
        "LARRE_RUNTIME_DIR": str(tmp / "runtime"), "LARRE_RECORD_DB": "1",
        "LARRE_DB_PATH": str(tmp / "runtime" / "cluster.sqlite"), "LARRE_DEFAULT_UNIT": UNITS[0],
    }
    quiet = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    procs = [subprocess.Popen([amqtt, "-c", str(tmp / "broker.yaml")], **quiet)]
    try:
        wait_for(lambda: socket.socket().connect_ex(("127.0.0.1", mqtt_port)) == 0, 15, "MQTT broker")
        procs.append(subprocess.Popen(
            [sys.executable, str(REPO / "twin.py"), "--units", ",".join(UNITS), "--experiment", "larre-twin",
             "--mqtt-host", "127.0.0.1", "--mqtt-port", str(mqtt_port), "--host", "127.0.0.1",
             "--port", str(http_port), "--bio-speed", str(a.bio_speed)], cwd=REPO, env=env, **quiet))
        wait_for(lambda: urllib.request.urlopen(f"{url}/api/health", timeout=2).status == 200, 15, "twin")

        say(f"Two simulated Pioreactors ({', '.join(UNITS)}) are up. Starting LARR-E's listener.")
        listen = [sys.executable, "agent.py", "listen"] + ([] if a.triage else ["--no-triage"])
        procs.append(subprocess.Popen(listen, cwd=REPO, env=env))
        time.sleep(3)

        for unit in UNITS:
            out = subprocess.run([sys.executable, "-c", START_RUN, unit, str(a.run_minutes / 60)],
                                 cwd=REPO, env=env, capture_output=True, text=True, timeout=60)
            if out.returncode != 0:
                sys.exit(f"Could not start the run on {unit}:\n{out.stderr[-1500:]}")
            say(f"{unit}: run started at 37 °C, 300 rpm, {a.run_minutes:g} min ({json.loads(out.stdout)['profile']})")

        say(f"Collecting a baseline for {a.baseline_s:g} s…")
        time.sleep(a.baseline_s)
        subprocess.run([sys.executable, "agent.py", "status"], cwd=REPO, env=env)

        say("Fault: twin1's stir bar decouples.")
        req = urllib.request.Request(f"{url}/twin/fault", data=json.dumps({"unit": "twin1", "fault": "stir_stall"}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=5).read()

        alerts = tmp / "runtime" / "alerts.jsonl"
        wait_for(lambda: alerts.exists() and "stirring" in alerts.read_text(), 90, "the stall alert")
        time.sleep(1)
        say("LARR-E caught it. Live status now:")
        subprocess.run([sys.executable, "agent.py", "status"], cwd=REPO, env=env)
        say("Still running for more footage. Ctrl-C to stop.")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for proc in reversed(procs):
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    main()
