import io
import json
import re
from datetime import datetime, timezone

import boto3
import pandas as pd
from botocore.exceptions import ClientError
from rapidfuzz import fuzz, process

s3 = boto3.client("s3")

MAX_RUNS = 100
MAX_POINTS_PER_PRODUCT = 500

# Built-in reference of Aldi items as listed on Instacart (illustrative SAMPLE prices, not live data).
DEFAULT_CATALOG = [
    {"name": "Clancy's Wavy Potato Chips 10 oz", "price": 2.19},
    {"name": "Friendly Farms Whole Milk 1 gal", "price": 3.85},
    {"name": "Goldhen Large Eggs 12 ct", "price": 2.75},
    {"name": "Millville Crispy Oats 12 oz", "price": 1.99},
    {"name": "Simply Nature Organic Baby Spinach 5 oz", "price": 2.89},
    {"name": "Kirkwood Boneless Skinless Chicken Breasts 1 lb", "price": 3.29},
    {"name": "L'oven Fresh White Bread 20 oz", "price": 1.65},
    {"name": "Friendly Farms 2% Reduced Fat Milk 1 gal", "price": 3.75},
    {"name": "Happy Farms Shredded Mozzarella Cheese 8 oz", "price": 2.45},
    {"name": "Happy Farms Unsalted Butter 16 oz", "price": 4.35},
    {"name": "Never Any! Ground Beef 80/20 1 lb", "price": 5.29},
    {"name": "Fresh Bananas 1 lb", "price": 0.55},
    {"name": "Fresh Gala Apples 3 lb", "price": 4.49},
    {"name": "Russet Potatoes 5 lb", "price": 3.49},
    {"name": "Yellow Onions 3 lb", "price": 2.99},
    {"name": "Specially Selected Pasta Sauce 24 oz", "price": 2.95},
    {"name": "Priano Spaghetti Pasta 16 oz", "price": 1.29},
    {"name": "Season's Choice Frozen Broccoli Florets 12 oz", "price": 1.69},
    {"name": "Nature's Nectar Orange Juice 52 oz", "price": 3.49},
    {"name": "Bremer Cheddar Cheese Slices 12 oz", "price": 2.69},
    {"name": "Belmont Peanut Butter 16 oz", "price": 2.15},
    {"name": "Specially Selected Greek Yogurt Plain 32 oz", "price": 3.99},
    {"name": "Fusia Soy Sauce 10 oz", "price": 2.29},
    {"name": "Benton's Chocolate Chip Cookies 13 oz", "price": 2.49},
    {"name": "Sweet Harvest Granulated Sugar 4 lb", "price": 3.39},
]


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


