import io
import json
import re
import time
from datetime import datetime, timezone

import boto3
import pandas as pd
from rapidfuzz import fuzz, process

s3 = boto3.client("s3")

# Built-in reference of Aldi items as listed on Instacart (illustrative prices; override via online-catalog/reference.json).
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


def load_catalog(bucket):
    try:
        obj = s3.get_object(Bucket=bucket, Key="online-catalog/reference.json")
        data = json.loads(obj["Body"].read())
        if isinstance(data, dict):
            data = data.get("items", [])
        return data
    except s3.exceptions.NoSuchKey:
        return DEFAULT_CATALOG
    except Exception as e:
        print("catalog load failed, using default:", repr(e))
        return DEFAULT_CATALOG


def put_json(bucket, key, obj):
    s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(obj, indent=2).encode(),
                  ContentType="application/json", CacheControl="no-cache")


def put_status(bucket, walk_id, stage, message):
    put_json(bucket, "public/status.json", {"walk_id": walk_id, "stage": stage, "message": message})


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
    try:
        keys = list_batches(bucket, walk_id)
        if len(keys) < total:
            print(f"{len(keys)}/{total} batches ready; waiting")
            return {"waiting": True}

        put_status(bucket, walk_id, "matching", "Matching items against Instacart catalog...")
        seen = {}
        for k in keys:
            items = json.loads(s3.get_object(Bucket=bucket, Key=k)["Body"].read())
            for it in items:
                if not isinstance(it, dict):
                    continue
                dk = norm(f"{it.get('brand', '')} {it.get('product_name', '')}")
                if dk and dk not in seen:
                    seen[dk] = it

        catalog = load_catalog(bucket)
        names = [c["name"] for c in catalog]
        norm_names = [norm(n) for n in names]
        rows = []
        for it in seen.values():
            label = f"{it.get('brand', '')} {it.get('product_name', '')} {it.get('size_text', '')}".strip()
            store = to_float(it.get("in_store_price"))
            row = {
                "walk_id": walk_id,
                "brand": it.get("brand"),
                "product_name": it.get("product_name"),
                "size_text": it.get("size_text"),
                "size_oz": to_float(it.get("size_oz")),
                "in_store_price": store,
                "unit_price_text": it.get("unit_price_text"),
                "canonical_commodity": it.get("canonical_commodity"),
                "confidence": it.get("confidence"),
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
                if store and online is not None:
                    row["dollar_markup"] = round(online - store, 2)
                    row["pct_markup"] = round((online - store) / store * 100, 1)
            rows.append(row)

        matched = [r for r in rows if r["online_price"] is not None and r["in_store_price"]]
        summary = {
            "total_items": len(rows),
            "matched_items": len(matched),
            "avg_pct_markup": round(sum(r["pct_markup"] for r in matched) / len(matched), 1) if matched else 0.0,
            "total_basket_savings": round(sum(r["dollar_markup"] for r in matched), 2),
        }
        now = datetime.now(timezone.utc)
        put_json(bucket, "public/latest_prices.json", {
            "walk_id": walk_id,
            "generated_at": now.isoformat(),
            "summary": summary,
            "items": rows,
        })

        if rows:
            df = pd.DataFrame(rows)
            for c in ("in_store_price", "size_oz", "online_price", "dollar_markup", "pct_markup", "match_score"):
                df[c] = pd.to_numeric(df[c], errors="coerce")
            df["captured_at"] = now.isoformat()
            buf = io.BytesIO()
            df.to_parquet(buf, engine="pyarrow", compression="snappy", index=False)
            s3.put_object(Bucket=bucket,
                          Key=f"parquet/compacted/prices_{now.strftime('%Y%m%dT%H%M%S')}.parquet",
                          Body=buf.getvalue())

        put_status(bucket, walk_id, "complete", f"Done: {len(rows)} items, {len(matched)} matched.")
        return {"ok": True, "items": len(rows)}
    except Exception as e:
        print("ERROR", repr(e))
        put_status(bucket, walk_id, "error", f"Matching failed: {e}")
        raise
