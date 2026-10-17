/** LARR-E 3DS bridge: connects to the paired 3DS and relays its requests to `larr-e api`.
 *
 *   LARRE_API_TOKEN=... bun bridge/serve.ts <3ds-ip> [api-url]
 */
import { connectOffloadProvider } from "@pocketjs/framework/offload/provider";

const address = process.argv[2] ?? process.env.LARRE_3DS_HOST;
const api = process.argv[3] ?? process.env.LARRE_API_URL ?? "http://127.0.0.1:8787";
const token = process.env.LARRE_API_TOKEN;
if (!address || !token) {
  console.error("Usage: LARRE_API_TOKEN=<token> bun bridge/serve.ts <3ds-ip> [api-url]  (or set LARRE_3DS_HOST)");
  process.exit(1);
}
const key = (await Bun.file(".local/pair.key").text()).trim();

// Fail fast if the API is unreachable or the token is wrong, before the 3DS sees errors.
const probe = await fetch(`${api}/api/presets`, { headers: { "X-Larre-Token": token } }).catch(() => undefined);
if (!probe?.ok) {
  console.error(`Cannot reach LARR-E API at ${api} (status ${probe?.status ?? "no connection"}). Is \`larr-e api\` running with this token?`);
  process.exit(1);
}

connectOffloadProvider({ address, key, worker: new URL("./worker.ts", import.meta.url), data: { api, token }, log: console.log });
console.log(`LARR-E 3DS bridge: ${api} -> 3DS at ${address}`);
