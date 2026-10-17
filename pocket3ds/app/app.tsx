import { Show } from "solid-js";
import { AuxiliarySurface, Text, View } from "@pocketjs/framework/components";
import { ClassicButton } from "@pocketjs/framework/classic";
import { ANSWER_LINES, CHART_BARS, MAX_UNITS, SPARK_HEIGHT } from "../shared/protocol.ts";
import { createLarre, type Larre } from "./store.ts";

const range = (n: number) => Array.from({ length: n }, (_, i) => i);
const TEAL = "#0f766e", INK = "#e6f4f1", MUTED = "#94a3b8", BG = "#0b1f1d", PANEL = "#12302c";

/** 400x240 upper screen: live dashboard. */
function Dashboard(p: { s: Larre }) {
  const { s } = p;
  // Fixed slots, built before their parents mount (PocketJS guidance for QuickJS stack depth).
  const units = range(MAX_UNITS).map(i => (
    <Text class="absolute left-[8] text-xs font-mono" style={{ insetT: 67 + i * 14,
      textColor: s.snap()?.units[i]?.alarm ? 0xff8888ff : 0xfff1f4e6 }}>
      {s.snap()?.units[i]?.text ?? (i === 0 ? s.snap()?.note || "Waiting for telemetry..." : "")}
    </Text>
  ));
  // Two charts side by side: temperature (left, orange) and stirring (right, teal).
  const CHARTS = [{ left: 8, color: "#fb923c" }, { left: 204, color: "#2dd4bf" }];
  const chartLabels = CHARTS.map((c, k) => (
    <Text class="absolute top-[111] text-xs" style={{ insetL: c.left, textColor: 0xffb8a394 }}>
      {s.snap()?.charts[k]?.label ?? (k === 0 ? "Temp" : "Stir")}
    </Text>
  ));
  const bars = CHARTS.flatMap((c, k) => range(CHART_BARS).map(i => {
    const h = () => s.snap()?.charts[k]?.bars[i] ?? 0;
    return <View class="absolute w-[7]" style={{ bgColor: c.color, insetL: c.left + i * 9, insetT: 125 + SPARK_HEIGHT - h(), height: h() }} />;
  }));
  const lines = range(ANSWER_LINES).map(i => (
    <Text class="absolute left-[8] text-xs" style={{ insetT: 160 + i * 15, textColor: 0xfff1f4e6 }}>{s.answer()[i] ?? ""}</Text>
  ));
  return <View debugName="Dashboard" class="relative w-full h-full overflow-hidden" style={{ bgColor: BG }}>
    <View class="absolute left-0 right-0 top-0 h-[20]" style={{ bgColor: TEAL }}>
      <Text class="absolute left-[8] top-[3] text-sm font-bold text-white">LARR-E</Text>
      <Text class="absolute left-[70] top-[5] text-xs" style={{ textColor: 0xffd4eaa7 }}>bioreactor ops</Text>
      <View class="absolute right-[64] top-[7] w-[6] h-[6] rounded-full" style={{ bgColor: s.online() ? "#4ade80" : "#f87171" }} />
      <Text class="absolute right-[8] top-[5] text-xs text-white">{s.online() ? "online" : "offline"}</Text>
    </View>
    <Show when={s.snap()?.alert}>{alert =>
      <View class="absolute left-[4] right-[4] top-[22] h-[42]" style={{ bgColor: alert().critical ? "#7f1d1d" : "#713f12" }}>
        <Text class="absolute left-[4] top-[2] text-xs font-bold text-white">{alert().title}</Text>
        <Text class="absolute left-[4] top-[15] text-xs text-white">{alert().lines[0] ?? ""}</Text>
        <Text class="absolute left-[4] top-[28] text-xs text-white">{alert().lines[1] ?? ""}</Text>
      </View>}
    </Show>
    {units}
    {chartLabels}
    {bars}
    <View class="absolute left-[4] right-[4] top-[157] h-[1]" style={{ bgColor: "#1f4d47" }} />
    {lines}
  </View>;
}

/** 320x240 lower screen: requests, and the approval gate when the agent wants to touch hardware. */
function Controls(p: { s: Larre }) {
  const { s } = p;
  const buttons = range(4).map(i => (
    <ClassicButton debugName={`Preset${i}`} surface="auxiliary"
      label={s.presets()[i]?.label ?? ""} tone={s.presets()[i]?.exec ? "danger" : "primary"}
      disabled={!s.presets()[i] || !!s.job() || !!s.approval()}
      style={{ posType: 1, insetL: 8 + (i % 2) * 156, insetT: 34 + Math.floor(i / 2) * 58, width: 148, height: 50 }}
      onPress={() => s.run(i)} />
  ));
  const approve = <ClassicButton debugName="Approve" surface="auxiliary" allowWhenBlocked label="Approve (A)" tone="primary"
    disabled={s.deciding()} style={{ posType: 1, insetL: 12, insetT: 168, width: 140, height: 52 }} onPress={() => s.decide("approve")} />;
  const deny = <ClassicButton debugName="Deny" surface="auxiliary" allowWhenBlocked label="Deny (B)" tone="danger"
    disabled={s.deciding()} style={{ posType: 1, insetL: 168, insetT: 168, width: 140, height: 52 }} onPress={() => s.decide("deny")} />;
  const approvalLines = range(4).map(i => (
    <Text class="absolute left-[16] text-xs text-white" style={{ insetT: 70 + i * 16 }}>{s.approval()?.lines[i] ?? ""}</Text>
  ));
  return <View debugName="Controls" class="relative w-full h-full overflow-hidden" style={{ bgColor: PANEL }}>
    <View class="absolute left-0 right-0 top-0 h-[26]" style={{ bgColor: TEAL }}>
      <Text class="absolute left-[8] top-[6] text-xs font-bold text-white">Ask LARR-E</Text>
      <Text class="absolute right-[8] top-[6] text-xs text-white">{s.status()}</Text>
    </View>
    <View debugName="Selection" class="absolute w-[154] h-[56]" style={{ bgColor: "#fbbf24",
      insetL: 5 + (s.selected() % 2) * 156, insetT: 31 + Math.floor(s.selected() / 2) * 58 }} />
    {buttons}
    <Text class="absolute left-[8] top-[154] text-xs" style={{ textColor: 0xffb8a394 }}>Tap a request, or D-pad + A. Red ones move hardware.</Text>
    <Text class="absolute left-[8] top-[222] text-xs" style={{ textColor: 0xffb8a394 }}>L+R+START: exit</Text>
    <View debugName="ApprovalGate" class="absolute left-0 top-0 w-full h-full"
      style={{ display: s.approval() ? 0 : 1, bgColor: "#422006" }}>
      <View class="absolute left-0 right-0 top-0 h-[30]" style={{ bgColor: "#b45309" }}>
        <Text class="absolute left-[10] top-[7] text-sm font-bold text-white">HARDWARE ACTION</Text>
      </View>
      <Text class="absolute left-[16] top-[42] text-sm font-bold" style={{ textColor: 0xff24bffb }}>{s.approval()?.title ?? ""}</Text>
      {approvalLines}
      {approve}{deny}
    </View>
  </View>;
}

export default function LarreApp() {
  const s = createLarre();
  const top = <Dashboard s={s} />;
  const bottom = <Controls s={s} />;
  return <>
    {top}
    <AuxiliarySurface>{bottom}</AuxiliarySurface>
  </>;
}
