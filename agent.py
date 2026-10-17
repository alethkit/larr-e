"""LARR-E CLI agent: natural language -> Pioreactor actions and data queries.

    larr-e exec  "pump 1ml of media into worker1"
    larr-e query "plot the last 10 minutes of OD against temperature"
    larr-e chat
    larr-e listen | status | alerts | slack | api | twin
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import anthropic
import click
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from config import Settings, settings as default_settings
from openai_compat import ChatBackend, LLMError, to_openai_tools
from tools import AUDIT_KINDS, METRIC_TABLES, PUMP_JOBS, STARTABLE_JOBS, STOPPABLE_JOBS, PioreactorTools, ToolError, utc_timestamp

console = Console()

SYSTEM_PROMPT = """You are LARR-E, an operations agent for a Pioreactor bioreactor cluster.
Lab members talk to you from a terminal or chat. You act on the hardware and read data only
through the tools you are given.

How the cluster works:
- Each Pioreactor is a "unit" (e.g. worker1). Telemetry and commands flow over MQTT on the leader.
- The cluster can mix real units and digital-twin units (simulated, named e.g. twin1). They behave the
  same through your tools; when you report on a twin, say it is simulated.
- With more than one unit, a hardware request that doesn't name a unit is ambiguous: ask.
- Pumps: "media" adds fresh media, "waste" removes culture, "alt_media" adds alternative media.
- Temperature is held by the thermostat temperature automation; stirring by the stirring job.
- Growth rate is in h^-1; OD is unitless optical density.

Leader SQLite schema (read-only):
- experiments(experiment, created_at, description, media_used, organism_used)
- od_readings(experiment, pioreactor_unit, timestamp, od_reading, angle, channel)
- od_readings_filtered(experiment, pioreactor_unit, timestamp, normalized_od_reading)
- temperature_readings(experiment, pioreactor_unit, timestamp, temperature_c)
- growth_rates(experiment, pioreactor_unit, timestamp, rate)
- stirring_rates(experiment, pioreactor_unit, timestamp, measured_rpm)
- dosing_events(experiment, pioreactor_unit, timestamp, event, volume_change_ml, source_of_event)
Timestamps are ISO-8601 UTC strings, so they sort and compare as text.

Experiments:
- When someone describes a whole experiment ("37 °C, 180 rpm for 18 hours, keep me posted"), use
  start_experiment, not separate start_job calls. It becomes a Pioreactor experiment profile, so the
  Pioreactor itself runs the schedule and stops the jobs at the end, even if LARR-E goes offline.
- If anything that matters is missing (duration, temperature, speed), ask. Use plan_experiment to show
  the plan before starting it if the user wants to review it first.
- The <context> block carries the active plan's expected state. Read changes against it: a job
  stopping at the planned end is not a fault.

Rules:
- Prefer the purpose-built tools (plot_timeseries, get_recent_timeseries, get_live_status) over raw SQL.
- Make one hardware change per request unless the user clearly asked for several.
- The tools enforce safety limits. If a tool rejects a value, tell the user the limit; never try to
  get around it, e.g. by splitting one large dose into several small ones.
- If a request is ambiguous in a way that matters for hardware (which unit, how much), ask instead of guessing.
- If the user declines a confirmation, stop and say nothing was changed.
- Never state a reading you have not fetched with a tool in this conversation.
- For what LARR-E did or was asked to do (pump runs, who approved what, changes today), use audit_log;
  the Pioreactor's own dosing table is on the Pi, not here.
- Live readings marked stale (see seconds_since_last_reading and age_s) are old: the job that
  publishes them has probably stopped. Say so instead of presenting them as current.
- Reply in a few short lines suitable for a terminal or Slack message: what you did or found, with numbers.
"""

QUERY_MODE_NOTE = (
    "This session is read-only: you cannot change hardware. If the user asks for an action, "
    "explain what you would do and tell them to rerun it with `larr-e exec`."
)

TRIAGE_PROMPT = """An automated anomaly alert just fired:

{alert}

What the experiment plan says should be happening on this unit right now (null: no plan on file):

{plan}

If the plan explains the change (e.g. jobs stopped because the experiment reached its end), say so
in one line and recommend no action.

