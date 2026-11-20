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

# Official Kroger Developer API banners mapped to their real banner website domains.
KROGER_BANNER_DOMAINS = {
    "kroger": "www.kroger.com",
    "gerbes": "www.gerbes.com",
    "harris-teeter": "www.harristeeter.com",
    "dillons": "www.dillons.com",
    "ralphs": "www.ralphs.com",
    "fred-meyer": "www.fredmeyer.com",
    "king-soopers": "www.kingsoopers.com",
    "frys": "www.frysfood.com",
    "smiths": "www.smithsfoodanddrug.com",
    "marianos": "www.marianos.com",
    "qfc": "www.qfc.com",
    "pick-n-save": "www.picknsave.com",
    "metro-market": "www.metromarket.net",
    "city-market": "www.citymarket.com",
    "bakers": "www.bakersplus.com",
    "jay-c": "www.jaycfoods.com",
    "pay-less": "www.pay-less.com",
    "foods-co": "www.foodsco.net",
    "ruler": "www.rulerfoods.com",
}
KROGER_BANNERS = set(KROGER_BANNER_DOMAINS.keys())

# Direct retailer websites for targeted search & store-match verification.
STORE_DOMAINS = {
    **KROGER_BANNER_DOMAINS,
    "aldi": "www.aldi.us",
    "walmart": "www.walmart.com",
    "target": "www.target.com",
    "wegmans": "www.wegmans.com",
    "giant": "giantfood.com",
    "schnucks": "schnucks.com",
    "hy-vee": "www.hy-vee.com",
    "lidl": "www.lidl.com",
    "publix": "www.publix.com",
    "safeway": "www.safeway.com",
    "h-e-b": "www.heb.com",
    "heb": "www.heb.com",
    "meijer": "www.meijer.com",
    "food-lion": "foodlion.com",
    "sprouts": "www.sprouts.com",
    "shoprite": "www.shoprite.com",
    "whole-foods": "www.wholefoodsmarket.com",
    "costco": "www.costco.com",
    "sams-club": "www.samsclub.com",
    "trader-joes": "www.traderjoes.com",
    "winco": "www.wincofoods.com",
}

BLOCKED_MERCHANTS = ("ebay", "mercari", "poshmark", "etsy", "bonanza", "ubuy", "desertcart")
MULTIPACK_RE = re.compile(r"\b(?:pack of\s*[2-9]|pack of\s*\d{2}|\d+\s*-?\s*pack|case of\s*\d+|bundle|lot of\s*\d+)\b", re.I)

_kroger_token_cache = {"token": None, "expires_at": 0.0}

# Built-in fallback reference (illustrative sample prices when Aldi has no live API configured).
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


