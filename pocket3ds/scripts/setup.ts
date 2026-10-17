/** Link the pinned runtime submodule as @pocketjs/framework (same layout as Pocket Doc). */
import { existsSync, mkdirSync, rmSync, symlinkSync } from "node:fs";
import { dirname, relative, resolve } from "node:path";
const root = resolve(import.meta.dir, "..");
for (const [name, target] of Object.entries({
  "@pocketjs/framework": "runtime", "solid-js": "runtime/node_modules/solid-js",
  "@types": "runtime/node_modules/@types", "bun-types": "runtime/node_modules/bun-types",
})) {
  const source = resolve(root, target), destination = resolve(root, "node_modules", name);
  if (!existsSync(source)) throw new Error(`Missing ${target}: run \`git submodule update --init pocket3ds/runtime\` and \`bun install --cwd runtime\``);
  mkdirSync(dirname(destination), { recursive: true });
  rmSync(destination, { recursive: true, force: true });
  symlinkSync(relative(dirname(destination), source), destination, "dir");
}
console.log("Linked runtime into node_modules");