Investigate with the read-only tools (recent time series around the event, live status, recent
alerts) and write a short triage note: the likely cause (sensor artifact, e.g. a bubble or vial
removed, vs. real biology or hardware failure), the evidence, and the one recommended next action
an operator could approve with `larr-e exec`. Five lines at most."""


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


UNIT = {"type": "string", "description": "Pioreactor unit name, e.g. worker1. Omit to use the default unit."}
METRIC = {"type": "string", "enum": sorted(METRIC_TABLES)}

READ_TOOLS = [
    _tool(
        "get_live_status",
        "Latest live telemetry (OD, temperature, growth rate, stirring RPM) per unit from the MQTT listener.",
        {"unit": UNIT},
        [],
    ),
    _tool(
        "get_recent_timeseries",
        "Summary statistics plus a downsampled list of points for one metric over the most recent N minutes of data.",
        {"metric": METRIC, "unit": UNIT, "minutes": {"type": "number", "minimum": 0.5, "maximum": 1440}},
        ["metric"],
    ),
    _tool(
        "plot_timeseries",
        "Plot one metric, or two on twin y-axes (e.g. od vs temperature), over the most recent N minutes. "
        "Draws in the user's terminal, saves a PNG, and returns summaries and the Pearson r for two metrics.",
        {
            "metrics": {"type": "array", "items": METRIC, "minItems": 1, "maxItems": 2},
            "unit": UNIT,
            "minutes": {"type": "number", "minimum": 0.5, "maximum": 1440},
        },
        ["metrics"],
    ),
    _tool(
        "query_database",
        "Run one read-only SELECT/WITH query against the Pioreactor SQLite database. Results are capped at 200 rows.",
        {"sql": {"type": "string"}},
        ["sql"],
    ),
    _tool(
        "list_recent_alerts",
        "The most recent anomaly alerts raised by the LARR-E listener.",
        {"limit": {"type": "integer", "minimum": 1, "maximum": 50}},
        [],
    ),
    _tool(
        "audit_log",
        "LARR-E's own record of hardware actions over the last N hours: who asked, who approved, and "
        "whether it worked. Use it for questions like 'audit the pump runs' or 'what changed today'.",
        {"hours": {"type": "number", "minimum": 0.1, "maximum": 720},
         "kind": {"type": "string", "enum": sorted(AUDIT_KINDS)}},
        [],
    ),
]

EXPERIMENT_PROPS = {
    "name": {"type": "string", "description": "Short human name, e.g. 'E. coli overnight'."},
    "duration_h": {"type": "number", "exclusiveMinimum": 0, "description": "Total run time in hours."},
    "temperature_c": {"type": "number", "description": "Thermostat target. Omit to leave temperature uncontrolled."},
    "rpm": {"type": "number", "exclusiveMinimum": 0, "description": "Stirring target. Omit for no stirring."},
    "od_reading": {"type": "boolean", "description": "Measure OD (default true)."},
    "growth_rate": {"type": "boolean", "description": "Compute growth rate from OD (default: same as od_reading)."},
    "update_every_h": {"type": "number", "description": "Hours between Slack progress updates (default 2)."},
    "unit": UNIT,
    "notes": {"type": "string", "description": "Anything else the scientist said: organism, media, purpose."},
}

READ_TOOLS += [
    _tool(
        "list_units",
        "Every Pioreactor unit LARR-E can see, real or digital twin: which leader controls it, whether it "
        "is streaming telemetry, its experiment and its experiment plan. Use it for cluster-wide questions.",
        {},
        [],
    ),
    _tool(
        "plan_experiment",
        "Preview an experiment without starting it: returns the plan and the Pioreactor experiment profile "
        "YAML it would run. Changes nothing.",
        EXPERIMENT_PROPS,
        ["name", "duration_h"],
    ),
    _tool(
        "get_experiment_plan",
        "The stored experiment plan(s): what was asked for, when it started, when it ends, expected state.",
        {"unit": UNIT},
        [],
    ),
]

ACTION_TOOLS = [
    _tool(
        "start_experiment",
        "Start a complete experiment: uploads a Pioreactor experiment profile that starts the jobs now and "
        "stops them after duration_h, runs it on the leader, and stores the plan for monitoring and updates.",
        EXPERIMENT_PROPS,
        ["name", "duration_h"],
    ),
    _tool(
        "stop_experiment",
        "Stop the running experiment on a unit before its scheduled end (stops the profile and its jobs).",
        {"unit": UNIT},
        [],
    ),
    _tool(
        "dose",
        "Run a peristaltic pump once. Give exactly one of ml or duration_s. Uncalibrated pumps "
        "accept only duration_s (the tool says so if you try ml).",
        {
            "pump": {"type": "string", "enum": sorted(PUMP_JOBS)},
            "unit": UNIT,
            "ml": {"type": "number", "exclusiveMinimum": 0},
            "duration_s": {"type": "number", "exclusiveMinimum": 0},
        },
        ["pump"],
    ),
    _tool(
        "set_target_temperature",
        "Change the thermostat setpoint of a running temperature automation.",
        {"celsius": {"type": "number"}, "unit": UNIT},
        ["celsius"],
    ),
    _tool(
        "set_stirring_rpm",
        "Change the target RPM of a running stirring job.",
        {"rpm": {"type": "number", "minimum": 0}, "unit": UNIT},
        ["rpm"],
    ),
    _tool(
        "start_job",
        "Start a background job. Options, for example: stirring {target_rpm}; "
        "temperature_automation {automation_name: 'thermostat', target_temperature}.",
        {
            "job": {"type": "string", "enum": sorted(STARTABLE_JOBS)},
            "unit": UNIT,
            "options": {
                "type": "object",
                "properties": {
                    "target_rpm": {"type": "number", "description": "stirring only"},
                    "automation_name": {"type": "string", "enum": ["thermostat"], "description": "temperature_automation only"},
                    "target_temperature": {"type": "number", "description": "temperature_automation only, in C"},
                },
            },
        },
        ["job"],
    ),
    _tool(
        "stop_job",
        "Stop a running job on a unit.",
        {"job": {"type": "string", "enum": sorted(STOPPABLE_JOBS)}, "unit": UNIT},
        ["job"],
    ),
]

ACTION_TOOL_NAMES = {t["name"] for t in ACTION_TOOLS}

ConfirmFn = Callable[[str, dict[str, Any]], bool]
# (tool name, arguments, result, is_error) -> None; lets front ends react to tool calls.
ToolResultFn = Callable[[str, dict[str, Any], dict[str, Any], bool], None]


class LarreAgent:
    """Manual tool-use loop so every hardware call passes through our confirmation gate.

    Two LLM backends share the tools, limits and gates: Claude (default) and open models
    through Hugging Face Inference Providers or Google Gemini (LARRE_LLM=hf | gemini), both via
    their OpenAI-compatible chat APIs."""

    def __init__(
        self,
        mode: str = "query",
        cfg: Settings = default_settings,
        dry_run: bool | None = None,
        confirm: ConfirmFn | None = None,
        default_unit: str | None = None,
        verbose: bool = True,
        extra_system: str = "",
        on_tool_result: ToolResultFn | None = None,
        render_terminal: bool = True,
    ):
        if mode not in {"query", "exec"}:
            raise ValueError("mode must be 'query' or 'exec'")
        self.mode = mode
        self.cfg = cfg
        self.tools = PioreactorTools(cfg, dry_run=dry_run, render_terminal=render_terminal)
        self.confirm = confirm or (lambda name, args: True)
        self.default_unit = default_unit or cfg.pioreactor.default_unit
        self.verbose = verbose
        self.on_tool_result = on_tool_result
        self.requester = ""  # set by front ends so plans record who asked
        self.provider = cfg.llm.provider
        if self.provider == "anthropic":
            self.client = anthropic.Anthropic()
        elif self.provider in ("hf", "gemini"):
            self.chat = ChatBackend(cfg.llm)
        else:
            raise ValueError(f"LARRE_LLM must be 'anthropic', 'hf' or 'gemini', not {self.provider!r}")
        self.messages: list[dict[str, Any]] = []
        self.tool_specs = READ_TOOLS + (ACTION_TOOLS if mode == "exec" else [])
        self.system = SYSTEM_PROMPT + ("" if mode == "exec" else "\n" + QUERY_MODE_NOTE)
        if extra_system:
            self.system += "\n" + extra_system

    # -------------------------------------------------------------- tool dispatch

    def _dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        t = self.tools
        spec = next((s for s in self.tool_specs if s["name"] == name), None)
        if spec is None:
            raise ToolError(f"Tool {name} is not available in {self.mode} mode.")
        # Claude enforces the schema; open models may not, so reject anything outside it.
        unknown = set(args) - set(spec["input_schema"]["properties"])
        if unknown:
            raise ToolError(f"Unknown argument(s) for {name}: {sorted(unknown)}")
        if "unit" in spec["input_schema"]["properties"]:
            args.setdefault("unit", self.default_unit)
        handlers: dict[str, Callable[..., dict[str, Any]]] = {
            "get_live_status": t.get_live_status,
            "get_recent_timeseries": t.get_recent_timeseries,
            "plot_timeseries": t.plot_timeseries,
            "query_database": t.query_database,
            "list_recent_alerts": t.list_recent_alerts,
            "audit_log": t.audit_log,
            "dose": t.dose,
            "set_target_temperature": t.set_target_temperature,
            "set_stirring_rpm": t.set_stirring_rpm,
            "start_job": t.start_job,
            "stop_job": t.stop_job,
            "list_units": t.list_units,
            "plan_experiment": t.plan_experiment,
            "get_experiment_plan": t.get_experiment_plan,
            "start_experiment": t.start_experiment,
            "stop_experiment": t.stop_experiment,
        }
        if name not in handlers or name not in {s["name"] for s in self.tool_specs}:
            raise ToolError(f"Tool {name} is not available in {self.mode} mode.")
        if name == "start_experiment" and self.requester:
            args.setdefault("requested_by", self.requester)
        if name in ACTION_TOOL_NAMES:
            handlers[name](**args, check_only=True)  # raises ToolError on a limit, before anyone is asked
            if not self.confirm(name, args):
                return {"ok": False, "declined": True, "note": "The operator declined this action. Nothing was changed."}
        return handlers[name](**args)

    def _execute(self, name: str, args: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        if self.verbose:
            console.print(f"[dim]→ {name}({json.dumps(args)})[/dim]")
        try:
            result = self._dispatch(name, args)
            is_error = False
        except (ToolError, TypeError, ValueError) as exc:
            result = {"error": str(exc)}
            is_error = True
        if self.verbose:
            style = "red" if is_error else "green"
            console.print(f"[{style}]  ← {json.dumps(result, default=str)[:300]}[/{style}]")
        if self.on_tool_result:
            self.on_tool_result(name, args, result, is_error)
        if name in ACTION_TOOL_NAMES and not result.get("declined"):
            self._audit_result(name, args, result, is_error)
        return result, is_error

    def _audit_result(self, name: str, args: dict[str, Any], result: dict[str, Any], is_error: bool) -> None:
        """The front end logged the decision; this logs what actually happened."""
        record = {
            "at": utc_timestamp(), "source": "agent", "tool": name, "args": args,
            "result": "error" if is_error else "ok",
            "detail": str(result.get("error") or result.get("note") or "")[:200],
            "dry_run": self.tools.dry_run,
        }
        path = self.cfg.output.audit_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except OSError as exc:  # an unwritable log must not stop the lab
            console.print(f"[yellow]Could not write the audit log: {exc}[/yellow]")

    def _run_tool(self, block: Any) -> dict[str, Any]:
        result, is_error = self._execute(block.name, dict(block.input or {}))
        return {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": json.dumps(result, default=str),
            "is_error": is_error,
        }

    # -------------------------------------------------------------- LLM loop

    def _create(self) -> Any:
        llm = self.cfg.llm
        kwargs: dict[str, Any] = dict(
            model=llm.model,
            max_tokens=llm.max_tokens,
            system=self.system,
            tools=self.tool_specs,
            messages=self.messages,
            output_config={"effort": llm.effort},
        )
        if llm.use_fallbacks:
            kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        return self.client.beta.messages.create(**kwargs)

    def ask(self, prompt: str) -> str:
        context = {
            "now_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "default_unit": self.default_unit,
            "mode": self.mode,
            "experiment_plan": self.tools.active_plan_context(self.default_unit),
        }
        content = f"<context>{json.dumps(context)}</context>\n\n{prompt}"
        return self._ask_claude(content) if self.provider == "anthropic" else self._ask_openai(content)

    def _ask_claude(self, content: str) -> str:
        self.messages.append({"role": "user", "content": content})
        for _ in range(self.cfg.llm.max_turns):
            response = self._create()
            # Append the full content (thinking and fallback blocks included) so the
            # conversation stays valid for the next request.
            self.messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "refusal":
                category = getattr(response.stop_details, "category", None) if response.stop_details else None
                return f"The model declined this request (category: {category}). Nothing was changed."

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason == "tool_use" and tool_uses:
                results = [self._run_tool(b) for b in tool_uses]
                self.messages.append({"role": "user", "content": results})
                continue

            text = "\n".join(b.text for b in response.content if b.type == "text").strip()
            if response.stop_reason == "max_tokens":
                text += "\n\n[truncated: hit max_tokens]"
            return text or "(no response)"
        return f"Stopped after {self.cfg.llm.max_turns} tool rounds without a final answer."

    def _ask_openai(self, content: str) -> str:
        if not self.messages:
            self.messages.append({"role": "system", "content": self.system})
        self.messages.append({"role": "user", "content": content})
        tools = to_openai_tools(self.tool_specs, self.provider)
        for _ in range(self.cfg.llm.max_turns):
            choice = self.chat.create(self.messages, tools)["choices"][0]
            message = choice.get("message") or {}
            calls = message.get("tool_calls") or []
            entry: dict[str, Any] = {"role": "assistant", "content": message.get("content") or ""}
            if calls:
                # Echo the calls verbatim: Gemini 3 attaches thought signatures to them that must come back.
                entry["tool_calls"] = calls
            self.messages.append(entry)

            if calls:
                for call in calls:
                    function = call.get("function") or {}
                    name, raw = function.get("name", ""), function.get("arguments") or "{}"
                    try:
                        args = raw if isinstance(raw, dict) else json.loads(raw)
                        if not isinstance(args, dict):
                            raise ValueError("arguments must be a JSON object")
                    except (json.JSONDecodeError, ValueError) as exc:
                        result: dict[str, Any] = {"error": f"Tool arguments were not valid JSON ({exc}); call {name} again with a JSON object."}
                    else:
                        result, _ = self._execute(name, args)
                    self.messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "name": name,
                                          "content": json.dumps(result, default=str)})
                continue

            text = (message.get("content") or "").strip()
            if choice.get("finish_reason") == "length":
                text += "\n\n[truncated: hit max_tokens]"
            return text or "(no response)"
        return f"Stopped after {self.cfg.llm.max_turns} tool rounds without a final answer."


def triage_alert(alert: dict[str, Any], cfg: Settings = default_settings) -> str:
    """Read-only investigation of an alert; called by listener.py."""
    agent = LarreAgent(mode="query", cfg=cfg, default_unit=alert.get("unit"), verbose=False)
    plan = agent.tools.active_plan_context(alert.get("unit"))
    return agent.ask(TRIAGE_PROMPT.format(alert=json.dumps(alert, indent=2), plan=json.dumps(plan, indent=2)))


# ------------------------------------------------------------------ CLI

def _interactive_confirm(name: str, args: dict[str, Any]) -> bool:
    pretty = ", ".join(f"{k}={v}" for k, v in args.items() if v is not None)
    console.print(Panel(f"[bold]{name}[/bold]({pretty})", title="⚠ hardware action", border_style="yellow"))
    return click.confirm("Execute on the bioreactor?", default=False)


def _print_answer(text: str) -> None:
    console.print(Panel(Markdown(text), title="LARR-E", border_style="cyan"))


def _handle_api_errors(fn: Callable[[], None]) -> None:
    try:
        fn()
    except anthropic.AuthenticationError:
        console.print("[red]Anthropic authentication failed. Set ANTHROPIC_API_KEY.[/red]")
        sys.exit(2)
    except anthropic.RateLimitError:
        console.print("[red]Rate limited by the Anthropic API; try again shortly.[/red]")
        sys.exit(3)
    except anthropic.APIConnectionError:
        console.print("[red]Cannot reach the Anthropic API. Check the network.[/red]")
        sys.exit(3)
    except anthropic.APIStatusError as exc:
        console.print(f"[red]Anthropic API error {exc.status_code}: {exc.message}[/red]")
        sys.exit(3)
    except LLMError as exc:
        console.print(f"[red]{exc}[/red]")
        sys.exit(3)


@click.group()
def cli() -> None:
    """LARR-E: talk to your Pioreactor cluster in plain English."""


@cli.command("exec")
@click.argument("prompt")
@click.option("--unit", "-u", help="Default Pioreactor unit for this command.")
@click.option("--yes", "-y", is_flag=True, help="Skip the hardware confirmation prompt.")
@click.option("--dry-run", is_flag=True, help="Show the MQTT messages without sending them.")
def exec_cmd(prompt: str, unit: str | None, yes: bool, dry_run: bool) -> None:
    """Run a command that may change hardware, e.g. "pump 1ml of media"."""
    confirm = (lambda n, a: True) if yes else _interactive_confirm
    agent = LarreAgent("exec", dry_run=dry_run or None, confirm=confirm, default_unit=unit)
    _handle_api_errors(lambda: _print_answer(agent.ask(prompt)))


@cli.command("query")
@click.argument("prompt")
@click.option("--unit", "-u", help="Default Pioreactor unit for this query.")
def query_cmd(prompt: str, unit: str | None) -> None:
    """Ask a read-only question, e.g. "plot the last 10 minutes of OD against temperature"."""
    agent = LarreAgent("query", default_unit=unit)
    _handle_api_errors(lambda: _print_answer(agent.ask(prompt)))


@cli.command("chat")
@click.option("--unit", "-u", help="Default Pioreactor unit.")
@click.option("--read-only", is_flag=True, help="Disable hardware tools.")
@click.option("--dry-run", is_flag=True, help="Show the MQTT messages without sending them.")
def chat_cmd(unit: str | None, read_only: bool, dry_run: bool) -> None:
    """Interactive session that keeps conversation context."""
    agent = LarreAgent(
        "query" if read_only else "exec",
        dry_run=dry_run or None,
        confirm=_interactive_confirm,
        default_unit=unit,
    )
    console.print("[cyan]LARR-E chat. Ctrl-D to exit.[/cyan]")
    while True:
        try:
            prompt = console.input("[bold green]you ▸ [/bold green]").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return
        if prompt:
            _handle_api_errors(lambda: _print_answer(agent.ask(prompt)))


@cli.command("status")
def status_cmd() -> None:
    """Live telemetry table from the listener (no LLM call)."""
    state = PioreactorTools().get_live_status()
    if not state.pop("available"):
        console.print(f"[yellow]{state['note']}[/yellow]")
        return
    table = Table(title="Pioreactor live status")
    for col in ("unit", "experiment", "OD", "temp °C", "growth h⁻¹", "RPM", "last seen"):
        table.add_column(col)
    for unit, s in sorted(state.items()):
        def show(entry: dict, fmt: str) -> str:
            text = format(entry["value"], fmt)
            if entry.get("stale"):
                age = entry.get("age_s")
                text = f"[dim]{text} ({age // 60} min old)[/dim]" if age is not None else f"[dim]{text} (stale)[/dim]"
            return text

        od = ", ".join(f"{k}={show(v, '.3f')}" for k, v in sorted(s.get("od", {}).items()))
        get = lambda key, fmt: show(s[key], fmt) if key in s else "—"  # noqa: E731
        table.add_row(
            unit, s.get("experiment", "—"), od or "—", get("temperature", ".2f"),
            get("growth_rate", ".3f"), get("stirring", ".0f"), s.get("last_seen", "—"),
        )
    console.print(table)


@cli.command("alerts")
@click.option("-n", "limit", default=10, show_default=True)
def alerts_cmd(limit: int) -> None:
    """Recent anomaly alerts."""
    for alert in PioreactorTools().list_recent_alerts(limit)["alerts"]:
        color = "red" if alert["severity"] == "critical" else "yellow"
        console.print(f"[{color}]{alert['detected_at']} {alert['unit']} {alert['metric']}: {alert['message']}[/{color}]")


@cli.command("listen")
@click.option("--no-triage", is_flag=True, help="Do not run the LLM triage on alerts.")
def listen_cmd(no_triage: bool) -> None:
    """Start the MQTT anomaly listener in the foreground."""
    import listener

    listener.run(auto_triage=False if no_triage else None)


@cli.command("api")
@click.option("--host", help="Bind address (default from LARRE_API_HOST, 0.0.0.0).")
@click.option("--port", type=int, help="Port (default from LARRE_API_PORT, 8787).")
def api_cmd(host: str | None, port: int | None) -> None:
    """Run the HTTP API and the 3DS browser dashboard."""
    import api_server

    api_server.run(host=host, port=port)


@cli.command("twin", context_settings={"ignore_unknown_options": True, "allow_extra_args": True})
@click.pass_context
def twin_cmd(ctx: click.Context) -> None:
    """Run the digital-twin cluster (virtual Pioreactors). Options: see `python twin.py --help`."""
    import twin

    sys.argv = ["twin.py", *ctx.args]
    twin.main()


@cli.command("slack")
def slack_cmd() -> None:
    """Run the Slack bot front end (Socket Mode)."""
    import slack_bot

    slack_bot.run()


if __name__ == "__main__":
    cli()
