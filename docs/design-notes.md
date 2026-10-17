# Design notes: how LARR-E got here

This distils the design conversation (a long ChatGPT thread, 17 Oct) and the organisers' prompting
playbook. Decisions that stand are in `CLAUDE.md`. This file keeps the reasoning, the ideas we
parked, and the places where the earlier advice turned out wrong or unchecked, so nobody spends time
re-deriving them.

## 1. How the idea evolved

1. **A Nintendo 3DS as a bioreactor control panel.** What the hardware offers: two screens (state on
   top, controls below), a stylus for *drawing* a feed-rate or stirring profile instead of typing
   setpoints, physical buttons for acknowledging alarms, a clamshell you can carry round the lab.
   Stereoscopic 3D and the gyroscope add little. → Built later as `web/dashboard.html` and
   `pocket3ds/`, then **deprioritised**: it is a gimmick, not the product.
2. **The primary interface is chat** (Slack or Discord), with an agent babysitting the reactor. The
   3DS became one more front end.
3. **Twitch, in the spirit of "Claude Plays Pokémon"**, with a real culture that can't be reset. Best
   version: the audience *predicts* the growth curve, and predictions are compared with LARR-E's and
   the measured one. Viewers never send commands to hardware. → **Bonus only**, if time permits.
4. **The scientist's actual workflow** settled the product (see `CLAUDE.md`, "What LARR-E is for").
   It moved the priority from anomaly detection to *setting up a whole experiment from one message
   and reporting on it*.

## 2. Architecture discussions and where they landed

| Question | Conclusion |
|---|---|
| Build on Tau (a Python agent runtime) or keep the custom loop in `agent.py`? | Keep the custom loop for the hackathon; keep the tools independent so Tau could replace the loop later. Tau's real advantages (sessions, context management, event-triggered turns) don't help the demo. (The thread once mixed Tau up with Pi, a TypeScript agent runtime: Tau is Python, which is why it was considered.) |
| How many schedulers and interrupts? | No new hardware scheduler. Pioreactor jobs, automations and profiles own all timing. LARR-E wakes Claude on a human message, an alert, or a periodic update. One agent run per conversation thread at a time. |
| Is a Pioreactor job the same thing as an agent step? | No. A job is a long-running process; an agent turn is short. A tool call may *start* a job and return straight away, with the job carrying on by itself. |
| Pioreactor's MCP server: use it instead of our own tools? | Not adopted. Our tools enforce hard limits and approvals in Python, and the REST calls they make are the same ones MCP uses. The MCP server is still useful as a cross-check, and as the honest answer to "what does LARR-E add?" |
| Agent on the Pi or on a helper laptop? | Pioreactor on the Pi, LARR-E on the laptop: easier to develop, and if LARR-E dies the experiment keeps running. The cost is that the laptop doesn't have the Pi's history, which is why the listener now records its own. |
| Where does state live? | Measurements and history in Pioreactor. The leader has a SQLite database for history; each worker also has a small local store its jobs use. The plan in LARR-E's `runtime/`. Conversations in memory, and losing them is fine. No new database beyond the recorder. |
| Long experiments vs the context window | Keep experiment memory (plan, measurements, decisions) outside the model, and treat conversation memory as disposable. An 18-hour experiment shouldn't need an 18-hour conversation. |
| Jobs, automations, profiles: what are they, mathematically? | Job: a state machine. Automation: a feedback controller choosing actions from readings and a target. Profile: a time-indexed program of actions, with `if`, `when` and `repeat`, so a small reactive language. Restriction we chose: **one active profile per unit**. |

## 3. Ideas explored and parked

- **Datalog/Prolog.** Facts about the reactor, plus rules deciding when something needs attention,
  plus perhaps a model of permitted actions. Then pyDatalog, then dropped: a few Python conditions
  do the same job this weekend. It becomes worthwhile when the attention rules are numerous,
  user-written, or need explaining. Interesting future direction: *rules decide whether to wake the
  LLM*.
- **Event sourcing** (Kleppmann, "Turning the database inside out"). Separate *events* ("stirring
  stopped at 10:32"), *current state* ("stirring is stopped") and *derived facts* ("needs
  attention"). Derive facts from current state rather than retracting them by hand. Treat readings
  as known, stale or unknown by age. → Kept as a way of thinking, no event bus built.
- **Cybernetics.** It adds something only if it changes design decisions. It does, in three:
  1. **Don't duplicate feedback loops:** Pioreactor holds setpoints, Claude supervises.
  2. **A deviation isn't a failure:** investigate before acting.
  3. **Escalate only when needed.**

  There are three loops at three timescales: Pioreactor (seconds to minutes), LARR-E (minutes to
  hours), the scientist (hours to days). Good for one slide, not for jargon in the pitch.

## 4. Earlier advice that was wrong or unchecked

- **"The `run/<job>` MQTT path changed between releases."** Worse than that: current Pioreactor has
  no subscriber for it at all. Use REST. See `CLAUDE.md` decision 3.
- **"Use the profile dry-run as a pre-flight check."** Pioreactor's dry-run executes in real time,
  so an 18-hour profile logs for 18 hours. The leader validates profiles on upload instead.
- **"Stop the profile job to end an experiment."** That stops every profile in the experiment. Stop
  by job id instead. See decision 4.
- **"Use Pioreactor's mocks as a simulator."** They don't respond to control. See decision 6.
- **The MCP server's maturity and release timeline** as the thread stated them: not independently
  checked.
- **Hardware:** the board looked Pi Zero 2 W-sized in a photo, not the Pi 3B first assumed. The
  Pi Zero 2 W only does 2.4 GHz Wi-Fi, which matters for hotspots. Confirm with
  `/unit_api/versions/app` or `cat /proc/device-tree/model` on the Pi.
- **The first code review of LARR-E** was a review of untested code. Nothing counts as working until
  it has run against the Pi or the twin.

## 5. The organisers' prompting playbook (summary)

"Hackathon Prompting Playbook" (Rory Byrne, 16 Oct 2026). Five stages, with prompts for each:

1. **Talk through the design before code.** Ask for good design, not over-engineering. Decide
   what's out of scope, what to keep, wrap or rebuild from the old tool, and who the first user is,
   since the user decides the interface. Ask: "is this actually useful, or a thin layer over
   something people already have?"
2. **Get a skeleton running end to end.** The whole pipeline runs with one command, stubs where the
   real parts will go, the smallest real input from the start, and one end-to-end test that stays
   green. Compare against the old tool's answer where there is one. Build a stand-in for hardware you
   don't have, including how it fails.
3. **Build a piece at a time, test first.** The known answer comes from the team, not the model.
   The model doesn't edit a test to make it pass. Commit each working piece. When a session gets
   long or confused, record decisions in CLAUDE.md and start fresh.
4. **Go looking for problems in a fresh session.** A model defends the design it proposed. Hunt
   for results that are wrong but look right, swallowed errors, unit mistakes and silent defaults.
   Rank findings before fixing.
5. **Ship.** One command starts the tool with example data. A README written by people. Check the
   repo (public, MIT/Apache, no secrets, no pre-hackathon commits, works from a fresh clone). A
   3-minute pitch plus the 5 likeliest judge questions.

How this project measures up:
- **Followed:** the end-to-end test via the twin, a stand-in that fails like the real thing, and the
  repo checks.
- **Not followed early on:** stage 2. The repo grew many front ends before one real run.
- **Still to do:** a fresh-session problem hunt, a human-written README, and the pitch questions.
