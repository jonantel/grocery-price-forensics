import base64
import json
import os
import re
import time
import urllib.error
import urllib.request

import boto3
import budget_guard

s3 = boto3.client("s3")
lam = boto3.client("lambda")

API_URL = "https://api.anthropic.com/v1/messages"
PRIMARY_MODEL = "claude-haiku-4-5"
FALLBACK_MODEL = "claude-sonnet-4-5"
LOW_CONF_THRESHOLD = 0.30
CLAUDE_MAX_TOKENS = int(os.environ.get("CLAUDE_MAX_TOKENS", "1536"))
MATCHER = os.environ.get("MATCHER_FUNCTION_NAME", "aldi-catalog-matcher")

SYSTEM_PROMPT = (
    "You are a grocery price analyst. You are shown frames from a video walk through a grocery store aisle. "
    "Extract EVERY unique grocery item and shelf price tag visible across all frames. "
    "Return ONLY one valid JSON object (no prose, no markdown) with exactly two keys:\n"
    "\"items\": an array of objects, each with exactly these keys: "
    "brand (string), product_name (string), size_text (string, e.g. '12 ct' or '1 gal'), "
    "size_oz (number or null), in_store_price (float, dollars), unit_price_text (string, '' if not visible), "
    "canonical_commodity (string, generic name such as 'whole milk', 'eggs', 'potato chips'), "
    "confidence ('high' if the text is clearly legible, otherwise 'low'), "
    "upc_barcode (string of digits if a barcode number is clearly readable, otherwise null), "
    "barcode_status ('read' if upc_barcode is filled in, 'unreadable' if a barcode is visible but cannot be read, "
    "'not_visible' if no barcode is visible).\n"
    "\"unreadable_tags\": an array of objects for shelf tags or barcodes that are visible but cannot be read, each with "
    "reason ('blur' | 'glare' | 'cut_off' | 'barcode_unreadable') and partial_text (string, whatever text can be made out, '' if none).\n"
    "Never guess a barcode number or a price; use null / unreadable_tags instead. "
    "Deduplicate items seen in multiple frames. If nothing is visible return {\"items\": [], \"unreadable_tags\": []}."
)

VALID_REASONS = {"blur", "glare", "cut_off", "barcode_unreadable"}
VALID_BARCODE = {"read", "unreadable", "not_visible"}


def put_status(bucket, walk_id, stage, message, **extra):
    body = {"walk_id": walk_id, "stage": stage, "message": message}
    body.update(extra)
    s3.put_object(
        Bucket=bucket,
        Key="public/status.json",
        Body=json.dumps(body).encode(),
        ContentType="application/json",
        CacheControl="no-cache",
    )


