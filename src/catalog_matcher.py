import io
import json
import re
from datetime import datetime, timezone

import boto3
import budget_guard
import pandas as pd
import price_lookup
from botocore.exceptions import ClientError

s3 = boto3.client("s3")

MAX_RUNS = 100
MAX_POINTS_PER_PRODUCT = 500


def norm(s):
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower().replace("'", "")).strip()


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:20] or "other"


def get_json(bucket, key, default=None):
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "404", "NotFound"):
            return default
        raise


def put_json(bucket, key, obj, indent=None):
    s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(obj, indent=indent).encode(),
                  ContentType="application/json", CacheControl="no-cache")


def put_status(bucket, walk_id, stage, message, **extra):
    body = {"walk_id": walk_id, "stage": stage, "message": message}
    body.update(extra)
    put_json(bucket, "public/status.json", body, indent=2)


def list_batches(bucket, walk_id):
    keys, token = [], None
    while True:
        kw = {"Bucket": bucket, "Prefix": f"extracted-json/{walk_id}/"}
        if token:
            kw["ContinuationToken"] = token
        r = s3.list_objects_v2(**kw)
        keys += [o["Key"] for o in r.get("Contents", []) if o["Key"].endswith(".json")]
        if not r.get("IsTruncated"):
            return sorted(keys)
        token = r["NextContinuationToken"]


def to_float(v):
    try:
        return float(str(v).replace("$", "").strip())
    except Exception:
        return None


