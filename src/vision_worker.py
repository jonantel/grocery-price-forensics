import base64
import json
import os
import re
import time
import urllib.error
import urllib.request

import boto3
import budget_guard
import cv2
import numpy as np

s3 = boto3.client("s3")
lam = boto3.client("lambda")

API_URL = "https://api.anthropic.com/v1/messages"
PRIMARY_MODEL = "claude-haiku-4-5"
FALLBACK_MODEL = "claude-sonnet-4-5"
LOW_CONF_THRESHOLD = 0.30
CLAUDE_MAX_TOKENS = int(os.environ.get("CLAUDE_MAX_TOKENS", "2048"))
MATCHER = os.environ.get("MATCHER_FUNCTION_NAME", "aldi-catalog-matcher")

VIDEO_SYSTEM_PROMPT = (
    "You are a grocery price analyst. You are shown 0-indexed frames from a video walk through a grocery store aisle. "
    "Extract EVERY unique grocery item and shelf price tag visible across all frames. "
    "Return ONLY one valid JSON object (no prose, no markdown) with exactly two keys:\n"
    "\"items\": an array of objects, each with exactly these keys: "
    "brand (string), product_name (string), size_text (string, e.g. '12 ct' or '1 gal'), "
    "size_oz (number or null), in_store_price (float, dollars), unit_price_text (string, '' if not visible), "
    "canonical_commodity (string, generic name such as 'whole milk', 'eggs', 'potato chips'), "
    "confidence ('high' if the text is clearly legible, otherwise 'low'), "
    "upc_barcode (string of digits if a barcode number is clearly readable, otherwise null), "
    "barcode_status ('read' if upc_barcode is filled in, 'unreadable' if a barcode is visible but cannot be read, 'not_visible' if no barcode is visible), "
    "frame_index (integer 0-based index of the clearest frame showing this item's shelf tag), "
    "bbox (array of 4 normalized floats [x1, y1, x2, y2] in 0.0-1.0 tightly framing the shelf price tag/item label in that frame, or null if unsure).\n"
    "\"unreadable_tags\": an array of objects for shelf tags or barcodes that are visible but cannot be read, each with "
    "reason ('blur' | 'glare' | 'cut_off' | 'barcode_unreadable') and partial_text (string, whatever text can be made out, '' if none).\n"
    "Never guess a barcode number or a price; use null / unreadable_tags instead. "
    "Deduplicate items seen in multiple frames. If nothing is visible return {\"items\": [], \"unreadable_tags\": []}."
)

