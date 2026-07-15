# Evaluation harness

DirectorGraph evaluates system behavior at three levels:

1. **Contract adherence** — narrative, identity, continuity, camera, motion, dialogue, and safety dimensions.
2. **Repair efficiency** — rejected seconds, local repairs, full regenerations, and accepted/generated ratio.
3. **Resource efficiency** — text tokens, vision tokens, generated video seconds, and estimated spend.

Generate a report from a completed local run:

```bash
python evals/run_ablation.py \
  --database services/api/data/demo.db \
  --output examples/demo-output
```

The bundled report is a reproducible orchestration benchmark. For the final hackathon submission, follow `evals/methodology.md`, run this command against a live Qwen Cloud production, then promote the live report with `python scripts/write-live-evaluation.py` so `evals/live-results.json` uses the final submission schema.