def handler(event, context):
    bucket = event["bucket"]
    walk_id = event["walk_id"]
    total = int(event.get("total_batches", 1))
    store = event.get("store", "Other")
    zip_code = event.get("zip_code", "00000")
    scan_type = event.get("scan_type", "video")
    try:
        keys = list_batches(bucket, walk_id)
        if len(keys) < total:
            print(f"{len(keys)}/{total} batches ready; waiting")
            return {"waiting": True}

        put_status(bucket, walk_id, "matching", "Comparing with online prices...",
                   store=store, zip_code=zip_code, scan_type=scan_type)

        seen, unreadable, frame_metrics, receipt_meta = {}, [], {}, {}
        for k in keys:
            batch = json.loads(s3.get_object(Bucket=bucket, Key=k)["Body"].read())
            if isinstance(batch, list):
                batch = {"items": batch}
            store = batch.get("store", store)
            zip_code = batch.get("zip_code", zip_code)
            scan_type = batch.get("scan_type", scan_type)
            receipt_meta = receipt_meta or batch.get("receipt_meta") or {}
            frame_metrics = frame_metrics or batch.get("frame_metrics", {})
            for t in batch.get("unreadable_tags", []):
                if t not in unreadable:
                    unreadable.append(t)
            for it in batch.get("items", []):
                if not isinstance(it, dict):
                    continue
                dk = norm(f"{it.get('brand', '')} {it.get('product_name', '')}")
                if dk and dk not in seen:
                    seen[dk] = it

        items_list = list(seen.values())
        store_slug = slugify(store)
        matches, catalog_source, catalog_file, lookup_stats, resolved_store = price_lookup.resolve_prices(
            s3, bucket, store, store_slug, zip_code, items_list
        )

        rows = []
        for idx, it in enumerate(items_list):
            store_price = to_float(it.get("in_store_price"))
            ev_file = it.get("evidence_file")
            row = {
                "walk_id": walk_id,
                "store": store,
                "zip_code": zip_code,
                "scan_type": scan_type,
                "brand": it.get("brand"),
                "product_name": it.get("product_name"),
                "receipt_raw_text": it.get("receipt_raw_text"),
                "size_text": it.get("size_text"),
                "size_oz": to_float(it.get("size_oz")),
                "in_store_price": store_price,
                "unit_price_text": it.get("unit_price_text"),
                "canonical_commodity": it.get("canonical_commodity"),
                "confidence": it.get("confidence"),
                "upc_barcode": it.get("upc_barcode"),
                "barcode_status": it.get("barcode_status") or ("read" if it.get("upc_barcode") else "not_visible"),
                "evidence_url": f"/grocery/evidence/{walk_id}/{ev_file}" if ev_file else None,
                "human_verified": False,
                "flagged_wrong": False,
                "human_note": "",
                "matched_catalog_item": None,
                "match_score": None,
                "size_verified": None,
                "online_price": None,
                "promo_price": None,
                "dollar_markup": None,
                "pct_markup": None,
                "price_source": None,
                "store_match": None,
                "online_merchant": None,
                "online_url": None,
            }
            m = matches.get(idx)
            if m and m.get("price") is not None:
                online = to_float(m["price"])
                row["matched_catalog_item"] = m.get("name")
                row["match_score"] = m.get("match_score")
                row["size_verified"] = m.get("size_verified")
                row["online_price"] = online
                row["promo_price"] = to_float(m.get("promo_price"))
                row["price_source"] = m.get("source")
                row["store_match"] = m.get("store_match")
                row["online_merchant"] = m.get("merchant")
                row["online_url"] = m.get("url")
                if store_price and online is not None:
                    row["dollar_markup"] = round(online - store_price, 2)
                    row["pct_markup"] = round((online - store_price) / store_price * 100, 1)
            rows.append(row)

        matched = [r for r in rows if r["online_price"] is not None and r["in_store_price"]]
        summary = {
            "total_items": len(rows),
            "matched_items": len(matched),
            "verified_items": 0,
            "avg_pct_markup": round(sum(r["pct_markup"] for r in matched) / len(matched), 1) if matched else 0.0,
            "total_basket_savings": round(sum(r["dollar_markup"] for r in matched), 2),
        }
        scan_quality = {
            "total_seconds": frame_metrics.get("total_seconds", 0),
            "kept_frames": frame_metrics.get("kept_frames", 0),
            "dropped_blurry_frames": frame_metrics.get("dropped_blurry_frames", 0),
            "skipped_duplicate_frames": frame_metrics.get("skipped_duplicate_frames", 0),
            "capped_frames": frame_metrics.get("capped_frames", 0),
            "low_confidence_count": sum(1 for r in rows if str(r.get("confidence", "")).lower() != "high"),
            "unreadable_tags": unreadable,
            "barcodes_read": sum(1 for r in rows if r["barcode_status"] == "read"),
            "barcodes_unreadable": sum(1 for r in rows if r["barcode_status"] == "unreadable")
            + sum(1 for t in unreadable if t.get("reason") == "barcode_unreadable"),
        }
        cost = budget_guard.get_run_cost_summary(s3, bucket, walk_id)

        now = datetime.now(timezone.utc)
        run = {
            "walk_id": walk_id,
            "scan_type": scan_type,
            "receipt_meta": receipt_meta or None,
            "receipt_image_url": f"/grocery/evidence/{walk_id}/receipt.jpg" if scan_type == "receipt" else None,
            "timestamp": now.isoformat(),
            "generated_at": now.isoformat(),
            "store": store,
            "zip_code": zip_code,
            "resolved_store": resolved_store,
            "catalog_source": catalog_source,
            "catalog_file": catalog_file,
            "lookup_stats": lookup_stats,
            "cost": cost,
            "summary": summary,
            "scan_quality": scan_quality,
            "items": rows,
        }
        put_json(bucket, f"public/runs/{walk_id}.json", run, indent=2)
        put_json(bucket, "public/latest_prices.json", run, indent=2)

        # Runs index: newest first, last 100, one entry per walk_id.
        index = get_json(bucket, "public/runs_index.json", {"runs": []})
        runs = [r for r in index.get("runs", []) if r.get("walk_id") != walk_id]
        runs.insert(0, {
            "walk_id": walk_id,
            "scan_type": scan_type,
            "timestamp": now.isoformat(),
            "store": store,
            "zip_code": zip_code,
            "item_count": len(rows),
            "avg_markup_pct": summary["avg_pct_markup"],
            "catalog_source": catalog_source,
            "run_usd": cost["run_usd"],
        })
        put_json(bucket, "public/runs_index.json", {"runs": runs[:MAX_RUNS]})

        # Price history grouped by normalized brand + product name; one point per walk_id per product.
        hist = get_json(bucket, "public/price_history.json", {"products": {}})
        products = hist.setdefault("products", {})
        for r in rows:
            pk = norm(f"{r.get('brand', '')} {r.get('product_name', '')}")
            if not pk or r["in_store_price"] is None:
                continue
            entry = products.setdefault(pk, {"product_name": f"{r.get('brand') or ''} {r.get('product_name') or ''}".strip(),
                                             "points": []})
            pts = [p for p in entry["points"] if p.get("walk_id") != walk_id]
            pts.append({
                "date": now.strftime("%Y-%m-%d"),
                "store": store,
                "zip_code": zip_code,
                "scan_type": scan_type,
                "in_store_price": r["in_store_price"],
                "online_price": r["online_price"],
                "price_source": r["price_source"],
                "human_verified": False,
                "walk_id": walk_id,
            })
            entry["points"] = pts[-MAX_POINTS_PER_PRODUCT:]
        put_json(bucket, "public/price_history.json", hist)

        if rows:
            df = pd.DataFrame(rows)
            for c in ("in_store_price", "size_oz", "online_price", "promo_price", "dollar_markup", "pct_markup", "match_score"):
                df[c] = pd.to_numeric(df[c], errors="coerce")
            df["catalog_source"] = catalog_source
            df["captured_at"] = now.isoformat()
            buf = io.BytesIO()
            df.to_parquet(buf, engine="pyarrow", compression="snappy", index=False)
            s3.put_object(Bucket=bucket,
                          Key=f"parquet/compacted/prices_{now.strftime('%Y%m%dT%H%M%S')}.parquet",
                          Body=buf.getvalue())

        put_status(bucket, walk_id, "complete", f"Done: {len(rows)} items, {len(matched)} matched.",
                   store=store, zip_code=zip_code, scan_type=scan_type, catalog_source=catalog_source, run_usd=cost["run_usd"])
        return {"ok": True, "items": len(rows)}
    except Exception as e:
        print("ERROR", repr(e))
        put_status(bucket, walk_id, "error", f"Matching failed: {e}", store=store, zip_code=zip_code, scan_type=scan_type)
        raise
