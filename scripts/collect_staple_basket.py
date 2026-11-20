"""
Proactively collect live prices for the 120-item Master Staple Basket (100 grocery + 20 non-grocery)
across 6 real US Kroger-family locations ($0 official Kroger API) + budget-capped Serper Shopping
multi-retailer sweep (1 query per core item captures Walmart, Target, Instacart, Aldi, etc.).

Writes:
  - s3://<bucket>/public/staple_basket.json
  - s3://<bucket>/online-catalog/<store_slug>_<zip>.json  (seeds 7-day cache for video/receipt scans)
"""
import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import boto3
from rapidfuzz import fuzz

BUCKET = os.environ.get("S3_BUCKET", "aldi-price-forensics-575554646331")
KROGER_CLIENT_ID = os.environ.get("TF_VAR_kroger_client_id", os.environ.get("KROGER_CLIENT_ID", "")).strip()
KROGER_CLIENT_SECRET = os.environ.get("TF_VAR_kroger_client_secret", os.environ.get("KROGER_CLIENT_SECRET", "")).strip()
SERPER_API_KEY = os.environ.get("TF_VAR_serper_api_key", os.environ.get("SERPER_API_KEY", "")).strip()

KROGER_LOCATIONS = [
    {"key": "gerbes_65203", "store": "Gerbes", "slug": "gerbes", "zip": "65203", "city": "Columbia, MO", "domain": "www.gerbes.com"},
    {"key": "harris-teeter_20176", "store": "Harris Teeter", "slug": "harris-teeter", "zip": "20176", "city": "Leesburg, VA", "domain": "www.harristeeter.com"},
    {"key": "kroger_45202", "store": "Kroger", "slug": "kroger", "zip": "45202", "city": "Cincinnati, OH", "domain": "www.kroger.com"},
    {"key": "ralphs_90024", "store": "Ralphs", "slug": "ralphs", "zip": "90024", "city": "Los Angeles, CA", "domain": "www.ralphs.com"},
    {"key": "king-soopers_80202", "store": "King Soopers", "slug": "king-soopers", "zip": "80202", "city": "Denver, CO", "domain": "www.kingsoopers.com"},
    {"key": "fred-meyer_98109", "store": "Fred Meyer", "slug": "fred-meyer", "zip": "98109", "city": "Seattle, WA", "domain": "www.fredmeyer.com"},
]

BLOCKED_MERCHANTS = ("ebay", "mercari", "poshmark", "etsy", "bonanza", "ubuy", "desertcart", "amazon")
MULTIPACK_RE = re.compile(r"\b(?:pack of\s*[2-9]|pack of\s*\d{2}|\d+\s*-?\s*pack|case of\s*\d+|bundle|lot of\s*\d+)\b", re.I)


def norm(s):
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower().replace("'", "")).strip()


