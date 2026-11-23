"""
Proactively collect live prices for the 131-item Master Staple Basket (109 grocery + 22 non-grocery)
across 17 real US Kroger-family stores in 16 states ($0 official Kroger API, exact-UPC anchored),
fetching BOTH National Brand AND Store-Brand Equivalent prices at every store — PLUS strict
first-party Hy-Vee (Columbia MO 65203), Target, and Walmart sweeps via Serper Shopping.

Writes:
  - s3://<bucket>/public/staple_basket.json
  - s3://<bucket>/online-catalog/<store_slug>_<zip>.json
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

# 17 real US stores across 16 states (includes 2 distinct banners in VA: Kroger vs Harris Teeter).
KROGER_LOCATIONS = [
    {"key": "mo_gerbes_65203",        "state": "MO", "region": "Midwest",       "store": "Gerbes",        "slug": "gerbes",        "zip": "65203", "city": "Columbia, MO"},
    {"key": "oh_kroger_45202",        "state": "OH", "region": "Midwest",       "store": "Kroger",        "slug": "kroger",        "zip": "45202", "city": "Cincinnati, OH"},
    {"key": "in_kroger_46204",        "state": "IN", "region": "Midwest",       "store": "Kroger",        "slug": "kroger",        "zip": "46204", "city": "Indianapolis, IN"},
    {"key": "mi_kroger_48201",        "state": "MI", "region": "Midwest",       "store": "Kroger",        "slug": "kroger",        "zip": "48201", "city": "Detroit, MI"},
    {"key": "il_marianos_60607",      "state": "IL", "region": "Midwest",       "store": "Mariano's",     "slug": "marianos",      "zip": "60607", "city": "Chicago, IL"},
    {"key": "tn_kroger_37203",        "state": "TN", "region": "South",         "store": "Kroger",        "slug": "kroger",        "zip": "37203", "city": "Nashville, TN"},
    {"key": "ga_kroger_30308",        "state": "GA", "region": "South",         "store": "Kroger",        "slug": "kroger",        "zip": "30308", "city": "Atlanta, GA"},
    {"key": "tx_kroger_75201",        "state": "TX", "region": "South",         "store": "Kroger",        "slug": "kroger",        "zip": "75201", "city": "Dallas, TX"},
    {"key": "nc_harris-teeter_28202", "state": "NC", "region": "South",         "store": "Harris Teeter", "slug": "harris-teeter", "zip": "28202", "city": "Charlotte, NC"},
    {"key": "va_harris-teeter_20176", "state": "VA", "region": "Mid-Atlantic",  "store": "Harris Teeter", "slug": "harris-teeter", "zip": "20176", "city": "Leesburg, VA"},
    {"key": "va_kroger_22901",        "state": "VA", "region": "Mid-Atlantic",  "store": "Kroger",        "slug": "kroger",        "zip": "22901", "city": "Charlottesville, VA"},
    {"key": "co_king-soopers_80202",  "state": "CO", "region": "Mountain West", "store": "King Soopers",  "slug": "king-soopers",  "zip": "80202", "city": "Denver, CO"},
    {"key": "ut_smiths_84101",        "state": "UT", "region": "Mountain West", "store": "Smith's",       "slug": "smiths",        "zip": "84101", "city": "Salt Lake City, UT"},
    {"key": "az_frys_85004",          "state": "AZ", "region": "Southwest",     "store": "Fry's",         "slug": "frys",          "zip": "85004", "city": "Phoenix, AZ"},
    {"key": "ca_ralphs_90024",        "state": "CA", "region": "West Coast",    "store": "Ralphs",        "slug": "ralphs",        "zip": "90024", "city": "Los Angeles, CA"},
    {"key": "or_fred-meyer_97205",    "state": "OR", "region": "Pacific NW",    "store": "Fred Meyer",    "slug": "fred-meyer",    "zip": "97205", "city": "Portland, OR"},
    {"key": "wa_qfc_98109",           "state": "WA", "region": "Pacific NW",    "store": "QFC",           "slug": "qfc",           "zip": "98109", "city": "Seattle, WA"},
]

CHAIN_META = {
    "GERBES":       ("Gerbes",        "www.gerbes.com"),
    "KROGER":       ("Kroger",        "www.kroger.com"),
    "HART":         ("Harris Teeter", "www.harristeeter.com"),
    "MARIANOS":     ("Mariano's",     "www.marianos.com"),
    "KINGSOOPERS":  ("King Soopers",  "www.kingsoopers.com"),
    "SMITHS":       ("Smith's",       "www.smithsfoodanddrug.com"),
    "FRYS":         ("Fry's",         "www.frysfood.com"),
    "RALPHS":       ("Ralphs",        "www.ralphs.com"),
    "FRED":         ("Fred Meyer",    "www.fredmeyer.com"),
    "QFC":          ("QFC",           "www.qfc.com"),
    "DILLONS":      ("Dillons",       "www.dillons.com"),
}

# Kroger / Harris Teeter owned store brands (private labels).
STORE_BRAND_NAMES = (
    "kroger", "harris teeter", "simple truth", "private selection", "smart way",
    "big k", "everyday living", "home sense", "comforts", "psst", "ht traders",
    "farmers market", "heritage farm", "abound", "hd designs", "check this out",
)

MULTIPACK_RE = re.compile(
    r"\b(?:pack of\s*[2-9]|pack of\s*\d{2}|[2-9]\s*-?\s*pack|\d{2}\s*-?\s*pack|\d+\s*pk\b|"
    r"case of\s*\d+|per case|combo|bundle|lot of\s*\d+|set of\s*\d+|multi-?pack|variety\s*case|bulk|"
    r"[2-9]x\b|x\s*[2-9]\b|\(\s*[2-9]\s*(?:ct|pack|pk)\s*\))\b",
    re.I,
)


def norm(s):
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower().replace("'", "")).strip()


def is_kroger_store_brand(prod):
    b = norm(prod.get("brand"))
    d = norm(prod.get("description"))
    return any(sb in b or d.startswith(sb + " ") for sb in STORE_BRAND_NAMES)


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
    if re.search(r"\b1\s*gal(?:lon)?\b", t):
        return 128.0
    if re.search(r"\bhalf\s*gal|\b0\.5\s*gal|\b64\s*fl", t):
        return 64.0
    return None


def parse_ct(text):
    m = re.search(r"(\d+)\s*(?:ct|count|rolls?|mega|pk|pack|bags?|sheets?|wipes?)\b", str(text or "").lower())
    return int(m.group(1)) if m else None


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
        chain_code = (chosen.get("chain") or "").upper()
        store_label, domain = CHAIN_META.get(chain_code, (cfg["store"], "www.kroger.com"))
        addr = chosen.get("address") or {}
        resolved.append({
            **cfg,
            "store": store_label,
            "domain": domain,
            "locationId": chosen["locationId"],
            "chain": chain_code,
            "store_name": chosen.get("name"),
            "address": ", ".join(p for p in [addr.get("addressLine1"), addr.get("city"), addr.get("state"), addr.get("zipCode")] if p),
        })
    return resolved


def pick_best_kroger_product(products, item, term_override=None, require_store_brand=False, max_price=None):
    target_q = norm(term_override or item.get("kroger_term") or item["query"])
    want_oz = item.get("target_oz")
    want_ct = item.get("target_ct")
    best, best_rank = None, -1.0
    for p in products:
        if require_store_brand and not is_kroger_store_brand(p):
            continue
        first = (p.get("items") or [{}])[0] or {}
        price_obj = first.get("price") or {}
        reg = parse_price(price_obj.get("regular"))
        promo = parse_price(price_obj.get("promo"))
        if reg is None and promo is None:
            continue
        eff = reg if reg is not None else promo
        if max_price and eff > max_price * 1.15:
            continue
        desc = p.get("description", "")
        size_str = first.get("size") or ""
        combined = f"{desc} {size_str}"
        sim = fuzz.token_set_ratio(target_q, norm(combined))
        if sim < 48:
            continue
        adj = 0.0
        got_oz = parse_oz(combined)
        if want_oz and got_oz:
            ratio = max(want_oz, got_oz) / min(want_oz, got_oz)
            if ratio > 1.75:
                continue
            adj += 18.0 if ratio <= 1.2 else (-6.0 if ratio > 1.4 else 4.0)
        elif want_ct:
            got_ct = parse_ct(combined)
            if got_ct:
                ratio = max(want_ct, got_ct) / min(want_ct, got_ct)
                if ratio > 1.75:
                    continue
                adj += 16.0 if ratio <= 1.25 else 0.0
        if require_store_brand:
            # Prefer core store brand (Kroger / Harris Teeter / Smart Way) over pricier organic Simple Truth if equal.
            b = norm(p.get("brand"))
            if b in ("kroger", "harris teeter", "smart way", "big k", "everyday living"):
                adj += 8.0
        rank = sim + adj
        if rank > best_rank:
            best_rank = rank
            best = {
                "upc": p.get("upc"),
                "brand": p.get("brand") or "",
                "description": desc,
                "size": size_str,
                "regular": eff,
                "promo": promo if (promo and reg and promo < reg) else None,
            }
    return best


def fetch_kroger_for_item(item, locations, token):
    """Fetch main SKU (and store-brand equivalent for national-brand items) across all 17 locations."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    results = {}
    anchor_upc = None
    sb_anchor_upc = None
    term = (item.get("kroger_term") or item["query"])[:60]
    sb_term = (item.get("store_brand_term") or "")[:60]

    for idx, loc in enumerate(locations):
        lid = loc["locationId"]
        # 1. Main product (National Brand or Commodity)
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
                qs = urllib.parse.urlencode({"filter.term": term, "filter.locationId": lid, "filter.limit": 15})
                res = http_json(f"https://api.kroger.com/v1/products?{qs}", headers=headers)
                chosen = pick_best_kroger_product(res.get("data", []), item)
            except Exception:
                chosen = None

        if not chosen:
            continue
        if idx == 0 and chosen.get("upc"):
            anchor_upc = chosen["upc"]

        # 2. Store-brand equivalent for national-brand items ($0 extra Kroger API call)
        sb_obj = None
        if sb_term and item.get("brand_tier") == "national":
            sb_chosen = None
            if sb_anchor_upc and idx > 0 and loc["chain"] != "HART":
                try:
                    res = http_json(f"https://api.kroger.com/v1/products/{sb_anchor_upc}?filter.locationId={lid}", headers=headers)
                    if res.get("data"):
                        sb_chosen = pick_best_kroger_product([res["data"]], item, term_override=sb_term,
                                                             require_store_brand=True, max_price=chosen["regular"])
                except Exception:
                    pass
            if not sb_chosen:
                try:
                    prefix = "harris teeter" if loc["chain"] == "HART" else "kroger"
                    qs = urllib.parse.urlencode({"filter.term": f"{prefix} {sb_term}"[:60], "filter.locationId": lid, "filter.limit": 15})
                    res = http_json(f"https://api.kroger.com/v1/products?{qs}", headers=headers)
                    sb_chosen = pick_best_kroger_product(res.get("data", []), item, term_override=sb_term,
                                                         require_store_brand=True, max_price=chosen["regular"])
                except Exception:
                    sb_chosen = None
            if sb_chosen and sb_chosen["upc"] != chosen["upc"]:
                if idx == 0 and sb_chosen.get("upc"):
                    sb_anchor_upc = sb_chosen["upc"]
                sb_obj = {
                    "price": sb_chosen["regular"],
                    "promo_price": sb_chosen["promo"],
                    "brand": sb_chosen["brand"],
                    "size": sb_chosen["size"],
                    "product_title": f"{sb_chosen['description']} ({sb_chosen['size']})".strip() if sb_chosen["size"] else sb_chosen["description"],
                    "upc": sb_chosen["upc"],
                    "url": f"https://{loc['domain']}/p/-/{sb_chosen['upc']}" if sb_chosen.get("upc") else None,
                }

        results[loc["key"]] = {
            "state": loc["state"],
            "store": loc["store"],
            "store_name": loc["store_name"],
            "city": loc["city"],
            "zip": loc["zip"],
            "price": chosen["regular"],
            "promo_price": chosen["promo"],
            "size": chosen["size"],
            "product_title": f"{chosen['description']} ({chosen['size']})".strip() if chosen["size"] else chosen["description"],
            "upc": chosen["upc"],
            "same_upc": bool(anchor_upc and chosen["upc"] == anchor_upc),
            "store_brand": sb_obj,
            "source": "kroger_api",
            "url": f"https://{loc['domain']}/p/-/{chosen['upc']}" if chosen.get("upc") else None,
        }
    return item["id"], results


