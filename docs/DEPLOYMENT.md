# Alibaba Cloud Hybrid ECS Deployment

Status: live judging deployment path. This document describes the Alibaba ECS
shared-host deployment used for DirectorGraph during the hackathon judging
period. Do not claim high availability or managed RDS; this is a
production-oriented single-node deployment with live Qwen Cloud inference and
private OSS evidence.

## Target Hackathon Topology

- One Alibaba ECS VM in Singapore shared with the other submissions.
- Caddy terminates public HTTPS and routes the DirectorGraph hostname.
- Docker Compose runs React web, FastAPI API, task runtime, and PostgreSQL.
- `APP_MODE=web` runs the public API and judge task submission path.
- `APP_MODE=task` runs asynchronous production, provider polling, checkpoint recovery, and FFmpeg assembly.
- One private OSS bucket stores generated media, project manifests, proof objects, and deployment evidence.
- Qwen text calls use the OpenAI-compatible endpoint; Wan image/video calls use the Frankfurt Model Studio workspace; Qwen-TTS narration uses the dedicated Singapore speech workspace.

The public React console and FastAPI API are served behind the same Caddy host.
The API registers projects and submits asynchronous task work to the private task
container; paid-capable production should not run as an unbounded in-process
background thread. The task container owns production, provider polling,
checkpoint recovery, and FFmpeg assembly.

## Current Local Verification

Verified without cloud credentials:

- `APP_MODE=web` exposes `/api/health`, `/api/readiness`, `/api/config`, and serves the compiled SPA when `apps/web/dist` exists.
- `APP_MODE=task` can process an existing queued job through `python -m app.task_runtime --job-id ...`.
- `APP_MODE=task` exposes `POST /api/function-compute/tasks` as the internal task payload endpoint and rejects the normal public API surface through the task-mode route guard.
- Task payloads carry `project_id`, `job_id`, `task_id`, and `operation`; a fresh task scratch database can materialize the job from OSS checkpoints.
- `/api/tasks/{task_id}` can recover task polling state from the OSS task index without a local SQL job row.
- Project list/detail, production manifest, storage manifest, and public-demo reads can recover from OSS project read models without local SQL rows.
- The root `Dockerfile` defines the intended one-image build path.
- Root Docker build now passes for `linux/amd64`, and the built container can import the FastAPI app with the bundled frontend present. See `evidence/baseline/docker-build.log`.

## Local Commands

Build frontend and validate the local source path:

```bash
cd apps/web
npm ci
npm run build

cd ../..
./scripts/validate.sh
```

Run the single-origin web smoke path:

```bash
cd services/api
PYTHONPATH=. APP_MODE=web PROVIDER_MODE=mock python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Run the local task smoke path:

```bash
cd services/api
PYTHONPATH=. APP_MODE=task PROVIDER_MODE=mock python -m app.task_runtime --job-id <queued-job-id>
```

Build the image when Docker is available:

```bash
docker build --platform linux/amd64 \
  --build-arg BUILD_SHA="$(git rev-parse --short HEAD)" \
  --build-arg BUILD_TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  -t directorgraph:serverless-local .
```

Deploy on the shared Alibaba ECS host:

```bash
cd deploy/shared-host
BUILD_SHA="$(git rev-parse HEAD)" \
BUILD_TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
docker compose --env-file /opt/apps/directorgraph/shared-host/directorgraph.env \
  -f compose.prod.yml up -d --build
```

## Required Cloud Configuration

Set these in the root-owned ECS environment file. Never commit real values.

```dotenv
APP_MODE=web
PROVIDER_MODE=live
DATABASE_URL=postgresql+psycopg://directorgraph:<password>@postgres:5432/directorgraph
SEED_DEMO=false
INLINE_WORKER=false
DASHSCOPE_API_KEY=<secret>
DASHSCOPE_REGION=singapore
QWEN_TTS_API_KEY=<secret>
QWEN_TTS_BASE_URL=https://<singapore-workspace>.ap-southeast-1.maas.aliyuncs.com/api/v1
QWEN_TTS_WORKSPACE_ID=<singapore-workspace>
QWEN_TTS_MODEL=qwen3-tts-flash
QWEN_TTS_VOICE=Cherry
OSS_ENDPOINT=<oss-endpoint>
OSS_BUCKET=<private-bucket>
OSS_PUBLIC_BASE_URL=
MAX_TOTAL_LIVE_SPEND_USD=40
MAX_PROJECT_SPEND_USD=15
REPAIR_RESERVE_PERCENT=20
MAX_RENDER_ATTEMPTS_PER_SHOT=2
JUDGE_RUN_MAX_DURATION_SECONDS=15
JUDGE_RUN_MAX_SHOTS=3
PUBLIC_DEMO_PROJECT_ID=<precomputed-live-project-id>
JUDGE_CREATE_ACCESS_CODE=<secret>
FUNCTION_COMPUTE_TASK_URL=http://task:8000/api/function-compute/tasks
FUNCTION_COMPUTE_AUTH_HEADER=<optional-auth-header>
FUNCTION_COMPUTE_INVOKE_TIMEOUT_SECONDS=10
```

Use `APP_MODE=task` and the same application image for the task container.
PostgreSQL is private scratch/operational state; durable generated media and
proof objects must be in OSS.

## Remaining Deployment Work

- Publish the final public repository and demo video links.
- Run final image/dependency scans against the deployed image.
- Keep the current live master evidence aligned with the public demo project and regenerate it only through the app-controlled production path.
- Keep the ECS VM online through the official judging period.

## Optional Serverless Evolution Path

The Function Compute and Serverless Devs files remain in the repository as an
engineering path for a future scale-to-zero deployment, but the live hackathon
proof for this submission is the Alibaba ECS shared-host deployment described
above.
