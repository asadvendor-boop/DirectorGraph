# Serverless Architecture

Status: target architecture with partial local implementation. The Function Compute task HTTP receiver, Serverless Devs template, and local deployment helper scripts now exist, but no live cloud deployment has been verified.

```text
Judge browser
    |
    v
Function Compute HTTP/Web function (APP_MODE=web)
    - FastAPI API
    - compiled React/Vite SPA
    - project reads
    - short-lived media URLs
    - task submission and status polling
    |
    | asynchronous task invocation with deterministic TaskID
    v
Function Compute Task function (APP_MODE=task)
    - production state machine
    - Qwen/Wan/HappyHorse/TTS calls
    - checkpoint recovery
    - Qwen multimodal inspection
    - FFmpeg assembly
    |
    +--> Qwen Cloud / Alibaba Cloud Model Studio
    |
    +--> private OSS bucket
```

## One Image, Two Modes

The root `Dockerfile` builds the React frontend and Python API into one image. Runtime behavior is selected with `APP_MODE`:

- `APP_MODE=web`: serve API, health/readiness, and the compiled SPA from one origin.
- `APP_MODE=task`: process a queued production task. The current local implementation accepts deterministic `dg-...` task IDs backed by SQL jobs and exposes the Function Compute task receiver at `POST /api/function-compute/tasks`; the final cloud implementation must complete the migration to OSS-backed task state across separate web/task functions.

The strict final gate builds `directorgraph:final-check` for `linux/amd64` and runs `scripts/smoke-docker-image.sh` against that same image. The smoke verifies web mode by requesting `/`, `/api/health`, and `/api/readiness`, then verifies task mode by loading `APP_MODE=task`, checking the task-only route guard, and parsing a deterministic Function Compute task payload.

## Durable State Target

The current application still uses SQL for projects, jobs, shots, and events. The required production target is OSS-backed durable state:

```text
projects/{project_id}/manifest.json
projects/{project_id}/read-model.json
projects/{project_id}/requests/original.json
projects/{project_id}/story/story-ir.v{n}.json
projects/{project_id}/shots/{shot_id}/contract.v{n}.json
projects/{project_id}/shots/{shot_id}/status.json
projects/{project_id}/shots/{shot_id}/attempts/{attempt_id}/provider-task.json
projects/{project_id}/shots/{shot_id}/attempts/{attempt_id}/inspection.json
projects/{project_id}/events/{timestamp}-{event_id}.json
projects/{project_id}/ledger/entries/{timestamp}-{entry_id}.json
projects/{project_id}/ledger/current.json
projects/{project_id}/final/master.mp4
projects/{project_id}/final/manifest.json
indexes/projects/{created_at}-{project_id}.json
indexes/tasks/{task_id}/status-ref.json
```

Durable JSON must store OSS keys, not expiring signed URLs. The web function should generate short-lived signed URLs only at response time.

`services/api/app/oss_repository.py` now defines the contract for this target. Its filesystem-backed fake verifies safe key handling, JSON object metadata, project manifest ETags, append-only event and ledger writes, and local signed URL generation. Production settings select the Alibaba OSS adapter behind the same contract; live bucket verification still requires credentials.

`services/api/app/task_checkpoints.py` writes the first runtime bridge into this contract: task submission records the original request, a queued project manifest, an append-only task event, and a zero-spend ledger marker before any inline task execution starts. Local tests use the filesystem fake; OSS-configured production mode uses the Alibaba adapter.

Project creation, job queueing, status transitions, production-agent decisions, shot acceptance/rejection decisions, patch decisions, and failure events now mirror their SQL event rows into append-only OSS event objects and link those objects from the project manifest. Status transitions also refresh the OSS project read model, cold web-function reads overlay the append-only OSS event trace onto `projects/{project_id}/read-model.json`, and the SSE event stream can emit deduped OSS events when local SQL event rows are absent.

The same checkpoint bridge now writes StoryIR, per-shot `contract.v{n}.json` objects, and final production-manifest objects. The project manifest stores `story_ir_key`, `final_manifest_key`, and object keys; it does not store expiring signed URLs.

Project-manifest upserts validate the existing schema before merging. If a stale or malformed manifest is encountered, the next checkpoint rewrites it with `directorgraph.project-manifest.v1` under the current object ETag and preserves only safe object-key audit_trail.

Mutable production state now has compact OSS overlays as well. Ledger updates write `projects/{project_id}/ledger/current.json` with the latest computed totals, while append-only reservation entries remain under `ledger/entries/`. Shot state changes write `projects/{project_id}/shots/{shot_id}/status.json` with status, attempts, accepted flag, quality report, contract key, and materialization object-key references. These status snapshots intentionally omit signed/public media URLs.

