"""LARR-E CLI agent: natural language -> Pioreactor actions and data queries.

    larr-e exec  "pump 1ml of media into worker1"
    larr-e query "plot the last 10 minutes of OD against temperature"
    larr-e chat
    larr-e listen | status | alerts | slack | api
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
from tools import METRIC_TABLES, PUMP_JOBS, STARTABLE_JOBS, STOPPABLE_JOBS, PioreactorTools, ToolError

console = Console()

SYSTEM_PROMPT = """You are LARR-E, an operations agent for a Pioreactor bioreactor cluster.
Lab members talk to you from a terminal or chat. You act on the hardware and read data only
through the tools you are given.

How the cluster works:
- Each Pioreactor is a "unit" (e.g. worker1). Telemetry and commands flow over MQTT on the leader.
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

Rules:
- Prefer the purpose-built tools (plot_timeseries, get_recent_timeseries, get_live_status) over raw SQL.
- Make one hardware change per request unless the user clearly asked for several.
- The tools enforce safety limits. If a tool rejects a value, tell the user the limit; never try to
  get around it, e.g. by splitting one large dose into several small ones.
- If a request is ambiguous in a way that matters for hardware (which unit, how much), ask instead of guessing.
- If the user declines a confirmation, stop and say nothing was changed.
- Reply in a few short lines suitable for a terminal or Slack message: what you did or found, with numbers.
"""

QUERY_MODE_NOTE = (
    "This session is read-only: you cannot change hardware. If the user asks for an action, "
    "explain what you would do and tell them to rerun it with `larr-e exec`."
)

TRIAGE_PROMPT = """An automated anomaly alert just fired:

{alert}

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
]

ACTION_TOOLS = [
    _tool(
        "dose",
        "Run a peristaltic pump once. Give exactly one of ml or duration_s.",
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
            "options": {"type": "object"},
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
    """Manual tool-use loop so every hardware call passes through our confirmation gate."""

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
        self.client = anthropic.Anthropic()
        self.messages: list[dict[str, Any]] = []
        self.tool_specs = READ_TOOLS + (ACTION_TOOLS if mode == "exec" else [])
        self.system = SYSTEM_PROMPT + ("" if mode == "exec" else "\n" + QUERY_MODE_NOTE)
        if extra_system:
            self.system += "\n" + extra_system

    # -------------------------------------------------------------- tool dispatch

    def _dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        t = self.tools
        if "unit" in {p for spec in self.tool_specs if spec["name"] == name for p in spec["input_schema"]["properties"]}:
            args.setdefault("unit", self.default_unit)
        handlers: dict[str, Callable[..., dict[str, Any]]] = {
            "get_live_status": t.get_live_status,
            "get_recent_timeseries": t.get_recent_timeseries,
            "plot_timeseries": t.plot_timeseries,
            "query_database": t.query_database,
            "list_recent_alerts": t.list_recent_alerts,
            "dose": t.dose,
            "set_target_temperature": t.set_target_temperature,
            "set_stirring_rpm": t.set_stirring_rpm,
            "start_job": t.start_job,
            "stop_job": t.stop_job,
        }
        if name not in handlers or name not in {s["name"] for s in self.tool_specs}:
            raise ToolError(f"Tool {name} is not available in {self.mode} mode.")
        if name in ACTION_TOOL_NAMES and not self.confirm(name, args):
            return {"ok": False, "declined": True, "note": "The operator declined this action. Nothing was changed."}
        return handlers[name](**args)

    def _run_tool(self, block: Any) -> dict[str, Any]:
        args = dict(block.input or {})
        if self.verbose:
            console.print(f"[dim]→ {block.name}({json.dumps(args)})[/dim]")
        try:
            result = self._dispatch(block.name, args)
            is_error = False
        except (ToolError, TypeError, ValueError) as exc:
            result = {"error": str(exc)}
            is_error = True
        if self.verbose:
            style = "red" if is_error else "green"
            console.print(f"[{style}]  ← {json.dumps(result, default=str)[:300]}[/{style}]")
        if self.on_tool_result:
            self.on_tool_result(block.name, args, result, is_error)
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
        }
        self.messages.append(
            {"role": "user", "content": f"<context>{json.dumps(context)}</context>\n\n{prompt}"}
        )
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


def triage_alert(alert: dict[str, Any], cfg: Settings = default_settings) -> str:
    """Read-only investigation of an alert; called by listener.py."""
    agent = LarreAgent(mode="query", cfg=cfg, default_unit=alert.get("unit"), verbose=False)
    return agent.ask(TRIAGE_PROMPT.format(alert=json.dumps(alert, indent=2)))


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
        od = ", ".join(f"{k}={v['value']:.3f}" for k, v in sorted(s.get("od", {}).items()))
        get = lambda key, fmt: format(s[key]["value"], fmt) if key in s else "—"  # noqa: E731
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


@cli.command("slack")
def slack_cmd() -> None:
    """Run the Slack bot front end (Socket Mode)."""
    import slack_bot

    slack_bot.run()


if __name__ == "__main__":
    cli()