def parse_result(text):
    """Parse model output into (items, unreadable_tags). Accepts the new object or a legacy bare array."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S) or re.search(r"\[.*\]", text, re.S)
        if not m:
            raise
        data = json.loads(m.group(0))
    if isinstance(data, list):
        data = {"items": data, "unreadable_tags": []}
    items = [i for i in data.get("items", []) if isinstance(i, dict)]
    for it in items:
        upc = it.get("upc_barcode")
        it["upc_barcode"] = re.sub(r"\D", "", str(upc)) or None if upc else None
        status = str(it.get("barcode_status", "")).lower()
        if status not in VALID_BARCODE:
            status = "read" if it["upc_barcode"] else "not_visible"
        if status == "read" and not it["upc_barcode"]:
            status = "unreadable"
        it["barcode_status"] = status
    tags = []
    for t in data.get("unreadable_tags", []) or []:
        if not isinstance(t, dict):
            continue
        reason = str(t.get("reason", "blur")).lower()
        tags.append({"reason": reason if reason in VALID_REASONS else "blur",
                     "partial_text": str(t.get("partial_text", ""))[:120]})
    return items, tags


def call_claude(model, content, bucket, walk_id, batch_index):
    """Budget-guarded call to Anthropic Messages API; writes exact USD receipt to ops/ai-receipts/."""
    budget_guard.check_budget(s3, bucket, walk_id)

    payload = json.dumps({
        "model": model,
        "max_tokens": CLAUDE_MAX_TOKENS,
        "temperature": 0,
        "system": [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": content}],
    }).encode()
    headers = {
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    last = None
    for attempt in range(3):
        req = urllib.request.Request(API_URL, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read())
            usage = body.get("usage") or {}
            budget_guard.record_usage(s3, bucket, walk_id, batch_index, model, usage)
            return "".join(b.get("text", "") for b in body.get("content", []) if b.get("type") == "text")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            last = RuntimeError(f"Anthropic API HTTP {e.code}: {detail}")
            if e.code in (429, 500, 502, 503, 529) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise last
        except urllib.error.URLError as e:
            last = RuntimeError(f"Anthropic API connection error: {e}")
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise last
    raise last


def low_conf_ratio(items):
    if not items:
        return 0.0
    return sum(1 for i in items if str(i.get("confidence", "")).lower() != "high") / len(items)


def analyze(content, bucket, walk_id, batch_index):
    """Haiku first; fall back to Sonnet on error or >30% low-confidence tags (if budget allows)."""
    haiku = None
    try:
        haiku = parse_result(call_claude(PRIMARY_MODEL, content, bucket, walk_id, batch_index))
        ratio = low_conf_ratio(haiku[0])
        if ratio <= LOW_CONF_THRESHOLD:
            return haiku[0], haiku[1], PRIMARY_MODEL
        print(f"{PRIMARY_MODEL} low-confidence ratio {ratio:.2f} > {LOW_CONF_THRESHOLD}; falling back")
    except budget_guard.BudgetExceededError:
        raise
    except Exception as e:
        print(f"Model {PRIMARY_MODEL} failed: {e!r}")
    try:
        items, tags = parse_result(call_claude(FALLBACK_MODEL, content, bucket, walk_id, batch_index))
        return items, tags, FALLBACK_MODEL
    except budget_guard.BudgetExceededError:
        if haiku is not None:
            return haiku[0], haiku[1], PRIMARY_MODEL
        raise
    except Exception as e:
        print(f"Model {FALLBACK_MODEL} failed: {e!r}")
        if haiku is not None:
            return haiku[0], haiku[1], PRIMARY_MODEL
        raise


def handler(event, context):
    for rec in event.get("Records", []):
        msg = json.loads(rec["body"])
        bucket = msg["bucket"]
        walk_id = msg["walk_id"]
        idx = int(msg["batch_index"])
        total = int(msg["total_batches"])
        store = msg.get("store", "Other")
        zip_code = msg.get("zip_code", "00000")
        metrics = msg.get("frame_metrics", {})
        meta = {"store": store, "zip_code": zip_code, "frame_metrics": metrics}
        try:
            content = []
            for k in msg["frame_keys"]:
                raw = s3.get_object(Bucket=bucket, Key=k)["Body"].read()
                content.append({"type": "image", "source": {
                    "type": "base64", "media_type": "image/jpeg",
                    "data": base64.b64encode(raw).decode("ascii")}})
            content.append({"type": "text",
                            "text": "Extract all grocery items and shelf prices from these store frames. Return only the JSON object."})
            items, unreadable, model = analyze(content, bucket, walk_id, idx)
            s3.put_object(
                Bucket=bucket,
                Key=f"extracted-json/{walk_id}/batch_{idx:04d}.json",
                Body=json.dumps({
                    "walk_id": walk_id,
                    "batch_index": idx,
                    "total_batches": total,
                    "model": model,
                    "store": store,
                    "zip_code": zip_code,
                    "frame_metrics": metrics,
                    "items": items,
                    "unreadable_tags": unreadable,
                }).encode(),
                ContentType="application/json",
            )
            put_status(bucket, walk_id, "analyzing_frames",
                       f"Analyzed batch {idx + 1} of {total} ({len(items)} items, {model}).", **meta)
            lam.invoke(
                FunctionName=MATCHER,
                InvocationType="Event",
                Payload=json.dumps({
                    "bucket": bucket, "walk_id": walk_id,
                    "batch_index": idx, "total_batches": total,
                    "store": store, "zip_code": zip_code,
                }).encode(),
            )
        except budget_guard.BudgetExceededError as e:
            # Do NOT re-raise to SQS — we want the message deleted immediately, not retried 3x.
            print("BUDGET_GUARD_STOP", repr(e))
            put_status(bucket, walk_id, "error", str(e), **meta)
            continue
        except Exception as e:
            print("ERROR", repr(e))
            put_status(bucket, walk_id, "error", f"Vision analysis failed: {str(e)[:300]}", **meta)
            raise
    return {"ok": True}