def load_catalog(bucket, store_slug, zip_code):
    """zip catalog -> chain catalog -> reference.json -> built-in sample. Returns (items, source, file)."""
    candidates = [
        (f"online-catalog/{store_slug}_{zip_code}.json", "zip_catalog"),
        (f"online-catalog/{store_slug}.json", "chain_catalog"),
        ("online-catalog/reference.json", "chain_catalog"),
    ]
    for key, source in candidates:
        try:
            data = get_json(bucket, key)
        except Exception as e:
            print(f"catalog {key} unreadable: {e!r}")
            continue
        if isinstance(data, dict):
            data = data.get("items", [])
        if data:
            return data, source, key
    return DEFAULT_CATALOG, "sample_fallback", "built-in"


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
    try:
        keys = list_batches(bucket, walk_id)
        if len(keys) < total:
            print(f"{len(keys)}/{total} batches ready; waiting")
            return {"waiting": True}

        put_status(bucket, walk_id, "matching", "Comparing with online prices...", store=store, zip_code=zip_code)

        seen, unreadable, frame_metrics = {}, [], {}
        for k in keys:
            batch = json.loads(s3.get_object(Bucket=bucket, Key=k)["Body"].read())
            if isinstance(batch, list):  # legacy batch format
                batch = {"items": batch}
            store = batch.get("store", store)
            zip_code = batch.get("zip_code", zip_code)
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

        store_slug = slugify(store)
        catalog, catalog_source, catalog_file = load_catalog(bucket, store_slug, zip_code)
        names = [c["name"] for c in catalog]
        norm_names = [norm(n) for n in names]

        rows = []
        for it in seen.values():
            label = f"{it.get('brand', '')} {it.get('product_name', '')} {it.get('size_text', '')}".strip()
            store_price = to_float(it.get("in_store_price"))
            row = {
                "walk_id": walk_id,
                "store": store,
                "zip_code": zip_code,
                "brand": it.get("brand"),
                "product_name": it.get("product_name"),
                "size_text": it.get("size_text"),
                "size_oz": to_float(it.get("size_oz")),
                "in_store_price": store_price,
                "unit_price_text": it.get("unit_price_text"),
                "canonical_commodity": it.get("canonical_commodity"),
                "confidence": it.get("confidence"),
                "upc_barcode": it.get("upc_barcode"),
                "barcode_status": it.get("barcode_status") or ("read" if it.get("upc_barcode") else "not_visible"),
                "matched_catalog_item": None,
                "match_score": None,
                "online_price": None,
                "dollar_markup": None,
                "pct_markup": None,
            }
            m = process.extractOne(norm(label), norm_names, scorer=fuzz.token_set_ratio, score_cutoff=60)
            if m:
                ci = m[2]
                online = to_float(catalog[ci]["price"])
                row["matched_catalog_item"] = names[ci]
                row["match_score"] = round(m[1], 1)
                row["online_price"] = online
                if store_price and online is not None:
                    row["dollar_markup"] = round(online - store_price, 2)
                    row["pct_markup"] = round((online - store_price) / store_price * 100, 1)
            rows.append(row)

        matched = [r for r in rows if r["online_price"] is not None and r["in_store_price"]]
        summary = {
            "total_items": len(rows),
            "matched_items": len(matched),
            "avg_pct_markup": round(sum(r["pct_markup"] for r in matched) / len(matched), 1) if matched else 0.0,
            "total_basket_savings": round(sum(r["dollar_markup"] for r in matched), 2),
        }
        scan_quality = {
            "total_seconds": frame_metrics.get("total_seconds", 0),
            "kept_frames": frame_metrics.get("kept_frames", 0),
            "dropped_blurry_frames": frame_metrics.get("dropped_blurry_frames", 0),
            "low_confidence_count": sum(1 for r in rows if str(r.get("confidence", "")).lower() != "high"),
            "unreadable_tags": unreadable,
            "barcodes_read": sum(1 for r in rows if r["barcode_status"] == "read"),
            "barcodes_unreadable": sum(1 for r in rows if r["barcode_status"] == "unreadable")
            + sum(1 for t in unreadable if t.get("reason") == "barcode_unreadable"),
        }

        now = datetime.now(timezone.utc)
        run = {
            "walk_id": walk_id,
            "timestamp": now.isoformat(),
            "generated_at": now.isoformat(),
            "store": store,
            "zip_code": zip_code,
            "catalog_source": catalog_source,
            "catalog_file": catalog_file,
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
            "timestamp": now.isoformat(),
            "store": store,
            "zip_code": zip_code,
            "item_count": len(rows),
            "avg_markup_pct": summary["avg_pct_markup"],
            "catalog_source": catalog_source,
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
                "in_store_price": r["in_store_price"],
                "online_price": r["online_price"],
                "walk_id": walk_id,
            })
            entry["points"] = pts[-MAX_POINTS_PER_PRODUCT:]
        put_json(bucket, "public/price_history.json", hist)

        if rows:
            df = pd.DataFrame(rows)
            for c in ("in_store_price", "size_oz", "online_price", "dollar_markup", "pct_markup", "match_score"):
                df[c] = pd.to_numeric(df[c], errors="coerce")
            df["catalog_source"] = catalog_source
            df["captured_at"] = now.isoformat()
            buf = io.BytesIO()
            df.to_parquet(buf, engine="pyarrow", compression="snappy", index=False)
            s3.put_object(Bucket=bucket,
                          Key=f"parquet/compacted/prices_{now.strftime('%Y%m%dT%H%M%S')}.parquet",
                          Body=buf.getvalue())

        put_status(bucket, walk_id, "complete", f"Done: {len(rows)} items, {len(matched)} matched.",
                   store=store, zip_code=zip_code, catalog_source=catalog_source)
        return {"ok": True, "items": len(rows)}
    except Exception as e:
        print("ERROR", repr(e))
        put_status(bucket, walk_id, "error", f"Matching failed: {e}", store=store, zip_code=zip_code)
        raise
