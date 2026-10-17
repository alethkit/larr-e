/** Capability worker: PocketJS runs it per device connection; only allowlisted methods exist. */
declare const self: { onmessage: (event: MessageEvent) => void; postMessage(value: unknown): void };
import { dispatchOffload } from "@pocketjs/framework/offload/provider";
import { createMethods, type BridgeConfig } from "./larre.ts";

let methods: ReturnType<typeof createMethods>;
self.onmessage = async (event: MessageEvent) => {
  if (event.data.init) { methods = createMethods(event.data.init as BridgeConfig); return; }
  self.postMessage(await dispatchOffload(methods, event.data));
};
