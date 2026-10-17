import { expect, test } from "bun:test";
import { encode, shape, toAscii, wrap } from "../bridge/format.ts";
import { chartFor, sparkBars, unitRow } from "../bridge/larre.ts";
import { CHART_BARS, CHART_LABEL_CHARS, SPARK_HEIGHT, TOP_COLS } from "../shared/protocol.ts";

test("toAscii keeps meaning for lab symbols and drops the rest", () => {
  expect(toAscii("30.1 °C, µ = −0.4 h⁻¹ → crash… “ok” café 🧫")).toBe('30.1 C, u = -0.4 h^-1 -> crash... "ok" cafe ');
});

test("wrap respects width, hard-splits long words and marks truncation once", () => {
  const lines = wrap("a ".repeat(200) + "x".repeat(130), 20, 4);
  expect(lines.length).toBe(4);
  expect(lines.every(l => l.length <= 20)).toBe(true);
  expect(lines[3].endsWith("...")).toBe(true);
  expect(wrap("ends with a period. and more words here", 12, 2).at(-1)).not.toContain("....");
  expect(wrap("y".repeat(45), 20, 9)).toEqual(["y".repeat(20), "y".repeat(20), "y".repeat(5)]);
});

test("shape strips markdown and stays ASCII", () => {
  const lines = shape("**Bold** `code`\n# Head\n- item → next", TOP_COLS, 5);
  expect(lines).toEqual(["Bold code", "Head", "* item -> next"]);
  expect(lines.join("")).toMatch(/^[\x20-\x7e]*$/);
});

test("encode enforces the offload budget", () => {
  expect(() => encode({ s: "x".repeat(3000) })).toThrow(/budget/);
  expect(encode({ ok: true })).toBe('{"ok":true}');
});

test("unit rows fit the top screen and flag alarms", () => {
  const row = unitRow("a-very-long-unit-name", { od: { od1: { value: 1.23456 } }, temperature: { value: 18.2 },
    growth_rate: { value: 0.3 }, stirring: { value: 512.4 } });
  expect(row.text.length).toBeLessThanOrEqual(TOP_COLS);
  expect(row.alarm).toBe(true);
  expect(unitRow("w1", {}).text).toContain("OD     -");
});

test("chart bars: short series right-aligned, long series averaged, flat series mid-height", () => {
  const short = sparkBars([1, 2, 3]);
  expect(short.length).toBe(CHART_BARS);
  expect(short.slice(0, CHART_BARS - 3).every(h => h === 0)).toBe(true); // empty slots on the left
  expect(short.slice(-3)).toEqual([2, Math.round(2 + (SPARK_HEIGHT - 2) / 2), SPARK_HEIGHT]);
  const long = sparkBars(Array.from({ length: 40 }, (_, i) => i));
  expect(long.length).toBe(CHART_BARS);
  expect(long[0]).toBe(2); expect(long.at(-1)).toBe(SPARK_HEIGHT);
  expect(new Set(sparkBars([500, 500, 500]).slice(-3))).toEqual(new Set([Math.round(SPARK_HEIGHT / 2)]));
  expect(sparkBars([])).toEqual([]);
});

test("chart labels show unit, latest value and range, and fit their panel", () => {
  const c = chartFor("Temp", "pio01", [29.9, 31.2, 31.84], "C", 1);
  expect(c.label).toBe("Temp 31.8C (29.9-31.8)");
  expect(c.label.length).toBeLessThanOrEqual(CHART_LABEL_CHARS);
  expect(chartFor("Stir", undefined, [], "rpm", 0)).toEqual({ label: "Stir: no readings yet", bars: [] });
});