RECEIPT_SYSTEM_PROMPT = (
    "You are a grocery receipt analyst. You are shown a photo of a paper grocery checkout receipt. "
    "Extract EVERY purchased grocery or household product line item (skip subtotal, tax, total, discounts/savings lines, bag fees, payment lines). "
    "Expand abbreviated receipt names into clear human-readable brand + product_name when confident (e.g., 'GV WHL MLK 1G' -> brand 'Great Value', product_name 'Whole Milk', size_text '1 gal'). "
    "Return ONLY one valid JSON object (no prose, no markdown) with these keys:\n"
    "\"receipt_meta\": {\"receipt_date\": string or null, \"receipt_total\": number or null, \"store_header\": string or null},\n"
    "\"items\": an array of objects, each with: "
    "brand (string), product_name (string), receipt_raw_text (exact line text as printed), size_text (string, '' if unknown), "
    "size_oz (number or null), in_store_price (float, final line price in dollars), unit_price_text (string, '' if none), "
    "canonical_commodity (string, e.g. 'whole milk', 'eggs'), confidence ('high' or 'low'), "
    "upc_barcode (string of digits if printed on the receipt line, otherwise null), "
    "barcode_status ('read' if upc_barcode is present, otherwise 'not_visible'), "
    "frame_index (0), bbox (array of 4 normalized floats [x1, y1, x2, y2] in 0.0-1.0 framing that receipt line, or null).\n"
    "\"unreadable_tags\": an array of {\"reason\": \"blur\"|\"glare\"|\"cut_off\", \"partial_text\": string} for any smudged/cut-off receipt lines.\n"
    "If no items are legible return {\"receipt_meta\": {}, \"items\": [], \"unreadable_tags\": []}."
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
    """Parse model output into (items, unreadable_tags, receipt_meta)."""
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
    receipt_meta = data.get("receipt_meta") if isinstance(data.get("receipt_meta"), dict) else {}
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
        try:
            it["frame_index"] = int(it.get("frame_index", 0) or 0)
        except Exception:
            it["frame_index"] = 0
        bb = it.get("bbox")
        if isinstance(bb, list) and len(bb) == 4:
            try:
                it["bbox"] = [float(v) for v in bb]
            except Exception:
                it["bbox"] = None
        else:
            it["bbox"] = None
    tags = []
    for t in data.get("unreadable_tags", []) or []:
        if not isinstance(t, dict):
            continue
        reason = str(t.get("reason", "blur")).lower()
        tags.append({"reason": reason if reason in VALID_REASONS else "blur",
                     "partial_text": str(t.get("partial_text", ""))[:120]})
    return items, tags, receipt_meta


def save_evidence_crops(bucket, walk_id, batch_index, raw_frames, items, scan_type):
    """
    Save cropped evidence JPEG for each extracted item to public/evidence/<walk_id>/bXX_iYYY.jpg
    (60-day S3 lifecycle). Falls back to a clean resized frame preview if bbox is missing/invalid.
    """
    decoded = []
    for raw in raw_frames:
        arr = np.frombuffer(raw, np.uint8)
        decoded.append(cv2.imdecode(arr, cv2.IMREAD_COLOR))

    for idx, it in enumerate(items):
        fi = it.get("frame_index", 0)
        if fi < 0 or fi >= len(decoded):
            fi = 0
        img = decoded[fi] if decoded else None
        if img is None:
            continue
        h, w = img.shape[:2]
        crop = None
        bb = it.get("bbox")
        if bb and len(bb) == 4:
            x1, y1, x2, y2 = bb
            # Accept either 0..1 normalized coordinates or 0..1000 / pixel coordinates gracefully.
            if max(bb) > 1.5:
                if max(bb) <= 1000 and max(w, h) > 1000:
                    x1, y1, x2, y2 = x1 / 1000.0, y1 / 1000.0, x2 / 1000.0, y2 / 1000.0
                else:
                    x1, y1, x2, y2 = x1 / w, y1 / h, x2 / w, y2 / h
            if 0.0 <= x1 < x2 <= 1.05 and 0.0 <= y1 < y2 <= 1.05:
                pad_x = max(0.06, (x2 - x1) * 0.15)
                pad_y = max(0.04 if scan_type == "receipt" else 0.08, (y2 - y1) * 0.25)
                px1 = max(0, int((x1 - pad_x) * w))
                py1 = max(0, int((y1 - pad_y) * h))
                px2 = min(w, int((x2 + pad_x) * w))
                py2 = min(h, int((y2 + pad_y) * h))
                if (px2 - px1) >= 40 and (py2 - py1) >= 24:
                    crop = img[py1:py2, px1:px2]

        if crop is None:
            crop = img

        ch, cw = crop.shape[:2]
        max_w = 560 if scan_type == "receipt" else 480
        if cw > max_w:
            crop = cv2.resize(crop, (max_w, max(1, int(ch * (max_w / cw)))), interpolation=cv2.INTER_AREA)

        ok, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 78])
        if ok:
            fname = f"b{batch_index:02d}_i{idx:03d}.jpg"
            ev_key = f"public/evidence/{walk_id}/{fname}"
            s3.put_object(
                Bucket=bucket,
                Key=ev_key,
                Body=buf.tobytes(),
                ContentType="image/jpeg",
                CacheControl="public, max-age=86400",
            )
            it["evidence_file"] = fname
            it["evidence_key"] = ev_key


