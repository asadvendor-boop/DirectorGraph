# Architecture

## Design objective

DirectorGraph must remain autonomous without becoming an opaque prompt chain. The architecture therefore represents every creative decision as validated state, every external generation as a durable job, and every accepted asset as evidence linked to its originating Shot Contract.

## Control plane

The target cloud control plane is one FastAPI HTTP/Web function on Alibaba Function Compute. It owns public project reads, manifests, revisions, readiness, and task submission. The compiled React console is served from the same image and origin.

The current local implementation still supports SQLite/PostgreSQL and a SQL job worker. That path is local scaffolding. The final judging architecture must submit asynchronous Function Compute tasks with deterministic TaskIDs and persist durable state in OSS rather than relying on a database or always-on worker.

## Narrative production graph

`StoryPlan` is versioned StoryIR containing:

- characters and identity references;
- dramatic beats and emotional shifts;
- visual and audio rules;
- an ordered graph of Shot Contracts.

A Shot Contract is the compilation target. Renderers and evaluators receive the same contract, eliminating drift between what the planner requested and what the quality supervisor checks.

## Specialist-agent boundary

| Agent | Reasoning responsibility | Deterministic skills |
|---|---|---|
| Executive Showrunner | global objective, final arbitration | start/stop state transitions |
| Story Architect | beats, dialogue, character arc | JSON validation, duration checks |
| Visual Director | continuity bible, camera grammar | reference generation, seed handling |
| Production Manager | model route and repair choice | queueing, retries, idempotency, accounting |
| Continuity Supervisor | multimodal contract evaluation | thresholds, report persistence |
| Picture Editor | timing and final assembly decisions | FFmpeg normalization, captions, export |

The optional Studio MCP server exposes contract validation, budget estimates, edit-decision compilation, and semantic impact analysis. These operations are deterministic and auditable rather than delegated to free-form chat.

## Media plane

Storyboards, dialogue, rendered shots, repairs, and masters use a write-through asset store today. The production target is a private OSS bucket that stores both media and durable project JSON. Durable manifests must store OSS object keys; the web function generates short-lived signed URLs at response time.

## Failure handling

- external task IDs are retained by provider adapters;
- network calls retry only transport failures;
- provider errors fail the durable job with a visible event;
- stale running jobs can be recovered by the worker;
- a rejected shot consumes repair reserve before general production budget;
- the final editor only consumes accepted shots;
- all failures remain visible in the production trace and manifest.

## Semantic Patch Rendering

A completed project can receive a revision instruction plus explicit or inferred shot IDs. DirectorGraph mutates only those contracts, resets only those shot records, renders and validates replacements, and rebuilds the master from preserved and replacement clips. This transforms creative revision from whole-film regeneration into dependency-aware compilation.
