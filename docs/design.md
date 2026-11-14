# Hybro Design

This document describes where Hybro is going: its concepts, their names, and
the target architecture. It is the target, not a description of today's code.

- How the code works today:
  [backend/docs/System-Architecture.md](../backend/docs/System-Architecture.md)
  and
  [frontend/docs/System-Architecture.md](../frontend/docs/System-Architecture.md).
- The order of work: [roadmap.md](roadmap.md).

Hybro moves toward this design in small steps. Routes and MCP tools that exist
today keep working while it does.

## The idea

Hybro is infrastructure for multi-agent workflows. It runs steps and never
plans them.

A **step** is one prompt from a source, sent to one agent, with its result and
its score.

| Who | What it does |
|---|---|
| A source | Decides what to do and writes every prompt. Planning happens here, outside the core. |
| The core | Runs each step: finds the agent, checks whether the call may go, sends it, records what happens, scores the result, and replies. |
| The LLM helper | Optional. It may pick an agent when routing cannot decide, and judge a result for a score. It never writes or changes a prompt, and it never adds a step. |

A source is anything that asks for work: your own agent or code over HTTP, a
coding agent over MCP, or the bundled Room chat with its orchestrator. The
orchestrator plans and then sends steps like any other source. The core treats
every source the same way.

## Concepts and names

A record is a kind of stored item with its own identity. Each record has
exactly one module that may write it. Every other module reads it or asks that
module.

| Name | What it is | Written by | Name in today's code |
|---|---|---|---|
| Session | One piece of work: its steps and everything that follows from them. | `session` | room, session (`rooms`, `sessions`) |
| Step | One prompt from a source for one agent, stored once, with its result. It can point at an earlier result or a file in the same Session. | `session` | a `/sessions` message with its `client_request_id` |
| Agent call | One attempt to send a step to an agent. A retry adds another. | `execution` | run, agent call (`orchestrator_runs`, `orchestrator_agent_calls`) |
| Question | Something that must be answered before the work continues. | `approval` | HITL (`hitl_requests`, `hitl_interactions`) |
| Event | One line in a Session's log. Every part of the path adds one, and a source reads its reply from here. | `events` | room event (`room_events`) |
| Score | The outcome of one Standard on one Step: pass or fail. | `evaluation` | new |
| Agent | An agent Hybro can reach: its card, address and protocol. | `agent` | agent (`agents`) |
| Group | A named set of agents. | `agent` | agent group (`agent_groups`) |
| Standard | A requirement a step's result must meet, such as a time limit or a required field. It attaches to a label the source sends, or to an agent. | `evaluation` | new |
| Rule | One line set by the instance owner. There are three kinds: route, access and check. | `policy` | new |
| Source | One thing that asks for work. It names itself, and the name is not verified. | Source API | client (`client_label`) |

Three more words:

- **Owner:** the person who runs the instance.
- **Panel:** the pages of the Hybro app where the owner sees what needs them
  and answers.
- **Turn:** a word for the source side, such as one round of an orchestrator's
  thinking or a chat message with its answer. The core has no turns, only steps.

A Question is one of two kinds:

| Kind | Example | Who answers |
|---|---|---|
| Permission | "May the agent push to `main`?" | The owner, in the Panel. The owner can grant one source the right to answer these too, for setups where the user and the owner are the same person. |
| Information | "Which branch should I fix?" | The source that wrote the prompt. |

Naming rules:

- New code, new routes and new documents use these names.
- Existing code keeps its names until the roadmap phase that migrates its
  module. Each module is renamed in its own change; there is no single large
  rename.

## Target architecture

```text
sources    Room chat with its orchestrator | your agent or code | coding agents
               |
           Source API         open a session, send a step, follow events,
               |              answer a question, cancel
               v
           session ---> routing     which agent?
               |------> policy      may this call go? allow, deny, or ask
               |------> approval    hold a question until it is answered
               v
           execution                run one agent call to the end
               |
           Protocol adapter ---> agents (A2A today)

foundation        events, agent, llm, scheduler, settings, dal
beside the path   evaluation reads events and writes scores
```

