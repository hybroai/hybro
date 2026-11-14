# Hybro Roadmap

This is the order in which Hybro moves toward the [design](design.md).

## How we move

- Structure first, features after. We make today's code smaller and cleaner,
  then move parts out of the core, then give the remaining modules their target
  names, and only then build what does not exist yet.
- One small change at a time. There is no rewrite.
- Each change ships on its own, and Room chat keeps working throughout.
- A change adds the new path before it removes the old one. A route or MCP tool
  that exists today is marked as deprecated in one release and removed in a
  later one.
- The detailed plan for a phase is written when the phase starts, as issues.
- A finished change updates the System-Architecture documents, which describe
  today's code, and `design.md` if the target changed.

## Phases

| # | Phase | What it includes | What it gives | Status |
|---|---|---|---|---|
| 0 | Make the direction visible | `design.md`, this roadmap, and a short section in `CONTRIBUTING.md` and `AGENTS.md` that links to them. A quick start in the README for sending work from your own agent or code. | Contributors can see the target and which modules are stable, changing or frozen. A developer can run the loop from outside in a few minutes. | Now |
| 1 | Clean up and break down | An inventory of code to remove, agreed before anything is deleted. Removal of dead code and retired paths. `execution` is split into two parts behind one boundary: the part that runs agent calls, and the model loop. Behaviour does not change. | Smaller code, and pieces that can be changed one at a time. | Next |
| 2 | Move out | The model loop and `context_memory` leave the core and become the bundled orchestrator, a source. The `/sessions` routes grow into the Source API far enough to carry it: a step can point at earlier results and files, information questions go back to the source, several steps can run in one Session, and a source can follow events live. The new Room chat path runs beside the old one until it behaves the same. | The core only runs steps, and Room chat uses the same door as every other source. | Later |
| 3 | Migrate to the target modules | The remaining modules take their target names and boundaries, one module per change: `delivery` becomes `events`, `conversation` and `session` become `session`, `llm_gateway` becomes `llm`, `jobs` becomes `scheduler`, and `instance_settings` becomes `settings`. Code takes the concept names from `design.md`. Public routes keep working. | The code reads like the design. | Later |
| 4 | Build what does not exist yet | In this order: routing without a named agent (match, the LLM pick, owner route rules and labels) with the Rules page; evaluation by hand, with the step view on the Sessions page and the Evaluation page; owner check rules. | The new features, built once on the final structure. | Later |

After these: event sources such as CI, chat apps and notifications, and timers
that start work.

Phases 1 to 3 add no user features. The only new behaviour before phase 4 is
what the orchestrator needs in order to become a source.

## Where today's code already fits

The step path exists in a small form. `POST /sessions/messages` sends one
message to one named agent, returns a receipt without waiting, and uses
`client_request_id` so that a retry is safe. The owner approves in the app, and
the state is read from the event log.

It has four limits:

- A message is text only. Phase 2 removes this.
- A session runs one request at a time. Phase 2 removes this.
- The client polls for the result. Phase 2 removes this.
- The agent must be named. Phase 4 removes this.
