# DirectorGraph ablation report

Generated: 2026-06-23T19:55:57.762316+00:00

## Result

| System | Mean shot quality | Failed shots accepted |
|---|---:|---:|
| Single pass, no QC | 90.00% | 1 |
| DirectorGraph QC + repair | 93.00% | 0 |

DirectorGraph improved the measured mean by **3.00% absolute** and prevented **1** defective shot from entering the final timeline.

## Repair efficiency

- Final timeline: 21 seconds
- Surgically rerendered: 3 seconds
- Whole-film rerender avoided: 18 seconds
- Accepted/generated ratio: 87.5%
- Local repairs: 1
- Full regenerations: 0

## Interpretation

This is a deterministic, reproducible systems test of StoryIR, Shot Contracts, quality gates, repair routing, accounting, and final assembly. It is not presented as a benchmark of live Wan or HappyHorse visual quality. Use the same report generator on an Alibaba Cloud production database for submission evidence.
