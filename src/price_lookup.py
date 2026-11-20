import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from botocore.exceptions import ClientError
from rapidfuzz import fuzz, process

CACHE_TTL_DAYS = int(os.environ.get("PRICE_CACHE_TTL_DAYS", "7"))
MAX_LIVE_LOOKUPS_PER_RUN = int(os.environ.get("MAX_LIVE_LOOKUPS_PER_RUN", "15"))

KROGER_CLIENT_ID = os.environ.get("KROGER_CLIENT_ID", "").strip()
KROGER_CLIENT_SECRET = os.environ.get("KROGER_CLIENT_SECRET", "").strip()
SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "").strip()
ENABLE_OPEN_PRICES = os.environ.get("ENABLE_OPEN_PRICES", "true").lower() == "true"

# Banners covered by the official free Kroger Developer API.
KROGER_BANNERS = {
    "kroger", "gerbes", "harris-teeter", "ralphs", "fred-meyer", "king-soopers",
    "frys", "smiths", "dillons", "qfc", "marianos", "pick-n-save", "metro-market",
    "city-market", "bakers", "jay-c", "pay-less", "ruler", "foods-co",
}

_kroger_token_cache = {"token": None, "expires_at": 0.0}

# Built-in fallback reference (illustrative sample prices when no live API or ZIP catalog matches).
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


def norm_upc(upc):
    digits = re.sub(r"\D", "", str(upc or ""))
    if not digits:
        return ""
    return digits.lstrip("0") or "0"


def pad_upc13(upc):
    digits = re.sub(r"\D", "", str(upc or ""))
    return digits.zfill(13) if digits and len(digits) <= 13 else digits


