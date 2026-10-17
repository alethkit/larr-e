/** Run the compiled 3DS app on the PocketJS wasm renderer, wired through the real bridge
 * methods to a running `larr-e api`, and save screenshots of both screens to dist/qa/.
 *
 *   LARRE_API_TOKEN=... bun scripts/sim.ts [api-url]
 *
 * Needs `bun run 3ds --pocket-only` (the guest) and `bun runtime/tools/wasm.ts` (the renderer).
 * The scenario approves a pump dose, so it refuses to run unless the API is in dry-run mode.
 * Adapted from Pocket Doc's scripts/sim.ts.
 */
import { mkdirSync } from "node:fs";
import { createWasmUi } from "../runtime/hosts/web/wasm-ops.js";
import { BTN, ENUMS, NODE_TYPE, PROP } from "../runtime/contracts/spec/spec.ts";
import { encodePNG } from "../runtime/tests/png.ts";
import { dispatchOffload } from "../runtime/tools/offload-provider.ts";
import { createMethods } from "../bridge/larre.ts";

const api = process.argv[2] ?? process.env.LARRE_API_URL ?? "http://127.0.0.1:8787";
const token = process.env.LARRE_API_TOKEN ?? "";
const info = await fetch(`${api}/api/presets`, { headers: { "X-Larre-Token": token } })
  .then(r => r.json() as Promise<{ presets?: unknown[]; dry_run?: boolean }>).catch(() => undefined);
if (!info?.presets) throw new Error(`Cannot reach ${api} with this token`);
if (info.dry_run !== true) throw new Error("Refusing to simulate approvals against a live API: start it with LARRE_DRY_RUN=1");
const methods = createMethods({ api, token });

const wasm = await createWasmUi(await Bun.file("runtime/hosts/web/pocketjs.wasm").arrayBuffer(), { width: 400, height: 480 });
const ops = wasm.ops;
// Upper screen at (0,0) 400x240; lower screen centered below it at (40,240) 320x240, as on hardware.
ops.hitTestBoundsAuxiliary = (x, y) => ops.hitTestBounds!(x + 40, y + 240);
(ops as typeof ops & { __viewport: { w: number; h: number } }).__viewport = { w: 400, h: 240 };
const auxiliary = ops.createNode(NODE_TYPE.view);
ops.setProp(auxiliary, PROP.posType, ENUMS.PosType.Absolute);
ops.setProp(auxiliary, PROP.insetL, 40); ops.setProp(auxiliary, PROP.insetT, 240);
ops.setProp(auxiliary, PROP.width, 320); ops.setProp(auxiliary, PROP.height, 240);
ops.insertBefore(1, auxiliary, 0);
ops.__auxiliarySurface = { root: auxiliary, w: 320, h: 240 };

const requests: string[] = [], replies: { at: number; raw: string }[] = [];
const counts = new Map<string, number>();
let tick = 0;
Object.assign(globalThis, {
  ui: ops, __pak: await Bun.file("runtime/dist/3ds/guest/larr-e-main.pak").arrayBuffer(), __simHz: 60,
  offload: {
    session: () => 1,
    submit: (raw: string) => { requests.push(raw); return true; },
    take: () => { const i = replies.findIndex(r => r.at <= tick); return i < 0 ? undefined : replies.splice(i, 1)[0].raw; },
  },
});
(0, eval)(await Bun.file("runtime/dist/3ds/guest/larr-e-main.js").text());

let hit: number | undefined;
async function frames(n: number, buttons = 0, touch?: [number, number]) {
  if (touch && hit === undefined) { wasm.render(); hit = ops.hitTestBounds!(touch[0] + 40, touch[1] + 240); }
  if (!touch) hit = undefined;
  for (let i = 0; i < n; i++) {
    tick++;
    (globalThis as any).frame(buttons, 0x8080, touch ? [touch[0] | touch[1] << 9] : [], touch ? [hit] : [], touch ? [1] : [], 0x8080);
    wasm.tick();
    for (const raw of requests.splice(0)) {
      const req = JSON.parse(raw);
      counts.set(req.method, (counts.get(req.method) ?? 0) + 1);
      replies.push({ at: tick + 2, raw: JSON.stringify(await dispatchOffload(methods, req)) });
    }
  }
}
const tap = async (x: number, y: number) => { await frames(1, 0, [x, y]); await frames(2); };
const press = async (button: number) => { await frames(1, button); await frames(2); };
/** Let wall-clock time pass (agent jobs run in the API's threads) while frames keep ticking. */
async function settle(ms: number) {
  const end = Date.now() + ms;
  while (Date.now() < end) { await frames(15); await Bun.sleep(20); }
}
mkdirSync("dist/qa", { recursive: true });
const shot = (name: string) => Bun.write(`dist/qa/${name}.png`, encodePNG(wasm.render().slice(), 400, 480));

await settle(1500);
await shot("01-dashboard");
await tap(80, 58);            // "Health check" (top-left preset button)
await settle(2500);
await shot("02-answer");
await press(BTN.RIGHT); await press(BTN.DOWN); await press(BTN.DOWN); // bottom-right preset (a hardware action)
await press(BTN.CIRCLE);      // A
await settle(2500);
await shot("03-approval");
await press(BTN.CIRCLE);      // A: approve the media dose
await settle(2500);
await shot("04-second-approval");
await press(BTN.CROSS);       // B: deny the waste removal
await settle(3000);
await shot("05-done");
console.log("requests:", Object.fromEntries(counts));
console.log("screenshots: dist/qa/01-dashboard.png ... 05-done.png");
process.exit(0);