Live video submission writes `provider-task.json` immediately after DashScope returns a task ID and before polling. A successful provider poll writes `provider-result.json` before media download; this records success metadata and output host but not the provider's temporary output URL. After media download, `asset-materialization.json` records the durable media object key before orchestration links that media into the project manifest. Inspection results write `inspection.json` per shot attempt after Qwen-VL returns a quality report. These keys are linked from the project manifest on normal orchestration success.

On retry, live video generation checks `asset-materialization.json` first and restores the local scratch copy from the durable object key when needed. If no asset is materialized, it reads `provider-task.json` and polls the existing DashScope task instead of submitting a duplicate paid render. A new provider submission only happens when neither checkpoint exists.

Generated media assets now carry their OSS object keys through the provider result boundary. Character references, storyboards, dialogue files, rendered clips, repair clips, and final masters are linked from the project manifest; the local fake mirrors those media bytes so the same storage-manifest read path can sign them during zero-key tests.

Character references, storyboards, and dialogue audio also write media materialization checkpoints. On retry, the live provider restores those assets from their durable keys before invoking image or speech endpoints again.

Live character-reference, storyboard, and dialogue/TTS calls also write redacted `directorgraph.media-provider-result.v1` objects before media download/materialization. These store model, optional provider task id, usage, output URL presence, and output host without persisting the temporary provider URL. Successful static assets return the provider-result key so the orchestrator can link the JSON checkpoint into the project manifest.

The FFmpeg final master writes `projects/{project_id}/final/asset-materialization.json` immediately after storage. Normal completion then links that checkpoint, the final media key, and the final production manifest from the project manifest. On retry, the editor restores that materialized master and skips recomposition.

`/api/projects/{project_id}/storage-manifest` is the read-time URL boundary. It returns the durable project manifest plus short-lived signed URLs for the manifest's object keys. The durable manifest itself stores object keys only.

`projects/{project_id}/read-model.json` stores the API read model needed by the web function after a cold start. `indexes/projects/{created_at}-{project_id}.json` points to that read model for `/api/projects` listing. Project create, task submission, story planning, and final-manifest export update this read model. Project detail, listing, production manifest, storage-manifest, and public demo reads compare local scratch rows with the OSS read model and prefer the fresher OSS state, so web-function polling can observe task-function progress even when the web function still has stale queued SQL rows.

Cold read-model loading overlays append-only events, `ledger/current.json`, and per-shot `status.json` snapshots onto the stored read model, so mutable ledger totals and shot state can recover even when the read-model object is older than the latest production checkpoint.

`create_oss_repository(settings)` selects the Alibaba OSS adapter when OSS credentials are configured and falls back to the local filesystem fake otherwise. The adapter implements the same JSON/object/ref/signed-URL contract; live bucket verification still requires credentials.

`FUNCTION_COMPUTE_TASK_URL` selects Function Compute dispatch from the web function. The submitter sends the deterministic task payload with async invocation headers and stores the accepted Function Compute request metadata on the local job record.

The task function accepts `POST /api/function-compute/tasks` only when `APP_MODE=task`. If `FUNCTION_COMPUTE_AUTH_HEADER` is configured, the endpoint requires an exact `Authorization` header match. The task-mode route guard allows only `/api/health`, `/api/readiness`, and `/api/function-compute/tasks`; normal public API, SPA, and media routes return 404 on the task function.

The Function Compute payload is self-contained for separate-function execution: it carries `project_id`, `job_id`, `task_id`, and `operation`. If the task function's scratch SQLite database does not contain that job, task mode first loads the OSS project read model and materializes the local scratch `Project`, plan, ledger, final URL, and shot rows needed by the state machine. If no read model exists yet, it falls back to `projects/{project_id}/requests/original.json` and creates only the minimal queued project for a brand-new run. This keeps OSS as the durable authority and treats SQLite as temporary function scratch.

When the scratch project already contains a materialized StoryIR plan, `run_project` resumes from that plan, writes a `story.plan.resumed` event, and does not reserve spend or call the live story planner again. A fresh story-planning provider call is only made when no plan checkpoint has been rehydrated.

`projects/{project_id}/tasks/{task_id}/status.json` records the durable task-status snapshot for deterministic task IDs. Submission writes the object before Function Compute dispatch, worker execution updates it on running/succeeded/failed transitions, and `indexes/tasks/{task_id}/status-ref.json` maps the deterministic task ID back to that status object. `/api/tasks/{task_id}` can return the durable snapshot even if the local web scratch database no longer has the SQL job row. Live Function Compute cloud status mapping still needs cloud verification.

