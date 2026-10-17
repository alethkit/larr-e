# Red-team findings: LARR-E at `main` (dba60f3)

This is a fresh-session problem hunt (17 Oct 2026), written to find where LARR-E fails in real use. **Nothing was fixed.**
Findings are ranked worst first. Each one gives the file and line, a concrete failure, and a tag:

- **demo**: likely to go wrong or mislead during the pitch.
- **safety**: weakens the "a human approves hardware changes, and LARR-E knows what the reactor is doing" story.
- **polish**: wrong, but low impact this weekend.

How the evidence was gathered:

- Every claim about Pioreactor's behaviour was checked against the source at tag `26.11.0` (commit `89dc54e`), with the file named.
- The items marked "reproduced" were run offline against LARR-E's own classes, with no hardware and nothing sent to `pioreactor.local`.
- The offline suite passes: 26 passed, 1 skipped.
  - Inside Claude Code's sandbox the tests need two workarounds: they must run from a copy without `.env.example` (pytest can't stat the hidden file), and outside the sandbox (they bind local ports).

## Is the whole idea broken?

No. Every finding below is fixable. The core design is backed by the source:

- The leader runs the profile on its own scheduler (`actions/leader/experiment_profile.py`).
- Stopping a profile early kills the jobs it started, matched by `job_source` (lines 1129–1140).
- `stop_experiment`'s lookup by `profile_filename` matches what the leader stores, as plain text (`utils/job_manager.py:139`).

There is one root cause behind findings 1, 4, 5 and 6. **LARR-E records what it intended and never reads back what happened.** Its view comes from four places:

- plan status, worked out from the laptop's clock;
- live state, which keeps the last value it heard;
- MQTT commands, which are fire-and-forget;
- "ok" from the leader, which only means the process was spawned.

The leader already exposes what's needed to check: running jobs per unit (`GET /api/workers/<unit>/jobs/running`), running profiles, and the profile's published `start_time_utc`. So this is a gap in the implementation, not in the idea. Until it is closed, though, "triage that knows what's intentional" is built on guesses.

---

## 1. A plan's status is a guess, and the final Slack message states it as fact (demo, safety)

**Where:**

- `experiment.py:169-176` (`refresh_status`)
- `tools.py:412-414` (started_at)
- `tools.py:239-245` (what counts as ok)
- `slack_bot.py:428` (final message)

**What happens:**

- **"ok" from the leader means "the process didn't die within 1 second".** Pioreactor's `pio_run` (`web/tasks.py:603-655`) returns `{"ok": True}` once the child process is still alive after `grace_s=1.0`.
  - Only a failure inside that second comes back as `ok: false`. That is what LARR-E's `refusals` check looks for.
  - On a Pi 3 A+ a `pio run` process takes longer than a second just to import. So "job already running" (`background_jobs/base.py:1091`, `JobPresentError`) and most other startup failures will very likely surface after LARR-E has reported success. This is inferred from the source and Pi 3 speed; it hasn't been checked on `pio01`.
  - This applies to `start_job`, `dose`, and the `experiment_profile` job itself.
- **A failed start inside a profile is only logged.** `wrap_in_try_except` (`experiment_profile.py:72-85`) logs it and carries on, so the profile keeps going.
  - If a unit isn't assigned to the experiment, the profile skips that unit's actions with a warning (`experiment_profile.py:1082-1091`) and finishes immediately.
- **A plan becomes "finished" when the laptop's clock passes `started_at + duration`.** Nothing asks the leader whether the profile ran, failed, or was stopped in the UI.
- **The Slack progress loop then posts** ":checkered_flag: *name* finished after 5 h. Pioreactor stopped its jobs on schedule." Nothing checks that sentence.

**Scenario:** someone has started stirring from the Pioreactor UI. You run `larr-e exec "run pio01 at 30 degrees and 300 rpm for 5 minutes"`.

