# DirectorGraph Threat Model

Status: security model for the target Function Compute + private OSS judging deployment. Local controls are implemented where noted; cloud controls require account-specific verification before final submission.

## System Boundary

DirectorGraph has two trusted runtime roles:

- `APP_MODE=web`: public FastAPI/React Function Compute HTTP function. It creates projects, returns read models, mints short-lived media URLs, and submits deterministic task payloads.
- `APP_MODE=task`: asynchronous Function Compute task function. It owns paid model calls, provider polling, recovery checkpoints, FFmpeg assembly, and durable manifest writes.

Durable state and media are intended to live in one private OSS bucket. Qwen Cloud / Model Studio, Wan, HappyHorse, Qwen-TTS, Function Compute, ACR, RAM, and OSS are external Alibaba Cloud trust boundaries.

## Assets

| Asset | Sensitivity | Required Control |
|---|---|---|
| `DASHSCOPE_API_KEY`, OSS credentials, Function Compute auth header, judge code | Secret | Store only in cloud secret/env mechanisms; never commit, log, or bundle into frontend. |
| Provider task IDs and temporary provider output URLs | Sensitive operational metadata | Persist task IDs only in durable checkpoints; never expose raw provider payloads or temporary output URLs in manifests/errors. |
| Private OSS media and manifests | User/project data | Keep bucket private; store object keys durably; mint short-lived signed URLs only at API response time. |
| Production ledger and model route evidence | Audit evidence | Append deterministic ledger/checkpoint JSON before paid work; preserve evidence logs under `evidence/`. |
| Public demo project | Public read artifact | Expose only the configured completed project; public routes must not create jobs or trigger paid calls. |
| Judge access code | Abuse-control secret | Validate server-side with constant-time comparison; never expose in `/api/config` or the frontend bundle. |

## Entry Points

| Entry Point | Trust Level | Main Threats | Current Controls |
|---|---|---|---|
| `POST /api/projects` | Public/admin depending deployment | Prompt abuse, excessive duration/budget | Pydantic bounds on brief fields, duration, budget, shot count. Admin exposure still needs deployment routing decision. |
| `POST /api/projects/{id}/run` and `/patch` | Admin/live-gated | Anonymous spend exhaustion, duplicate paid work | Judge code required in live mode, deterministic task IDs, spend reservations before paid video work. |
| `POST /api/judge-test` | Public with code | Code brute force, capped spend abuse | Server-side judge code, capped duration, shots, and budget; code sent only as request header. |
| `GET /api/public/demo` | Public | Paid-work trigger, private data leak | Read-only completed configured project only; no task submission path. |
| `GET /api/projects/{id}/storage-manifest` | Auth boundary depends deployment | Long-lived signed URL leak | Durable manifests store object keys only; signed URLs minted on read with bounded expiry. |
| Provider output download | Trusted provider response, untrusted URL | SSRF to metadata/private services, unsafe redirects | `AssetStore.save_remote` validates HTTP(S), DNS, blocked address ranges, and every redirect before download. |
| Function Compute task invocation | Internal web-to-task | Forged task payload, duplicate task | Deterministic task ID payload, optional auth header, local contract tests; live FC IAM/auth verification still pending. |

## Threats And Mitigations

| Threat | Impact | Mitigation |
|---|---|---|
| Secret leakage through logs or errors | Account compromise, paid API abuse | Provider errors use typed redacted summaries; raw provider payloads and signed URLs are not stored in final manifests. Credential-pattern scan runs in validation. |
| Anonymous live generation drains credits | Cost overrun | Live run/patch and Judge Test require server-side code; Judge Test has strict duration/shot/project caps; live video work reserves spend before provider submission. |
| Function retry repeats paid render | Duplicate billing | Provider task IDs are checkpointed before polling; retries restore materialized assets or poll existing tasks before submitting new video work. |
| Provider output URL targets internal network | Metadata theft or lateral movement | Remote media downloader rejects localhost, private, link-local, reserved, multicast, unspecified, and non-global addresses, including redirects. |
| Expiring signed URLs are persisted | Broken demo or accidental leak | Project manifests store OSS object keys; storage-manifest route mints temporary URLs at read time. |
| Public demo endpoint mutates state | Unbounded spend or tampering | Public demo routes are read-only and scoped to `PUBLIC_DEMO_PROJECT_ID` when completed. |
| Judge code appears in frontend | Public abuse of live test | `/api/config` exposes only a boolean; UI sends code as a header; server compares with `hmac.compare_digest`. |
| Obsolete ECS/SQL docs confuse judges | False architecture claim | Serverless architecture docs mark Compose/ECS as local/local; remaining ECS assets still need replacement by serverless infra scripts. |
| Model-quality claims exceed evidence | Submission integrity risk | Docs label mock outputs as orchestration evidence only; live quality claims remain blocked until real model evidence exists. |

## Required Cloud Controls Before Live Submission

- Use a private OSS bucket with deny-public-access posture and least-privilege Function Compute execution role.
- Store secrets in Function Compute secret/environment mechanisms, not image layers, source, or frontend assets.
- Pin both Function Compute functions to one immutable ACR image digest matching the public source commit.
- Set minimum instances to zero for both web and task functions.
- Configure Function Compute logging with secret redaction and short retention appropriate for judging evidence.
- Verify Function Compute async task IDs, retry behavior, timeout, and stop semantics against the submitter account.
- Run one no-charge public demo readback and one code-protected Judge Test only after explicit spend approval.

## Residual Risks

- Live account behavior for Function Compute, OSS signing, and model payloads is not verified in this workspace.
- The main project read routes now have OSS read-model fallback for web-function cold starts, but live OSS behavior and remaining SQL write paths still need cloud verification.
- Admin/public routing is still deployment-policy dependent; unrestricted project creation should not be exposed anonymously in live mode.
- Future user-upload or user-supplied reference-image surfaces must reuse the remote URL and content-safety controls before release.
- An OSS-scoped runtime RAM policy template now exists under `infra/alibaba/serverless/`; it still requires final account, region, bucket substitution and live validation before use.