`POST /api/tasks/{task_id}/stop` supports the safe local stop case: a pending task that has not started execution is marked `canceled`, its durable task-status object is updated, the task cancellation event is mirrored to OSS, and the project read model is restored to the pre-task status. Running tasks return a 409 because live Function Compute cancellation and provider in-flight safety are not yet verified. The frontend exposes this safe stop action only while the active task status is `pending`.

The React console retains the deterministic task ID returned by run/patch submissions, polls `/api/tasks/{task_id}` and the project read model during active work, and stores the active task locally so page refreshes can resume polling. SSE remains available for local event streaming, but task polling is now the primary progress path.

Live production routes require `JUDGE_CREATE_ACCESS_CODE` to be configured server-side and require a matching `X-DirectorGraph-Judge-Code` header before accepting live work. Live run, patch, and Judge Test submissions also refuse live mode unless OSS credentials, `FUNCTION_COMPUTE_TASK_URL`, and a non-localhost `PUBLIC_MEDIA_BASE_URL` are configured. Live paid model calls check projected spend against project and total caps before invoking providers.

`/api/judge-test` provides the capped hands-on route for judges. In live mode it requires `JUDGE_CREATE_ACCESS_CODE` and a matching `X-DirectorGraph-Judge-Code`, creates a `judge_test` micro-brief using `JUDGE_RUN_MAX_DURATION_SECONDS`, `JUDGE_RUN_MAX_SHOTS`, and the project spend cap, then submits the same deterministic production task. The public-demo UI can launch this route with a password input; the code is sent only as a request header and is not bundled into the frontend.

Before a live StoryIR, character-reference, storyboard, dialogue/TTS, Qwen-VL inspection, video render, or video repair call submits provider work, the task records a deterministic spend-reservation ledger entry and updates the project ledger. Duplicate retries reuse the same reservation id, so the estimate is not counted twice. Pre-repair production reservations preserve the configured repair reserve.

`/api/public/demo` exposes only the configured completed `PUBLIC_DEMO_PROJECT_ID`; `/api/public/demo/storage-manifest` mints signed URLs for that demo's durable object keys. These public routes are read-only and do not submit tasks.

## Recovery Contract

Before any paid provider operation, the task function must persist:

- project ID;
- operation ID;
- attempt ID;
- model route;
- estimated spend reservation;
- provider task ID once submitted;
- checkpoint status.

Retries must resume from these checkpoints and must not submit a duplicate paid render when a provider task can be polled or downloaded.

## Security And Operations References

- `docs/THREAT_MODEL.md` records assets, trust boundaries, entry points, mitigations, and residual risks for the serverless judging deployment.
- `docs/OPERATIONS_RUNBOOK.md` records approval gates, local validation, serverless preflight, live configuration, Judge Test procedure, incident response, rollback, and final evidence collection.
- `infra/alibaba/serverless/ram-policy.runtime-oss.template.json` provides the OSS-scoped starting policy for the Function Compute runtime execution role.
- `infra/alibaba/serverless/s.yaml.template` defines the two custom-container Function Compute functions with the same `ACR_IMAGE_URI` and different `APP_MODE` values.
- `scripts/deploy-serverless.sh`, `scripts/verify-serverless-deployment.sh`, and `scripts/estimate-cost.py` provide local preflight, gated cloud mutation, live verification, provisioned-instance release with `s provision put --target 0`, and capped spend-estimate helpers.

## Verified So Far

