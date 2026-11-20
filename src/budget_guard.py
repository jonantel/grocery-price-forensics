import json
import os
from datetime import datetime, timezone

from botocore.exceptions import ClientError

# Per-million-token USD pricing for Anthropic Claude models.
PRICING_PER_M = {
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00, "cache_read": 0.10, "cache_write": 1.25},
    "claude-sonnet-4-5": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
}
DEFAULT_PRICING = {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75}

MAX_RUN_USD = float(os.environ.get("MAX_RUN_USD", "0.25"))
MAX_MONTHLY_USD = float(os.environ.get("MAX_MONTHLY_USD", "5.00"))


class BudgetExceededError(RuntimeError):
    """Raised before calling the AI API when per-run or monthly spend limits are reached."""


def _month_key(now=None):
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m")


def _get_json(s3, bucket, key, default):
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "404", "NotFound"):
            return default
        raise


def _put_json(s3, bucket, key, obj):
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(obj, indent=2).encode(),
        ContentType="application/json",
        CacheControl="no-cache",
    )


def estimate_cost_usd(model, usage):
    p = PRICING_PER_M.get(model, DEFAULT_PRICING)
    inp = int(usage.get("input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    c_read = int(usage.get("cache_read_input_tokens") or 0)
    c_write = int(usage.get("cache_creation_input_tokens") or 0)
    usd = (
        inp * p["input"]
        + out * p["output"]
        + c_read * p["cache_read"]
        + c_write * p["cache_write"]
    ) / 1_000_000.0
    return round(usd, 6)


def check_budget(s3, bucket, walk_id):
    """Hard circuit-breaker checked BEFORE every Claude API call."""
    month = _month_key()
    monthly = _get_json(s3, bucket, f"ops/ai-receipts/monthly_{month}.json", {"month": month, "total_usd": 0.0})
    run = _get_json(s3, bucket, f"ops/ai-receipts/run_{walk_id}.json", {"walk_id": walk_id, "total_usd": 0.0})
    m_spent = float(monthly.get("total_usd", 0.0))
    r_spent = float(run.get("total_usd", 0.0))

    if m_spent >= MAX_MONTHLY_USD:
        raise BudgetExceededError(
            f"Monthly AI budget cap reached (${m_spent:.4f} / ${MAX_MONTHLY_USD:.2f}). Stopping calls to protect spend."
        )
    if r_spent >= MAX_RUN_USD:
        raise BudgetExceededError(
            f"Per-video AI budget cap reached (${r_spent:.4f} / ${MAX_RUN_USD:.2f}). Stopping calls for {walk_id}."
        )
    return {"monthly_usd": m_spent, "run_usd": r_spent, "max_monthly_usd": MAX_MONTHLY_USD, "max_run_usd": MAX_RUN_USD}


def record_usage(s3, bucket, walk_id, batch_index, model, usage):
    """Write per-call receipt and update run + monthly + public spend summaries."""
    now = datetime.now(timezone.utc)
    month = _month_key(now)
    cost = estimate_cost_usd(model, usage)

    receipt = {
        "walk_id": walk_id,
        "batch_index": batch_index,
        "model": model,
        "timestamp": now.isoformat(),
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "cache_read_input_tokens": int(usage.get("cache_read_input_tokens") or 0),
        "cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens") or 0),
        "cost_usd": cost,
    }
    _put_json(s3, bucket, f"ops/ai-receipts/{walk_id}_batch_{batch_index:04d}_{model}.json", receipt)

    run_key = f"ops/ai-receipts/run_{walk_id}.json"
    run = _get_json(s3, bucket, run_key, {
        "walk_id": walk_id, "total_usd": 0.0, "calls": 0, "input_tokens": 0, "output_tokens": 0
    })
    run["total_usd"] = round(float(run.get("total_usd", 0.0)) + cost, 6)
    run["calls"] = int(run.get("calls", 0)) + 1
    run["input_tokens"] = int(run.get("input_tokens", 0)) + receipt["input_tokens"]
    run["output_tokens"] = int(run.get("output_tokens", 0)) + receipt["output_tokens"]
    run["updated_at"] = now.isoformat()
    _put_json(s3, bucket, run_key, run)

    m_key = f"ops/ai-receipts/monthly_{month}.json"
    monthly = _get_json(s3, bucket, m_key, {
        "month": month, "total_usd": 0.0, "calls": 0, "input_tokens": 0, "output_tokens": 0
    })
    monthly["total_usd"] = round(float(monthly.get("total_usd", 0.0)) + cost, 6)
    monthly["calls"] = int(monthly.get("calls", 0)) + 1
    monthly["input_tokens"] = int(monthly.get("input_tokens", 0)) + receipt["input_tokens"]
    monthly["output_tokens"] = int(monthly.get("output_tokens", 0)) + receipt["output_tokens"]
    monthly["max_monthly_usd"] = MAX_MONTHLY_USD
    monthly["updated_at"] = now.isoformat()
    _put_json(s3, bucket, m_key, monthly)

    _put_json(s3, bucket, "public/cost_summary.json", {
        "month": month,
        "monthly_usd": monthly["total_usd"],
        "max_monthly_usd": MAX_MONTHLY_USD,
        "max_run_usd": MAX_RUN_USD,
        "monthly_calls": monthly["calls"],
        "last_walk_id": walk_id,
        "last_run_usd": run["total_usd"],
        "updated_at": now.isoformat(),
    })
    return receipt, run, monthly


def get_run_cost_summary(s3, bucket, walk_id):
    month = _month_key()
    run = _get_json(s3, bucket, f"ops/ai-receipts/run_{walk_id}.json", {})
    monthly = _get_json(s3, bucket, f"ops/ai-receipts/monthly_{month}.json", {})
    return {
        "run_usd": round(float(run.get("total_usd", 0.0)), 6),
        "run_input_tokens": int(run.get("input_tokens", 0)),
        "run_output_tokens": int(run.get("output_tokens", 0)),
        "monthly_usd": round(float(monthly.get("total_usd", 0.0)), 6),
        "max_monthly_usd": MAX_MONTHLY_USD,
        "max_run_usd": MAX_RUN_USD,
        "month": month,
    }
