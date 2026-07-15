# API Compatibility Notes

Updated: 2026-06-24

This file records official documentation consulted for the serverless/live migration. It is not yet live account verification. Model availability, task limits, regions, pricing, and payload details must still be checked against the submitter's Alibaba Cloud account before paid generation or deployment.

## Function Compute

Source: Alibaba Cloud, "Create a task function", retrieved 2026-06-24.

- Task functions are the official Function Compute construct for large-scale asynchronous processing.
- The documentation states that Task Mode is enabled by default for a task function and supports submit, view, stop, and retry for asynchronous tasks.
- Relevance to DirectorGraph: the production function should use Function Compute Task Mode instead of PostgreSQL jobs, Redis, MNS, or a long-running worker.
- URL: https://help.aliyun.com/en/functioncompute/fc/user-guide/creating-a-task-function

Source: Alibaba Cloud, "Create a custom image function", retrieved 2026-06-24.

- Function Compute supports custom image functions.
- The image must come from an Alibaba Cloud Container Registry repository in the same region and account.
- For ARM development machines, the documented build target is Linux/Amd64, for example `docker build --platform linux/amd64`.
- Function Compute records image tag and digest; overwritten or missing source images can break invocations.
- Relevance to DirectorGraph: build one immutable ACR image and pin both the web and task functions to the same digest.
- URL: https://www.alibabacloud.com/help/en/functioncompute/fc/user-guide/create-a-custom-container-function-in-a-container-runtime

Source: devsapp/fc, "Yaml specification", retrieved 2026-06-24.

- Serverless Devs Function Compute YAML uses `edition`, `name`, `access`, `services`, `service`, `function`, `triggers`, `runtime: custom-container`, `caPort`, `customContainerConfig.image`, and `environmentVariables` fields.
- Relevance to DirectorGraph: `infra/alibaba/serverless/s.yaml.template` uses the documented custom-container fields to define the web and task functions with the same image and different `APP_MODE` values.
- URL: https://github.com/devsapp/fc/blob/main/docs/en/yaml/readme.md

## OSS

Source: Alibaba Cloud OSS, "Use a presigned URL to download an object (Python SDK V2)", retrieved 2026-06-24.

- Presigned URLs grant temporary unauthenticated GET access to private objects until expiration.
- The SDK V2 `presign` method can generate signed download URLs for object keys.
- V4 signatures have a documented maximum validity period of seven days.
- Relevance to DirectorGraph: durable manifests must store OSS keys, not signed URLs; the API should generate short-lived URLs at response time.
- URL: https://www.alibabacloud.com/help/en/oss/developer-reference/download-an-object-using-a-signed-url-generated-with-oss-sdk-for-python-v2

Source: Alibaba Cloud OSS, "RAM Policy", retrieved 2026-06-24.

- OSS access can be controlled with RAM policies attached to RAM identities.
- Relevance to DirectorGraph: the Function Compute runtime role should be limited to the private DirectorGraph bucket rather than account-wide OSS access.
- URL: https://www.alibabacloud.com/help/en/oss/user-guide/ram-policy/

Source: Alibaba Cloud OSS, "Common examples of RAM policies", retrieved 2026-06-24.

- The documented OSS resource format is `acs:oss:{region}:{bucket_owner}:{bucket_name}/{object_name}`; wildcard object paths such as `acs:oss:*:*:mybucket/*` can scope access to one bucket's objects.
- Relevance to DirectorGraph: `infra/alibaba/serverless/ram-policy.runtime-oss.template.json` uses account/region/bucket placeholders and avoids broad `oss:*`.
- URL: https://help.aliyun.com/en/oss/common-examples-of-ram-policies

## RAM

Source: Alibaba Cloud RAM, "Policy elements", retrieved 2026-06-24.

- Identity-based policies use `Version`, `Statement`, `Effect`, `Action`, `Resource`, and optional `Condition` elements.
- Relevance to DirectorGraph: RAM policy templates should remain JSON identity policies attached to the Function Compute execution role, while bucket policies and trust policies are separate resource/trust controls.
- URL: https://www.alibabacloud.com/help/en/ram/policy-elements

Source: Alibaba Cloud Function Compute, "Access policies and examples", retrieved 2026-06-24.

- Function Compute permissions can be granted through RAM custom policies.
- Relevance to DirectorGraph: current code submits tasks through `FUNCTION_COMPUTE_TASK_URL`; if that changes to OpenAPI-based invocation, exact Function Compute action/resource permissions must be added as a separate account-scoped policy.
- URL: https://help.aliyun.com/en/functioncompute/fc-2-0/security-and-compliance/policies

## Model Studio / Qwen

Source: Alibaba Cloud Model Studio, "DashScope API reference", retrieved 2026-06-24.

- Singapore DashScope native endpoints include text generation and multimodal generation paths under `https://dashscope-intl.aliyuncs.com/api/v1`.
- The same documentation includes Qwen-VL video URL input examples and notes support across Qwen3.5, Qwen3-VL, Qwen2.5-VL, and QVQ families for relevant video parameters.
- Relevance to DirectorGraph: the submitted deployment separates endpoints by capability: Qwen-compatible reasoning uses the international compatible endpoint, image/video media calls use the configured Frankfurt Model Studio workspace, and narration uses a dedicated Singapore Qwen-TTS workspace.
- URL: https://www.alibabacloud.com/help/en/model-studio/qwen-api-via-dashscope

Source: Alibaba Cloud Model Studio, "Make your first API call to Qwen", retrieved 2026-06-24.

- Model Studio supports API calls through OpenAI-compatible interfaces and the DashScope SDK.
- The official setup flow requires activating Model Studio, creating an API key, and storing it in an environment variable rather than hardcoding credentials.
- Relevance to DirectorGraph: live smoke tests need a human-provided `DASHSCOPE_API_KEY`; secrets must remain outside source and frontend bundles.
- URL: https://www.alibabacloud.com/help/en/model-studio/first-api-call-to-qwen

## Unverified Items

- Function Compute CPU, memory, ephemeral disk, timeout, retry, TTL, and concurrency limits for the selected account/region.
- Current Serverless Devs, Terraform, or Alibaba CLI resource schemas for Function Compute task functions.
- Current OSS SDK choice for production: existing code uses `oss2` SDK V1, while one consulted presigned URL document covers Python SDK V2.
- Exact account-scoped RAM role name, trust policy, and final OSS resource ARNs after the submitter chooses region/account/bucket names.
- Current account-scoped availability, pricing, and returned usage fields for the configured Qwen reasoning, Qwen-VL, Wan image/video, HappyHorse video, and Qwen-TTS models.
- Pricing and returned usage fields for all live models.
