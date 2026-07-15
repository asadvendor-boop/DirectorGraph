# DirectorGraph Operations Runbook

Status: runbook for local validation and the target serverless judging deployment. Commands that spend money, deploy infrastructure, publish public URLs, or mutate cloud resources require explicit human approval.

## Operating Modes

| Mode | Purpose | Required Setting |
|---|---|---|
| Local proof studio | Zero-key orchestration, QC, repair, FFmpeg, and evidence generation | `PROVIDER_MODE=mock` |
| Web function | Public API, React console, project reads, signed URL minting, task submission | `APP_MODE=web` |
| Task function | Paid model calls, checkpoint recovery, provider polling, final assembly | `APP_MODE=task` |
| Live studio | Alibaba/Qwen Cloud production after spend approval | `PROVIDER_MODE=live` plus DashScope/OSS/FC settings |

## Approval Gates

Stop and request explicit human approval before:

- applying, updating, or deleting Alibaba Cloud infrastructure;
- pushing an image to ACR or changing Function Compute function configuration;
- running paid live generation;
- increasing spend caps, render attempts, duration, shot count, or parallelism;
- publishing app, repository, demo, blog, or Devpost URLs;
- deleting OSS objects, logs, evidence, or cloud resources.

The approval request must state the exact command/action, expected spend or resource change, and where resulting evidence will be saved.

## Local Validation Gate

Run from repository root:

```bash
./scripts/validate.sh
ruff check services/api/app services/api/tests evals studio_mcp
scripts/deploy-serverless.sh plan
python scripts/estimate-cost.py --profile judge_test --duration-seconds 15 --shots 3 --project-cap-usd 3 --fail-over-cap
```

Expected result:

- Python syntax succeeds.
- Backend tests pass with only the intentional integration deselection.
- React production build succeeds.
- Shell syntax and JSON integrity succeed.
- Bundled mock master probes with H.264, AAC, subtitle stream, and expected duration.
- Credential-pattern scan reports no matches.
- Ruff reports no findings.
- The serverless plan action writes a redacted non-secret deployment plan and reports missing cloud values when credentials are absent.
- The cost-estimate smoke remains within the configured Judge Test cap.

Save command output under `evidence/baseline/` when it supports a committed slice.

## Serverless Preflight

Before configuring live deployment, verify these are known and recorded in `docs/API_COMPATIBILITY.md` or the final submission evidence:

- Alibaba region for ACR, Function Compute, OSS, and Model Studio.
- ACR repository name and immutable image digest.
- Function Compute web function name and task function name.
- Private OSS bucket name and endpoint.
- RAM execution role name and exact permission policy.
- DashScope/Model Studio API key stored outside source.
- `FUNCTION_COMPUTE_TASK_URL` and optional auth header.
- `PUBLIC_DEMO_PROJECT_ID` for a completed live public demo.
- `JUDGE_CREATE_ACCESS_CODE` stored server-side only.
- Serverless Devs CLI `s` installed only for the deployment operator environment, not required for local non-mutating plan checks.

Do not proceed if any required value is only present in the frontend bundle, source files, committed evidence, screenshots, or shell history.

## Live Configuration

Use these conservative caps unless a human approves different values:

```dotenv
MAX_TOTAL_LIVE_SPEND_USD=40
MAX_PROJECT_SPEND_USD=15
REPAIR_RESERVE_PERCENT=20
MAX_RENDER_ATTEMPTS_PER_SHOT=2
MAX_PARALLEL_RENDERS=3
JUDGE_RUN_MAX_DURATION_SECONDS=15
JUDGE_RUN_MAX_SHOTS=3
```

Live Function Compute variables must include:

```dotenv
APP_MODE=web
PROVIDER_MODE=live
DATABASE_URL=sqlite:////tmp/directorgraph/scratch.db
SEED_DEMO=false
INLINE_WORKER=false
DASHSCOPE_API_KEY=<secret>
DASHSCOPE_REGION=singapore
OSS_ENDPOINT=<private-oss-endpoint>
OSS_BUCKET=<private-bucket>
OSS_ACCESS_KEY_ID=<secret-or-role-derived>
OSS_ACCESS_KEY_SECRET=<secret-or-role-derived>
FUNCTION_COMPUTE_TASK_URL=<task-function-url>
PUBLIC_DEMO_PROJECT_ID=<completed-live-project-id>
JUDGE_CREATE_ACCESS_CODE=<secret>
```

Use `APP_MODE=task` for the task function with the same image digest and equivalent provider/OSS/spend settings. The SQLite database is per-instance scratch only; task submission, task status, and checkpoint recovery must continue to use OSS as durable state.

## Deployment Sequence

Before final publication, run `bash scripts/validate.sh` for local no-credential checks. Generate the release package from a clean tree with `bash scripts/build-source-archive.sh`; this writes ignored `submission/source-archive.tar.gz` and `submission/source-archive.tar.gz.sha256` artifacts that `scripts/verify-source-archive.py` checks against the current tracked source.

