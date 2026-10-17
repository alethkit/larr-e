/** Offload records between the 3DS app and the bridge. Every payload must stay
 * under PocketJS's 2,500-character budget and contain only ASCII: the 3DS draws
 * text from baked glyphs, so the bridge transliterates before sending. */

/** Text-xs columns that fit the 400px top screen, with margins. Tune on hardware. */
export const TOP_COLS = 56;
/** Columns for the 320px bottom screen. */
export const BOTTOM_COLS = 44;
export const ANSWER_LINES = 5;
export const CHART_BARS = 20; // per chart; two charts side by side on the top screen
export const SPARK_HEIGHT = 28;
export const CHART_LABEL_CHARS = 27;
export const MAX_UNITS = 3;

export type UnitRow = { text: string; alarm: boolean };
export type AlertInfo = { title: string; critical: boolean; lines: string[] };
export type ApprovalInfo = { id: string; title: string; lines: string[] };
export type Preset = { name: string; label: string; exec: boolean };
/** One mini bar chart: newest reading on the right; 0-height bars are empty slots. */
export type ChartInfo = { label: string; bars: number[] };

export type Snapshot = {
  units: UnitRow[];
  note: string; // shown when there are no units, e.g. "listener not running"
  alert: AlertInfo | null;
  approval: ApprovalInfo | null;
  charts: [ChartInfo, ChartInfo]; // temperature, stirring
  presets: Preset[];
};

export type JobState = { state: "running" | "done" | "error"; waiting: boolean; lines: string[] };

/** Shown before the first snapshot arrives; names must match api_server.PRESETS. */
export const DEFAULT_PRESETS: Preset[] = [
  { name: "summary", label: "Health check", exec: false },
  { name: "triage", label: "Explain last alert", exec: false },
  { name: "trend", label: "OD trend 30 min", exec: false },
  { name: "dilute", label: "Dilute 0.5 ml", exec: true },
];
