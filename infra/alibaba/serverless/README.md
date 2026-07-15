# DirectorGraph Serverless Infrastructure Notes

Status: serverless-first infrastructure workspace. These artifacts are templates and must be reviewed against the submitter's Alibaba Cloud account before applying.

## Runtime Execution Role

`ram-policy.runtime-oss.template.json` is the intended starting policy for the Function Compute runtime execution role used by both DirectorGraph functions. It grants only the OSS permissions the current application needs for durable JSON, media object writes, object reads, metadata/head-style reads, and prefix listing inside the private DirectorGraph bucket.

Replace these placeholders before attaching the policy:

| Placeholder | Value |
|---|---|
| `${ALIBABA_REGION}` | Region selected for OSS and Function Compute, for example `ap-southeast-1`. |
| `${ALIBABA_ACCOUNT_ID}` | Alibaba Cloud account ID that owns the OSS bucket. |
| `${OSS_BUCKET}` | Private DirectorGraph bucket name. |

The template intentionally does not include `oss:DeleteObject`, bucket administration, public ACL changes, ECS permissions, database permissions, or broad `oss:*`.

## Function Compute Permissions

The current application submits task work by posting to `FUNCTION_COMPUTE_TASK_URL` with Function Compute async headers and an optional authorization header. That means the runtime role primarily needs OSS access; deployment/operator permissions for creating, updating, invoking, stopping, or retrying Function Compute resources are separate human/operator permissions and should not be attached to the public web runtime unless the implementation changes to Alibaba OpenAPI invocation.

If the implementation changes from HTTP task invocation to Alibaba OpenAPI task invocation, create a second account-scoped policy that grants only the exact Function Compute invoke/status/stop/retry actions and exact function resources required by the web function.

## Serverless Devs Template

`s.yaml.template` defines two Function Compute custom-container functions that use the same `ACR_IMAGE_URI`:

- `directorgraph-web`: `APP_MODE=web`, HTTP trigger, serves FastAPI and the compiled React console.
- `directorgraph-task`: `APP_MODE=task`, HTTP trigger for asynchronous task payloads at `/api/function-compute/tasks`.

The task function must use `FUNCTION_COMPUTE_AUTH_HEADER`; the template keeps the Function Compute trigger anonymous because the current application-level task endpoint validates the shared authorization header sent by `services/api/app/function_compute.py`. The task-mode route guard rejects normal public API paths on the task function.

Both functions set `STATE_BACKEND=oss`, `DATABASE_URL=sqlite:////tmp/directorgraph/scratch.db`, `SEED_DEMO=false`, and `INLINE_WORKER=false`. In OSS state mode, application startup skips SQL initialization and demo seeding; the SQLite path is temporary scratch state only. The Function Compute task payload carries `project_id`, `job_id`, `task_id`, and `operation`, and task mode can rehydrate the scratch `Project` and `Job` rows from OSS `requests/original.json` if the task function starts without the web function's local SQL rows. Task polling uses `indexes/tasks/{task_id}/status-ref.json`, and project reads use `projects/{project_id}/read-model.json` plus `indexes/projects/...`, so the web function can recover task and project state after a cold start.

## Scripts

From the repository root:

```bash
scripts/deploy-serverless.sh plan
scripts/deploy-serverless.sh build
DIRECTORGRAPH_APPROVE_CLOUD_APPLY=push-serverless-image scripts/deploy-serverless.sh push
DIRECTORGRAPH_APPROVE_CLOUD_APPLY=deploy-serverless scripts/deploy-serverless.sh deploy
DIRECTORGRAPH_BASE_URL=https://<web-function-url> scripts/verify-serverless-deployment.sh
python scripts/estimate-cost.py --profile judge_test --duration-seconds 15 --shots 3 --project-cap-usd 3 --fail-over-cap
```

Only `plan` and `estimate-cost.py` are non-mutating. `push` and `deploy` intentionally require explicit approval environment variables.

The deploy action renders the Serverless Devs template, deploys both custom-container functions, and then releases provisioned instances for `directorgraph-web` and `directorgraph-task` with `s <resource> provision put --qualifier LATEST --target 0`. Final submission proof still must record the live Function Compute minimum/on-demand instance settings from the console or API under `evidence/deployment/function-min-instances.json`.

## Verification Before Apply

Before applying the policy:

1. Confirm the OSS resource format and action names against current Alibaba Cloud RAM/OSS documentation.
2. Substitute all placeholders with real account values outside source control.
3. Attach the policy to a dedicated Function Compute execution role, not a broad admin role.
4. Run a no-paid readiness check from both functions.
5. Verify both functions have minimum instances set to zero and no provisioned instances held warm.
6. Verify OSS JSON write/read, object upload/read, and signed URL generation.
7. Save the redacted policy, role name, bucket name, Function Compute scale settings, and verification logs under live evidence.

## Official Documentation Consulted

- Alibaba Cloud OSS RAM policy documentation, retrieved 2026-06-24: https://www.alibabacloud.com/help/en/oss/user-guide/ram-policy/
- Alibaba Cloud OSS RAM policy examples and resource format, retrieved 2026-06-24: https://help.aliyun.com/en/oss/common-examples-of-ram-policies
- Alibaba Cloud RAM policy elements, retrieved 2026-06-24: https://www.alibabacloud.com/help/en/ram/policy-elements
- Alibaba Cloud Function Compute policy documentation, retrieved 2026-06-24: https://help.aliyun.com/en/functioncompute/fc-2-0/security-and-compliance/policies
- Alibaba Cloud Function Compute provisioned instance command documentation, retrieved 2026-06-24: https://github.com/devsapp/fc/blob/main/docs/en/command/provision.md