def parse_price(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return round(float(v), 2) if v > 0 else None
    m = re.search(r"\$?\s*(\d{1,3}(?:,\d{3})*\.\d{2})", str(v))
    if not m:
        return None
    val = float(m.group(1).replace(",", ""))
    return round(val, 2) if 0.05 <= val <= 500.0 else None


def _is_fresh(entry, now):
    ts = entry.get("fetched_at")
    if not ts:
        return True  # static/user-seeded catalog item
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return (now - dt) <= timedelta(days=CACHE_TTL_DAYS)
    except Exception:
        return True


def _get_json(s3, bucket, key, default=None):
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


def _http_json(url, method="GET", headers=None, data=None, timeout=12):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


# ==================== TIER 1: OFFICIAL KROGER / GERBES API ====================

def _kroger_token():
    if not (KROGER_CLIENT_ID and KROGER_CLIENT_SECRET):
        return None
    now = datetime.now(timezone.utc).timestamp()
    if _kroger_token_cache["token"] and now < _kroger_token_cache["expires_at"] - 60:
        return _kroger_token_cache["token"]
    basic = base64.b64encode(f"{KROGER_CLIENT_ID}:{KROGER_CLIENT_SECRET}".encode()).decode("ascii")
    body = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": "product.compact"}).encode()
    res = _http_json(
        "https://api.kroger.com/v1/connect/oauth2/token",
        method="POST",
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data=body,
    )
    _kroger_token_cache["token"] = res["access_token"]
    _kroger_token_cache["expires_at"] = now + int(res.get("expires_in", 1800))
    return _kroger_token_cache["token"]


def _kroger_location_id(s3, bucket, store_slug, zip_code, token):
    if not zip_code or zip_code == "00000":
        return None
    cache_key = "online-catalog/_kroger_locations.json"
    locs = _get_json(s3, bucket, cache_key, {})
    lk = f"{store_slug}_{zip_code}"
    if lk in locs:
        return locs[lk]
    qs = urllib.parse.urlencode({"filter.zipCode.near": zip_code, "filter.limit": 5})
    res = _http_json(
        f"https://api.kroger.com/v1/locations?{qs}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    candidates = res.get("data", [])
    if not candidates:
        return None
    chosen = candidates[0]
    for c in candidates:
        chain = norm(c.get("chain", ""))
        name = norm(c.get("name", ""))
        if store_slug.replace("-", " ") in chain or store_slug.replace("-", " ") in name:
            chosen = c
            break
    loc_id = chosen.get("locationId")
    if loc_id:
        locs[lk] = loc_id
        _put_json(s3, bucket, cache_key, locs)
    return loc_id


def lookup_kroger(s3, bucket, store_slug, zip_code, item):
    if store_slug not in KROGER_BANNERS or not (KROGER_CLIENT_ID and KROGER_CLIENT_SECRET):
        return None
    try:
        token = _kroger_token()
        loc_id = _kroger_location_id(s3, bucket, store_slug, zip_code, token)
        if not loc_id:
            return None
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        upc = item.get("upc_barcode")
        products = []
        if upc:
            u13 = pad_upc13(upc)
            try:
                res = _http_json(f"https://api.kroger.com/v1/products/{u13}?filter.locationId={loc_id}", headers=headers)
                if res.get("data"):
                    products = [res["data"]]
            except urllib.error.HTTPError:
                pass
        if not products:
            term = f"{item.get('brand', '')} {item.get('product_name', '')}".strip()[:60]
            qs = urllib.parse.urlencode({"filter.term": term, "filter.locationId": loc_id, "filter.limit": 10})
            res = _http_json(f"https://api.kroger.com/v1/products?{qs}", headers=headers)
            products = res.get("data", [])
        if not products:
            return None

        target = norm(f"{item.get('brand', '')} {item.get('product_name', '')}")
        best, best_rank = None, -1.0
        for p in products:
            items_arr = p.get("items") or [{}]
            first = items_arr[0] or {}
            price_obj = first.get("price") or {}
            reg = parse_price(price_obj.get("regular"))
            promo = parse_price(price_obj.get("promo"))
            if reg is None and promo is None:
                continue
            size_str = first.get("size") or ""
            desc = p.get("description", "")
            full_name = f"{desc} ({size_str})".strip() if size_str and size_str.lower() not in desc.lower() else desc
            if upc and norm_upc(p.get("upc")) == norm_upc(upc):
                sim, rank = 100.0, 200.0
            else:
                sim = fuzz.token_set_ratio(target, norm(desc))
                if sim < 62:
                    continue
                rank = sim + _size_score_adjustment(item, f"{desc} {size_str}")
            if rank > best_rank and rank >= 55:
                best_rank = rank
                best = {
                    "name": full_name,
                    "upc": p.get("upc"),
                    "price": reg if reg is not None else promo,
                    "promo_price": promo if (promo and reg and promo < reg) else None,
                    "match_score": round(sim, 1),
                    "source": "kroger_api",
                    "merchant": store_slug.replace("-", " ").title(),
                    "url": f"https://www.kroger.com/p/-/{p.get('upc', '')}" if p.get("upc") else None,
                }
        return best
    except Exception as e:
        print("kroger lookup error:", repr(e))
        return None


# ==================== TIER 2: SERPER GOOGLE SHOPPING + INSTACART SEARCH ====================

def _parse_oz(text):
    """Best-effort extract fluid/weight ounces from a size string like '16 fl oz' or '30 oz'."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:fl\.?\s*)?oz\b", str(text or "").lower())
    return float(m.group(1)) if m else None


def _size_score_adjustment(item, candidate_text):
    """Reward close package sizes and penalize >1.6x size mismatches (e.g., 30 oz vs 15 oz)."""
    want = item.get("size_oz") or _parse_oz(item.get("size_text"))
    got = _parse_oz(candidate_text)
    if not want or not got:
        return 0.0
    try:
        ratio = max(float(want), got) / min(float(want), got)
    except ZeroDivisionError:
        return 0.0
    if ratio <= 1.2:
        return 12.0
    if ratio <= 1.5:
        return 0.0
    return -30.0


def _is_lookup_worthy(item):
    """Don't burn live API calls on items with no shelf price or an unknown brand + vague name."""
    if parse_price(item.get("in_store_price")) is None:
        return False
    brand = norm(item.get("brand"))
    prod = norm(item.get("product_name"))
    if not prod:
        return False
    if brand in ("", "unknown", "none", "na") and len(prod.split()) < 2:
        return False
    return True


def _price_plausible(online, in_store):
    if online is None:
        return False
    if not in_store:
        return True
    ratio = online / in_store
    return 0.30 <= ratio <= 3.50


def lookup_serper(store, zip_code, item):
    """Live Google Shopping + Instacart search across ALL grocery stores (requires SERPER_API_KEY)."""
    if not SERPER_API_KEY:
        return None
    label = f"{item.get('brand', '')} {item.get('product_name', '')} {item.get('size_text', '')}".strip()
    in_store = parse_price(item.get("in_store_price"))
    store_norm = norm(store)
    headers = {"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"}

    # 2a. Google Shopping structured results
    try:
        q = f"{store} {label}".strip() if store and store.lower() != "other" else label
        payload = {"q": q, "gl": "us", "hl": "en", "num": 8}
        if zip_code and zip_code != "00000":
            payload["location"] = f"{zip_code}, United States"
        res = _http_json("https://google.serper.dev/shopping", method="POST", headers=headers,
                         data=json.dumps(payload).encode())
        best, best_rank = None, -1.0
        for s in res.get("shopping", []) or []:
            price = parse_price(s.get("price"))
            if not _price_plausible(price, in_store):
                continue
            title = s.get("title", "")
            merch = s.get("source", "") or "Online"
            sim = fuzz.token_set_ratio(norm(label), norm(title))
            if sim < 55:
                continue
            m_norm = norm(merch)
            # Prefer exact store or Instacart listings over generic third-party merchants, and respect size.
            bonus = 25 if "instacart" in m_norm else (20 if store_norm and store_norm in m_norm else 0)
            rank = sim + bonus + _size_score_adjustment(item, title)
            if rank > best_rank and rank >= 50:
                best_rank = rank
                best = {
                    "name": title,
                    "upc": item.get("upc_barcode"),
                    "price": price,
                    "promo_price": None,
                    "match_score": round(sim, 1),
                    "source": "instacart_search" if "instacart" in m_norm else "google_shopping",
                    "merchant": merch,
                    "url": s.get("link") or s.get("productUrl"),
                }
        if best and (best["source"] == "instacart_search" or (store_norm and store_norm in norm(best["merchant"]))):
            return best
        shopping_fallback = best
    except Exception as e:
        print("serper shopping error:", repr(e))
        shopping_fallback = None

    # 2b. Targeted Instacart web search fallback (great for private-label Aldi/Wegmans/Giant/Schnucks items)
    try:
        store_term = f'"{store}"' if store and store.lower() != "other" else ""
        q = f'site:instacart.com {store_term} "{item.get("brand", "")} {item.get("product_name", "")}" {item.get("size_text", "")}'.strip()
        res = _http_json("https://google.serper.dev/search", method="POST", headers=headers,
                         data=json.dumps({"q": q, "gl": "us", "hl": "en", "num": 5}).encode())
        for r in res.get("organic", []) or []:
            title = r.get("title", "")
            sim = fuzz.token_set_ratio(norm(label), norm(title))
            if sim < 55:
                continue
            price = (
                parse_price(r.get("priceRange"))
                or parse_price((r.get("attributes") or {}).get("Price"))
                or parse_price(r.get("snippet"))
            )
            if not _price_plausible(price, in_store):
                continue
            return {
                "name": title,
                "upc": item.get("upc_barcode"),
                "price": price,
                "promo_price": None,
                "match_score": round(sim, 1),
                "source": "instacart_search",
                "merchant": f"Instacart ({store})" if store and store.lower() != "other" else "Instacart",
                "url": r.get("link"),
            }
    except Exception as e:
        print("serper instacart search error:", repr(e))

    return shopping_fallback


# ==================== TIER 3: OPEN PRICES (FREE, ZERO AUTH, UPC-BASED) ====================

def lookup_open_prices(item):
    if not ENABLE_OPEN_PRICES:
        return None
    upc = item.get("upc_barcode")
    if not upc:
        return None
    in_store = parse_price(item.get("in_store_price"))
    code = pad_upc13(upc)
    if not code:
        return None
    try:
        qs = urllib.parse.urlencode({"product_code": code, "currency": "USD", "size": 5})
        res = _http_json(
            f"https://prices.openfoodfacts.org/api/v1/prices?{qs}",
            headers={"User-Agent": "GroceryPriceForensics/1.0"},
            timeout=4,
        )
        items = sorted(res.get("items", []) or [], key=lambda x: str(x.get("date") or ""), reverse=True)
        for p in items:
            price = parse_price(p.get("price"))
            if not _price_plausible(price, in_store):
                continue
            loc = p.get("location") or {}
            merch = loc.get("osm_name") or "Open Prices"
            prod = p.get("product") or {}
            return {
                "name": prod.get("product_name") or f"UPC {code}",
                "upc": code,
                "price": price,
                "promo_price": None,
                "match_score": 100.0,
                "source": "open_prices",
                "merchant": f"{merch} (Open Prices)",
                "url": f"https://prices.openfoodfacts.org/products/{code}",
            }
    except Exception as e:
        print("open_prices lookup error:", repr(e))
    return None


# ==================== MATCHING & S3 7-DAY CACHE ORCHESTRATOR ====================

def _match_in_catalog(item, catalog, default_source, now, cutoff=60):
    """UPC exact match first, then rapidfuzz name match. Skips stale live-cached entries."""
    fresh = [c for c in catalog if _is_fresh(c, now)]
    if not fresh:
        return None

    item_upc = norm_upc(item.get("upc_barcode"))
    if item_upc:
        for c in fresh:
            if c.get("upc") and norm_upc(c.get("upc")) == item_upc:
                return {
                    "name": c.get("name"),
                    "price": parse_price(c.get("price")),
                    "promo_price": parse_price(c.get("promo_price")),
                    "match_score": 100.0,
                    "source": c.get("source") or default_source,
                    "merchant": c.get("merchant"),
                    "url": c.get("url"),
                }

    label = norm(f"{item.get('brand', '')} {item.get('product_name', '')} {item.get('size_text', '')}")
    if not label:
        return None
    norm_names = [norm(c.get("name", "")) for c in fresh]
    m = process.extractOne(label, norm_names, scorer=fuzz.token_set_ratio, score_cutoff=cutoff)
    if not m:
        return None
    c = fresh[m[2]]
    return {
        "name": c.get("name"),
        "price": parse_price(c.get("price")),
        "promo_price": parse_price(c.get("promo_price")),
        "match_score": round(m[1], 1),
        "source": c.get("source") or default_source,
        "merchant": c.get("merchant"),
        "url": c.get("url"),
    }


def resolve_prices(s3, bucket, store, store_slug, zip_code, items):
    """
    Resolve online prices for extracted items using:
      0. S3 ZIP catalog / 7-day live cache (UPC first, then fuzzy name)
      1. S3 chain catalog / reference.json
      2. Live APIs (Kroger/Gerbes API -> Serper Google Shopping & Instacart -> Open Prices UPC)
         capped at MAX_LIVE_LOOKUPS_PER_RUN and written back to S3 cache
      3. Built-in sample fallback (clearly flagged as sample_fallback)
    Returns (matches_by_index, catalog_source, catalog_file, lookup_stats).
    """
    now = datetime.now(timezone.utc)
    zip_key = f"online-catalog/{store_slug}_{zip_code}.json"
    chain_key = f"online-catalog/{store_slug}.json"
    ref_key = "online-catalog/reference.json"

    zip_doc = _get_json(s3, bucket, zip_key)
    zip_cat = (zip_doc.get("items", []) if isinstance(zip_doc, dict) else zip_doc) or []
    chain_doc = _get_json(s3, bucket, chain_key)
    chain_cat = (chain_doc.get("items", []) if isinstance(chain_doc, dict) else chain_doc) or []
    ref_doc = _get_json(s3, bucket, ref_key)
    ref_cat = (ref_doc.get("items", []) if isinstance(ref_doc, dict) else ref_doc) or []

    matches = {}
    new_cache_entries = []
    live_calls = 0
    stats = {"cache_hits": 0, "live_lookups": 0, "sample_hits": 0, "unmatched": 0,
             "providers_configured": {
                 "kroger_api": bool(KROGER_CLIENT_ID and KROGER_CLIENT_SECRET),
                 "serper_search": bool(SERPER_API_KEY),
                 "open_prices": ENABLE_OPEN_PRICES,
             }}

    has_live_provider = bool((store_slug in KROGER_BANNERS and KROGER_CLIENT_ID and KROGER_CLIENT_SECRET) or SERPER_API_KEY)

    for idx, it in enumerate(items):
        # Skip unpriced or brand-less vague rows before burning cache/live matches.
        if not _is_lookup_worthy(it):
            stats["unmatched"] += 1
            continue

        # Tier 0a: ZIP catalog / 7-day S3 cache
        m = _match_in_catalog(it, zip_cat, "zip_catalog", now, cutoff=68)
        if m:
            matches[idx] = m
            stats["cache_hits"] += 1
            continue

        # Tier 0b: Store-wide chain catalog or uploaded reference.json
        m = _match_in_catalog(it, chain_cat, "chain_catalog", now, cutoff=68)
        if not m:
            m = _match_in_catalog(it, ref_cat, "chain_catalog", now, cutoff=65)
        if m:
            matches[idx] = m
            stats["cache_hits"] += 1
            continue

        # Tier 1–3: Live lookup (strictly capped per run to keep costs near $0)
        if live_calls < MAX_LIVE_LOOKUPS_PER_RUN:
            live = None
            if store_slug in KROGER_BANNERS and KROGER_CLIENT_ID and KROGER_CLIENT_SECRET:
                live_calls += 1
                live = lookup_kroger(s3, bucket, store_slug, zip_code, it)
            if not live and SERPER_API_KEY and live_calls < MAX_LIVE_LOOKUPS_PER_RUN:
                live_calls += 1
                live = lookup_serper(store, zip_code, it)
            if not live and it.get("upc_barcode") and ENABLE_OPEN_PRICES and live_calls < MAX_LIVE_LOOKUPS_PER_RUN:
                live_calls += 1
                live = lookup_open_prices(it)
            if live and live.get("price") is not None:
                matches[idx] = live
                stats["live_lookups"] += 1
                new_cache_entries.append({
                    "name": live["name"],
                    "upc": live.get("upc") or it.get("upc_barcode"),
                    "price": live["price"],
                    "promo_price": live.get("promo_price"),
                    "merchant": live.get("merchant"),
                    "source": live["source"],
                    "url": live.get("url"),
                    "fetched_at": now.isoformat(),
                })
                continue

        # Tier 4: Built-in Aldi sample fallback ONLY for Aldi when no live provider is configured.
        if store_slug == "aldi" and not has_live_provider:
            m = _match_in_catalog(it, DEFAULT_CATALOG, "sample_fallback", now, cutoff=65)
            if m:
                m["merchant"] = "Sample catalog"
                matches[idx] = m
                stats["sample_hits"] += 1
                continue
        stats["unmatched"] += 1

    # Persist newly discovered live prices into S3 7-day cache so future scans cost $0.
    if new_cache_entries:
        merged = [c for c in zip_cat if _is_fresh(c, now)]
        existing_keys = {(norm_upc(c.get("upc")), norm(c.get("name"))) for c in merged}
        for e in new_cache_entries:
            ek = (norm_upc(e.get("upc")), norm(e.get("name")))
            if ek not in existing_keys:
                merged.append(e)
                existing_keys.add(ek)
        _put_json(s3, bucket, zip_key, {
            "store": store,
            "zip_code": zip_code,
            "updated_at": now.isoformat(),
            "items": merged[-2000:],
        })

    # Determine overall run-level catalog_source badge.
    sources = {m["source"] for m in matches.values()}
    live_sources = {"kroger_api", "instacart_search", "google_shopping", "open_prices"}
    if not sources:
        catalog_source, catalog_file = "sample_fallback", "none"
    elif sources == {"sample_fallback"}:
        catalog_source, catalog_file = "sample_fallback", "built-in"
    elif "sample_fallback" in sources:
        catalog_source, catalog_file = "mixed", zip_key
    elif sources & live_sources:
        catalog_source, catalog_file = "live_api", zip_key
    elif "zip_catalog" in sources:
        catalog_source, catalog_file = "zip_catalog", zip_key
    else:
        catalog_source, catalog_file = "chain_catalog", chain_key if chain_cat else ref_key

    return matches, catalog_source, catalog_file, stats