def parse_price(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return round(float(v), 2) if 0.10 <= float(v) <= 200.0 else None
    m = re.search(r"\$?\s*(\d{1,3}(?:,\d{3})*\.\d{2})", str(v))
    if not m:
        return None
    val = float(m.group(1).replace(",", ""))
    return round(val, 2) if 0.10 <= val <= 200.0 else None


def parse_oz(text):
    t = str(text or "").lower()
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:fl\.?\s*)?oz\b", t)
    if m:
        return float(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*lb\b", t)
    if m:
        return float(m.group(1)) * 16.0
    if "1 gal" in t or "1 gallon" in t:
        return 128.0
    return None


def http_json(url, method="GET", headers=None, data=None, timeout=15):
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
        except Exception as e:
            last = e
            if attempt < 2:
                time.sleep(1.0)
                continue
            raise
    raise last


def get_kroger_token():
    basic = base64.b64encode(f"{KROGER_CLIENT_ID}:{KROGER_CLIENT_SECRET}".encode()).decode("ascii")
    res = http_json(
        "https://api.kroger.com/v1/connect/oauth2/token",
        method="POST",
        headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
        data=urllib.parse.urlencode({"grant_type": "client_credentials", "scope": "product.compact"}).encode(),
    )
    return res["access_token"]


def resolve_kroger_locations(token):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    resolved = []
    for cfg in KROGER_LOCATIONS:
        qs = urllib.parse.urlencode({"filter.zipCode.near": cfg["zip"], "filter.limit": 5})
        res = http_json(f"https://api.kroger.com/v1/locations?{qs}", headers=headers)
        cands = res.get("data", [])
        if not cands:
            continue
        chosen = cands[0]
        for c in cands:
            if cfg["slug"].replace("-", " ") in norm(c.get("chain", "")) or cfg["slug"].replace("-", " ") in norm(c.get("name", "")):
                chosen = c
                break
        addr = chosen.get("address") or {}
        resolved.append({
            **cfg,
            "locationId": chosen["locationId"],
            "chain": chosen.get("chain"),
            "store_name": chosen.get("name"),
            "address": ", ".join(p for p in [addr.get("addressLine1"), addr.get("city"), addr.get("state"), addr.get("zipCode")] if p),
        })
    return resolved


def pick_best_kroger_product(products, item):
    target_q = norm(item["query"])
    want_oz = item.get("target_oz")
    best, best_rank = None, -1.0
    for p in products:
        first = (p.get("items") or [{}])[0] or {}
        price_obj = first.get("price") or {}
        reg = parse_price(price_obj.get("regular"))
        promo = parse_price(price_obj.get("promo"))
        if reg is None and promo is None:
            continue
        desc = p.get("description", "")
        size_str = first.get("size") or ""
        sim = fuzz.token_set_ratio(target_q, norm(f"{desc} {size_str}"))
        if sim < 50:
            continue
        got_oz = parse_oz(f"{desc} {size_str}")
        adj = 0.0
        if want_oz and got_oz:
            ratio = max(want_oz, got_oz) / min(want_oz, got_oz)
            if ratio > 1.65:
                continue
            adj = 15.0 if ratio <= 1.2 else 0.0
        rank = sim + adj
        if rank > best_rank:
            best_rank = rank
            best = {
                "upc": p.get("upc"),
                "description": desc,
                "size": size_str,
                "regular": reg if reg is not None else promo,
                "promo": promo if (promo and reg and promo < reg) else None,
            }
    return best


def fetch_kroger_for_item(item, locations, token):
    """Find best anchor SKU at location 0, then query exact UPC at every other location (fallback to search)."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    results = {}
    anchor_upc = None

    for idx, loc in enumerate(locations):
        lid = loc["locationId"]
        chosen = None
        if anchor_upc and idx > 0:
            try:
                res = http_json(f"https://api.kroger.com/v1/products/{anchor_upc}?filter.locationId={lid}", headers=headers)
                if res.get("data"):
                    chosen = pick_best_kroger_product([res["data"]], item)
            except Exception:
                pass
        if not chosen:
            try:
                qs = urllib.parse.urlencode({"filter.term": item["query"][:60], "filter.locationId": lid, "filter.limit": 10})
                res = http_json(f"https://api.kroger.com/v1/products?{qs}", headers=headers)
                chosen = pick_best_kroger_product(res.get("data", []), item)
            except Exception:
                chosen = None
        if chosen:
            if idx == 0 and chosen.get("upc"):
                anchor_upc = chosen["upc"]
            results[loc["key"]] = {
                "store": loc["store"],
                "store_name": loc["store_name"],
                "city": loc["city"],
                "zip": loc["zip"],
                "price": chosen["regular"],
                "promo_price": chosen["promo"],
                "product_title": f"{chosen['description']} ({chosen['size']})".strip() if chosen["size"] else chosen["description"],
                "upc": chosen["upc"],
                "same_upc": bool(anchor_upc and chosen["upc"] == anchor_upc),
                "source": "kroger_api",
                "url": f"https://{loc['domain']}/p/-/{chosen['upc']}" if chosen.get("upc") else None,
            }
    return item["id"], results


def fetch_serper_multi_retailer(item):
    """1 Serper Shopping call captures Walmart, Target, Instacart, Aldi, etc. in a single response."""
    if not SERPER_API_KEY:
        return item["id"], {}
    headers = {"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"}
    try:
        res = http_json(
            "https://google.serper.dev/shopping",
            method="POST",
            headers=headers,
            data=json.dumps({"q": item["query"], "gl": "us", "hl": "en", "num": 12}).encode(),
        )
    except Exception:
        return item["id"], {}

    want_retailers = {
        "walmart": ("Walmart", "walmart_us"),
        "target": ("Target", "target_us"),
        "instacart": ("Instacart", "instacart_us"),
        "aldi": ("Aldi", "aldi_us"),
        "wegmans": ("Wegmans", "wegmans_us"),
        "hy-vee": ("Hy-Vee", "hy-vee_us"),
    }
    found = {}
    want_oz = item.get("target_oz")
    for s in res.get("shopping", []) or []:
        merch = s.get("source", "") or ""
        mlow = merch.lower()
        if any(b in mlow for b in BLOCKED_MERCHANTS) or "seller" in mlow:
            continue
        title = s.get("title", "")
        if MULTIPACK_RE.search(title) and not MULTIPACK_RE.search(item["query"]):
            continue
        price = parse_price(s.get("price"))
        if price is None:
            continue
        if fuzz.token_set_ratio(norm(item["query"]), norm(title)) < 55:
            continue
        got_oz = parse_oz(title)
        if want_oz and got_oz and (max(want_oz, got_oz) / min(want_oz, got_oz) > 1.65):
            continue
        for needle, (label, rkey) in want_retailers.items():
            if needle in mlow and rkey not in found:
                url = s.get("link") or s.get("productUrl")
                if not url and s.get("productId"):
                    url = f"https://www.google.com/shopping/product/{s['productId']}"
                found[rkey] = {
                    "store": label,
                    "store_name": merch,
                    "city": "Online US",
                    "zip": "US",
                    "price": price,
                    "promo_price": None,
                    "product_title": title,
                    "upc": None,
                    "same_upc": False,
                    "source": "instacart_search" if needle == "instacart" else "google_shopping",
                    "url": url,
                }
                break
    return item["id"], found


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--basket", default="data/master_basket.json")
    p.add_argument("--serper-core", action="store_true", default=True, help="Query Serper for core 25 items only (low quota use)")
    p.add_argument("--no-serper", action="store_true", help="Skip Serper queries entirely")
    p.add_argument("--max-items", type=int, default=120)
    args = p.parse_args()

    if not (KROGER_CLIENT_ID and KROGER_CLIENT_SECRET):
        print("ERROR: Kroger credentials missing in env.", file=sys.stderr)
        sys.exit(1)

    with open(args.basket, encoding="utf-8") as f:
        basket = json.load(f)[: args.max_items]

    print(f"Loaded {len(basket)} master basket items ({sum(1 for b in basket if b['type']=='grocery')} grocery, {sum(1 for b in basket if b['type']=='non_grocery')} non-grocery).")
    token = get_kroger_token()
    locations = resolve_kroger_locations(token)
    print(f"Resolved {len(locations)} Kroger-family locations across US:")
    for loc in locations:
        print(f"  - {loc['store']} ({loc['city']} {loc['zip']}): {loc['store_name']} [{loc['locationId']}]")

    prices_by_id = {b["id"]: {} for b in basket}

    t0 = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(fetch_kroger_for_item, b, locations, token) for b in basket]
        for fut in as_completed(futs):
            iid, res = fut.result()
            prices_by_id[iid].update(res)
            done += 1
            if done % 25 == 0 or done == len(basket):
                print(f"  Kroger API progress: {done}/{len(basket)} items ({time.time() - t0:.1f}s)")

    serper_queries = 0
    if not args.no_serper and SERPER_API_KEY:
        core_items = [b for b in basket if b.get("core")]
        print(f"Running budget-capped Serper multi-retailer sweep on {len(core_items)} core items...")
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = [ex.submit(fetch_serper_multi_retailer, b) for b in core_items]
            for fut in as_completed(futs):
                iid, res = fut.result()
                prices_by_id[iid].update(res)
                serper_queries += 1
        print(f"  Serper sweep complete ({serper_queries} queries used).")

    # Assemble rich item rows + location spread stats + apples-to-apples store leaderboard.
    now = datetime.now(timezone.utc).isoformat()
    out_items = []
    store_meta = {
        loc["key"]: {
            "key": loc["key"], "store": loc["store"], "store_name": loc["store_name"],
            "city": loc["city"], "zip": loc["zip"], "address": loc["address"], "locationId": loc["locationId"],
            "kind": "kroger_api",
        }
        for loc in locations
    }

    for b in basket:
        pmap = prices_by_id[b["id"]]
        for sk, pv in pmap.items():
            if sk not in store_meta:
                store_meta[sk] = {"key": sk, "store": pv["store"], "store_name": pv["store_name"],
                                  "city": pv["city"], "zip": pv["zip"], "kind": pv["source"]}
        vals = [(sk, pv["price"]) for sk, pv in pmap.items() if pv.get("price") is not None]
        if vals:
            min_sk, min_p = min(vals, key=lambda x: x[1])
            max_sk, max_p = max(vals, key=lambda x: x[1])
            spread = round(max_p - min_p, 2)
            pct = round((max_p - min_p) / min_p * 100, 1) if min_p > 0 else 0.0
        else:
            min_sk = max_sk = None
            min_p = max_p = spread = pct = None

        out_items.append({
            **b,
            "prices": pmap,
            "min_price": min_p,
            "max_price": max_p,
            "cheapest_store_key": min_sk,
            "priciest_store_key": max_sk,
            "dollar_spread": spread,
            "pct_spread": pct,
            "stores_priced": len(vals),
        })

    # Compute apples-to-apples leaderboard across Kroger locations (items priced at ALL 6 Kroger stores).
    k_keys = [loc["key"] for loc in locations]
    common_items = [it for it in out_items if all(k in it["prices"] for k in k_keys)]
    leaderboard = []
    for sk, meta in store_meta.items():
        all_priced = [it["prices"][sk]["price"] for it in out_items if sk in it["prices"]]
        common_total = round(sum(it["prices"][sk]["price"] for it in common_items), 2) if sk in k_keys and common_items else None
        avg_vs_min = []
        for it in out_items:
            if sk in it["prices"] and it["min_price"] and it["stores_priced"] >= 2:
                avg_vs_min.append((it["prices"][sk]["price"] - it["min_price"]) / it["min_price"] * 100.0)
        leaderboard.append({
            **meta,
            "items_priced": len(all_priced),
            "common_basket_items": len(common_items) if common_total is not None else 0,
            "common_basket_total": common_total,
            "avg_pct_above_lowest": round(sum(avg_vs_min) / len(avg_vs_min), 1) if avg_vs_min else 0.0,
        })
    leaderboard.sort(key=lambda r: (0 if r["common_basket_total"] is not None else 1, r["common_basket_total"] or 9999, r["avg_pct_above_lowest"]))

    doc = {
        "generated_at": now,
        "total_items": len(out_items),
        "grocery_count": sum(1 for i in out_items if i["type"] == "grocery"),
        "non_grocery_count": sum(1 for i in out_items if i["type"] == "non_grocery"),
        "common_kroger_items": len(common_items),
        "serper_queries_used": serper_queries,
        "stores": leaderboard,
        "items": out_items,
    }

    s3 = boto3.client("s3")
    s3.put_object(
        Bucket=BUCKET,
        Key="public/staple_basket.json",
        Body=json.dumps(doc).encode(),
        ContentType="application/json",
        CacheControl="no-cache",
    )
    print(f"Uploaded s3://{BUCKET}/public/staple_basket.json ({len(out_items)} items, {len(common_items)} common across all {len(k_keys)} Kroger locations)")

    # Also seed online-catalog/<slug>_<zip>.json for each Kroger location so scans hit S3 cache for $0!
    for loc in locations:
        sk = loc["key"]
        cat_items = []
        for it in out_items:
            pv = it["prices"].get(sk)
            if not pv:
                continue
            cat_items.append({
                "name": pv["product_title"],
                "upc": pv["upc"],
                "price": pv["price"],
                "promo_price": pv["promo_price"],
                "merchant": pv["store_name"],
                "source": "kroger_api",
                "store_match": "exact_store",
                "url": pv["url"],
                "fetched_at": now,
            })
        ckey = f"online-catalog/{loc['slug']}_{loc['zip']}.json"
        s3.put_object(
            Bucket=BUCKET,
            Key=ckey,
            Body=json.dumps({
                "store": loc["store"],
                "zip_code": loc["zip"],
                "resolved_store": {
                    "locationId": loc["locationId"], "chain": loc["chain"],
                    "name": loc["store_name"], "address": loc["address"],
                },
                "updated_at": now,
                "items": cat_items,
            }, indent=2).encode(),
            ContentType="application/json",
        )
        print(f"  Seeded {ckey} ({len(cat_items)} items)")

    print("\n=== STORE LEADERBOARD (COMMON BASKET) ===")
    for row in leaderboard:
        tot = f"${row['common_basket_total']:.2f} ({row['common_basket_items']} common)" if row["common_basket_total"] else f"{row['items_priced']} items"
        print(f"  {row['store']:15s} {row['city']:18s} | {tot:26s} | avg +{row['avg_pct_above_lowest']}% vs lowest")


if __name__ == "__main__":
    main()
