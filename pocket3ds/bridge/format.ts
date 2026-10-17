/** Text shaping for the 3DS: ASCII only, pre-wrapped, inside the offload budget. */

const REPLACEMENTS: [RegExp, string][] = [
  [/°\s?C/g, "C"], [/°/g, " deg"], [/[µμ]/g, "u"], [/⁻¹/g, "^-1"], [/[→⇒]/g, "->"], [/[←⇐]/g, "<-"],
  [/[–—−]/g, "-"], [/…/g, "..."], [/×/g, "x"], [/±/g, "+/-"], [/≥/g, ">="], [/≤/g, "<="],
  [/[‘’]/g, "'"], [/[“”]/g, '"'], [/[•·]/g, "*"], [/ /g, " "],
];

export function toAscii(text: string): string {
  let out = text;
  for (const [pattern, replacement] of REPLACEMENTS) out = out.replace(pattern, replacement);
  // Drop combining marks after decomposition (é -> e), then anything still non-ASCII.
  out = out.normalize("NFKD").replace(/[̀-ͯ]/g, "");
  return out.replace(/[^\x20-\x7e\n]/g, "");
}

/** Strip the Markdown the model sometimes uses despite instructions. */
export function stripMarkdown(text: string): string {
  return text
    .replace(/```[a-z]*\n?/g, "")
    .replace(/\*\*(.+?)\*\*/g, "$1")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/^#{1,6}\s+/gm, "")
    .replace(/^\s*[-*]\s+/gm, "* ");
}

/** Greedy word wrap to `cols`, at most `maxLines`; the last kept line ends in "..." if cut. */
export function wrap(text: string, cols: number, maxLines: number): string[] {
  const lines: string[] = [];
  for (const paragraph of text.split("\n")) {
    if (!paragraph.trim()) continue;
    let line = "";
    for (let word of paragraph.trim().split(/\s+/)) {
      while (word.length > cols) { // hard-split words longer than a line (URLs, IDs)
        if (line) { lines.push(line); line = ""; }
        lines.push(word.slice(0, cols));
        word = word.slice(cols);
      }
      if (!line) line = word;
      else if (line.length + 1 + word.length <= cols) line += " " + word;
      else { lines.push(line); line = word; }
    }
    if (line) lines.push(line);
  }
  if (lines.length <= maxLines) return lines;
  const kept = lines.slice(0, maxLines);
  const last = kept[maxLines - 1];
  kept[maxLines - 1] = (last.length > cols - 3 ? last.slice(0, cols - 3) : last).replace(/[\s.,;:]+$/, "") + "...";
  return kept;
}

export function shape(text: string, cols: number, maxLines: number): string[] {
  return wrap(toAscii(stripMarkdown(text)), cols, maxLines);
}

/** Serialize, failing loudly instead of letting the provider drop the connection. */
export function encode(value: unknown, budget = 2400): string {
  const json = JSON.stringify(value);
  if (json.length > budget) throw new Error(`Reply of ${json.length} chars exceeds the ${budget}-char budget`);
  return json;
}
