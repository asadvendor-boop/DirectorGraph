# Deployment Proof

DirectorGraph's judged deployment is the Alibaba ECS hosted app at:

```text
https://directorgraph.47.84.232.193.sslip.io/
```

This file is the stable rubric anchor for deployment proof; the dual-region
architecture is documented in
[`HYBRID_ALIBABA_ARCHITECTURE.md`](HYBRID_ALIBABA_ARCHITECTURE.md).

## Code Evidence

- [`services/api/app/clients/qwen.py`](../services/api/app/clients/qwen.py)
  handles Qwen reasoning, Qwen-VL inspection, and Singapore Qwen TTS routing.
- [`services/api/app/clients/dashscope.py`](../services/api/app/clients/dashscope.py)
  handles DashScope task calls.
- [`services/api/app/providers/live.py`](../services/api/app/providers/live.py)
  routes live media production through Qwen Cloud, Wan, HappyHorse, and OSS.
- [`deploy/shared-host/compose.prod.yml`](../deploy/shared-host/compose.prod.yml)
  is the judged ECS Compose profile.

## Proof Artifacts

- Public deployed master:
  [`evidence/deployment/public-demo.json`](../evidence/deployment/public-demo.json)
- Live deployment verification:
  [`evidence/deployment/serverless-live-verification.json`](../evidence/deployment/serverless-live-verification.json)
- Private OSS proof:
  [`evidence/deployment/private-oss-access-check.json`](../evidence/deployment/private-oss-access-check.json)

## External Publication Items

The public repository, demo video, and Devpost URLs are published on the Devpost
submission. DirectorGraph runtime is frozen on the verified `Autumn Path` master.
