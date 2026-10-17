/** Offload methods the 3DS may call, each mapped onto the LARR-E HTTP API (api_server.py).
 * The provider kills a request after 9 s, so every call here is short: agent questions
 * start a job and the 3DS polls it. */
import {
  ANSWER_LINES, BOTTOM_COLS, CHART_BARS, CHART_LABEL_CHARS, DEFAULT_PRESETS, MAX_PRESETS, MAX_UNITS, SPARK_HEIGHT, TOP_COLS,
  type AlertInfo, type ApprovalInfo, type ChartInfo, type JobState, type Preset, type Snapshot, type UnitRow,
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

type Entry = { value: number } | undefined;
type ApiUnit = { od?: Record<string, Entry>; temperature?: Entry; growth_rate?: Entry; stirring?: Entry;
  history?: Record<string, number[]>; seconds_since_last_reading?: number };
const STALE_AFTER_S = 120;

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
  const stale = (u.seconds_since_last_reading ?? 0) > STALE_AFTER_S;
  return { text: toAscii(stale ? `${text} old` : text).slice(0, TOP_COLS), alarm };
}

/** Scale values to exactly `count` bar heights in 2..SPARK_HEIGHT. Longer series are bucket-averaged;
 * shorter ones are right-aligned (newest on the right) with empty 0-height slots on the left.
 * A flat series sits at mid-height so a steady 500 RPM doesn't look like zero. */
export function sparkBars(values: number[], count = CHART_BARS): number[] {
  if (!values.length) return [];
  let series = values;
  if (values.length > count) {
    const size = values.length / count;
    series = Array.from({ length: count }, (_, i) => {
      const bucket = values.slice(Math.floor(i * size), Math.floor((i + 1) * size));
      return bucket.reduce((a, b) => a + b, 0) / bucket.length;
    });
  }
  const lo = Math.min(...series), hi = Math.max(...series), span = hi - lo;
  const heights = series.map(v => span < 1e-9 ? Math.round(SPARK_HEIGHT / 2) : Math.round(2 + ((SPARK_HEIGHT - 2) * (v - lo)) / span));
  return [...Array(count - heights.length).fill(0), ...heights];
}

/** Chart for one metric of one unit, e.g. "Temp 31.8C (29.9-32.0)"; the unit name is in the rows above. */
export function chartFor(title: string, unit: string | undefined, values: number[], suffix: string, digits: number): ChartInfo {
  if (!unit || !values.length) return { label: toAscii(`${title}: no readings yet`), bars: [] };
  const last = values[values.length - 1], lo = Math.min(...values), hi = Math.max(...values);
  const label = `${title} ${last.toFixed(digits)}${suffix} (${lo.toFixed(digits)}-${hi.toFixed(digits)})`;
  return { label: toAscii(label).slice(0, CHART_LABEL_CHARS), bars: sparkBars(values) };
}

export function createMethods(cfg: BridgeConfig) {
  const api = new LarreApi(cfg);
  let presets: Preset[] | undefined;

  async function loadPresets() {
    if (presets) return presets;
    const data = await api.call<{ presets: { name: string; label: string; mode: string }[] }>("GET", "/api/presets");
    presets = data.presets.slice(0, MAX_PRESETS).map(p => ({ name: p.name, label: toAscii(p.label).slice(0, 20), exec: p.mode === "exec" }));
    return presets;
  }

  async function snapshot(): Promise<Snapshot> {
    const [status, alerts, approvals, presetList] = await Promise.all([
      api.call<{ available: boolean; note?: string } & Record<string, unknown>>("GET", "/api/status?history=1"),
      api.call<{ alerts: { severity: string; unit: string; metric: string; message: string; age_s: number | null }[] }>("GET", "/api/alerts?n=1"),
      api.call<{ approvals: { id: string; tool: string; args: Record<string, unknown> }[] }>("GET", "/api/approvals"),
      loadPresets().catch(() => DEFAULT_PRESETS),
    ]);
    const { available, note, ...units } = status;
    const names = available ? Object.keys(units).sort() : [];
    const rows = names.slice(0, MAX_UNITS).map(name => unitRow(name, units[name] as ApiUnit));
    // Chart the first unit that has history for each metric.
    const historyOf = (metric: string) => {
      const name = names.find(n => ((units[n] as ApiUnit).history?.[metric]?.length ?? 0) > 0);
      return { name, values: name ? (units[name] as ApiUnit).history![metric] : [] };
    };
    const temp = historyOf("temperature"), stir = historyOf("stirring");
    const charts: [ChartInfo, ChartInfo] = [
      chartFor("Temp", temp.name, temp.values, "C", 1),
      chartFor("Stir", stir.name, stir.values, "rpm", 0),
    ];
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
      alert, approval, charts, presets: presetList,
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