| Layer | Module | Job | Owns |
|---|---|---|---|
| Door | Source API | The one API every source uses. The bundled orchestrator has no private path. | Source |
| Step path | `session` | Stores each Session and Step. A small function with no I/O decides what happens next: route, check, ask, start a call, or finish. | Session, Step |
| Step path | `routing` | Finds the agent for a step and fits the prompt to that agent's input. It never rewords the prompt. | nothing |
| Step path | `policy` | Stores the owner's rules and answers from them. | Rule |
| Step path | `approval` | Holds each Question until it is answered, by the owner in the Panel or by the source through the Source API. | Question |
| Step path | `execution` | Runs one Agent call to the end: queue, send, wait, retry, recover, cancel, and take in the result. | Agent call |
| Foundation | `events` | Keeps the log of every step, streams it live to sources and the Panel, and gives other modules publish and subscribe for work that does not finish at once. | Event |
| Foundation | `agent` | Registry, discovery, search and groups. | Agent, Group |
| Foundation | `llm` | The optional helper. It picks an agent for `routing` and judges a result for `evaluation`. | nothing |
| Foundation | `scheduler` | The queue and limits for running calls. Timers that start work come later. | nothing yet |
| Foundation | `settings` | Instance-level configuration, set by the owner. | the configuration file |
| Foundation | `dal` | Database connections and generic clients. Each module keeps its own repository on top of it. | nothing |
| Beside the path | `evaluation` | Reads Events, compares each step with the Standards a user set, and writes a Score. It shows which step of a workflow failed. | Standard, Score |

### One step, from request to reply

1. A source sends a step through the Source API: a prompt, optional labels, and
   optionally the agent to use.
2. `session` stores the Step, with a request id so that a retry does not run it
   twice. The send returns at once with a step id.
3. `session` asks `routing` which agent gets the step.
4. `session` asks `policy` whether the call may go. The answer is allow, deny
   or ask.
5. On "ask", `approval` holds a Question and the Session waits until it is
   answered.
6. `session` starts an Agent call in `execution`, which puts it in the queue.
7. `execution` sends the prompt, unchanged, through the Protocol adapter.
8. The agent's result returns to `execution`, then to `session`. If the agent
   asks a question instead, 5 runs again.
9. `session` writes the result as an Event. The source, which follows the
   Session's events, receives it as the reply.
10. `evaluation` reads the Events and writes a Score. This happens after the
    reply and does not delay it.

### How routing finds the agent

Routing goes from the most certain answer to the least certain:

1. The agent the source named. It is always used.
2. The owner's route rule, such as "all CI events go to Agent X". A rule
   matches on the source and on the labels the source sends with a step.
3. Search and match in the agent registry.
4. The LLM helper picks, only when more than one agent still fits.

Each step records how its agent was chosen. When no agent fits, or the named
agent is not allowed, the step fails back to the source with a reason. It is
never sent to a different agent silently.

| Kind of rule | Question it answers | Where it runs |
|---|---|---|
| Route | Where does this step go? | Inside routing, only when the source names no agent |
| Access | Who may use what? | Inside routing, as a filter on the agents it may choose |
| Check | May this call go now? | After routing. "Ask" pauses the Session. |

### Labels

A source can send labels with a step. A label is a short key and value, such
as `team=billing` or `kind=ci`. The owner chooses the labels their rules match
on, and a source sends the ones it wants a rule to match.

- There is no list of labels to define first. A label exists when a rule or a
  source uses it. A label that no rule matches is stored on the step and
  otherwise ignored.
- Three keys are predefined, because evaluation groups results by them:
  `workflow` is the name of the whole piece of work, `step` is this step's role
  in it, and `version` is the version of the workflow's prompts. Every other
  label means only what the owner's rules make it mean.
