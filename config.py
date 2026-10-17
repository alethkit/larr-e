"""LARR-E configuration.

Every value can be overridden with an environment variable so the same code runs
on a laptop (rehearsal against a local Mosquitto) and on the Pioreactor leader
(live demo against real hardware) without edits.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_env_file(path: Path) -> None:
    """Load KEY=VALUE lines from a .env file into os.environ, never overriding variables already set.

    Secrets (Anthropic, Slack, MQTT) live in this gitignored file so they never need to be in the
    shell, a settings UI, or a prompt. Supports comments, blank lines, `export KEY=...` and quotes.
    """
    try:
        lines = path.read_text().splitlines()
    except (FileNotFoundError, PermissionError):
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()  # trailing comment on an unquoted value
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file(Path(os.environ.get("LARRE_ENV_FILE", Path(__file__).resolve().parent / ".env")))


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _env_float(name: str, default: float) -> float:
    return float(_env(name, str(default)))


def _env_int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, "1" if default else "0").lower() in {"1", "true", "yes", "on"}


BASE_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = Path(_env("LARRE_RUNTIME_DIR", str(BASE_DIR / "runtime")))


@dataclass(frozen=True)
class MQTTConfig:
    # The Pioreactor leader runs the Mosquitto broker for the whole cluster.
    host: str = _env("LARRE_MQTT_HOST", "pioreactor.local")
    port: int = _env_int("LARRE_MQTT_PORT", 1883)
    # Recent Pioreactor images ship with broker auth enabled (see [mqtt] in
    # ~/.pioreactor/config.ini on the leader). Leave unset for an anonymous broker.
    username: str | None = _env("LARRE_MQTT_USERNAME")
    password: str | None = _env("LARRE_MQTT_PASSWORD")
    keepalive: int = _env_int("LARRE_MQTT_KEEPALIVE", 60)
    client_id_prefix: str = "larr-e"
    # Where LARR-E publishes its own alerts, outside the pioreactor/ namespace
    # so the leader's mqtt_to_db_streaming job never stores them.
    alert_topic_prefix: str = "larre/alerts"
    # How long the broker (on the Pi) may be unreachable before "lost contact" is announced.
    link_grace_s: float = _env_float("LARRE_LINK_GRACE_S", 20.0)
    triage_topic_prefix: str = "larre/triage"


@dataclass(frozen=True)
class TopicConfig:
    """Telemetry topics, following pioreactor/<unit>/<experiment>/<job>/<attribute>."""

    od: str = "pioreactor/+/+/od_reading/+"  # od1, od2 (plus ods/$state, filtered out)
    growth_rate: str = "pioreactor/+/+/growth_rate_calculating/growth_rate"
    temperature: str = "pioreactor/+/+/temperature_automation/temperature"
    temperature_legacy: str = "pioreactor/+/+/temperature_control/temperature"
    stirring: str = "pioreactor/+/+/stirring/measured_rpm"
    # Job states, so a deliberate stop or a thermostat start isn't mistaken for a fault.
    stirring_state: str = "pioreactor/+/+/stirring/$state"
    temperature_state: str = "pioreactor/+/+/temperature_automation/$state"

    def all(self) -> list[str]:
        return [self.od, self.growth_rate, self.temperature, self.temperature_legacy, self.stirring,
                self.stirring_state, self.temperature_state]


@dataclass(frozen=True)
class PioreactorConfig:
    # Default on the leader: /home/pioreactor/.pioreactor/storage/pioreactor.sqlite
    db_path: Path = Path(
        _env("LARRE_DB_PATH", "/home/pioreactor/.pioreactor/storage/pioreactor.sqlite")
    )
    default_unit: str | None = _env("LARRE_DEFAULT_UNIT")
    # If unset, the experiment is resolved from live telemetry, then from SQLite.
    experiment: str | None = _env("LARRE_EXPERIMENT")
    # How jobs and pumps are started:
    # "rest": the leader's HTTP API, as the Pioreactor web UI and MCP server do (works from any machine).
    # "pios": shell out to `pios run ...` (only on the leader; it calls the same API).
    # There is no MQTT option: Pioreactor 26.11 has no subscriber for pioreactor/<unit>/<exp>/run/<job>,
    # and on a real Pi a publish there started nothing. Anything else is refused when a job starts.
    # LARRE_JOB_TRANSPORT is the same setting under its newer name.
    pump_transport: str = _env("LARRE_JOB_TRANSPORT", _env("LARRE_PUMP_TRANSPORT", "rest"))
    # Leader web server (port 80 on a stock image). Used by the "rest" transport and experiment plans.
    leader_url: str = _env("LARRE_LEADER_URL", "http://pioreactor.local")
    # Leader's unit name: experiment profiles execute on the leader. In a one-Pi cluster it is the worker too.
    leader_unit: str | None = _env("LARRE_LEADER_UNIT")
    # Units served by a different leader, e.g. digital-twin units next to the real Pi:
    #   LARRE_LEADERS="twin1=http://127.0.0.1:8790,twin2=http://127.0.0.1:8790"
    # A unit listed here is its own leader. Unlisted units use leader_url / leader_unit.
    leaders: dict[str, str] = field(
        default_factory=lambda: dict(
            pair.split("=", 1) for pair in (_env("LARRE_LEADERS", "") or "").replace(" ", "").split(",") if "=" in pair
        )
    )
    # Record every telemetry reading the listener sees into db_path (Pioreactor schema). Use this
    # when LARR-E runs on a laptop: the leader's own database lives on the Pi, and twin units have none.
    record_db: bool = _env_bool("LARRE_RECORD_DB", False)
    # Where the leader keeps profiles (the execute command needs the full path on the leader).
    profile_dir: str = _env("LARRE_PROFILE_DIR", "/home/pioreactor/.pioreactor/experiment_profiles")
    dry_run: bool = _env_bool("LARRE_DRY_RUN", False)


@dataclass(frozen=True)
class SafetyLimits:
    """Hard limits enforced in Python, no matter what the LLM asks for."""

    max_dose_ml: float = _env_float("LARRE_MAX_DOSE_ML", 5.0)
    max_dose_duration_s: float = _env_float("LARRE_MAX_DOSE_DURATION_S", 30.0)
    # Without a pump calibration the Pioreactor can't turn ml into pump time: dose by seconds only.
    pumps_calibrated: bool = _env_bool("LARRE_PUMPS_CALIBRATED", True)
    min_temperature_c: float = _env_float("LARRE_MIN_TEMP_C", 20.0)
    max_temperature_c: float = _env_float("LARRE_MAX_TEMP_C", 45.0)
    max_stirring_rpm: float = _env_float("LARRE_MAX_RPM", 1500.0)
    max_query_rows: int = _env_int("LARRE_MAX_QUERY_ROWS", 200)
    query_timeout_s: float = _env_float("LARRE_QUERY_TIMEOUT_S", 5.0)


@dataclass(frozen=True)
class AnomalyConfig:
    window_size: int = _env_int("LARRE_WINDOW", 30)
    min_samples: int = _env_int("LARRE_MIN_SAMPLES", 5)
    cooldown_s: float = _env_float("LARRE_ALERT_COOLDOWN_S", 60.0)
    # OD: flag a jump above the rolling median larger than BOTH the relative and
    # the absolute threshold (the absolute floor stops false alarms at low OD).
    od_spike_rel: float = _env_float("LARRE_OD_SPIKE_REL", 0.5)
    od_spike_abs: float = _env_float("LARRE_OD_SPIKE_ABS", 0.1)
    od_drop_rel: float = _env_float("LARRE_OD_DROP_REL", 0.5)
    # Temperature: sudden drop against the rolling median, or out of the safe band.
    temp_drop_c: float = _env_float("LARRE_TEMP_DROP_C", 2.0)
    temp_min_c: float = _env_float("LARRE_TEMP_ALERT_MIN_C", 22.0)
    temp_max_c: float = _env_float("LARRE_TEMP_ALERT_MAX_C", 42.0)
    # Below temp_min_c is warming up, not a fault, for this long after the thermostat starts.
    temp_warmup_s: float = _env_float("LARRE_TEMP_WARMUP_S", 1200.0)
    # Growth rate (h^-1): a strongly negative rate means the culture is crashing.
    growth_rate_crash: float = _env_float("LARRE_GR_CRASH", -0.1)
    # Stirring: measured RPM collapsing below this fraction of its baseline.
    stir_stall_fraction: float = _env_float("LARRE_STIR_STALL_FRACTION", 0.3)
    stir_min_baseline_rpm: float = _env_float("LARRE_STIR_MIN_BASELINE_RPM", 200.0)
    # A stall alert waits this long: stopping stirring on purpose publishes 0 RPM just before the
    # job's $state becomes "disconnected" (Pioreactor 26.11 stirring.py, base.py).
    stop_grace_s: float = _env_float("LARRE_STOP_GRACE_S", 5.0)


@dataclass(frozen=True)
class LLMConfig:
    # "anthropic" (Claude, the default), "hf" (open models via Hugging Face Inference Providers)
    # or "gemini" (Google Gemini; free tier via aistudio.google.com).
    provider: str = _env("LARRE_LLM", "anthropic")
    model: str = _env("LARRE_MODEL", "claude-opus-5-5")
    # low | medium | high | xhigh | max. "low" gives snappier stage responses.
    effort: str = _env("LARRE_EFFORT", "medium")
    max_tokens: int = _env_int("LARRE_MAX_TOKENS", 16000)
    max_turns: int = _env_int("LARRE_MAX_TURNS", 12)
    # Server-side refusal fallback: if the primary model declines, the API reruns
    # the request on a fallback model inside the same call.
    use_fallbacks: bool = _env_bool("LARRE_USE_FALLBACKS", True)
    # Hugging Face backend. Append ":cheapest" or ":<provider>" to the model to pick a provider.
    hf_model: str = _env("LARRE_HF_MODEL", "openai/gpt-oss-120b:fastest")
    hf_base_url: str = _env("LARRE_HF_BASE_URL", "https://router.huggingface.co/v1")
    hf_max_tokens: int = _env_int("LARRE_HF_MAX_TOKENS", 8192)
    # Bill a Hugging Face organization (e.g. a hackathon org holding the credits) instead of yourself.
    hf_bill_to: str | None = _env("LARRE_HF_BILL_TO")
    # Gemini backend (OpenAI-compatible endpoint). Check your free-tier models at aistudio.google.com/rate-limit.
    gemini_model: str = _env("LARRE_GEMINI_MODEL", "gemini-3.8-flash")
    gemini_base_url: str = _env("LARRE_GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
    # Optional reasoning_effort for hf/gemini (e.g. low | medium | high); support varies by model.
    reasoning_effort: str | None = _env("LARRE_REASONING_EFFORT", _env("LARRE_HF_REASONING_EFFORT"))


@dataclass(frozen=True)
class OutputConfig:
    runtime_dir: Path = RUNTIME_DIR
    state_path: Path = RUNTIME_DIR / "live_state.json"
    alerts_path: Path = RUNTIME_DIR / "alerts.jsonl"
    # Recent readings kept per unit/metric in the live state (for the 3DS charts): at most
    # history_points samples, at least history_step_s apart.
    history_points: int = _env_int("LARRE_HISTORY_POINTS", 40)
    history_step_s: float = _env_float("LARRE_HISTORY_STEP_S", 5.0)
    audit_path: Path = RUNTIME_DIR / "audit.jsonl"  # who requested and who approved each hardware action
    plans_path: Path = RUNTIME_DIR / "experiment_plans.json"  # the active plan per unit
    plots_dir: Path = RUNTIME_DIR / "plots"
    # A live reading older than this is reported as stale (Pioreactor publishes every 5-60 s).
    stale_after_s: float = _env_float("LARRE_STALE_S", 180.0)
    # Webhook posting from listener.py. Leave unset when slack_bot.py is running,
    # which posts the same alerts itself with buttons and threads.
    slack_webhook_url: str | None = _env("LARRE_SLACK_WEBHOOK_URL")
    auto_triage: bool = _env_bool("LARRE_AUTO_TRIAGE", True)


@dataclass(frozen=True)
class SlackConfig:
    """Slack bot front end (slack_bot.py), connected over Socket Mode."""

    bot_token: str | None = _env("SLACK_BOT_TOKEN")  # xoxb-...
    app_token: str | None = _env("SLACK_APP_TOKEN")  # xapp-... with connections:write
    # Channel ID (C...) that receives anomaly alerts from listener.py.
    alert_channel: str | None = _env("LARRE_SLACK_ALERT_CHANNEL")
    # Slack user IDs (U...) allowed to approve hardware actions. Empty: anyone who can see the request.
    approvers: frozenset[str] = frozenset(
        u.strip() for u in (_env("LARRE_SLACK_APPROVERS", "") or "").split(",") if u.strip()
    )
    # False enforces a two-person rule: the requester cannot approve their own action.
    allow_self_approval: bool = _env_bool("LARRE_SLACK_ALLOW_SELF_APPROVAL", True)
    approval_timeout_s: float = _env_float("LARRE_SLACK_APPROVAL_TIMEOUT_S", 180.0)
    max_conversations: int = _env_int("LARRE_SLACK_MAX_CONVERSATIONS", 200)
    workers: int = _env_int("LARRE_SLACK_WORKERS", 8)


@dataclass(frozen=True)
class ApiConfig:
    """Local HTTP API (api_server.py) used by the 3DS browser dashboard and the PocketJS bridge."""

    # 0.0.0.0 so a 3DS on the same Wi-Fi can reach it. Use a trusted network: it is plain HTTP.
    host: str = _env("LARRE_API_HOST", "0.0.0.0")
    port: int = _env_int("LARRE_API_PORT", 8787)
    # Shared secret required on every request (?k=... or X-Larre-Token). Generated at startup if unset.
    token: str | None = _env("LARRE_API_TOKEN")
    # Where phones in the Slack channel can reach `larr-e api`, e.g. http://192.168.1.20:8787. With a
    # token set, Slack plan cards get an "Adjust in LARR-E" button that opens /plan. The link carries
    # the token, so only set this for a private channel.
    public_url: str | None = _env("LARRE_PUBLIC_URL")
    approval_timeout_s: float = _env_float("LARRE_API_APPROVAL_TIMEOUT_S", 180.0)
    workers: int = _env_int("LARRE_API_WORKERS", 4)
    max_jobs: int = _env_int("LARRE_API_MAX_JOBS", 100)
    # Which preset requests the 3DS / dashboard show, in order (the 3DS shows the first four).
    # Available: summary, triage, trend, dilute, stir, heat, pump, exchange, stop.
    presets: str = _env("LARRE_PRESETS", "summary,triage,trend,dilute")


@dataclass(frozen=True)
class PlanConfig:
    """Experiment plans (experiment.py): one scheduled profile per unit, persisted across restarts."""

    max_duration_h: float = _env_float("LARRE_MAX_EXPERIMENT_H", 72.0)
    default_update_every_h: float = _env_float("LARRE_UPDATE_EVERY_H", 2.0)
    # Pioreactor's own profile dry-run executes in real time (an 18 h profile logs for 18 h), so we
    # don't use it as a pre-flight check. The leader validates a profile when it is uploaded instead.
    # Slack channel for progress updates; falls back to the alert channel.
    update_channel: str | None = _env("LARRE_SLACK_UPDATE_CHANNEL")
    check_every_s: float = _env_float("LARRE_PLAN_CHECK_EVERY_S", 60.0)


@dataclass(frozen=True)
class Settings:
    mqtt: MQTTConfig = field(default_factory=MQTTConfig)
    topics: TopicConfig = field(default_factory=TopicConfig)
    pioreactor: PioreactorConfig = field(default_factory=PioreactorConfig)
    safety: SafetyLimits = field(default_factory=SafetyLimits)
    anomaly: AnomalyConfig = field(default_factory=AnomalyConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    slack: SlackConfig = field(default_factory=SlackConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    plans: PlanConfig = field(default_factory=PlanConfig)

    def ensure_dirs(self) -> None:
        self.output.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.output.plots_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