def _parse_oz(text):
    """Best-effort extract fluid/weight ounces from a size string like '16 fl oz' or '30 oz'."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:fl\.?\s*)?oz\b", str(text or "").lower())
    return float(m.group(1)) if m else None


def _size_check(item, candidate_text):
    """
    Compare extracted size_oz vs candidate size_oz.
    Returns (allowed, bonus, size_verified):
      - ratio <= 1.25 -> (True, +14, True)
      - 1.25 < ratio <= 1.50 -> (True, -10, False)
      - ratio > 1.50 -> (False, -50, False)  # reject clear size mismatch (e.g. 30 oz vs 15 oz)
      - either unknown -> (True, 0, None)
    """
    want = item.get("size_oz") or _parse_oz(item.get("size_text"))
    got = _parse_oz(candidate_text)
    if not want or not got:
        return True, 0.0, None
    try:
        ratio = max(float(want), got) / min(float(want), got)
    except ZeroDivisionError:
        return True, 0.0, None
    if ratio <= 1.25:
        return True, 14.0, True
    if ratio <= 1.50:
        return True, -10.0, False
    return False, -50.0, False


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


def _is_fresh(entry, now):
    ts = entry.get("fetched_at")
    if not ts:
        return True
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


def _classify_store_match(store_slug, store, merchant, url):
    m_norm = norm(merchant)
    u_low = (url or "").lower()
    s_norm = norm(store)
    domain = STORE_DOMAINS.get(store_slug, "")
    if "instacart" in m_norm or "instacart.com" in u_low:
        return "instacart_store"
    if (domain and domain.replace("www.", "") in u_low) or (s_norm and s_norm != "other" and s_norm in m_norm):
        return "exact_store"
    return "other_retailer"


def _kroger_banner_url(store_slug, chain_code, upc):
    if not upc:
        return None
    # Prefer the store's own banner domain (e.g. gerbes.com, harristeeter.com, dillons.com).
    chain_map = {
        "GERBES": "www.gerbes.com",
        "HART": "www.harristeeter.com",
        "DILLONS": "www.dillons.com",
        "RALPHS": "www.ralphs.com",
        "FRED": "www.fredmeyer.com",
        "KINGSOOPERS": "www.kingsoopers.com",
        "FRYS": "www.frysfood.com",
        "SMITHS": "www.smithsfoodanddrug.com",
        "MARIANOS": "www.marianos.com",
        "QFC": "www.qfc.com",
        "KROGER": "www.kroger.com",
    }
    dom = KROGER_BANNER_DOMAINS.get(store_slug) or chain_map.get((chain_code or "").upper(), "www.kroger.com")
    return f"https://{dom}/p/-/{upc}"


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


def _kroger_location(s3, bucket, store_slug, zip_code, token):
    """Resolve and cache exact physical store details (locationId, chain, name, address) for ZIP."""
    if not zip_code or zip_code == "00000":
        return None
    cache_key = "online-catalog/_kroger_locations.json"
    locs = _get_json(s3, bucket, cache_key, {})
    lk = f"{store_slug}_{zip_code}"
    cached = locs.get(lk)
    if isinstance(cached, dict) and cached.get("locationId"):
        return cached
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
    addr = chosen.get("address") or {}
    info = {
        "locationId": chosen.get("locationId"),
        "chain": chosen.get("chain"),
        "name": chosen.get("name"),
        "address": ", ".join(p for p in [addr.get("addressLine1"), addr.get("city"), addr.get("state"), addr.get("zipCode")] if p),
    }
    if info["locationId"]:
        locs[lk] = info
        _put_json(s3, bucket, cache_key, locs)
    return info


def lookup_kroger(s3, bucket, store_slug, zip_code, item, loc_info=None):
    if store_slug not in KROGER_BANNERS or not (KROGER_CLIENT_ID and KROGER_CLIENT_SECRET):
        return None
    try:
        token = _kroger_token()
        loc = loc_info or _kroger_location(s3, bucket, store_slug, zip_code, token)
        if not loc or not loc.get("locationId"):
            return None
        loc_id = loc["locationId"]
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
                sim, rank, size_ok = 100.0, 200.0, True
            else:
                sim = fuzz.token_set_ratio(target, norm(desc))
                if sim < 62:
                    continue
                allowed, adj, size_ok = _size_check(item, f"{desc} {size_str}")
                if not allowed:
                    continue
                rank = sim + adj
            if rank > best_rank and rank >= 55:
                best_rank = rank
                best = {
                    "name": full_name,
                    "size_text": size_str or None,
                    "size_verified": size_ok,
                    "upc": p.get("upc"),
                    "price": reg if reg is not None else promo,
                    "promo_price": promo if (promo and reg and promo < reg) else None,
                    "match_score": round(sim, 1),
                    "source": "kroger_api",
                    "store_match": "exact_store",
                    "merchant": loc.get("name") or store_slug.replace("-", " ").title(),
                    "url": _kroger_banner_url(store_slug, loc.get("chain"), p.get("upc")),
                }
        return best
    except Exception as e:
        print("kroger lookup error:", repr(e))
        return None


# ==================== TIER 2: SERPER DIRECT STORE + INSTACART + GOOGLE SHOPPING ====================

def _is_blocked_listing(merchant, title, item_label):
    m = (merchant or "").lower()
    if any(b in m for b in BLOCKED_MERCHANTS):
        return True
    if MULTIPACK_RE.search(title or "") and not MULTIPACK_RE.search(item_label or ""):
        return True
    return False


def lookup_serper(store, store_slug, zip_code, item):
    """
    Live search across ALL grocery stores + Instacart (requires SERPER_API_KEY).
    Always returns a direct product URL (never null), rejects resellers/multipacks/size mismatches,
    and flags whether the match is from the exact store, Instacart, or another retailer.
    """
    if not SERPER_API_KEY:
        return None
    label = f"{item.get('brand', '')} {item.get('product_name', '')} {item.get('size_text', '')}".strip()
    in_store = parse_price(item.get("in_store_price"))
    headers = {"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"}

    # 2a. Google Shopping structured results
    shopping_fallback = None
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
            if _is_blocked_listing(merch, title, label):
                continue
            sim = fuzz.token_set_ratio(norm(label), norm(title))
            if sim < 58:
                continue
            allowed, adj, size_ok = _size_check(item, title)
            if not allowed:
                continue
            url = s.get("link") or s.get("productUrl")
            if not url and s.get("productId"):
                url = f"https://www.google.com/shopping/product/{s['productId']}"
            smatch = _classify_store_match(store_slug, store, merch, url)
            bonus = 28 if smatch == "exact_store" else (24 if smatch == "instacart_store" else 0)
            rank = sim + bonus + adj
            if rank > best_rank and rank >= 55:
                best_rank = rank
                best = {
                    "name": title,
                    "size_verified": size_ok,
                    "upc": item.get("upc_barcode"),
                    "price": price,
                    "promo_price": None,
                    "match_score": round(sim, 1),
                    "source": "instacart_search" if smatch == "instacart_store" else "google_shopping",
                    "store_match": smatch,
                    "merchant": merch,
                    "url": url,
                }
        if best and best["store_match"] in ("exact_store", "instacart_store"):
            return best
        shopping_fallback = best
    except Exception as e:
        print("serper shopping error:", repr(e))

    # 2b. Targeted store-site / Instacart search fallback (direct URL on store website or Instacart)
    try:
        domain = STORE_DOMAINS.get(store_slug)
        site_clause = f"(site:{domain} OR site:instacart.com)" if domain else "site:instacart.com"
        store_term = f'"{store}"' if store and store.lower() != "other" and not domain else ""
        q = f'{site_clause} {store_term} "{item.get("brand", "")} {item.get("product_name", "")}" {item.get("size_text", "")}'.strip()
        res = _http_json("https://google.serper.dev/search", method="POST", headers=headers,
                         data=json.dumps({"q": q, "gl": "us", "hl": "en", "num": 5}).encode())
        for r in res.get("organic", []) or []:
            title = r.get("title", "")
            url = r.get("link")
            if not url or _is_blocked_listing("", title, label):
                continue
            sim = fuzz.token_set_ratio(norm(label), norm(title))
            if sim < 58:
                continue
            allowed, _, size_ok = _size_check(item, title)
            if not allowed:
                continue
            price = (
                parse_price(r.get("priceRange"))
                or parse_price((r.get("attributes") or {}).get("Price"))
                or parse_price(r.get("snippet"))
            )
            if not _price_plausible(price, in_store):
                continue
            is_ic = "instacart.com" in url.lower()
            return {
                "name": title,
                "size_verified": size_ok,
                "upc": item.get("upc_barcode"),
                "price": price,
                "promo_price": None,
                "match_score": round(sim, 1),
                "source": "instacart_search" if is_ic else "store_search",
                "store_match": "instacart_store" if is_ic else "exact_store",
                "merchant": (f"Instacart ({store})" if is_ic else store) if store and store.lower() != "other" else "Online",
                "url": url,
            }
    except Exception as e:
        print("serper store/instacart search error:", repr(e))

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
                "size_verified": True,
                "upc": code,
                "price": price,
                "promo_price": None,
                "match_score": 100.0,
                "source": "open_prices",
                "store_match": "other_retailer",
                "merchant": f"{merch} (Open Prices)",
                "url": f"https://prices.openfoodfacts.org/products/{code}",
            }
    except Exception as e:
        print("open_prices lookup error:", repr(e))
    return None


# ==================== MATCHING & S3 7-DAY CACHE ORCHESTRATOR ====================

def _match_in_catalog(item, catalog, default_source, now, cutoff=65):
    """UPC exact match first, then rapidfuzz name + size match. Skips stale live-cached entries."""
    fresh = [c for c in catalog if _is_fresh(c, now)]
    if not fresh:
        return None

    item_upc = norm_upc(item.get("upc_barcode"))
    if item_upc:
        for c in fresh:
            if c.get("upc") and norm_upc(c.get("upc")) == item_upc:
                return {
                    "name": c.get("name"),
                    "size_verified": True,
                    "price": parse_price(c.get("price")),
                    "promo_price": parse_price(c.get("promo_price")),
                    "match_score": 100.0,
                    "source": c.get("source") or default_source,
                    "store_match": c.get("store_match") or "exact_store",
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
    allowed, _, size_ok = _size_check(item, c.get("name", ""))
    if not allowed:
        return None
    return {
        "name": c.get("name"),
        "size_verified": size_ok,
        "price": parse_price(c.get("price")),
        "promo_price": parse_price(c.get("promo_price")),
        "match_score": round(m[1], 1),
        "source": c.get("source") or default_source,
        "store_match": c.get("store_match") or "exact_store",
        "merchant": c.get("merchant"),
        "url": c.get("url"),
    }


def resolve_prices(s3, bucket, store, store_slug, zip_code, items):
    """
    Resolve online prices for extracted items using:
      0. S3 ZIP catalog / 7-day live cache (UPC first, then fuzzy name + size check)
      1. S3 chain catalog / reference.json
      2. Live APIs (Kroger/Gerbes exact store -> Serper Shopping & Direct Store/Instacart -> Open Prices UPC)
         capped at MAX_LIVE_LOOKUPS_PER_RUN and written back to S3 cache
      3. Built-in Aldi sample fallback ONLY for Aldi when no live API is configured
    Returns (matches_by_index, catalog_source, catalog_file, lookup_stats, resolved_store).
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

    resolved_store = None
    if store_slug in KROGER_BANNERS and KROGER_CLIENT_ID and KROGER_CLIENT_SECRET and zip_code != "00000":
        try:
            tok = _kroger_token()
            resolved_store = _kroger_location(s3, bucket, store_slug, zip_code, tok)
        except Exception as e:
            print("kroger location resolve error:", repr(e))

    matches = {}
    new_cache_entries = []
    live_calls = 0
    stats = {
        "cache_hits": 0, "live_lookups": 0, "sample_hits": 0, "unmatched": 0,
        "providers_configured": {
            "kroger_api": bool(KROGER_CLIENT_ID and KROGER_CLIENT_SECRET),
            "serper_search": bool(SERPER_API_KEY),
            "open_prices": ENABLE_OPEN_PRICES,
        },
    }
    has_live_provider = bool((store_slug in KROGER_BANNERS and KROGER_CLIENT_ID and KROGER_CLIENT_SECRET) or SERPER_API_KEY)

    for idx, it in enumerate(items):
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
                live = lookup_kroger(s3, bucket, store_slug, zip_code, it, loc_info=resolved_store)
            if not live and SERPER_API_KEY and live_calls < MAX_LIVE_LOOKUPS_PER_RUN:
                live_calls += 1
                live = lookup_serper(store, store_slug, zip_code, it)
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
                    "store_match": live.get("store_match"),
                    "url": live.get("url"),
                    "fetched_at": now.isoformat(),
                })
                continue

        # Tier 4: Built-in Aldi sample fallback ONLY for Aldi when no live provider is configured.
        if store_slug == "aldi" and not has_live_provider:
            m = _match_in_catalog(it, DEFAULT_CATALOG, "sample_fallback", now, cutoff=65)
            if m:
                m["merchant"] = "Sample catalog"
                m["store_match"] = "sample"
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
            "resolved_store": resolved_store,
            "updated_at": now.isoformat(),
            "items": merged[-2000:],
        })

    sources = {m["source"] for m in matches.values()}
    live_sources = {"kroger_api", "instacart_search", "store_search", "google_shopping", "open_prices"}
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

    return matches, catalog_source, catalog_file, stats, resolved_store
