# Grocery Price Forensics — Project Rules

Follow these rules automatically for all work in `C:\aldi-price-forensics`:

## 1. Execution & Shell Environment
- Always run PowerShell commands yourself (`git`, `docker`, `terraform`, `aws`, `wrangler`) instead of asking the user to run them manually.
- Before running AWS, Terraform, or Node/Wrangler commands in PowerShell, always initialize the environment:
  ```powershell
  fnm env --use-on-cd --shell powershell | Out-String | Invoke-Expression
  $env:AWS_PROFILE = "aldi-antigravity"
  $env:AWS_DEFAULT_REGION = "us-east-1"
  $env:TF_VAR_anthropic_api_key = [System.Environment]::GetEnvironmentVariable("TF_VAR_anthropic_api_key", "User")
  ```
- Never print or hardcode secret values (API keys, AWS secrets, tokens).

## 2. AWS Namespace & Cost Guardrails
- Keep all AWS resources prefixed with `aldi-*` (plus Glue/Athena `aldi_*` and Cloudflare IAM signer `cloudflare-grocery-*`) to stay within Account `575554646331` and IAM user `Jon_antigravity` (`AldiProjectOnlyAccess`). Never list or touch resources outside this scope.
- Keep operating costs near $0. Use S3 JSON files and existing serverless resources; do not introduce new paid databases or services without asking the user first.

## 3. Git & GitHub Hygiene
- Remote repository (`origin`): `https://github.com/jonantel/grocery-price-forensics.git` (private), default branch `main`.
- Always verify staged files before committing and **never** commit `.tfstate`, `.tfstate.*`, `.terraform/`, `.env`, `*.env`, `*.tfvars`, `*credentials*`, or `*.mp4` files.

## 4. Public UI Guidelines
- Keep the public web app at `discourseforensics.com/grocery` friendly, bright, consumer-oriented, and written in plain English.
- Keep all public page titles, headers, and UI headings free of store brand names.
