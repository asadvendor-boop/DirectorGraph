# Evaluation methodology

## Systems compared

1. **Single-pass baseline:** accept every first render; no multimodal quality gate and no repair.
2. **DirectorGraph:** inspect every shot against its contract, repair failed footage, reinspect, then edit accepted clips.

A second live ablation can disable contract fields while preserving the same models, isolating the value of typed contracts from raw generation quality.

## Metrics

| Metric | Definition |
|---|---|
| Mean shot quality | Mean final contract score across accepted shots |
| Defective shots accepted | First renders below threshold that would enter the baseline timeline |
| Acceptance ratio | Accepted seconds / all generated seconds |
| Repair locality | Surgically rerendered seconds / final timeline seconds |
| Regeneration avoided | Final timeline seconds minus surgically rerendered seconds |
| Cost per accepted second | Estimated or actual spend / accepted seconds |
| Token efficiency | Text + visual tokens / final minute |
| Reliability | Completed jobs / started jobs and restart recovery success |
| Human preference | Blind A/B ratings for coherence, continuity, emotional clarity, and overall appeal |

## Bundled result

The bundled mock run is a reproducible systems validation. It generates seven shots, deliberately removes the required red paper crane from S05, records a failed score, performs one local repair, passes the repaired clip, and exports a 21-second vertical master. The report is generated from persisted database evidence rather than hand-entered UI values.

It does **not** claim that local storyboard animation represents live Wan or HappyHorse output quality. Run the identical harness after the live cloud production and publish that output as the final submission benchmark.

## Human study protocol

For the hackathon submission:

1. Export one no-QC baseline and one DirectorGraph master from the same brief and seeds.
2. Randomize labels A/B.
3. Collect at least 15 independent ratings on 1–5 scales.
4. Ask continuity and emotional-clarity questions separately from visual polish.
5. Publish anonymized aggregate results and the exact prompt/seed manifest.

## Reproduction

```bash
python evals/run_ablation.py \
  --database services/api/data/demo.db \
  --output examples/demo-output
```