1. Run `scripts/deploy-serverless.sh plan` and save the redacted plan evidence.
2. After explicit approval for a minimal paid connectivity check, run `DIRECTORGRAPH_APPROVE_LIVE_API_SMOKE=run-live-api-smoke python scripts/run-live-api-smoke.py`; this writes `evidence/live-api/model-smoke.json` and `evidence/live-api/redacted-response-fixtures.json`.
3. Build the React/API image from a clean tree with `scripts/deploy-serverless.sh build`.
4. After explicit approval, push one immutable image to Alibaba Container Registry with `DIRECTORGRAPH_APPROVE_CLOUD_APPLY=push-serverless-image scripts/deploy-serverless.sh push`.
5. After explicit approval, deploy or update the Function Compute web and task functions from `infra/alibaba/serverless/s.yaml.template` with `DIRECTORGRAPH_APPROVE_CLOUD_APPLY=deploy-serverless scripts/deploy-serverless.sh deploy`; the helper also calls `s <resource> provision put --qualifier LATEST --target 0` for both resources.
6. Confirm both functions use the same image digest, minimum instances set to zero, and no provisioned instances held warm; write the digest proof with `python scripts/write-image-digest.py` and the zero-instance proof with `python scripts/write-function-min-instances.py --input <fc-export.json>`.
7. Run `DIRECTORGRAPH_BASE_URL=<web-function-url> scripts/verify-serverless-deployment.sh`; it saves redacted responses and the final `evidence/deployment/serverless-live-verification.json` artifact.
8. Verify a private OSS object denies anonymous access with `python scripts/check-private-oss-access.py --url <private-object-url>`.
9. Run `scripts/run-image-vulnerability-scan.sh --image <image-ref>` after authenticating to the registry if needed.
10. Request human approval before the first paid live production.
11. Run one full flagship production and one capped Judge Test. Save the Judge Test evidence with `DIRECTORGRAPH_BASE_URL=<web-function-url> DIRECTORGRAPH_JUDGE_CODE=<code> python scripts/run-deployment-task-smoke.py --mode live-judge-test`.
12. Save manifests, storage-manifest readbacks, deployment proof, media probes, and cost evidence.
13. After public publication, run `python scripts/write-public-links.py` with `PUBLIC_REPOSITORY_URL`, `PUBLIC_APP_URL`, `DEMO_VIDEO_URL`, and one proof URL configured; this writes `submission/public-links.json`.

## Judge Test Procedure

1. Confirm `JUDGE_CREATE_ACCESS_CODE` is configured server-side.
2. Open the public app URL and verify the public demo loads without create/run/patch controls.
3. Enter the judge code in the Judge Test panel.
4. Submit the capped test and watch `/api/tasks/{task_id}` progress.
5. Confirm the created project uses `production_profile=judge_test`, duration no higher than `JUDGE_RUN_MAX_DURATION_SECONDS`, and shots no higher than `JUDGE_RUN_MAX_SHOTS`.
6. Export the project manifest and storage manifest.
7. Save the task ID, Function Compute request ID, manifest references, and final media probe.

## Incident Response

| Symptom | Immediate Action | Evidence To Preserve |
|---|---|---|
| Spend cap refusal | Do not increase caps automatically. Inspect ledger and reservation entries. | Project manifest, ledger entry key, task status. |
| Provider auth/quota/model error | Keep task failed; fix credentials/model config only after human approval. | Redacted `ProviderCallError` category/detail, readiness response. |
| Provider task timeout | Retry only through deterministic task replay so provider task checkpoints are reused. | Provider task checkpoint, task status, worker logs. |
| Web/task scratch database loss | Do not restore from local files. Rehydrate from OSS request, manifest, task-index, and status objects. | `requests/original.json`, task index, task status, project manifest. |
| SSRF URL rejection | Treat provider output as unsafe; do not bypass validation. | Redacted provider result, rejected URL host/path, error category. |
| Public demo missing | Disable public promotion until `PUBLIC_DEMO_PROJECT_ID` points to a completed live project. | Readiness response and project status. |
| Secret exposure suspicion | Rotate affected secret, invalidate signed URLs, remove public artifact, and preserve incident notes outside committed source. | Commit SHA, artifact path, rotation timestamp. |

## Rollback

Function Compute rollback should repoint both web and task functions to the prior known-good image digest. Do not delete the failed image, OSS evidence, or logs until the incident is documented and the final submission evidence is backed up.

## Evidence Checklist

For final submission, collect:

- public source commit SHA and clean `git status`;
- image digest for both Function Compute functions;
- `/api/health` and `/api/readiness` redacted responses;
- successful public demo readback;
- successful Judge Test task status;
- flagship production manifest and storage-manifest signed URL readback;
- final MP4 probe and demo video URL;
- cost ledger and model usage evidence;
- deployment proof recording/link;
- Devpost URL after human submission.
- source archive and SHA-256 checksum generated by `bash scripts/build-source-archive.sh`.
