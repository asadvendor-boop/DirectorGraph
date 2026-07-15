# DirectorGraph Live Evaluation Methodology

This document defines the evidence protocol for the final Track 2 submission. It is intentionally separate from `docs/EVALUATION.md`: the docs page explains the evaluation concept, while this file is the release artifact checked by the final submission gate.

## Scope

DirectorGraph is evaluated as an AI showrunner system, not as a single-model image or video benchmark. The live evaluation must measure whether the system converts one creative brief into a traceable, contract-checked short video while controlling cost, retries, and recovery risk.

The bundled mock evidence may be used only as reproducible orchestration evidence. Public model-quality claims require live Qwen/DashScope and Alibaba Cloud outputs generated after deployment.

## Systems Compared

| System | Description | Purpose |
|---|---|---|
| Single-pass baseline | Use the same story plan and first rendered clips, then accept every first render without multimodal inspection or repair. | Estimates what the production would ship without DirectorGraph quality control. |
| DirectorGraph QC + repair | Inspect each clip against its Shot Contract, repair failed clips within the configured attempt cap, reinspect, then assemble only accepted clips. | Measures contract enforcement and repair efficiency. |
| Contract-ablation variant | Run the same model route with reduced or generic shot requirements while preserving caps and models. | Optional live ablation for isolating the value of typed Shot Contracts. |

## Required Runs

1. A flagship live production using the public-demo brief selected for submission.
2. A capped Judge Test production using `/api/judge-test` with the configured shot, duration, and project spend caps.
3. The single-pass baseline derived from the first render attempts of the flagship run.
4. If spend and time allow, one contract-ablation variant with the same brief and provider route.

Every run must record the project ID, task ID, image digest, deployment URL, start/end timestamps, provider route, model names, configured caps, and final object keys. Signed URLs must not be stored as durable evidence.

## Metrics

| Metric | Source | Definition |
|---|---|---|
| Mean accepted-shot score | Shot inspection reports | Mean final multimodal contract score for accepted clips. |
| Defective shots prevented | Rejected first attempts | First attempts below threshold that the baseline would have accepted. |
| Acceptance ratio | Ledger snapshot | Accepted final seconds divided by generated video seconds. |
| Repair locality | Shot contracts and ledger | Seconds rerendered or locally repaired divided by final timeline seconds. |
| Full regeneration avoided | Ledger snapshot | Final timeline seconds not regenerated after targeted repair. |
| Cost per accepted second | Spend ledger and provider billing export | Actual live spend divided by accepted final seconds. |
| Restart recovery | OSS task/status checkpoints | Whether task polling, provider task IDs, media keys, and final master materialization recover after scratch-state loss. |
| Public readiness | Deployment evidence | Public app, public demo readback, private OSS denial, zero minimum instances, and immutable image digest. |

## Acceptance Thresholds

The final result is submission-ready only if all of these hold:

- The flagship production completes and exports a playable MP4 with captions.
- The Judge Test completes within `JUDGE_RUN_MAX_DURATION_SECONDS`, `JUDGE_RUN_MAX_SHOTS`, `MAX_PROJECT_SPEND_USD`, and `MAX_TOTAL_LIVE_SPEND_USD`.
- No live run exceeds `MAX_RENDER_ATTEMPTS_PER_SHOT`.
- At least one quality-control decision is visible in the agent trace or, if every shot passes first try, the trace shows inspection reports for every shot.
- Durable OSS manifests contain object keys and ETags, not signed URL query strings.
- The source archive, dependency audit, image scan, and deployment proof pass the final submission gate.

## Artifact Contract

The final live evaluation artifacts are:

- `evals/live-results.json`: machine-readable metrics, run metadata, selected model names, spend values, and artifact references.
- `evals/live-results.md`: human-readable summary with the main deltas, limitations, and links to public proof.
- `evals/methodology.md`: this protocol.
- `evidence/live-api/model-smoke.json`: redacted live API connectivity and model-route proof.
- `evidence/live-api/redacted-response-fixtures.json`: sanitized provider response examples.
- `evidence/deployment/live-judge-test-smoke.json`: deployed Judge Test status and output proof.
- `evidence/deployment/mock-task-smoke.json`: deployed no-spend task smoke against the same Function Compute split.

The JSON artifact should use stable top-level keys:

```json
{
  "schema": "directorgraph.live-evaluation.v1",
  "generated_at": "ISO-8601 timestamp",
  "repository_commit": "git commit SHA",
  "deployment": {
    "web_url": "public HTTPS URL",
    "image_digest": "sha256:..."
  },
  "runs": [],
  "metrics": {},
  "limitations": []
}
```

## Reproduction Commands

Local no-key validation:

```bash
bash scripts/validate.sh
```

Generate the source archive after the final commit:

```bash
bash scripts/build-source-archive.sh
python3 scripts/verify-source-archive.py
```

After live deployment and explicit spend approval, generate the live results from the production database or recovered OSS read model with the ablation harness:

```bash
python evals/run_ablation.py \
  --database services/api/data/demo.db \
  --output evals
```

Promote the generated report to final artifacts only after verifying that the inputs came from live Alibaba Cloud/Qwen production evidence:

```bash
PUBLIC_APP_URL=<public-web-function-url> \
IMAGE_DIGEST=sha256:<immutable-image-digest> \
python scripts/write-live-evaluation.py \
  --eval-report-json evals/eval-report.json \
  --eval-report-md evals/eval-report.md
```

## Non-Claims

The local mock report does not claim live Wan, HappyHorse, or Qwen visual quality. The final public submission must label mock outputs as orchestration evidence and live outputs as measured provider evidence.
