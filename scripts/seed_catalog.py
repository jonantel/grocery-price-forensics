"""
Seed or update an S3 ZIP/store price catalog from a local CSV or JSON file ($0 API cost).

CSV columns: name,price[,upc,merchant,url]
JSON format: [{"name": "...", "price": 3.49, "upc": "...", "merchant": "Instacart", "url": "..."}]

Usage:
  python scripts/seed_catalog.py --store Aldi --zip 20176 --input my_instacart_prices.csv
"""
import argparse
import csv
import json
import os
import re
from datetime import datetime, timezone

import boto3

BUCKET = os.environ.get("S3_BUCKET", "aldi-price-forensics-575554646331")


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:20] or "other"


def load_items(path, default_merchant):
    ext = os.path.splitext(path)[1].lower()
    items = []
    if ext == ".csv":
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                name = (row.get("name") or "").strip()
                price = str(row.get("price") or "").replace("$", "").strip()
                if not name or not price:
                    continue
                items.append({
                    "name": name,
                    "price": round(float(price), 2),
                    "upc": re.sub(r"\D", "", row.get("upc") or "") or None,
                    "merchant": (row.get("merchant") or default_merchant).strip(),
                    "url": (row.get("url") or "").strip() or None,
                    "source": "zip_catalog",
                })
    else:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        raw = data.get("items", data) if isinstance(data, dict) else data
        for r in raw:
            items.append({
                "name": str(r["name"]).strip(),
                "price": round(float(str(r["price"]).replace("$", "")), 2),
                "upc": re.sub(r"\D", "", str(r.get("upc") or "")) or None,
                "merchant": r.get("merchant") or default_merchant,
                "url": r.get("url"),
                "source": r.get("source") or "zip_catalog",
            })
    return items


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--store", required=True, help="Store name (e.g. Aldi, Giant, Wegmans, Gerbes)")
    p.add_argument("--zip", default="", help="5-digit ZIP code (omit for chain-wide catalog)")
    p.add_argument("--input", required=True, help="Path to CSV or JSON file")
    p.add_argument("--merchant", default="Instacart", help="Default merchant label")
    args = p.parse_args()

    slug = slugify(args.store)
    key = f"online-catalog/{slug}_{args.zip}.json" if re.fullmatch(r"\d{5}", args.zip) else f"online-catalog/{slug}.json"
    items = load_items(args.input, args.merchant)

    s3 = boto3.client("s3")
    doc = {
        "store": args.store,
        "zip_code": args.zip or None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "items": items,
    }
    s3.put_object(Bucket=BUCKET, Key=key, Body=json.dumps(doc, indent=2).encode(), ContentType="application/json")
    print(f"Uploaded {len(items)} items to s3://{BUCKET}/{key}")


if __name__ == "__main__":
    main()
