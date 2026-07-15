# Model routing and budget policy

## Principles

1. Spend quality where narrative salience is highest.
2. Use references whenever character identity matters.
3. Inspect before accepting.
4. Prefer a local edit for an isolated defect.
5. Stop retrying when remaining budget cannot justify expected improvement.

## Salience

Beat type establishes the base score:

| Beat | Base salience |
|---|---:|
| Hook | 0.96 |
| Setup | 0.52 |
| Escalation | 0.66 |
| Reveal | 0.94 |
| Climax | 1.00 |
| Resolution | 0.78 |

Dialogue and multi-character interaction add small bonuses. Hero shots receive a higher quality threshold and, when budget permits, 1080P rendering plus an additional repair opportunity.

## Renderer route

| Contract characteristics | Default route |
|---|---|
| No visible principal character | HappyHorse text-to-video |
| One reference-sensitive character | Wan image-to-video |
| Multiple characters or critical identity continuity, up to the R2V duration limit | Wan reference-to-video with one canonical image per principal character |
| Isolated object/style defect | HappyHorse video edit |
| Structural action/camera failure | Regenerate from corrected contract and reference |

Canonical one-subject character images are generated and persisted before shot production. The live adapter supplies only those identity references to Wan R2V; storyboards remain first-frame inputs rather than being misused as character references. Multi-character clips longer than the configured R2V limit route to Wan image-to-video.

All model names are environment configuration because availability can differ by region, account, and release date.

## Token discipline

- System policy and static story bible precede variable shot state.
- Agent handoffs contain compact JSON rather than full conversation transcripts.
- StoryIR is persisted once and addressed by ID.
- Storyboards are inspected before expensive motion generation.
- Fast per-shot visual inspection precedes final holistic review.
- The final master is never sent repeatedly when one shot is sufficient.

## Ledger caveat

The repository ships conservative configurable estimates to support routing and comparisons. They are not claims about current provider billing. For submission, replace estimates with rates from the hackathon account or attach actual usage exports.
