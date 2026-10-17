"""LARR-E Slack front end.

Connects over Socket Mode (outbound WebSocket), so it needs no public URL. That
suits hackathon Wi-Fi and a leader Pi behind NAT.

What it does:
* @mention the bot, DM it, or use /larr-e <request>: the agent answers in a thread.
  Replies in that thread continue the same conversation, with no further @mention needed.
* Plots the agent draws are uploaded into the thread.
* Every hardware action posts an Approve / Deny card and waits for a human click.
  Approvers can be restricted, and the two-person rule enforced, in config.py.
* Alerts from listener.py (MQTT larre/alerts/+) go to the alert channel with a
  "Plot" button. The listener's triage note (larre/triage/+) is threaded under its
  alert, and replies in that thread go to an agent that already knows the alert.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import anthropic
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.errors import SlackApiError

from agent import LarreAgent
from config import Settings, settings as default_settings
from listener import build_client
from tools import PioreactorTools, ToolError, utc_timestamp

log = logging.getLogger("larre.slack")

SLACK_STYLE = """You are replying inside a Slack thread. Use Slack mrkdwn: *bold*, _italic_, `code`,
and "•" bullets. No Markdown headings or tables. Charts you plot are uploaded to the thread
automatically, so point to the chart instead of listing its points."""

ACTION_TITLES = {
    "dose": "Run a pump",
    "set_target_temperature": "Change temperature setpoint",
    "set_stirring_rpm": "Change stirring speed",
    "start_job": "Start a job",
    "stop_job": "Stop a job",
    "start_experiment": "Start an experiment",
    "stop_experiment": "Stop an experiment early",
}

HELP_TEXT = """*LARR-E*: talk to your Pioreactor cluster.
• `/larr-e status`: live telemetry (no AI)
• `/larr-e alerts`: recent anomaly alerts
• `/larr-e plot the last 10 minutes of OD against temperature`
• `/larr-e pump 1ml of media into worker1`: asks for approval before touching hardware
• `/larr-e run the vial at 37C and 180 rpm for 18 hours, update me every 2 hours`
• `/larr-e experiment`: the running experiment plan (no AI)
Or @mention me in a channel, or DM me. Reply in my thread to keep the conversation going."""


def to_mrkdwn(text: str) -> str:
    """Patch the Markdown habits that render badly in Slack."""
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"__(.+?)__", r"_\1_", text)
    text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"<\2|\1>", text)
    return text


def format_args(args: dict[str, Any]) -> str:
    return ", ".join(f"{k}=`{v}`" for k, v in args.items() if v is not None) or "(none)"


def format_status(state: dict[str, Any]) -> str:
    if not state.pop("available", False):
        return f":warning: {state.get('note', 'No live telemetry.')}"
    lines = [f"{'unit':<10} {'OD':<16} {'temp °C':>8} {'growth':>8} {'RPM':>6}  last seen"]
    for unit, s in sorted(state.items()):
        od = ",".join(f"{k}={v['value']:.3f}" for k, v in sorted(s.get("od", {}).items())) or "—"
        get = lambda key, fmt: format(s[key]["value"], fmt) if key in s else "—"  # noqa: E731
        lines.append(
            f"{unit:<10} {od:<16} {get('temperature', '.2f'):>8} {get('growth_rate', '.3f'):>8} "
            f"{get('stirring', '.0f'):>6}  {s.get('last_seen', '—')[11:19]}"
        )
    return "*Live status*\n```" + "\n".join(lines) + "```"


@dataclass
class PendingApproval:
    id: str
    tool: str
    args: dict[str, Any]
    requester: str
    channel: str
    thread_ts: str | None
    ts: str = ""
    approved: bool = False
    decided_by: str | None = None
    event: threading.Event = field(default_factory=threading.Event)


@dataclass
class Conversation:
    channel: str
    thread_ts: str | None  # None means top-level replies (DMs)
    agent: LarreAgent | None = None
    requester: str = ""
    preamble: str = ""  # alert context, prepended to the next question
    status_ts: str | None = None
    tool_log: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


class LarreBot:
    def __init__(self, cfg: Settings = default_settings, app: App | None = None, bot_user_id: str | None = None):
        self.cfg = cfg
        self.app = app or App(token=cfg.slack.bot_token)
        self.client = self.app.client
        self.bot_user_id = bot_user_id or self.client.auth_test()["user_id"]
        self.tools = PioreactorTools(cfg, render_terminal=False)
        self.pool = ThreadPoolExecutor(max_workers=cfg.slack.workers, thread_name_prefix="larre-slack")
        self.lock = threading.Lock()
        self.conversations: OrderedDict[tuple[str, str], Conversation] = OrderedDict()
        self.approvals: dict[str, PendingApproval] = {}
        self.alerts: dict[str, dict[str, Any]] = {}
        self.alert_threads: dict[str, tuple[str, str]] = {}
        self._register_handlers()

    # ------------------------------------------------------------ conversations

    def conversation(self, channel: str, thread_ts: str | None) -> Conversation:
        key = (channel, thread_ts or "")
        with self.lock:
            conv = self.conversations.get(key)
            if conv:
                self.conversations.move_to_end(key)
                return conv
            conv = Conversation(channel, thread_ts)
            conv.agent = LarreAgent(
                "exec",
                cfg=self.cfg,
                confirm=lambda name, args, c=conv: self.request_approval(c, name, args),
                verbose=False,
                extra_system=SLACK_STYLE,
                on_tool_result=lambda *a, c=conv: self.on_tool_result(c, *a),
                render_terminal=False,
            )
            self.conversations[key] = conv
            while len(self.conversations) > self.cfg.slack.max_conversations:
                self.conversations.popitem(last=False)
            return conv

    def has_conversation(self, channel: str, thread_ts: str | None) -> bool:
        with self.lock:
            return (channel, thread_ts or "") in self.conversations

    def submit(self, fn: Callable[..., Any], *args: Any) -> None:
        def guarded() -> None:
            try:
                fn(*args)
            except Exception:
                log.exception("Background task %s failed", getattr(fn, "__name__", fn))

        self.pool.submit(guarded)

    def handle_prompt(self, channel: str, thread_ts: str | None, user: str, text: str) -> None:
        self.submit(self._answer, channel, thread_ts, user, text)

    def _answer(self, channel: str, thread_ts: str | None, user: str, text: str) -> None:
        conv = self.conversation(channel, thread_ts)
        with conv.lock:  # one question at a time per thread
            conv.requester = user
            conv.agent.requester = user
            conv.tool_log = []
            conv.status_ts = self.client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=":brain: working on it…"
            )["ts"]
            prompt = f"(Slack user <@{user}> asks)\n{text}"
            if conv.preamble:
                prompt, conv.preamble = conv.preamble + "\n\n" + prompt, ""
            try:
                answer = to_mrkdwn(conv.agent.ask(prompt))
            except anthropic.APIStatusError as exc:
                answer = f":warning: Claude API error {exc.status_code}: {exc.message}"
            except anthropic.APIConnectionError:
                answer = ":warning: Cannot reach the Claude API right now."
            footer = f"\n_tools: {' '.join(conv.tool_log)}_" if conv.tool_log else ""
            self.client.chat_update(channel=channel, ts=conv.status_ts, text=answer + footer)

    def on_tool_result(
        self, conv: Conversation, name: str, args: dict[str, Any], result: dict[str, Any], is_error: bool
    ) -> None:
        conv.tool_log.append(f"{'✗' if is_error else '✓'}`{name}`")
        if conv.status_ts:
            self.client.chat_update(
                channel=conv.channel,
                ts=conv.status_ts,
                text=":brain: working on it… " + " ".join(conv.tool_log),
            )
        if name == "plot_timeseries" and not is_error:
            self.upload_plot(conv.channel, conv.thread_ts, result)
        if name == "start_experiment" and not is_error and result.get("ok"):
            # Progress updates for this plan go to the thread that started it.
            plan = self.tools.plans.get(result["plan"]["unit"])
            if plan:
                plan.slack_channel, plan.slack_thread_ts = conv.channel, conv.thread_ts
                self.tools.plans.put(plan)

    def upload_plot(self, channel: str, thread_ts: str | None, result: dict[str, Any], note: str = "") -> None:
        caption = f"`{result['unit']}`{note}"
        if "pearson_r" in result:
            caption += f" · Pearson r = {result['pearson_r']}"
        try:
            self.client.files_upload_v2(
                channel=channel,
                thread_ts=thread_ts,
                file=result["png_path"],
                title=f"LARR-E plot · {result['unit']}",
                initial_comment=caption,
            )
        except SlackApiError as exc:
            log.warning("Plot upload failed: %s", exc.response.get("error"))

    # ------------------------------------------------------------ approvals

    def request_approval(self, conv: Conversation, tool: str, args: dict[str, Any]) -> bool:
        """Called from the agent's worker thread; blocks until someone clicks or it times out."""
        pending = PendingApproval(uuid.uuid4().hex[:12], tool, args, conv.requester, conv.channel, conv.thread_ts)
        with self.lock:
            self.approvals[pending.id] = pending
        dry = " _(dry run: nothing will move)_" if self.tools.dry_run else ""
        who = (
            "any approver"
            if self.cfg.slack.allow_self_approval
            else "a second person (two-person rule)"
        )
        summary = f":warning: *{ACTION_TITLES.get(tool, tool)}*{dry}\n`{tool}` {format_args(args)}"
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": summary}},
            *self._plan_preview_blocks(tool, args),
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"Requested by <@{conv.requester}> · needs {who}"}],
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "larre_approve",
                        "style": "primary",
                        "text": {"type": "plain_text", "text": "Approve"},
                        "value": pending.id,
                    },
                    {
                        "type": "button",
                        "action_id": "larre_deny",
                        "style": "danger",
                        "text": {"type": "plain_text", "text": "Deny"},
                        "value": pending.id,
                    },
                ],
            },
        ]
        pending.ts = self.client.chat_postMessage(
            channel=conv.channel, thread_ts=conv.thread_ts, text=f"Approval needed: {tool}", blocks=blocks
        )["ts"]

        decided = pending.event.wait(self.cfg.slack.approval_timeout_s)
        with self.lock:
            self.approvals.pop(pending.id, None)
        if not decided:
            self.client.chat_update(
                channel=conv.channel,
                ts=pending.ts,
                text=f":hourglass: *Expired*: `{tool}` {format_args(args)} (no decision in "
                f"{self.cfg.slack.approval_timeout_s:g}s; nothing was changed)",
                blocks=[],
            )
        self.audit(pending, "approved" if pending.approved else ("denied" if decided else "expired"))
        return pending.approved

    def _plan_preview_blocks(self, tool: str, args: dict[str, Any]) -> list[dict[str, Any]]:
        """For start_experiment, show the plan and the exact profile the approver is agreeing to."""
        if tool != "start_experiment":
            return []
        try:
            preview = self.tools.plan_experiment(**args)
        except (ToolError, TypeError) as exc:
            return [{"type": "section", "text": {"type": "mrkdwn", "text": f":x: Plan is invalid: {exc}"}}]
        plan = preview["plan"]
        text = f"*{plan['summary']}*\nPioreactor profile (it runs the schedule and stops the jobs at the end):\n```{preview['profile_yaml']}```"
        if preview["conflict"]:
            text += f"\n:warning: Already running: {preview['conflict']}"
        return [{"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}}]

    def decide(self, approval_id: str, user: str, approve: bool) -> str | None:
        """Apply a button click. Returns an error to show the clicker privately, or None on success."""
        with self.lock:
            pending = self.approvals.get(approval_id)
            if pending is None or pending.event.is_set():
                return "This request was already handled or has expired."
            approvers = self.cfg.slack.approvers
            if approvers and user not in approvers:
                return "You are not on the approver list for hardware actions."
            if approve and user == pending.requester and not self.cfg.slack.allow_self_approval:
                return "Two-person rule: someone other than the requester must approve this."
            pending.approved, pending.decided_by = approve, user
            pending.event.set()
        verdict = ":white_check_mark: *Approved*" if approve else ":no_entry: *Denied*"
        self.client.chat_update(
            channel=pending.channel,
            ts=pending.ts,
            text=f"{verdict} by <@{user}>: `{pending.tool}` {format_args(pending.args)}"
            f" (requested by <@{pending.requester}>)",
            blocks=[],
        )
        return None

    def audit(self, pending: PendingApproval, decision: str) -> None:
        record = {
            "at": utc_timestamp(),
            "source": "slack",
            "tool": pending.tool,
            "args": pending.args,
            "requester": pending.requester,
            "decision": decision,
            "decided_by": pending.decided_by,
            "channel": pending.channel,
            "dry_run": self.tools.dry_run,
        }
        with self.cfg.output.audit_path.open("a") as f:
            f.write(json.dumps(record) + "\n")

    # ------------------------------------------------------------ alerts

    def post_alert(self, alert: dict[str, Any]) -> None:
        channel = self.cfg.slack.alert_channel
        if not channel:
            log.warning("Alert received but LARRE_SLACK_ALERT_CHANNEL is not set")
            return
        emoji = ":rotating_light:" if alert["severity"] == "critical" else ":warning:"
        text = f"{emoji} *{alert['severity'].upper()}* · `{alert['unit']}` · {alert['metric']}\n{alert['message']}"
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": text}},
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"experiment `{alert['experiment']}` · value {alert['value']:.3f} · "
                        f"{alert['reading_timestamp']} · reply in thread to act",
                    }
                ],
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "larre_plot_alert",
                        "text": {"type": "plain_text", "text": "📈 Plot last 15 min"},
                        "value": alert["id"],
                    }
                ],
            },
        ]
        ts = self.client.chat_postMessage(channel=channel, text=text, blocks=blocks)["ts"]
        with self.lock:
            self.alerts[alert["id"]] = alert
            self.alert_threads[alert["id"]] = (channel, ts)
        conv = self.conversation(channel, ts)
        conv.agent.default_unit = alert["unit"]
        conv.preamble = f"This thread is about an anomaly alert:\n{json.dumps(alert, indent=2)}"

    def post_triage(self, payload: dict[str, Any]) -> None:
        with self.lock:
            target = self.alert_threads.get(payload.get("alert_id", ""))
        note = f":brain: *Triage*\n{to_mrkdwn(payload['note'])}"
        if target is None:
            if self.cfg.slack.alert_channel:
                self.client.chat_postMessage(
                    channel=self.cfg.slack.alert_channel, text=f"`{payload['unit']}` {note}"
                )
            return
        channel, ts = target
        self.client.chat_postMessage(channel=channel, thread_ts=ts, text=note)
        conv = self.conversation(channel, ts)
        conv.preamble += f"\n\nAutomated triage of the alert:\n{payload['note']}"

    def plot_alert(self, alert_id: str, user: str) -> None:
        with self.lock:
            alert, target = self.alerts.get(alert_id), self.alert_threads.get(alert_id)
        if not alert or not target:
            return
        channel, ts = target
        metric = "od" if alert["metric"].startswith("od") else alert["metric"]
        metrics = [metric, "od" if metric == "temperature" else "temperature"]
        try:
            result = self.tools.plot_timeseries(metrics, alert["unit"], minutes=15)
        except ToolError as exc:
            self.client.chat_postMessage(channel=channel, thread_ts=ts, text=f":warning: Cannot plot: {exc}")
            return
        self.upload_plot(channel, ts, result, note=f" · requested by <@{user}>")

    # ------------------------------------------------------------ experiment progress

    def _update_target(self, plan: Any) -> tuple[str, str | None] | None:
        if plan.slack_channel:
            return plan.slack_channel, plan.slack_thread_ts
        channel = self.cfg.plans.update_channel or self.cfg.slack.alert_channel
        return (channel, None) if channel else None

    def progress_text(self, plan: Any, final: bool = False) -> str:
        elapsed = min(plan.elapsed_h() or 0.0, plan.duration_h)
        if final:
            head = f":checkered_flag: *{plan.name}* finished after {plan.duration_h:g} h. Pioreactor stopped its jobs on schedule."
        else:
            head = f":test_tube: *{plan.name}*: {elapsed:.1f} h of {plan.duration_h:g} h ({plan.duration_h - elapsed:.1f} h left)"
        live = self.tools.get_live_status(plan.unit) if plan.unit in self.tools._live_state() else {}
        s = live.get(plan.unit, {})
        bits = []
        if "temperature" in s:
            tgt = f" (target {plan.temperature_c:g})" if plan.temperature_c is not None else ""
            bits.append(f"temp {s['temperature']['value']:.1f} °C{tgt}")
        if "stirring" in s:
            tgt = f" (target {plan.rpm:g})" if plan.rpm is not None else ""
            bits.append(f"stirring {s['stirring']['value']:.0f} rpm{tgt}")
        if s.get("od"):
            bits.append("OD " + ", ".join(f"{v['value']:.3f}" for _, v in sorted(s["od"].items())))
        if "growth_rate" in s:
            bits.append(f"growth {s['growth_rate']['value']:.3f} h⁻¹")
        return head + ("\n" + " · ".join(bits) if bits else "\n_(no live telemetry: is `larr-e listen` running?)_")

    def post_progress(self, plan: Any, final: bool = False) -> None:
        target = self._update_target(plan)
        if not target:
            log.warning("Progress update for %s due, but no Slack channel is known", plan.unit)
            return
        channel, thread_ts = target
        self.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=self.progress_text(plan, final))
        minutes = max(1.0, min(plan.elapsed_h() or 0.0, plan.duration_h) * 60)
        for metrics in (["od", "growth_rate"], ["od", "temperature"]):
            try:
                result = self.tools.plot_timeseries(metrics, plan.unit, minutes=minutes)
            except ToolError:
                continue  # e.g. no growth rate yet early on; fall back to OD vs temperature
            self.upload_plot(channel, thread_ts, result, note=f" · {plan.name}")
            break

    def check_plans(self) -> None:
        """One pass: post due updates and the end-of-experiment message. Runs no LLM."""
        for plan in self.tools.plans.all():
            if plan.status == "finished" and not plan.final_posted:
                self.post_progress(plan, final=True)
                plan.final_posted = True
                self.tools.plans.put(plan)
            elif plan.update_due():
                self.post_progress(plan)
                plan.last_update_at = utc_timestamp()
                self.tools.plans.put(plan)

    def start_progress_loop(self) -> None:
        def loop() -> None:
            while True:
                try:
                    self.check_plans()
                except Exception:
                    log.exception("Progress check failed")
                time.sleep(self.cfg.plans.check_every_s)

        threading.Thread(target=loop, name="larre-progress", daemon=True).start()

    def start_mqtt(self) -> None:
        m = self.cfg.mqtt
        client = build_client(self.cfg, role="slackbot")
        alert_topic, triage_topic = f"{m.alert_topic_prefix}/+", f"{m.triage_topic_prefix}/+"

        def on_connect(client, userdata, flags, reason_code, properties):
            if not reason_code.is_failure:
                client.subscribe([(alert_topic, 1), (triage_topic, 1)])
                log.info("Subscribed to %s and %s", alert_topic, triage_topic)

        def on_message(client, userdata, msg):
            try:
                payload = json.loads(msg.payload)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return
            if msg.topic.startswith(m.alert_topic_prefix + "/"):
                self.submit(self.post_alert, payload)
            else:
                self.submit(self.post_triage, payload)

        client.on_connect, client.on_message = on_connect, on_message
        client.connect_async(m.host, m.port, m.keepalive)
        client.loop_start()

    # ------------------------------------------------------------ Slack handlers

    def _strip_mention(self, text: str) -> str:
        return text.replace(f"<@{self.bot_user_id}>", "").strip()

    def _register_handlers(self) -> None:
        app = self.app

        @app.event("app_mention")
        def on_mention(event: dict[str, Any]) -> None:
            text = self._strip_mention(event.get("text", ""))
            thread_ts = event.get("thread_ts") or event["ts"]
            if not text:
                self.client.chat_postMessage(channel=event["channel"], thread_ts=thread_ts, text=HELP_TEXT)
                return
            self.handle_prompt(event["channel"], thread_ts, event["user"], text)

        @app.event("message")
        def on_message(event: dict[str, Any]) -> None:
            if event.get("subtype") or event.get("bot_id") or "user" not in event:
                return  # edits, joins, and bot messages (our own included)
            text = event.get("text", "")
            if f"<@{self.bot_user_id}>" in text:
                return  # app_mention handles it
            channel = event["channel"]
            if event.get("channel_type") == "im":
                self.handle_prompt(channel, event.get("thread_ts"), event["user"], text)
            elif event.get("thread_ts") and self.has_conversation(channel, event["thread_ts"]):
                self.handle_prompt(channel, event["thread_ts"], event["user"], text)

        @app.command("/larr-e")
        def on_command(ack: Callable, command: dict[str, Any], respond: Callable) -> None:
            text = command.get("text", "").strip()
            if text.lower() in {"", "help"}:
                ack(HELP_TEXT)
                return
            if text.lower() == "status":
                ack()
                respond(text=format_status(self.tools.get_live_status()), response_type="in_channel")
                return
            if text.lower() in {"experiment", "plan"}:
                ack()
                plans = [p for p in self.tools.plans.all() if p.status == "running"]
                msg = "\n\n".join(self.progress_text(p) for p in plans) or "No experiment is running."
                respond(text=msg, response_type="in_channel")
                return
            if text.lower() == "alerts":
                ack()
                alerts = self.tools.list_recent_alerts(8)["alerts"]
                lines = [f"• `{a['unit']}` {a['metric']}: {a['message']}" for a in alerts] or ["No alerts yet."]
                respond(text="*Recent alerts*\n" + "\n".join(lines), response_type="in_channel")
                return
            ack()
            try:
                parent = self.client.chat_postMessage(
                    channel=command["channel_id"], text=f"<@{command['user_id']}> asked LARR-E: _{text}_"
                )
            except SlackApiError as exc:
                respond(
                    text=f"I can't post here ({exc.response.get('error')}). Invite me with `/invite @LARR-E`.",
                    response_type="ephemeral",
                )
                return
            self.handle_prompt(parent["channel"], parent["ts"], command["user_id"], text)

        def on_decision(approve: bool) -> Callable[..., None]:
            def handler(ack: Callable, body: dict[str, Any]) -> None:
                ack()
                user = body["user"]["id"]
                error = self.decide(body["actions"][0]["value"], user, approve)
                if error:
                    self.client.chat_postEphemeral(channel=body["channel"]["id"], user=user, text=error)

            return handler

        app.action("larre_approve")(on_decision(True))
        app.action("larre_deny")(on_decision(False))

        @app.action("larre_plot_alert")
        def on_plot(ack: Callable, body: dict[str, Any]) -> None:
            ack()
            self.submit(self.plot_alert, body["actions"][0]["value"], body["user"]["id"])


def run(cfg: Settings = default_settings) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not (cfg.slack.bot_token and cfg.slack.app_token):
        sys.exit("Set SLACK_BOT_TOKEN (xoxb-…) and SLACK_APP_TOKEN (xapp-…). See README section 4.")
    cfg.ensure_dirs()
    bot = LarreBot(cfg)
    bot.start_mqtt()
    bot.start_progress_loop()
    log.info("LARR-E bot online as %s (dry_run=%s)", bot.bot_user_id, bot.tools.dry_run)
    SocketModeHandler(bot.app, cfg.slack.app_token).start()


if __name__ == "__main__":
    run()