def call_claude(model, system_prompt, content, bucket, walk_id, batch_index):
    """Budget-guarded call to Anthropic Messages API; writes exact USD receipt to ops/ai-receipts/."""
    budget_guard.check_budget(s3, bucket, walk_id)

    payload = json.dumps({
        "model": model,
        "max_tokens": CLAUDE_MAX_TOKENS,
        "temperature": 0,
        "system": [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
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


def analyze(system_prompt, content, bucket, walk_id, batch_index):
    """Haiku first; fall back to Sonnet on error or >30% low-confidence tags (if budget allows)."""
    haiku = None
    try:
        haiku = parse_result(call_claude(PRIMARY_MODEL, system_prompt, content, bucket, walk_id, batch_index))
        ratio = low_conf_ratio(haiku[0])
        if ratio <= LOW_CONF_THRESHOLD:
            return haiku[0], haiku[1], haiku[2], PRIMARY_MODEL
        print(f"{PRIMARY_MODEL} low-confidence ratio {ratio:.2f} > {LOW_CONF_THRESHOLD}; falling back")
    except budget_guard.BudgetExceededError:
        raise
    except Exception as e:
        print(f"Model {PRIMARY_MODEL} failed: {e!r}")
    try:
        items, tags, meta = parse_result(call_claude(FALLBACK_MODEL, system_prompt, content, bucket, walk_id, batch_index))
        return items, tags, meta, FALLBACK_MODEL
    except budget_guard.BudgetExceededError:
        if haiku is not None:
            return haiku[0], haiku[1], haiku[2], PRIMARY_MODEL
        raise
    except Exception as e:
        print(f"Model {FALLBACK_MODEL} failed: {e!r}")
        if haiku is not None:
            return haiku[0], haiku[1], haiku[2], PRIMARY_MODEL
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
        scan_type = msg.get("scan_type", "video")
        metrics = msg.get("frame_metrics", {})
        meta = {"store": store, "zip_code": zip_code, "scan_type": scan_type, "frame_metrics": metrics}
        try:
            raw_frames = []
            content = []
            for fi, k in enumerate(msg["frame_keys"]):
                raw = s3.get_object(Bucket=bucket, Key=k)["Body"].read()
                raw_frames.append(raw)
                content.append({"type": "text", "text": f"Frame {fi}:"})
                content.append({"type": "image", "source": {
                    "type": "base64", "media_type": "image/jpeg",
                    "data": base64.b64encode(raw).decode("ascii")}})
            if scan_type == "receipt":
                sys_prompt = RECEIPT_SYSTEM_PROMPT
                user_prompt = "Extract every purchased grocery/household item from this checkout receipt photo. Return only the JSON object."
            else:
                sys_prompt = VIDEO_SYSTEM_PROMPT
                user_prompt = "Extract all grocery items, shelf prices, and bounding boxes from these frames. Return only the JSON object."
            content.append({"type": "text", "text": user_prompt})

            items, unreadable, receipt_meta, model = analyze(sys_prompt, content, bucket, walk_id, idx)
            save_evidence_crops(bucket, walk_id, idx, raw_frames, items, scan_type)

            s3.put_object(
                Bucket=bucket,
                Key=f"extracted-json/{walk_id}/batch_{idx:04d}.json",
                Body=json.dumps({
                    "walk_id": walk_id,
                    "scan_type": scan_type,
                    "receipt_meta": receipt_meta,
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
                    "scan_type": scan_type,
                    "batch_index": idx, "total_batches": total,
                    "store": store, "zip_code": zip_code,
                }).encode(),
            )
        except budget_guard.BudgetExceededError as e:
            print("BUDGET_GUARD_STOP", repr(e))
            put_status(bucket, walk_id, "error", str(e), **meta)
            continue
        except Exception as e:
            print("ERROR", repr(e))
            put_status(bucket, walk_id, "error", f"Vision analysis failed: {str(e)[:300]}", **meta)
            raise
    return {"ok": True}
