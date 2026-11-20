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

## Multi-Store Kroger/Gerbes API & Regional Grocery Catalog Plan

Goal: replace the sample catalog with real, ZIP-specific online prices. Nothing below is implemented yet, and no external scrapers should be built until these items are approved.

- **Catalog file contract (already supported by `catalog_matcher.py`).** The matcher looks up `online-catalog/<store_slug>_<zip>.json`, then `online-catalog/<store_slug>.json`, then `online-catalog/reference.json`, then the built-in sample. Each file is `{"items": [{"name": "...", "price": 0.00, "upc": "..."}]}`. The result records `catalog_source` as `zip_catalog`, `chain_catalog` or `sample_fallback`, so the UI always says whether prices are live or sample.
- **Kroger family (Kroger, Gerbes, Fred Meyer, Harris Teeter, etc.).**
  - Use the official Kroger Developer API (OAuth2 client credentials, free tier): Locations API to find the store ID for a ZIP code, then Products API with `filter.locationId` for per-store prices.
  - A scheduled job (EventBridge Scheduler plus a small `aldi-*` Lambda) would refresh `online-catalog/gerbes_<zip>.json` for ZIP codes seen in `runs_index.json`.
  - Store the client ID and secret in SSM Parameter Store (`SecureString`), per the Phase 6 secrets plan.
- **Match by barcode first.** The vision worker now returns `upc_barcode` and `barcode_status`. Add an exact-UPC match before the fuzzy name match, which should raise match quality and cut false positives.
- **Other chains (Giant, Wegmans, Schnucks, Lidl, Aldi).**
  - Review each retailer's terms of service and any official API or partner feed first.
  - Prefer user-supplied or licensed catalogs, or Instacart/retailer feeds, where permitted.
  - Do not scrape sites that prohibit it.
- **Freshness and cost.**
  - Stamp each catalog file with `fetched_at`, and show "prices as of <date>" in the UI.
  - Refresh only for ZIP codes with recent uploads, and cap the daily API call count.
  - Keep everything as S3 JSON so there are no new paid services.
- **UI follow-ups.** Show the catalog source and age on past runs. Let users choose a store/ZIP pair when comparing history.
