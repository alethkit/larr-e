import { expect, test } from "bun:test";
import { encode, shape, toAscii, wrap } from "../bridge/format.ts";
import { sparkBars, unitRow } from "../bridge/larre.ts";
import { SPARK_BARS, SPARK_HEIGHT, TOP_COLS } from "../shared/protocol.ts";

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

test("spark bars resample to a fixed count within the height", () => {
  const bars = sparkBars([1, 2, 3]);
  expect(bars.length).toBe(SPARK_BARS);
  expect(Math.max(...bars)).toBe(SPARK_HEIGHT);
  expect(Math.min(...bars)).toBe(2);
  expect(sparkBars([])).toEqual([]);
  expect(new Set(sparkBars([5, 5, 5])).size).toBe(1);
});
