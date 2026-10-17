/** Offload methods the 3DS may call, each mapped onto the LARR-E HTTP API (api_server.py).
 * The provider kills a request after 9 s, so every call here is short: agent questions
 * start a job and the 3DS polls it. */
import {
  ANSWER_LINES, BOTTOM_COLS, DEFAULT_PRESETS, MAX_UNITS, SPARK_BARS, SPARK_HEIGHT, TOP_COLS,
  type AlertInfo, type ApprovalInfo, type JobState, type Preset, type Snapshot, type UnitRow,
} from "../shared/protocol.ts";
import { encode, shape, toAscii } from "./format.ts";

export type BridgeConfig = { api: string; token: string };

const TOOL_TITLES: Record<string, string> = {
  dose: "Run a pump",
  set_target_temperature: "Change temperature",
  set_stirring_rpm: "Change stirring",
  start_job: "Start a job",
  stop_job: "Stop a job",
};
const ALERT_MAX_AGE_S = 600;
const SPARK_TTL_MS = 15_000;

type Entry = { value: number } | undefined;
type ApiUnit = { od?: Record<string, Entry>; temperature?: Entry; growth_rate?: Entry; stirring?: Entry };

export class LarreApi {
  constructor(private cfg: BridgeConfig) {}

  async call<T>(method: "GET" | "POST", path: string, body?: unknown): Promise<T> {
    const response = await fetch(this.cfg.api.replace(/\/$/, "") + path, {
      method,
      headers: { "X-Larre-Token": this.cfg.token, "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(4000),
    });
    const data = await response.json().catch(() => ({})) as T & { error?: string };
    if (!response.ok) throw new Error(`LARR-E ${response.status}: ${data.error ?? "request failed"}`);
    return data;
  }
}

const num = (e: Entry, digits: number) => (e && typeof e.value === "number" ? e.value.toFixed(digits) : "-");

export function unitRow(name: string, u: ApiUnit): UnitRow {
  const od = u.od?.od1 ?? u.od?.od2;
  const t = u.temperature?.value, g = u.growth_rate?.value;
  const alarm = (t !== undefined && (t < 22 || t > 42)) || (g !== undefined && g < -0.1);
  const text = `${name.slice(0, 9).padEnd(9)} OD ${num(od, 3).padStart(5)}  ${num(u.temperature, 1).padStart(4)}C  ` +
    `${num(u.growth_rate, 2).padStart(5)}/h  ${num(u.stirring, 0).padStart(4)}rpm`;
  return { text: toAscii(text).slice(0, TOP_COLS), alarm };
}

/** Scale values to bar heights, resampled to exactly SPARK_BARS. */
export function sparkBars(values: number[]): number[] {
  if (!values.length) return [];
  const lo = Math.min(...values), hi = Math.max(...values), span = hi - lo || 1;
  return Array.from({ length: SPARK_BARS }, (_, i) => {
    const v = values[Math.min(values.length - 1, Math.floor((i * values.length) / SPARK_BARS))];
    return Math.round(2 + ((SPARK_HEIGHT - 2) * (v - lo)) / span);
  });
}

export function createMethods(cfg: BridgeConfig) {
  const api = new LarreApi(cfg);
  let spark: { at: number; bars: number[]; label: string } | undefined;
  let presets: Preset[] | undefined;

  async function loadSpark() {
    if (spark && Date.now() - spark.at < SPARK_TTL_MS) return spark;
    try {
      const s = await api.call<{ unit: string; values: number[] }>("GET", "/api/series?metric=od&minutes=30&points=40");
      const lo = s.values.length ? Math.min(...s.values) : 0, hi = s.values.length ? Math.max(...s.values) : 0;
      spark = { at: Date.now(), bars: sparkBars(s.values),
        label: toAscii(`OD ${s.unit}, last 30 min: ${lo.toFixed(3)} - ${hi.toFixed(3)}`) };
    } catch (error) { // no default unit or no data yet: keep the dashboard alive
      spark = { at: Date.now(), bars: [], label: toAscii(`OD: ${(error as Error).message}`).slice(0, TOP_COLS) };
    }
    return spark;
  }

  async function loadPresets() {
    if (presets) return presets;
    const data = await api.call<{ presets: { name: string; label: string; mode: string }[] }>("GET", "/api/presets");
    presets = data.presets.slice(0, 4).map(p => ({ name: p.name, label: toAscii(p.label).slice(0, 20), exec: p.mode === "exec" }));
    return presets;
  }

  async function snapshot(): Promise<Snapshot> {
    const [status, alerts, approvals, sparkData, presetList] = await Promise.all([
      api.call<{ available: boolean; note?: string } & Record<string, unknown>>("GET", "/api/status"),
      api.call<{ alerts: { severity: string; unit: string; metric: string; message: string; age_s: number | null }[] }>("GET", "/api/alerts?n=1"),
      api.call<{ approvals: { id: string; tool: string; args: Record<string, unknown> }[] }>("GET", "/api/approvals"),
      loadSpark(),
      loadPresets().catch(() => DEFAULT_PRESETS),
    ]);
    const { available, note, ...units } = status;
    const rows = available
      ? Object.keys(units).sort().slice(0, MAX_UNITS).map(name => unitRow(name, units[name] as ApiUnit))
      : [];
    const last = alerts.alerts.at(-1);
    const alert: AlertInfo | null = last && last.age_s !== null && last.age_s <= ALERT_MAX_AGE_S
      ? { title: toAscii(`${last.severity.toUpperCase()}  ${last.unit}  ${last.metric}`), critical: last.severity === "critical",
          lines: shape(last.message, TOP_COLS, 2) }
      : null;
    const a = approvals.approvals[0];
    const approval: ApprovalInfo | null = a
      ? { id: a.id, title: TOOL_TITLES[a.tool] ?? a.tool,
          lines: shape(Object.entries(a.args).filter(([, v]) => v !== null && v !== undefined)
            .map(([k, v]) => `${k} = ${v}`).join("\n"), BOTTOM_COLS, 4) }
      : null;
    return {
      units: rows,
      note: available ? "" : toAscii(note ?? "No telemetry yet").slice(0, TOP_COLS),
      alert, approval, spark: sparkData.bars, sparkLabel: sparkData.label, presets: presetList,
    };
  }

  const wrapJson = <T>(f: (input: T) => Promise<unknown>) =>
    async (raw: string) => encode(await f((raw ? JSON.parse(raw) : {}) as T));

  return {
    "larre.snapshot": wrapJson(() => snapshot()),
    "larre.preset": wrapJson(async (p: { name: string }) => {
      if (!/^[a-z0-9_-]{1,32}$/.test(p.name ?? "")) throw new Error("Bad preset name");
      const r = await api.call<{ job_id: string }>("POST", `/api/presets/${p.name}`, { style: "3ds", source: "3ds" });
      return { job: r.job_id };
    }),
    "larre.job": wrapJson(async (p: { id: string }): Promise<JobState> => {
      if (!/^[a-f0-9]{1,32}$/.test(p.id ?? "")) throw new Error("Bad job id");
      const j = await api.call<{ state: JobState["state"]; answer: string; approval: unknown }>("GET", `/api/jobs/${p.id}`);
      return { state: j.state, waiting: !!j.approval, lines: j.state === "running" ? [] : shape(j.answer, TOP_COLS, ANSWER_LINES) };
    }),
    "larre.decide": wrapJson(async (p: { id: string; decision: string }) => {
      if (!/^[a-f0-9]{1,32}$/.test(p.id ?? "") || !["approve", "deny"].includes(p.decision)) throw new Error("Bad decision");
      await api.call("POST", `/api/approvals/${p.id}`, { decision: p.decision, by: "3ds" });
      return { ok: true };
    }),
  };
}
