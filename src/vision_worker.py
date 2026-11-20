import base64
import json
import os
import re
import time
import urllib.error
import urllib.request

import boto3

s3 = boto3.client("s3")
lam = boto3.client("lambda")

API_URL = "https://api.anthropic.com/v1/messages"
PRIMARY_MODEL = "claude-haiku-4-5"
FALLBACK_MODEL = "claude-sonnet-4-5"
LOW_CONF_THRESHOLD = 0.30
MATCHER = os.environ.get("MATCHER_FUNCTION_NAME", "aldi-catalog-matcher")

SYSTEM_PROMPT = (
    "You are a grocery price forensics analyst. You are shown frames from a video walk through an Aldi store. "
    "Extract EVERY unique grocery item and shelf price tag visible across all frames. "
    "Return ONLY a valid JSON array (no prose, no markdown) of objects with exactly these keys: "
    "brand (string), product_name (string), size_text (string, e.g. '12 ct' or '1 gal'), "
    "size_oz (number or null), in_store_price (float, dollars), unit_price_text (string, '' if not visible), "
    "canonical_commodity (string, generic name such as 'whole milk', 'eggs', 'potato chips'), "
    "confidence ('high' if the text is clearly legible, otherwise 'low'). "
    "Do not invent items that are not visible. Deduplicate items seen in multiple frames. "
    "If nothing is visible return []."
)


def put_status(bucket, walk_id, stage, message):
    s3.put_object(
        Bucket=bucket,
        Key="public/status.json",
        Body=json.dumps({"walk_id": walk_id, "stage": stage, "message": message}).encode(),
        ContentType="application/json",
        CacheControl="no-cache",
    )


def parse_json_array(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", text, re.S)
        if not m:
            raise
        data = json.loads(m.group(0))
    if isinstance(data, dict):
        data = data.get("items", [data])
    return data


def call_claude(model, content):
    payload = json.dumps({
        "model": model,
        "max_tokens": 4096,
        "temperature": 0,
        "system": SYSTEM_PROMPT,
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


def analyze(content):
    """Haiku first; fall back to Sonnet on error or >30% low-confidence tags."""
    haiku_items = None
    try:
        haiku_items = parse_json_array(call_claude(PRIMARY_MODEL, content))
        ratio = low_conf_ratio(haiku_items)
        if ratio <= LOW_CONF_THRESHOLD:
            return haiku_items, PRIMARY_MODEL
        print(f"{PRIMARY_MODEL} low-confidence ratio {ratio:.2f} > {LOW_CONF_THRESHOLD}; falling back")
    except Exception as e:
        print(f"Model {PRIMARY_MODEL} failed: {e!r}")
    try:
        return parse_json_array(call_claude(FALLBACK_MODEL, content)), FALLBACK_MODEL
    except Exception as e:
        print(f"Model {FALLBACK_MODEL} failed: {e!r}")
        if haiku_items is not None:
            return haiku_items, PRIMARY_MODEL
        raise


def handler(event, context):
    for rec in event.get("Records", []):
        msg = json.loads(rec["body"])
        bucket = msg["bucket"]
        walk_id = msg["walk_id"]
        idx = int(msg["batch_index"])
        total = int(msg["total_batches"])
        try:
            content = []
            for k in msg["frame_keys"]:
                raw = s3.get_object(Bucket=bucket, Key=k)["Body"].read()
                content.append({"type": "image", "source": {
                    "type": "base64", "media_type": "image/jpeg",
                    "data": base64.b64encode(raw).decode("ascii")}})
            content.append({"type": "text",
                            "text": "Extract all grocery items and shelf prices from these store frames. Return only the JSON array."})
            items, model = analyze(content)
            s3.put_object(
                Bucket=bucket,
                Key=f"extracted-json/{walk_id}/batch_{idx:04d}.json",
                Body=json.dumps(items).encode(),
                ContentType="application/json",
            )
            put_status(bucket, walk_id, "analyzing_frames",
                       f"Analyzed batch {idx + 1} of {total} ({len(items)} items, {model}).")
            lam.invoke(
                FunctionName=MATCHER,
                InvocationType="Event",
                Payload=json.dumps({
                    "bucket": bucket, "walk_id": walk_id,
                    "batch_index": idx, "total_batches": total,
                }).encode(),
            )
        except Exception as e:
            print("ERROR", repr(e))
            put_status(bucket, walk_id, "error", f"Vision analysis failed: {str(e)[:300]}")
            raise
    return {"ok": True}
