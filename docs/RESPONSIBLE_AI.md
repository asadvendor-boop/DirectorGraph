# Responsible AI and production safeguards

## Consent and source records

DirectorGraph's default implementation uses stock synthetic voices and fictional characters. It does not expose voice cloning or real-person likeness controls. Any future cloning feature must require recorded consent, a clear source record, revocation support, and visible disclosure.

## Intellectual property

The story compiler instructs models to avoid copyrighted characters, celebrity likenesses, logos, and generated on-screen text. Users remain responsible for rights to reference images, scripts, music, trademarks, and training or source material they upload.

## Safety gates

The Quality Report includes a safety dimension. A production deployment should add input moderation, generated-media moderation, age-appropriate policy, abuse reporting, and a human approval checkpoint before public distribution.

## Privacy

Private productions should use private OSS objects and signed URLs. Reject logs that contain secrets or personal data. Add retention policies so rejected footage and temporary references expire automatically.

## Transparency

The production manifest records model routes, attempts, scores, prompts/contracts, repair decisions, and asset audit_trail. Final releases should disclose material AI generation and retain the manifest for audit.
