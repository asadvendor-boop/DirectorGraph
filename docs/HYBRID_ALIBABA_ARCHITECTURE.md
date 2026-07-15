# Hybrid Alibaba Architecture

DirectorGraph's judging deployment runs on the shared Alibaba ECS VM as a
production-oriented single-node service. This is the same cost-controlled hybrid
strategy used for the three hackathon submissions: stable ECS hosting for the
public app, live Qwen Cloud inference, private Alibaba OSS for generated media
and proof objects, and bounded Docker Compose services with health checks and
log rotation.

This document describes what is actually deployed for judging. The older
Function Compute material remains useful as an optional serverless evolution
path, but it is not the live deployment proof target for this submission.

## Topology

```text
Judge browser
    |
    v
Caddy on Alibaba ECS
    |
    +--> DirectorGraph React console
    |
    +--> FastAPI API container (APP_MODE=web)
            |
            +--> PostgreSQL scratch-state container on directorgraph-db
            +--> Task container over private network
            |
            v
        Task container (APP_MODE=task)
            |
            +--> Qwen Cloud Frankfurt OpenAI-compatible endpoint
            +--> Alibaba Model Studio Frankfurt Qwen Cloud workspace
            +--> Private Alibaba Cloud OSS bucket
            +--> FFmpeg assembly
```

## Runtime Boundaries

- `directorgraph-edge` exposes only Caddy, the public web container, and the API
  alias needed by the shared host proxy.
- `directorgraph-db` is internal and contains only the API, task container, and
  PostgreSQL.
- `directorgraph-internal` is internal application traffic.
- `directorgraph-egress` is used by the task runtime for provider/API egress.
- PostgreSQL is scratch operational state for jobs and project reads. Durable
  generated media, evidence, manifests, and proof objects are written to OSS.

## Alibaba Cloud Services Used

- Alibaba ECS hosts the live public app for the full judging period.
- Alibaba OSS stores private media/evidence objects and proves cloud-backed
  durable asset handling.
- Qwen Cloud / Alibaba Model Studio handles live Qwen reasoning and configured
  media model calls.
- The same application image is used by `APP_MODE=web` and `APP_MODE=task`
  containers so the async production path and API path stay consistent.

## Operational Controls

- Docker Compose services use `restart: unless-stopped`.
- PostgreSQL, API, task, and web services have container health checks.
- Docker logging uses the `local` driver with `max-size=20m` and `max-file=5`.
- Live judge runs are capped by configured shot, duration, and spend limits.
- Live work requires the server-side judge access code.
- Provider failures are redacted, categorized, and recorded without exposing
  API keys, signed URLs, or reusable secrets.

## Current Live Proof

The submitted public master is a completed live Qwen Cloud production. The
current evidence file records a character-bound Qwen story plan, six
reference-video shots rendered through `wan_r2v`, six re-hosted narration files,
Qwen-VL accepted scores from 0.85 to 0.92, and zero local media fallback. Older
deployment evidence may still show historical entitlement-denied attempts; those
records remain historical evidence and are not the submitted master.