1. The profile's stirring start hits `JobPresentError`, after the grace window, so nobody sees it.
2. The reactor stirs at the UI's speed, not 300.
3. LARR-E shows "running" and posts progress with "target 300".
4. At 5 minutes it announces that everything stopped on schedule.
5. Meanwhile the profile's `stop` at t=5 min kills the UI's stirring job, which the user never asked LARR-E to touch.

**Also:** a profile that dies at once (missing plugin, unassigned unit) still leaves a plan marked "running" for its full duration, up to 72 h. That blocks any new plan on the unit and tells triage which jobs "should be running".

## 2. Normal events raise critical alerts, and triage is told they are unexpected (demo)

**Where:**

- `listener.py:162-180` (rules)
- `config.py` `AnomalyConfig` (temperature band 22–42 °C)
- `experiment.py:178-188` (`expected_state`)
- `tools.py:413` (`started_at`)

Reproduced with `AnomalyDetector`:

| Event | Result |
|---|---|
| First temperature reading of 21 °C when a plan starts heating | `critical: Temperature 21.00 °C outside safe band 22.0-42.0 °C` |
| Stirring target lowered on purpose from 500 to 100 RPM | `critical: Stirring stall … Stir bar decoupled or fan failure?` |
| Thermostat target lowered from 37 to 34.5 °C | `critical: Temperature drop … Heater fault or vial removed?` |

**Stirring at the end of a plan.** The stall alert when stirring stops was already seen on 17 Oct, after a stop from the UI (CLAUDE.md). The source shows why: `stop_stirring` publishes `measured_rpm=0` (`background_jobs/stirring.py:455-459`). At the end of a plan, triage is then given the wrong expected state:

