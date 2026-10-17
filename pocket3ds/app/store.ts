import { batch, createSignal } from "solid-js";
import { offload } from "@pocketjs/framework/offload";
import { onFrame } from "@pocketjs/framework/lifecycle";
import { BTN } from "@pocketjs/framework/input";
import { DEFAULT_PRESETS, type JobState, type Snapshot } from "../shared/protocol.ts";

const SNAPSHOT_EVERY = 60; // frames (~1 s)
const JOB_EVERY = 45;

/** On the 3DS: A = CIRCLE, B = CROSS (hosts/3ds/src/input.c). */
const A = BTN.CIRCLE, B = BTN.CROSS;

export function createLarre() {
  const io = offload();
  const [snap, setSnap] = createSignal<Snapshot | undefined>();
  const [answer, setAnswer] = createSignal<string[]>(["Pick a request on the touch screen."]);
  const [status, setStatus] = createSignal("Connecting to the bridge...");
  const [job, setJob] = createSignal<string | undefined>();
  const [selected, setSelected] = createSignal(0);
  const [deciding, setDeciding] = createSignal(false);
  const [online, setOnline] = createSignal(false);
  const inflight = new Set<string>();

  const presets = () => snap()?.presets ?? DEFAULT_PRESETS;
  const approval = () => snap()?.approval ?? null;

  /** One outstanding request per key; offload capacity is small. */
  function call<T>(key: string, method: string, data: unknown, done: (value: T) => void, failed?: (error: string) => void) {
    if (inflight.has(key)) return;
    const ticket = io.request(method, JSON.stringify(data), result => {
      inflight.delete(key);
      if (!result.ok) { failed ? failed(result.error) : setStatus(result.error.slice(0, 44)); return; }
      try { done(JSON.parse(result.value) as T); } catch { setStatus("Malformed reply from bridge"); }
    });
    if (ticket) inflight.add(key);
  }

  function run(index: number) {
    const preset = presets()[index];
    if (!preset || job() || approval()) return;
    batch(() => { setSelected(index); setAnswer([`${preset.label}...`]); setStatus("Sending..."); });
    call<{ job: string }>("preset", "larre.preset", { name: preset.name }, r => {
      batch(() => { setJob(r.job); setStatus("Thinking..."); });
    }, error => batch(() => { setAnswer([error.slice(0, 56)]); setStatus("Request failed"); }));
  }

  function decide(decision: "approve" | "deny") {
    const a = approval();
    if (!a || deciding()) return;
    setDeciding(true);
    call("decide", "larre.decide", { id: a.id, decision }, () => {
      batch(() => { setDeciding(false); setStatus(decision === "approve" ? "Approved" : "Denied"); });
      refresh();
    }, error => batch(() => { setDeciding(false); setStatus(error.slice(0, 44)); }));
  }

  function refresh() {
    call<Snapshot>("snapshot", "larre.snapshot", {}, s => {
      batch(() => { setSnap(s); if (!job()) setStatus(s.approval ? "Approval needed" : "Ready"); });
    });
  }

  function pollJob() {
    const id = job();
    if (!id) return;
    call<JobState>("job", "larre.job", { id }, j => {
      if (j.state === "running") { setStatus(j.waiting ? "Waiting for approval..." : "Thinking..."); return; }
      batch(() => { setAnswer(j.lines.length ? j.lines : ["(no answer)"]); setJob(undefined);
        setStatus(j.state === "done" ? "Done" : "Error"); });
    });
  }

  let frames = 0, previous = 0;
  onFrame(buttons => {
    frames++;
    if (frames % 30 === 0 && online() !== io.connected()) setOnline(io.connected());
    if (frames % SNAPSHOT_EVERY === 1) refresh();
    if (frames % JOB_EVERY === 0) pollJob();
    const pressed = buttons & ~previous;
    previous = buttons;
    if (!pressed) return;
    if (approval()) {
      if (pressed & A) decide("approve");
      else if (pressed & B) decide("deny");
      return;
    }
    const count = presets().length, i = selected();
    if (pressed & BTN.RIGHT && i % 2 === 0 && i + 1 < count) setSelected(i + 1);
    if (pressed & BTN.LEFT && i % 2 === 1) setSelected(i - 1);
    if (pressed & BTN.DOWN && i + 2 < count) setSelected(i + 2);
    if (pressed & BTN.UP && i >= 2) setSelected(i - 2);
    if (pressed & A) run(selected());
  });

  return { snap, answer, status, job, selected, presets, approval, online, deciding, run, decide };
}

export type Larre = ReturnType<typeof createLarre>;
