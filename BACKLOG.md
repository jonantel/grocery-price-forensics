# Backlog

## Phase 5 Backlog: Automatic CI/CD deployment

Wire automatic GitHub Actions CI/CD deployment (`deploy.yml`) for `grocery-price-forensics` using GitHub Actions Secrets (`ANTHROPIC_API_KEY`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`).

Notes for when this is picked up:
- No `.github/workflows/` exists yet, so pushes do not trigger any automated runs.
- Planned steps: build and push the ECR image, `terraform apply` (with `TF_VAR_anthropic_api_key` from the `ANTHROPIC_API_KEY` secret), then `aws lambda update-function-code` on `aldi-frame-extractor`, `aldi-vision-worker` and `aldi-catalog-matcher`.
- Prerequisite: move Terraform state to a remote backend (for example an `aldi-*` S3 bucket). State is currently local and gitignored, so a CI run would start from empty state.
- Keep the AWS resource prefix `aldi-*` so the work stays within the `Jon_antigravity` IAM policy (`AldiProjectOnlyAccess`).

## Phase 6 Backlog: Pricing Controls, Cost Optimization & Security Alerting (Ported from Discourse Forensics)

### 1. AWS Budgets, SNS & Email Alerting (`infra/monitoring.tf`)
- **SNS topic `aldi-ops-alerts`:** email subscription, plus a topic policy that lets AWS Budgets, EventBridge (events) and CloudWatch publish to it.
- **Budget:** `aws_budgets_budget` named `aldi-monthly-cost-budget`, \$15/month, with three notifications:
  - 50% of actual spend
  - 80% of actual spend
  - 100% of forecasted spend
- **EventBridge CloudTrail security rule:** matches `ConsoleLogin`, `CreateAccessKey` and IAM mutations. A human-readable SNS Input Transformer formats the alert.
- **Pipeline failure EventBridge rule:** alerts on Lambda/pipeline failures.
- **CloudWatch Alarm on the SQS DLQ:** fires at `>= 1` message.
- **`scripts/audit_secrets.py`:**
  - 0-token Anthropic check via `/v1/models`
  - Cloudflare token 14-day expiry check
  - GitHub PAT expiry check
  - IAM access key age check (> 90 days)
  - SES quota check

### 2. Anthropic Claude API Token & Cost Controls
- **`ClaudeBudgetGuard`:** wrap every Vision call. It records per-run `input_tokens`, `output_tokens` and cache tokens, and writes an exact USD receipt to `ops/ai-receipts/`.
- **Hard caps:** `MAX_RUN_USD`, `max_tokens = 1024`, and `MAX_FRAMES_PER_VIDEO`.
- **Cloudflare D1 cache:** frame/SKU hash cache so repeated shelf tags are not re-sent to the API.
- **Anthropic Console:** set a monthly spend cap and keep Auto-Reload OFF.
- **Secrets:** store them in free AWS SSM Parameter Store (`SecureString`) instead of Lambda environment variables.

### 3. Storage & Compute Cost Guardrails
- **S3:** suspend bucket versioning, and add lifecycle rules:
  - `athena-results/` expires after 1 day
  - `abort_incomplete_multipart_upload` after 1 day
  - noncurrent versions expire after 1 day
  - `raw-videos/` expires after 2-14 days
- **CloudWatch Logs:** 7-day retention on `/aws/lambda/aldi-*`.
- **Lambda:** use `arm64`, right-size memory, and set `reserved_concurrent_executions = 3-5`.
- **SQS DLQ:** `maxReceiveCount = 3`, with queue visibility timeout = Lambda timeout + 60s.

### 4. Athena & Cloudflare Zero-Cost Edge Guardrails
- Snappy Parquet compaction via `pyarrow`.
- Strict column projection in Athena queries (no `SELECT *`).
- Serve the public UI strictly from Cloudflare Workers Assets + D1, so there are zero AWS hits per visitor.
- Path-filtered CI/CD deploys.
- CodeBuild ARM for recurring jobs.

### 5. IAM Permissions Update Required for `Jon_antigravity` (`AldiProjectOnlyAccess`)
- **`aldi-*` scoped actions needed:** `sns:*`, `events:*`, `cloudwatch:*`, `logs:*`, `ssm:*`, `codebuild:*`.
- **Account-wide (`"Resource": "*"`) actions:** `budgets:*`, `ce:GetCostAndUsage` and `ses:SendEmail` cannot be scoped to `aldi-*`.
- **Recommendation:** create the budget once from an admin session, so `Jon_antigravity` stays read-only on account-wide billing.