- FastAPI can serve the compiled SPA from the same origin.
- The local `linux/amd64` Docker image can run in both `APP_MODE=web` and `APP_MODE=task` through the same-image smoke gate.
- `/api/health` exposes app mode, build SHA, provider mode, and non-secret deployment metadata.
- `/api/readiness` reports live credentials, OSS readiness, media publication readiness, and configured spend/judge caps without invoking paid APIs.
- A local `APP_MODE=task` process can complete a queued mock production.
- Run and patch submissions use deterministic TaskIDs and duplicate submissions reuse the existing task rather than creating another job.
- The local OSS repository fake passes contract tests and a smoke run for manifest optimistic concurrency, event/ledger append keys, object reads, and signed URLs.
- Task submission writes a durable local checkpoint and duplicate submissions converge on the same event and ledger objects.
- StoryIR, per-shot Shot Contract checkpoints, and final production manifests are written as durable local JSON objects and linked from the project manifest.
- Provider task IDs, provider results, asset materializations, and inspection reports are written as per-shot attempt checkpoints and linked from the project manifest on success.
- Live video retries resume from materialized assets or existing provider task IDs before any new paid submission.
- Static image and speech assets resume from materialization checkpoints before any new paid provider call.
- Static image and speech provider-result metadata is checkpointed before media download/materialization without storing temporary provider URLs.
- Final-master materialization is written before final production-manifest export, and the editor can resume from that checkpoint without recomposing.
- Semantic patch rendering restores character references, storyboards, dialogue audio, and accepted clips from durable materialization object keys before using URL-cache fallback.
- The storage-manifest read path regenerates signed URLs from stored object keys instead of persisting signed URLs.
- Generated media asset keys are linked from the manifest and verified through local signed readback.
- The Alibaba OSS adapter is contract-tested with a fake bucket and selected by OSS settings.
- Function Compute dispatch is wired behind configuration and contract-tested without a live network call.
- The Function Compute task receiver endpoint is wired for `APP_MODE=task`, enforces the optional authorization header, and blocks the normal public API surface on the task function.
- Function Compute task payloads include the IDs needed for separate-function execution, and task mode can rehydrate its empty scratch database from OSS original-request checkpoints.
- Task mode can also rehydrate patch-ready project plans and shot rows from OSS project read models before executing a task in an empty scratch database.
- StoryIR resume is covered in the production path: a task with a rehydrated plan skips the live story-planning reservation and provider call.
- Durable task status writes maintain an OSS task index, and `/api/tasks/{task_id}` can recover polling state without a local SQL job row.
- Pending tasks can be safely canceled through the task stop route, with durable canceled status and project read-model restoration; running-task cancellation remains unverified.
- Project list, project detail, production manifest, storage manifest, and public-demo routes can recover from OSS project read models without local SQL rows and prefer fresher OSS state over stale local SQL rows.
- Project and production events are mirrored into append-only OSS event objects, linked from the project manifest, overlaid onto cold project reads, and available to the SSE event stream without SQL event rows.
- Stale or malformed project manifests recover to the current schema on the next checkpoint while filtering unsafe local object keys.
- Current ledger snapshots and per-shot mutable status snapshots are linked from the project manifest and overlaid onto cold project reads without trusting stale SQL/read-model state.
- A Serverless Devs template now defines the web and task custom-container functions using one image, and deploy/push actions are guarded by explicit approval environment variables.
- The deploy helper releases provisioned instances for both functions after deployment; final minimum-instance proof still must come from a live Function Compute console/API verification artifact.
- Serverless deployment plan rendering and the Judge Test cost-estimate smoke path run locally without mutating cloud resources.
- Local ECS deploy/verify scripts now refuse to run unless `DIRECTORGRAPH_ALLOW_LOCAL_ECS=true` is set.
- Deterministic TaskID status reads are available through `/api/tasks/{task_id}`, with local OSS-style task-status snapshots linked from the project manifest.
- The frontend polls deterministic task status and project reads during active work, with browser-storage recovery for the active task ID after refresh.
- Judge-code approval and live spend-cap checks are enforced before live paid run/patch and provider-call paths.
- A capped Judge Test route and UI path creates a short two-to-three-shot task through the same run pipeline under configured caps.
- Live story-planning fallback is marked as degraded in the provider result and event trace instead of appearing as normal live planning evidence.
- Qwen/DashScope provider client errors are categorized and redacted locally, covering auth, quota, unsupported model, moderation, timeout, invalid response, transport, and provider failure without exposing auth headers, signed URL queries, tokens, access-key-like values, or provider task IDs in error summaries.
- Remote media downloads validate provider URLs and redirect targets before fetching, rejecting non-HTTP(S), localhost, private, link-local, reserved, and non-global address ranges.
- An OSS-scoped RAM policy template exists for the Function Compute runtime role; final live role attachment and bucket verification still need cloud credentials.
- Live StoryIR, image, TTS, Qwen-VL inspection, video render, and video repair attempts reserve estimated spend before paid provider submission, with idempotent ledger entries and repair-reserve protection before repair.
- Read-only public demo routes expose the completed configured project without paid actions.

## Not Yet Verified

- ACR image push and immutable digest pinning.
- Final image vulnerability scan with Trivy or Grype.
- Serverless Devs template application to a real Alibaba Cloud account.
- Function Compute web/task functions.
- Production replacement of the remaining SQL read-write assumptions outside the verified OSS rehydration, read-model, and materialization recovery paths.
- Live Alibaba OSS bucket write/read/signed URL verification.
- Live Function Compute asynchronous invocation, stop, retry, TTL, and cloud status mapping.
- Live Qwen/Wan/HappyHorse/TTS model calls.