- `started_at` is stamped on the laptop only *after* the upload, the run request and the task poll have all returned.
- So the laptop's end time is always later than the moment the Pi's profile stops the jobs (the Pi's scheduler starts counting earlier).
- During that gap, `expected_state` still says `jobs_should_be_running: [stirring, …]` and `target_rpm: 300`.

**Other effects:**

- The Slack alert goes out *before* triage runs (known item 3 in CLAUDE.md).
- **Room temperature.** If the venue is below 22 °C, the 5-minute demo will probably open with a red critical alert. If the Pi publishes `measured_rpm=0` when the profile stops stirring, it will also close with one.
- The safety limits let you set 20–45 °C, but the alert band is 22–42 °C. A plan at 43 °C, or at 20–21 °C, raises a critical alert every 60 s (the cooldown) for the whole run, and each one starts an LLM triage call.

## 3. Any exception in the MQTT callback kills the listener for good (demo, safety)

**Where:** `listener.py:353-362`, `listener.py:151-158`

**What happens:**

- paho-mqtt 2.1.0 re-raises exceptions from `on_message`, because `suppress_exceptions` is False by default (`paho/mqtt/client.py:4502-4506`).
- `loop_forever` then exits, `run()` prints "Listener stopped." and the process ends.
- `on_message` does several things that can raise, with no try/except around them:
  - the OD rule divides by the rolling median;
  - `float(value)`;
  - writing `live_state.json`;
  - the recorder's SQLite insert;
  - appending to `alerts.jsonl`.

**Reproduced:** six OD readings of 0.0 followed by 0.5 raises `ZeroDivisionError: float division by zero` from `delta / baseline` in the spike message.

**Scenario:**

1. A blank vial, or a calibrated OD clamped to 0, gives a median of 0. Or the disk fills up, or the recorder hits "database is locked".
2. The listener dies mid-experiment.
3. No more alerts, no more recording.
4. `larr-e status` and the Slack progress updates keep showing the last snapshot as if it were live (finding 4).

Nobody is told that monitoring has stopped. For a tool whose job is "keep an eye on it", this is the worst kind of failure.

## 4. Stale state looks live (demo, safety)

**Where:**

- `listener.py:90-91`, `listener.py:193-204` (LiveState)
- `tools.py:629-644` (`get_live_status`)
- `tools.py:508-534` (`get_series`)
- `slack_bot.py:431-444` (progress text)

**What happens:**

- When a Pioreactor job stops cleanly, it clears its retained settings by publishing empty payloads (`background_jobs/base.py:1057-1070`).
  - `parse_message` returns None for an empty payload, so LiveState never removes the metric.
  - Reproduced: after the clear, `live_state.json` still holds `stirring: 500`.
- `live_state.json` stays on disk after the listener dies. `get_live_status` returns it as `available: True` and never checks its age.
- `get_series` anchors "the last N minutes" on the newest stored reading, not on the current time. If OD stopped three hours ago, "OD over the last 10 minutes" returns data that is three hours old. The only hint is `window_end`.

**Scenario:** the plan ends and stirring stops. The final Slack message reads "stirring 300 rpm (target 300)", and `/larr-e status` shows 300 RPM indefinitely. The agent is told "never state a reading you have not fetched", and it fetched this one, so it repeats it.

## 5. Commands can go to the wrong experiment and still report success (safety)

**Where:** `tools.py:120-131` (`resolve_experiment`), `tools.py:203-205`, `tools.py:296-334`

**What happens:**

- `resolve_experiment` prefers the experiment named in `live_state.json` over the leader's assignment (`GET /api/workers/<unit>/experiment`), which is the source of truth.
- That file can be stale: the listener is down, or the unit was moved to a new experiment and no telemetry has arrived yet.
  - Retained values from a job that wasn't shut down cleanly also stay on the broker under the *old* experiment.
  - They are replayed to the listener on every reconnect and overwrite `experiment`.
- `set_target_temperature`, `set_stirring_rpm` and `stop_job` publish to `pioreactor/<unit>/<that experiment>/…/set`.
  - MQTT has no acknowledgement from the receiver.
  - A job only listens on its own experiment's topic, so nothing happens.
  - The tool still returns `ok: True`, and `stop_job` adds `"state": "disconnected"`.

**Scenario:** "stop the heater on pio01" is approved, reported done, and the heater keeps running.

The same silent success happens in other cases:

- a set sent while no job is running (the tool's note admits this for temperature only);
- `target_temperature` sent while the automation isn't `thermostat`.

REST starts are protected here: the leader returns 404 if the unit isn't assigned (`web/api.py:1237-1243`), so the mismatch is caught. MQTT sets and stops aren't.

## 6. `stop_experiment` forgets a plan the Pi is still running (safety)

**Where:** `tools.py:431-454`, `tools.py:406-411`, `leader_api.py:59-69`

**What happens:**

- If the leader can't be reached (the flaky mDNS noted in CLAUDE.md), `stop_experiment` still marks the plan `stopped` and saves it.
- **Reproduced** with the leader pointed at a closed port:
  - The first call returns `ok: False`, with 4 errors, and the plan is now "stopped".
  - The second call is refused: "No running experiment plan on u".
- The profile keeps heating and stirring on the Pi. LARR-E can no longer stop it with `stop_experiment`. `start_experiment` would now allow a second profile on the same unit, and the source of `start_experiment` says two profiles "interfere with each other".
- The reverse also happens. If `run_profile` times out after the profile actually started, the plan is marked `failed`. The `LeaderAPI` poll gives up after 30 s and itself says "It may still complete". The profile then runs with no plan behind it: no expected state, no updates, and `stop_experiment` refuses.
- The comment at `tools.py:445` says that also stopping each job by name "is harmless". It isn't: it stops a same-named job someone started from the UI. Pioreactor's own early stop only kills jobs whose `job_source` is the profile.

## 7. Errors outside the tool layer leave Slack stuck after a human approved (demo)

**Where:** `tools.py:199-200`, `agent.py:342`, `slack_bot.py:190-199`, `openai_compat.py:124`

**What happens:**

- `_execute` catches only `ToolError`, `TypeError` and `ValueError`. Anything else propagates out of `agent.ask`.
- A wrong MQTT password is one example. paho's `publish.single` raises `MQTTException` from its connect callback (`paho/mqtt/publish.py:85-91`), but `_publish` only catches `OSError`.
- Other examples:
  - a `KeyError` from `int(run["job_id"])`;
  - `ConnectionResetError` from the hf/gemini backend (only `URLError` and `TimeoutError` are caught).
- In Slack, `_answer` catches only LLM API errors. `guarded` logs the exception, so the "🧠 working on it…" message is never updated.

**Scenario:** someone clicks Approve on a temperature change. The thread says "working on it…" forever. The audit log says "approved". Nobody knows whether the change happened.

In the CLI the same failure is a traceback.

## 8. The approval gate is weaker than the pitch claims (safety)

**Where:**

- `agent.py:278`
- `config.py` `SlackConfig`
- `slack_bot.py:79-80`, `slack_bot.py:308-320`
- `api_server.py:195-218`, `api_server.py:286-288`

**What's weak:**

- **It fails open.** `LarreAgent`'s default `confirm` is `lambda name, args: True`. Every current caller passes a real one, but any new front end, or a test copied into a script, approves hardware actions automatically.
- **Slack defaults:** there's no approver list, and self-approval is allowed. The person who asked clicks Approve on their own request, so the "human approval" is a confirmation click.
- **HTTP API / 3DS:**
  - Anyone with the token can approve.
  - The token travels in the URL (`?k=`) over plain HTTP on `0.0.0.0`, on a shared hotspot.
  - `by` is whatever the client sends, and it goes into the audit log as `decided_by`.
  - The API approval doesn't show the profile YAML for `start_experiment`. Slack does.
- **The card can hide the unit.** `format_args` drops arguments that are `None`. When the model leaves `unit` out and there's no default, the card shows no unit. The tool then resolves it at execution to "the only unit streaming", so the approver doesn't see which reactor will move.
- Slack and the API have separate approval queues (already listed in CLAUDE.md).

## 9. Doses: an overflow guard is bypassed, and the "Dilute" preset is unsafe (safety; demo via the 3DS)

**Where:** `tools.py:251-276`, `api_server.py:79-83`, `config.py` (`LARRE_PRESETS` default)

**What happens:**

- Pioreactor refuses a manual `add_media` or `add_alt_media` that would overfill the vial, but it only checks when `ml` is given (`web/unit_api.py:1316-1345`: `ml = options.get("ml"); if ml is None: return False`).
  - LARR-E's `dose(duration_s=…)` sends `duration` instead, up to 30 s, which **skips that check**.
  - Nothing in LARR-E limits the total over several doses.
- The "Dilute 0.5 ml" preset is in the default first four on the 3DS.
  - It adds media first, then removes waste.
  - Each step needs its own approval. Approving the first and denying, or letting expire, the second leaves a net addition.
- On `pio01`, which has no pump, a dose "succeeds" and moves nothing (finding 1). If someone presses Dilute during the demo, LARR-E reports a dilution that didn't happen.

## 10. The plan file has no locking, and corruption silently deletes every plan (safety, polish)

**Where:** `experiment.py:204-231`

**What happens:**

- Several processes read, modify and write `experiment_plans.json`:
  - the Slack bot's progress loop;
  - Slack and API agent threads;
  - the CLI;
  - listener triage, through `PlanStore.get()`, which can write.
- They all use the same `.tmp` filename, so two writers can interleave. A write can also be lost: process A's `last_update_at` overwrites B's new plan.
- A corrupt file is treated as "no plans" (`json.JSONDecodeError` returns `{}`). **Reproduced.**
- The next `put()` then saves only one unit, wiping the rest. After that:
  - triage loses `expected_state`;
  - the conflict check lets a second profile start on a unit whose first profile is still running.

## 11. Open models: the string "false" becomes True (polish)

**Where:** `tools.py:359-360`

`bool("false")` is True. Reproduced: `plan_experiment(od_reading="false")` gives `od_reading=True, growth_rate=True`.

Claude follows the schema, but `hf` and `gemini` models often send booleans and numbers as strings. Numbers are mostly caught: a `TypeError` from comparing a string with a number is reported back to the model. The approval card's YAML would show the extra job, so a careful approver could still catch this one.

## 12. Data quality in recording and charts (polish)

**Where:** `listener.py:233-254`, `tools.py:47-54`, `tools.py:508-534`, `schema.py`

- **Duplicate rows.** The broker replays retained readings on every listener reconnect (the 17 Oct hotspot drop, for example). `measured_rpm`, `temperature`, `od1`…`od4` and `growth_rate` are all retained published settings (`background_jobs/base.py:766-781`). The recorder inserts them again with their old timestamps.
- **OD channels are mixed.** `get_series("od")` doesn't filter by channel or angle.
  - This is harmless with the default `1=REF, 2=90` (one channel published), but it interleaves angles on a setup with two OD channels.
  - With an active OD calibration, `od1` carries calibrated OD (`od_reading.py:1620-1628`, `CalibratedODReading`), while the 0.1 absolute spike floor assumes one scale.
- **`od_filtered` fails on the laptop.** The recorder's schema has no `od_readings_filtered` table, so that metric always errors on a laptop database.

## 13. Times are shown in UTC without saying so (polish)

**Where:** `slack_bot.py:92`

- `/larr-e status` prints `last_seen[11:19]`, which is UTC, with no label.
- The team is on UTC+1 (BST), so "last seen 12:00:00" looks like local time and is an hour off.
- The plots and the agent's context are labelled UTC, so they are fine.
- LARR-E never reads the Pi's log (UTC+1), so there is no conversion bug in the code.
  - Anyone comparing alerts with `/var/log/pioreactor.log` by hand, as the pitch's stall claim does, must convert.

## 14. Smaller things (polish)

- **`api_server.py:326`:**
  - `?n=0` returns every alert, because `[-0:]` is the whole list.
  - `?n=-3` returns all but the first three.
  - Both reproduced through `list_recent_alerts`.
- **`api_server.py:364-367`:** any exception other than `LookupError`, `ValueError`, `ToolError` or `JSONDecodeError` drops the connection with no response. A `sqlite3.OperationalError` from a missing table is one.
- **`tools.py:494`:** `with sqlite3.connect(...)` commits but doesn't close, so every query leaks a connection.
- **`tools.py:361-367`:**
  - `update_every_h=0` is rejected instead of meaning "no updates". Only leaving the argument out does that.
  - A 2-minute run silently drops its updates.
- **`tools.py:650-651`:** one corrupt line in `alerts.jsonl` makes `list_recent_alerts`, `/larr-e alerts` and the 3DS alert view fail.
- **`listener.py:99`:** when a payload has no timestamp, the laptop's clock is used. Readings from the Pi and the laptop are then compared on two clocks, and the Pi has no RTC.

---

## Suggested order before the demo (not done)

1. Read back from the leader before stating facts:
   - A plan is "running" only if the profile's `job_id` is running.
   - It is "finished" only if the profile ended normally.
   - A started job counts only if `GET /api/workers/<unit>/jobs/running` lists it a few seconds later.
   - Stamp `started_at` from the profile's `start_time_utc`.
   - This addresses findings 1, 2 (end-of-plan timing), 6 and 9 (the no-pump dose).
2. Wrap `on_message` in try/except, add a heartbeat or age to `live_state.json`, and drop a metric when its retained topic is cleared. This addresses findings 3 and 4.
3. Resolve the experiment from the leader before trusting `live_state.json`. This addresses finding 5.
4. Make the alert rules aware of the plan: allow a ramp-up after starts and setpoint changes, align the band with the safety limits, and triage before posting. This addresses finding 2.
5. Before the pitch, set `LARRE_PRESETS` without `dilute`, and set `LARRE_SLACK_APPROVERS`. These are config only, for findings 8 and 9.