def fetch_first_party_retailer(item, retailer, kroger_median, location=None, state="US", city="Online US", zip_code="US"):
    """
    Strict first-party Target, Walmart, or Hy-Vee lookup via Serper Shopping:
      - Requires EXACT source name ('target', 'walmart', 'hy-vee') -> zero 3rd-party resellers!
      - Rejects multipacks/cases/bundles.
      - Enforces size check + 0.55x-1.90x median sanity check.
      - Also captures a store-brand item if present in the same response (e.g., Hy-Vee / Good & Gather / Great Value).
    """
    if not SERPER_API_KEY:
        return None
    headers = {"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"}
    payload = {"q": f"{retailer} {item['query']}", "gl": "us", "hl": "en", "num": 15}
    if location:
        payload["location"] = location
    try:
        res = http_json("https://google.serper.dev/shopping", method="POST", headers=headers, data=json.dumps(payload).encode())
    except Exception:
        return None

    want_src = retailer.lower()
    want_oz = item.get("target_oz")
    want_ct = item.get("target_ct")
    best, best_rank = None, -1.0

    for s in res.get("shopping", []) or []:
        src = (s.get("source") or "").strip().lower()
        if src != want_src:
            continue
        title = s.get("title", "")
        if MULTIPACK_RE.search(title) and not MULTIPACK_RE.search(item["query"]):
            continue
        price = parse_price(s.get("price"))
        if price is None:
            continue
        if kroger_median and not (0.55 * kroger_median <= price <= 1.90 * kroger_median):
            continue
        sim = fuzz.token_set_ratio(norm(item.get("kroger_term") or item["query"]), norm(title))
        if sim < 50:
            continue
        adj = 0.0
        got_oz = parse_oz(title)
        if want_oz and got_oz:
            ratio = max(want_oz, got_oz) / min(want_oz, got_oz)
            if ratio > 1.45:
                continue
            adj += 15.0 if ratio <= 1.2 else 0.0
        elif want_ct:
            got_ct = parse_ct(title)
            if got_ct and (max(want_ct, got_ct) / min(want_ct, got_ct) > 1.45):
                continue
        url = s.get("link") or s.get("productUrl")
        if not url and s.get("productId"):
            url = f"https://www.google.com/shopping/product/{s['productId']}"
        if not url:
            continue
        rank = sim + adj
        if rank > best_rank:
            best_rank = rank
            best = {
                "state": state,
                "store": retailer,
                "store_name": f"{retailer} ({city})",
                "city": city,
                "zip": zip_code,
                "price": price,
                "promo_price": None,
                "size": item.get("target_size", ""),
                "product_title": title,
                "upc": None,
                "same_upc": False,
                "store_brand": None,
                "source": "first_party_shopping",
                "url": url,
            }
    return best


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--basket", default="data/master_basket.json")
    p.add_argument("--no-serper", action="store_true")
    p.add_argument("--serper-max-core", type=int, default=40)
    args = p.parse_args()

    if not (KROGER_CLIENT_ID and KROGER_CLIENT_SECRET):
        print("ERROR: Kroger credentials missing in env.", file=sys.stderr)
        sys.exit(1)

    with open(args.basket, encoding="utf-8") as f:
        basket = json.load(f)

    print(f"Loaded {len(basket)} items ({sum(1 for b in basket if b['brand_tier']=='national')} national w/ store-brand pair, {sum(1 for b in basket if b['brand_tier']!='national')} commodities).")
    token = get_kroger_token()
    locations = resolve_kroger_locations(token)
    print(f"Resolved {len(locations)} Kroger-family stores across {len({l['state'] for l in locations})} US states.")

    prices_by_id = {b["id"]: {} for b in basket}
    t0 = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(fetch_kroger_for_item, b, locations, token) for b in basket]
        for fut in as_completed(futs):
            iid, res = fut.result()
            prices_by_id[iid].update(res)
            done += 1
            if done % 25 == 0 or done == len(basket):
                print(f"  17-Store Kroger + Store-Brand progress: {done}/{len(basket)} items ({time.time() - t0:.1f}s)")

    medians = {}
    for b in basket:
        ks = sorted(pv["price"] for pv in prices_by_id[b["id"]].values() if pv.get("price"))
        medians[b["id"]] = ks[len(ks) // 2] if ks else None

    serper_queries = 0
    if not args.no_serper and SERPER_API_KEY:
        core_items = [b for b in basket if b.get("core")][: args.serper_max_core]
        retailers = [
            ("mo_hyvee_65203", "Hy-Vee", "Columbia, Missouri, United States", "MO", "Columbia, MO", "65203"),
            ("us_target", "Target", None, "US", "Online US", "US"),
            ("us_walmart", "Walmart", None, "US", "Online US", "US"),
        ]
        print(f"Running strict first-party Hy-Vee (Columbia MO 65203), Target & Walmart sweep on {len(core_items)} core items ({len(core_items)*len(retailers)} queries)...")
        tasks = []
        with ThreadPoolExecutor(max_workers=6) as ex:
            for b in core_items:
                for rk, ret, loc_str, st, city, zp in retailers:
                    tasks.append((b["id"], rk, ex.submit(fetch_first_party_retailer, b, ret, medians[b["id"]], loc_str, st, city, zp)))
            for iid, rk, fut in tasks:
                serper_queries += 1
                res = fut.result()
                if res:
                    prices_by_id[iid][rk] = res
        print(f"  First-party sweep complete ({serper_queries} queries used).")

    now = datetime.now(timezone.utc).isoformat()
    store_meta = {
        loc["key"]: {
            "key": loc["key"], "state": loc["state"], "region": loc["region"],
            "store": loc["store"], "store_name": loc["store_name"],
            "city": loc["city"], "zip": loc["zip"], "address": loc["address"],
            "locationId": loc["locationId"], "kind": "kroger_api",
        }
        for loc in locations
    }
    extra_meta = [
        ("mo_hyvee_65203", "MO", "Midwest", "Hy-Vee", "Hy-Vee (Columbia, MO)", "Columbia, MO", "65203", "Columbia, MO (First-Party Shopping)"),
        ("us_target", "US", "National Online", "Target", "Target (First-Party Online)", "Online US", "US", "First-party online (3rd-party sellers excluded)"),
        ("us_walmart", "US", "National Online", "Walmart", "Walmart (First-Party Online)", "Online US", "US", "First-party online (3rd-party sellers excluded)"),
    ]
    for rk, st, reg, name, sname, city, zp, addr in extra_meta:
        if any(rk in prices_by_id[b["id"]] for b in basket):
            store_meta[rk] = {
                "key": rk, "state": st, "region": reg, "store": name,
                "store_name": sname, "city": city, "zip": zp, "address": addr,
                "kind": "first_party_shopping",
            }

    out_items = []
    for b in basket:
        pmap = prices_by_id[b["id"]]
        vals = [(sk, pv["price"]) for sk, pv in pmap.items() if pv.get("price") is not None]
        sb_vals = [pv["store_brand"]["price"] for pv in pmap.values() if pv.get("store_brand") and pv["store_brand"].get("price") is not None]
        if vals:
            sorted_p = sorted(v[1] for v in vals)
            med = sorted_p[len(sorted_p) // 2]
            avg = round(sum(sorted_p) / len(sorted_p), 2)
            min_sk, min_p = min(vals, key=lambda x: x[1])
            max_sk, max_p = max(vals, key=lambda x: x[1])
            spread = round(max_p - min_p, 2)
            pct = round((max_p - min_p) / min_p * 100, 1) if min_p > 0 else 0.0
        else:
            med = avg = min_sk = max_sk = min_p = max_p = spread = pct = None
        sb_avg = round(sum(sb_vals) / len(sb_vals), 2) if sb_vals else None

        out_items.append({
            **b,
            "prices": pmap,
            "median_price": med,
            "avg_price": avg,
            "store_brand_avg_price": sb_avg,
            "min_price": min_p,
            "max_price": max_p,
            "cheapest_store_key": min_sk,
            "priciest_store_key": max_sk,
            "dollar_spread": spread,
            "pct_spread": pct,
            "stores_priced": len(vals),
        })

    k_keys = [loc["key"] for loc in locations]
    common_items = [it for it in out_items if all(k in it["prices"] for k in k_keys)]
    common_grocery = [it for it in common_items if it["type"] == "grocery"]
    common_nongrocery = [it for it in common_items if it["type"] == "non_grocery"]

    leaderboard = []
    for sk, meta in store_meta.items():
        all_priced = [it for it in out_items if sk in it["prices"]]
        same_upc_cnt = sum(1 for it in all_priced if it["prices"][sk].get("same_upc"))
        is_k = sk in k_keys
        common_total = round(sum(it["prices"][sk]["price"] for it in common_items), 2) if (is_k and common_items) else None
        groc_total = round(sum(it["prices"][sk]["price"] for it in common_grocery), 2) if (is_k and common_grocery) else None
        ng_total = round(sum(it["prices"][sk]["price"] for it in common_nongrocery), 2) if (is_k and common_nongrocery) else None

        # Store-brand mode total across common items (swaps national brand -> store brand where available).
        sb_pairs = 0
        sb_total = 0.0
        sb_saved = 0.0
        if is_k and common_items:
            for it in common_items:
                pv = it["prices"][sk]
                sb = pv.get("store_brand")
                if sb and sb.get("price") is not None and sb["price"] < pv["price"]:
                    sb_total += sb["price"]
                    sb_saved += (pv["price"] - sb["price"])
                    sb_pairs += 1
                else:
                    sb_total += pv["price"]
            sb_total = round(sb_total, 2)
            sb_saved = round(sb_saved, 2)
        else:
            sb_total = None

        vs_med = [(it["prices"][sk]["price"] - it["median_price"]) / it["median_price"] * 100.0 for it in all_priced if it["median_price"]]
        vs_min = [(it["prices"][sk]["price"] - it["min_price"]) / it["min_price"] * 100.0 for it in all_priced if it["min_price"] and it["stores_priced"] >= 2]

        leaderboard.append({
            **meta,
            "items_priced": len(all_priced),
            "grocery_priced": sum(1 for it in all_priced if it["type"] == "grocery"),
            "non_grocery_priced": sum(1 for it in all_priced if it["type"] == "non_grocery"),
            "same_upc_count": same_upc_cnt,
            "store_brand_pairs": sb_pairs,
            "store_brand_basket_total": sb_total,
            "store_brand_savings": sb_saved if sb_total is not None else None,
            "common_basket_items": len(common_items) if common_total is not None else 0,
            "common_basket_total": common_total,
            "common_grocery_total": groc_total,
            "common_non_grocery_total": ng_total,
            "avg_pct_vs_median": round(sum(vs_med) / len(vs_med), 1) if vs_med else 0.0,
            "avg_pct_above_lowest": round(sum(vs_min) / len(vs_min), 1) if vs_min else 0.0,
        })

    k_totals = [r["common_basket_total"] for r in leaderboard if r["common_basket_total"] is not None]
    cheapest_total = min(k_totals) if k_totals else None
    avg_state_total = round(sum(k_totals) / len(k_totals), 2) if k_totals else None
    for r in leaderboard:
        if r["common_basket_total"] is not None and cheapest_total:
            r["pct_above_cheapest_state"] = round((r["common_basket_total"] - cheapest_total) / cheapest_total * 100.0, 1)
            r["dollars_above_cheapest_state"] = round(r["common_basket_total"] - cheapest_total, 2)
        else:
            r["pct_above_cheapest_state"] = None
            r["dollars_above_cheapest_state"] = None

    leaderboard.sort(key=lambda r: (0 if r["common_basket_total"] is not None else 1, r["common_basket_total"] or 9999, r["avg_pct_vs_median"]))

    doc = {
        "generated_at": now,
        "total_items": len(out_items),
        "grocery_count": sum(1 for i in out_items if i["type"] == "grocery"),
        "non_grocery_count": sum(1 for i in out_items if i["type"] == "non_grocery"),
        "states_count": len({loc["state"] for loc in locations}),
        "common_kroger_items": len(common_items),
        "common_grocery_items": len(common_grocery),
        "common_non_grocery_items": len(common_nongrocery),
        "avg_state_basket_total": avg_state_total,
        "serper_queries_used": serper_queries,
        "stores": leaderboard,
        "items": out_items,
    }

    s3 = boto3.client("s3")
    s3.put_object(Bucket=BUCKET, Key="public/staple_basket.json", Body=json.dumps(doc).encode(),
                  ContentType="application/json", CacheControl="no-cache")
    print(f"\nUploaded s3://{BUCKET}/public/staple_basket.json ({len(out_items)} total, {len(common_items)} shared across all {len(locations)} Kroger-family stores, avg state basket=${avg_state_total})")

    # Seed S3 7-day caches for Kroger stores + Hy-Vee Columbia MO!
    for loc in locations:
        sk = loc["key"]
        cat_items = []
        for it in out_items:
            pv = it["prices"].get(sk)
            if not pv:
                continue
            cat_items.append({
                "name": pv["product_title"], "upc": pv["upc"], "price": pv["price"],
                "promo_price": pv["promo_price"], "merchant": pv["store_name"],
                "source": "kroger_api", "store_match": "exact_store", "url": pv["url"], "fetched_at": now,
            })
            sb = pv.get("store_brand")
            if sb:
                cat_items.append({
                    "name": sb["product_title"], "upc": sb["upc"], "price": sb["price"],
                    "promo_price": sb["promo_price"], "merchant": pv["store_name"],
                    "source": "kroger_api", "store_match": "exact_store", "url": sb["url"], "fetched_at": now,
                })
        s3.put_object(Bucket=BUCKET, Key=f"online-catalog/{loc['slug']}_{loc['zip']}.json",
                      Body=json.dumps({"store": loc["store"], "zip_code": loc["zip"],
                                       "resolved_store": {"locationId": loc["locationId"], "chain": loc["chain"], "name": loc["store_name"], "address": loc["address"]},
                                       "updated_at": now, "items": cat_items}, indent=2).encode(),
                      ContentType="application/json")

    print("\n=== 17-STORE (16 STATES) + HY-VEE / TARGET / WALMART LEADERBOARD ===")
    for r in leaderboard:
        if r["common_basket_total"] is not None:
            print(f"  [{r['state']}] {r['store']:14s} {r['city']:20s} | {r['items_priced']:3d} items | Name=${r['common_basket_total']:6.2f} | StoreBrand=${r['store_brand_basket_total']:6.2f} (saves ${r['store_brand_savings']:5.2f} on {r['store_brand_pairs']} pairs)")
        else:
            print(f"  [{r['state']}] {r['store']:14s} {r['city']:20s} | {r['items_priced']:3d} core items | avg vs median: {r['avg_pct_vs_median']:+5.1f}%")


if __name__ == "__main__":
    main()
