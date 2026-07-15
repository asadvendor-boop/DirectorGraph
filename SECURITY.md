# Security policy

Never commit cloud credentials. Use `.env` only for local development; use Alibaba Cloud KMS, Secrets Manager, or secured Function Compute environment configuration for deployment.
Generated media URLs can be sensitive; use private OSS buckets and short-lived signed URLs for non-public projects.
Voice cloning must remain disabled unless the speaker has supplied explicit, verifiable consent.
Report vulnerabilities privately to the repository owner before public disclosure.