- Route rules are an ordered list, and the first rule that matches wins.
- Hybro limits only the size and the number of labels.

### How modules talk to each other

| Way | Used when | Example |
|---|---|---|
| Direct call | The caller needs an answer to continue. Each module offers one public interface for this. | `session` asks `routing` which agent, and tells `execution` to start the call. |
| Event | Something already happened, and the sender does not care who listens. | "Step finished" is read by `evaluation` and by the live stream to sources. |

The design waits in three places only. The source gets a step id at once and
reads the result later. An agent call waits in a queue. A question waits for an
answer. Storing, routing and checking are direct calls in order.

### Design rules

1. One writer per record.
2. Only `session` decides what happens next in a step. Other modules do one job
   and report back.
3. Every source uses the same Source API.
4. The core never plans and never rewrites a prompt. The LLM helper may only
   pick and judge.
5. Only the Protocol adapter talks to agents.
6. An event never decides what happens next in a step. If every listener were
   removed, the step would still finish.
7. The records are the truth, and a notification is only a wake-up. A module
   saves its result before it tells another module.
8. The Session and its Events are the history. Choosing and shortening what an
   agent should know is the source's job. The core only passes along,
   unchanged, what a step points to.

## The Panel

The Panel has one page for each concept. A page's layout is decided when the
work on it starts. This list fixes which pages exist and what belongs on each.

| Page | What it shows | Today |
|---|---|---|
| Overview | What needs the owner now, what failed, and recent activity. Its Inbox is the one home for open Questions. | Exists |
| Chat | Room chat, where a person places an order with the bundled orchestrator. | Exists |
| Sessions | Every Session from every source, Room chat included. One Session shows its Steps: the prompt, the agent and how it was chosen, the result, the timing, the Questions and the Scores. | Exists under the name Inbox, for API and MCP clients; the step view is new |
| Network | Agents and Groups. | Exists |
| Rules | The ordered route rules, and the check rules. | New |
| Evaluation | Standards, and checking one against a run. | New |
| Connect | How a source connects, over the API or MCP. | Exists |
| Settings | Model provider, configuration and system. | Exists |

## Today's modules

Each module in today's code has one of three statuses:

- **Stable:** it stays as it is. Contribute freely.
- **Changing:** the target differs from today. Open an issue before a larger
  change.
- **Frozen:** it will move out of the core. Bug fixes only.

| Today's code | Status | In the target |
|---|---|---|
| `agent`, `local_agents` | Stable | `agent` |
| `approval` | Stable | `approval`; a Question gains a kind |
| `a2a_adapter` | Stable | The Protocol adapter |
| `dal`, `common`, `models` | Stable | Foundation and shared contracts |
| `instance_settings` | Stable | `settings` |
| `diagnostics`, `room_files` | Stable | Support: logs and files |
| `jobs` | Stable | `scheduler` |
| `MCP/server.py` | Stable | A source that uses the Source API |
| `default_agents` | Stable | The bundled agents |
| `api_gateway` | Changing | Hosts the routes of the Source API and the Panel |
| `session` | Changing | Its `/sessions` routes grow into the Source API |
| `conversation` | Changing | The store behind `session` |
| `chat` | Changing | Room chat, a bundled source |
| `delivery` | Changing | `events` |
| `llm_gateway` | Changing | `llm`, the helper |
| `execution`: Run lifecycle, cancellation, recovery, the `passthrough` profile | Changing | `execution`, which runs one agent call |
| `execution`: the model loop of the `fast` and `ultimate` profiles | Frozen | Moves out and becomes the bundled orchestrator, a source |
| `context_memory` | Frozen | Moves out with the orchestrator |

Nothing is deleted before its replacement has shipped.

## Open questions

- How a source is identified. For now a source names itself, as `client_label`
  does today. We will discuss this in the future.

## Changing this document

Change it by pull request. For a change to a concept, a name or a design rule,
open an issue first.
