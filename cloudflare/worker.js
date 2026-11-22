// Cloudflare Worker: grocery price comparison app (route: discourseforensics.com/grocery*)
// Secrets/vars required: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, S3_BUCKET
// Optional: AWS_REGION (default us-east-1), ADMIN_PIN (unlocks human-verified edits in UI)

const enc = new TextEncoder();

const toHex = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
const sha256Hex = async (data) => toHex(await crypto.subtle.digest("SHA-256", typeof data === "string" ? enc.encode(data) : data));

async function hmac(key, data) {
  const k = await crypto.subtle.importKey("raw", key, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return new Uint8Array(await crypto.subtle.sign("HMAC", k, enc.encode(data)));
}

async function signingKey(secret, date, region) {
  const kDate = await hmac(enc.encode("AWS4" + secret), date);
  const kRegion = await hmac(kDate, region);
  const kService = await hmac(kRegion, "s3");
  return hmac(kService, "aws4_request");
}

const uriEncode = (s) =>
  encodeURIComponent(s).replace(/[!'()*]/g, (c) => "%" + c.charCodeAt(0).toString(16).toUpperCase());

const encodeKey = (key) => key.split("/").map(uriEncode).join("/");

function amzDates(now = new Date()) {
  const iso = now.toISOString().replace(/[:-]|\.\d{3}/g, "");
  return { amzDate: iso, dateStamp: iso.slice(0, 8) };
}

function cfg(env) {
  return {
    accessKeyId: String(env.AWS_ACCESS_KEY_ID || "").trim(),
    secretAccessKey: String(env.AWS_SECRET_ACCESS_KEY || "").trim(),
    bucket: String(env.S3_BUCKET || "").trim(),
    region: String(env.AWS_REGION || "us-east-1").trim(),
    adminPin: String(env.ADMIN_PIN || "").trim(),
  };
}

function s3Host(env) {
  const { bucket, region } = cfg(env);
  return { region, host: `${bucket}.s3.${region}.amazonaws.com` };
}

function safeEqual(a, b) {
  const x = enc.encode(a), y = enc.encode(b);
  if (x.length !== y.length || x.length === 0) return false;
  let diff = 0;
  for (let i = 0; i < x.length; i++) diff |= x[i] ^ y[i];
  return diff === 0;
}

// Presigned URL (query-string SigV4) for browser PUT upload (video or receipt photo).
async function presignPut(env, key, expires = 900) {
  const { accessKeyId, secretAccessKey } = cfg(env);
  const { region, host } = s3Host(env);
  const { amzDate, dateStamp } = amzDates();
  const scope = `${dateStamp}/${region}/s3/aws4_request`;
  // Only `host` is signed (never content-type), so browser MIME differences cannot break the signature.
  const params = {
    "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
    "X-Amz-Credential": `${accessKeyId}/${scope}`,
    "X-Amz-Date": amzDate,
    "X-Amz-Expires": String(expires),
    "X-Amz-SignedHeaders": "host",
  };
  const canonicalQuery = Object.keys(params)
    .sort()
    .map((k) => `${uriEncode(k)}=${uriEncode(params[k])}`)
    .join("&");
  const canonicalUri = "/" + encodeKey(key);
  const canonicalRequest = ["PUT", canonicalUri, canonicalQuery, `host:${host}\n`, "host", "UNSIGNED-PAYLOAD"].join("\n");
  const stringToSign = ["AWS4-HMAC-SHA256", amzDate, scope, await sha256Hex(canonicalRequest)].join("\n");
  const sig = toHex(await hmac(await signingKey(secretAccessKey, dateStamp, region), stringToSign));
  return `https://${host}${canonicalUri}?${canonicalQuery}&X-Amz-Signature=${sig}`;
}

// Header-signed GET or PUT against S3.
async function s3Request(env, method, key, body = null, contentType = null) {
  const { accessKeyId, secretAccessKey } = cfg(env);
  const { region, host } = s3Host(env);
  const { amzDate, dateStamp } = amzDates();
  const scope = `${dateStamp}/${region}/s3/aws4_request`;
  const payload = body == null ? "" : body;
  const payloadHash = await sha256Hex(payload);
  const canonicalUri = "/" + encodeKey(key);
  const signedHeaders = "host;x-amz-content-sha256;x-amz-date";
  const canonicalHeaders = `host:${host}\nx-amz-content-sha256:${payloadHash}\nx-amz-date:${amzDate}\n`;
  const canonicalRequest = [method, canonicalUri, "", canonicalHeaders, signedHeaders, payloadHash].join("\n");
  const stringToSign = ["AWS4-HMAC-SHA256", amzDate, scope, await sha256Hex(canonicalRequest)].join("\n");
  const sig = toHex(await hmac(await signingKey(secretAccessKey, dateStamp, region), stringToSign));
  const auth = `AWS4-HMAC-SHA256 Credential=${accessKeyId}/${scope}, SignedHeaders=${signedHeaders}, Signature=${sig}`;
  const headers = { "x-amz-content-sha256": payloadHash, "x-amz-date": amzDate, Authorization: auth };
  if (contentType) headers["content-type"] = contentType;
  return fetch(`https://${host}${canonicalUri}`, { method, headers, body: method === "PUT" ? payload : undefined });
}

const s3Get = (env, key) => s3Request(env, "GET", key);
const s3PutJson = (env, key, obj) => s3Request(env, "PUT", key, JSON.stringify(obj, null, 2), "application/json");

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });

async function proxyJson(env, key, fallback) {
  const r = await s3Get(env, key);
  if (r.status === 404 || r.status === 403) {
    return fallback === null ? json({ error: "not_found" }, 404) : json(fallback);
  }
  if (!r.ok) return json({ error: "s3_error", status: r.status }, 502);
  return new Response(r.body, { headers: { "content-type": "application/json", "cache-control": "no-store" } });
}

function slugify(s) {
  const slug = String(s || "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 20)
    .replace(/-+$/, "");
  return slug || "other";
}

const cleanZip = (z) => (/^\d{5}$/.test(String(z || "").trim()) ? String(z).trim() : "00000");

function recalcSummary(run) {
  const rows = run.items || [];
  const matched = rows.filter((r) => r.online_price != null && r.in_store_price && !r.flagged_wrong);
  const avgPct = matched.length ? matched.reduce((a, r) => a + (r.pct_markup || 0), 0) / matched.length : 0;
  const savings = matched.reduce((a, r) => a + (r.dollar_markup || 0), 0);
  run.summary = {
    total_items: rows.length,
    matched_items: matched.length,
    verified_items: rows.filter((r) => r.human_verified).length,
    avg_pct_markup: Math.round(avgPct * 10) / 10,
    total_basket_savings: Math.round(savings * 100) / 100,
  };
}

async function handleVerify(request, env) {
  const { adminPin } = cfg(env);
  if (!adminPin) return json({ error: "Verification editing is not enabled (ADMIN_PIN secret not configured)." }, 503);
  const supplied = String(request.headers.get("x-admin-pin") || "").trim();
  if (!safeEqual(supplied, adminPin)) return json({ error: "Incorrect PIN." }, 401);

  const body = await request.json();
  if (body && body.action === "check_pin") return json({ ok: true });

  const walkId = String(body.walk_id || "");
  if (!/^[A-Za-z0-9_-]{1,80}$/.test(walkId)) return json({ error: "Invalid walk_id" }, 400);
  const idx = Number(body.item_index);
  if (!Number.isInteger(idx) || idx < 0) return json({ error: "Invalid item_index" }, 400);

  const runKey = `public/runs/${walkId}.json`;
  const r = await s3Get(env, runKey);
  if (!r.ok) return json({ error: "Run not found" }, 404);
  const run = await r.json();
  if (!run.items || !run.items[idx]) return json({ error: "Item not found" }, 404);

  const it = run.items[idx];
  if (typeof body.verified === "boolean") {
    it.human_verified = body.verified;
    if (body.verified) it.flagged_wrong = false;
  }
  if (typeof body.flagged_wrong === "boolean") {
    it.flagged_wrong = body.flagged_wrong;
    if (body.flagged_wrong) it.human_verified = false;
  }
  if (typeof body.note === "string") it.human_note = body.note.slice(0, 200);
  if (typeof body.corrected_name === "string" && body.corrected_name.trim()) {
    it.brand = "";
    it.product_name = body.corrected_name.trim().slice(0, 120);
  }
  if (typeof body.corrected_size === "string") it.size_text = body.corrected_size.trim().slice(0, 40);
  if (body.corrected_price != null && body.corrected_price !== "") {
    const p = Number(body.corrected_price);
    if (Number.isFinite(p) && p > 0 && p < 1000) {
      it.in_store_price = Math.round(p * 100) / 100;
      if (it.online_price != null) {
        it.dollar_markup = Math.round((it.online_price - it.in_store_price) * 100) / 100;
        it.pct_markup = Math.round(((it.online_price - it.in_store_price) / it.in_store_price) * 1000) / 10;
      }
    }
  }
  if (body.clear_online_match) {
    it.matched_catalog_item = null;
    it.online_price = null;
    it.promo_price = null;
    it.dollar_markup = null;
    it.pct_markup = null;
    it.online_url = null;
    it.online_merchant = null;
    it.store_match = null;
  }
  it.verified_at = new Date().toISOString();
  recalcSummary(run);

  const putRun = await s3PutJson(env, runKey, run);
  if (!putRun.ok) return json({ error: "Failed saving run update" }, 502);

  // Also update public/latest_prices.json if this walk is currently the latest.
  try {
    const lr = await s3Get(env, "public/latest_prices.json");
    if (lr.ok) {
      const latest = await lr.json();
      if (latest && latest.walk_id === walkId) {
        await s3PutJson(env, "public/latest_prices.json", run);
      }
    }
  } catch (_) {}

  return json({ ok: true, item: it, summary: run.summary });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";

    if (request.method === "OPTIONS") {
      return new Response(null, {
        headers: {
          "access-control-allow-origin": "*",
          "access-control-allow-methods": "GET,POST,OPTIONS",
          "access-control-allow-headers": "content-type,x-admin-pin",
        },
      });
    }

    try {
      if (path === "/grocery" && request.method === "GET") {
        return new Response(PAGE, { headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" } });
      }
      if (path === "/grocery/presign" && request.method === "POST") {
        let body = {};
        try { body = (await request.json()) || {}; } catch (_) {}
        const mode = body.mode === "receipt" ? "receipt" : "video";
        let ext = mode === "receipt" ? "jpg" : "mp4";
        if (mode === "receipt" && /^(jpg|jpeg|png|webp)$/i.test(body.ext || "")) ext = body.ext.toLowerCase();
        if (mode === "video" && /^(mp4|mov)$/i.test(body.ext || "")) ext = body.ext.toLowerCase();
        const store = slugify(body.store || "other");
        const zip = cleanZip(body.zip);
        const prefix = mode === "receipt" ? "raw-receipts" : "raw-videos";
        const tag = mode === "receipt" ? "receipt" : "walk";
        const walkId = `${tag}_${Date.now()}_${store}_${zip}`;
        const key = `${prefix}/${walkId}.${ext}`;
        return json({ url: await presignPut(env, key), key, walk_id: walkId, store, zip, mode });
      }
      if (path === "/grocery/status" && request.method === "GET") {
        return proxyJson(env, "public/status.json", { stage: "idle", message: "Waiting for upload." });
      }
      if (path === "/grocery/prices" && request.method === "GET") {
        return proxyJson(env, "public/latest_prices.json", { summary: {}, items: [] });
      }
      if (path === "/grocery/runs" && request.method === "GET") {
        return proxyJson(env, "public/runs_index.json", { runs: [] });
      }
      const runMatch = path.match(/^\/grocery\/runs\/([A-Za-z0-9_-]{1,80})$/);
      if (runMatch && request.method === "GET") {
        return proxyJson(env, `public/runs/${runMatch[1]}.json`, null);
      }
      const evMatch = path.match(/^\/grocery\/evidence\/([A-Za-z0-9_-]{1,80})\/([A-Za-z0-9_.-]{1,40}\.jpg)$/);
      if (evMatch && request.method === "GET") {
        const r = await s3Get(env, `public/evidence/${evMatch[1]}/${evMatch[2]}`);
        if (!r.ok) return new Response("Not found", { status: 404 });
        return new Response(r.body, {
          headers: { "content-type": "image/jpeg", "cache-control": "public, max-age=86400" },
        });
      }
      if (path === "/grocery/history" && request.method === "GET") {
        return proxyJson(env, "public/price_history.json", { products: {} });
      }
      if (path === "/grocery/basket" && request.method === "GET") {
        return proxyJson(env, "public/staple_basket.json", { stores: [], items: [] });
      }
      if (path === "/grocery/cost" && request.method === "GET") {
        return proxyJson(env, "public/cost_summary.json", { monthly_usd: 0, max_monthly_usd: 5 });
      }
      if (path === "/grocery/verify" && request.method === "POST") {
        return handleVerify(request, env);
      }
      return json({ error: "not_found" }, 404);
    } catch (e) {
      return json({ error: String((e && e.message) || e) }, 500);
    }
  },
};

const PAGE = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Grocery Price Forensics</title>
<script src="https://cdn.tailwindcss.com"></script>
<script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js"></script>
<style>
  body { font-family: ui-rounded, "SF Pro Rounded", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  [x-cloak] { display: none !important; }
</style>
</head>
<body class="bg-[#FAFAF9] text-stone-800 min-h-screen" x-data="app()" x-init="init()" x-cloak>
<div class="max-w-5xl mx-auto px-4 py-6 sm:py-10 space-y-6">

  <header class="text-center space-y-1.5">
    <div class="text-4xl">&#128722;</div>
    <h1 class="text-2xl sm:text-3xl font-bold text-[#15803D]">Grocery Price Forensics</h1>
    <p class="text-stone-600 text-sm sm:text-base max-w-2xl mx-auto">
      Compare real in-store shelf prices and checkout receipts against live online prices, verify every match with cropped photo proof and direct links, and track how prices vary across stores and ZIP codes.
    </p>
  </header>

  <!-- Purpose & Instructions -->
  <section class="bg-white rounded-2xl border border-stone-200 shadow-sm">
    <button type="button" @click="helpOpen = !helpOpen" class="w-full flex items-center justify-between gap-3 px-5 py-3.5 text-left">
      <span class="font-semibold text-sm sm:text-base text-stone-700">&#128214; How this works &amp; tips for accurate price matches</span>
      <span class="text-xs font-medium text-[#15803D]" x-text="helpOpen ? 'Hide' : 'Show guide'"></span>
    </button>
    <div x-show="helpOpen" class="px-5 pb-5 pt-2 border-t border-stone-100 text-sm text-stone-600 space-y-3">
      <p>
        <strong>Why we built this:</strong> Online grocery apps often add markups over shelf prices, and supermarkets frequently charge different prices from one ZIP code to the next. Upload a quick aisle video or a paper receipt photo to build transparent, photo-backed, link-verified price comparisons.
      </p>
      <div class="grid grid-cols-1 sm:grid-cols-3 gap-3 pt-1">
        <div class="rounded-xl bg-stone-50 border border-stone-200 p-3">
          <div class="font-semibold text-stone-800">1. Pick store &amp; ZIP</div>
          <p class="text-xs text-stone-500 mt-1">Your 5-digit ZIP resolves the exact neighborhood store for live store-API pricing and powers cross-location comparisons.</p>
        </div>
        <div class="rounded-xl bg-stone-50 border border-stone-200 p-3">
          <div class="font-semibold text-stone-800">2. Video or receipt photo</div>
          <p class="text-xs text-stone-500 mt-1">For videos, hold steady ~1 sec on each shelf tag so size &amp; barcode stay crisp. For receipts, snap a flat, well-lit photo of the paper receipt.</p>
        </div>
        <div class="rounded-xl bg-stone-50 border border-stone-200 p-3">
          <div class="font-semibold text-stone-800">3. Verify every match</div>
          <p class="text-xs text-stone-500 mt-1">Click any item's cropped shelf-tag thumbnail (kept 60 days) and direct online link to confirm store, size, and price &mdash; then mark it Verified.</p>
        </div>
      </div>
    </div>
  </section>

  <!-- Upload -->
  <section class="bg-white rounded-2xl border border-stone-200 shadow-sm p-5 sm:p-6 space-y-4">
    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
      <h2 class="text-lg font-semibold">Check a new store visit</h2>
      <div class="inline-flex rounded-xl bg-stone-100 p-1 text-sm">
        <button type="button" @click="setMode('video')"
          :class="mode === 'video' ? 'bg-white shadow-sm text-[#15803D] font-semibold' : 'text-stone-500'"
          class="px-3.5 py-1.5 rounded-lg transition">&#127909; Aisle Video</button>
        <button type="button" @click="setMode('receipt')"
          :class="mode === 'receipt' ? 'bg-white shadow-sm text-[#15803D] font-semibold' : 'text-stone-500'"
          class="px-3.5 py-1.5 rounded-lg transition">&#129534; Receipt Photo</button>
      </div>
    </div>

    <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
      <div class="space-y-2">
        <label class="block">
          <span class="text-sm text-stone-600">Which store?</span>
          <select x-model="store" class="mt-1 w-full rounded-xl border border-stone-300 bg-white px-3 py-2.5 focus:outline-none focus:ring-2 focus:ring-[#15803D]">
            <optgroup label="Live Store-Exact Prices (Kroger Family)">
              <template x-for="s in krogerStores" :key="s"><option :value="s" x-text="s"></option></template>
            </optgroup>
            <optgroup label="Major National &amp; Regional Chains">
              <template x-for="s in otherStores" :key="s"><option :value="s" x-text="s"></option></template>
            </optgroup>
            <option value="Other">Other (type store name)</option>
          </select>
        </label>
        <input x-show="store === 'Other'" x-model="customStore" type="text" maxlength="30" placeholder="Enter store name (e.g. Moser's, Woodman's)"
          class="w-full rounded-xl border border-stone-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[#15803D]">
      </div>
      <label class="block">
        <span class="text-sm text-stone-600">ZIP code</span>
        <input x-model="zip" inputmode="numeric" maxlength="5" placeholder="e.g. 65203 or 20176"
          class="mt-1 w-full rounded-xl border border-stone-300 px-3 py-2.5 focus:outline-none focus:ring-2 focus:ring-[#15803D]">
      </label>
    </div>

    <div @dragover.prevent="drag = true" @dragleave.prevent="drag = false" @drop.prevent="onDrop($event)"
      :class="drag ? 'border-[#15803D] bg-green-50' : 'border-stone-300 bg-stone-50'"
      class="rounded-2xl border-2 border-dashed p-6 text-center space-y-3 transition-colors">
      <div class="text-3xl" x-text="mode === 'receipt' ? '\\u{1F9FE}' : '\\u{1F3A5}'"></div>
      <p class="text-stone-600 text-sm"
        x-text="file ? 'Ready: ' + file.name : (mode === 'receipt' ? 'Drag a receipt photo here (JPG/PNG), or snap one below' : 'Drag an aisle video here (MP4/MOV), or record one below')"></p>
      <div class="flex flex-col sm:flex-row gap-3 justify-center">
        <button type="button" @click="$refs.pick.click()"
          class="px-5 py-3 rounded-xl bg-white border border-stone-300 font-medium hover:bg-stone-100"
          x-text="mode === 'receipt' ? 'Choose receipt photo' : 'Choose a video'"></button>
        <button type="button" @click="$refs.cam.click()"
          class="px-5 py-3 rounded-xl bg-white border border-stone-300 font-medium hover:bg-stone-100"
          x-text="mode === 'receipt' ? 'Snap receipt with camera' : 'Use phone camera'"></button>
      </div>
      <input x-ref="pick" type="file" :accept="mode === 'receipt' ? 'image/*' : 'video/*'" class="hidden" @change="setFile($event.target.files[0])">
      <input x-ref="cam" type="file" :accept="mode === 'receipt' ? 'image/*' : 'video/*'" capture="environment" class="hidden" @change="setFile($event.target.files[0])">
    </div>

    <button @click="upload()" :disabled="!file || uploading"
      class="w-full sm:w-auto px-8 py-3.5 rounded-xl bg-[#15803D] text-white font-semibold text-base shadow-sm hover:bg-green-800 disabled:opacity-40 disabled:cursor-not-allowed">
      <span x-text="uploading ? 'Uploading ' + progress + '%' : (mode === 'receipt' ? 'Upload receipt & compare prices' : 'Upload video & compare prices')"></span>
    </button>
    <div x-show="uploading || progress > 0" class="w-full bg-stone-200 rounded-full h-3 overflow-hidden">
      <div class="bg-[#15803D] h-3 rounded-full transition-all duration-300" :style="'width:' + progress + '%'"></div>
    </div>
    <p x-show="uploadError" class="rounded-xl bg-red-50 border border-red-200 text-red-700 text-sm px-4 py-3" x-text="uploadError"></p>

    <div x-show="status.stage && status.stage !== 'idle'" class="rounded-xl bg-stone-50 border border-stone-200 px-4 py-3 flex items-center gap-3 text-sm">
      <span class="inline-block w-2.5 h-2.5 rounded-full"
        :class="status.stage === 'complete' ? 'bg-[#15803D]' : status.stage === 'error' ? 'bg-red-500' : 'bg-amber-400 animate-pulse'"></span>
      <span class="font-medium" x-text="stageText(status.stage)"></span>
      <span class="text-stone-500" x-show="status.stage === 'error'" x-text="status.message"></span>
    </div>
  </section>

  <!-- Tabs -->
  <nav class="grid grid-cols-2 sm:grid-cols-4 gap-1.5 bg-stone-100 rounded-2xl p-1.5">
    <template x-for="t in tabs" :key="t.id">
      <button @click="openTab(t.id)" x-text="t.label"
        :class="tab === t.id ? 'bg-white shadow-sm text-[#15803D]' : 'text-stone-500 hover:text-stone-700'"
        class="rounded-xl py-2.5 px-2 text-sm font-semibold transition"></button>
    </template>
  </nav>

  <!-- TAB 1: Latest scan -->
  <section x-show="tab === 'latest'" class="space-y-5">
    <div x-show="viewingPast" class="rounded-xl bg-amber-50 border border-amber-200 px-4 py-3 text-sm flex items-center justify-between gap-3">
      <span>Viewing an earlier scan from <b x-text="when(run.timestamp || run.generated_at)"></b>.</span>
      <button @click="backToLatest()" class="font-semibold text-[#15803D] underline">Back to latest</button>
    </div>

    <div x-show="!run.walk_id" class="bg-white rounded-2xl border border-stone-200 p-8 text-center text-stone-500">
      No scans yet. Upload a video or receipt above to get started.
    </div>

    <template x-if="run.walk_id">
      <div class="space-y-5">
        <div class="grid grid-cols-1 sm:grid-cols-3 gap-4">
          <div class="bg-white rounded-2xl border border-stone-200 p-5">
            <div class="flex items-center justify-between">
              <span class="text-sm text-stone-500">Items found</span>
              <span class="rounded-full bg-stone-100 text-stone-600 px-2 py-0.5 text-xs font-medium"
                x-text="run.scan_type === 'receipt' ? 'Receipt photo' : 'Aisle video'"></span>
            </div>
            <div class="text-4xl font-bold mt-1" x-text="items().length"></div>
            <div class="text-xs text-stone-600 mt-1 font-medium" x-text="(run.resolved_store && run.resolved_store.name) || ((run.store || 'Store') + (run.zip_code && run.zip_code !== '00000' ? ' \\u00B7 ZIP ' + run.zip_code : ''))"></div>
            <div class="text-xs text-stone-400" x-show="run.resolved_store && run.resolved_store.address" x-text="run.resolved_store && run.resolved_store.address"></div>
          </div>
          <div class="bg-white rounded-2xl border border-stone-200 p-5">
            <div class="text-sm text-stone-500">Average savings in-store</div>
            <div class="text-4xl font-bold mt-1 text-[#15803D]" x-text="'$' + avgSavings().toFixed(2)"></div>
            <div class="text-xs text-stone-400 mt-1" x-text="'per matched item \\u00B7 online runs ' + avgPct().toFixed(1) + '% higher'"></div>
            <div class="text-xs text-stone-500 mt-1" x-show="verifiedCount() > 0" x-text="'\\u2713 ' + verifiedCount() + ' human-verified'"></div>
          </div>
          <div class="bg-white rounded-2xl border border-stone-200 p-5">
            <div class="text-sm text-stone-500">Where online prices come from</div>
            <div class="mt-2"><span class="inline-block rounded-full px-3 py-1 text-sm font-semibold" :class="source().cls" x-text="source().label"></span></div>
            <div class="text-xs text-stone-400 mt-2" x-text="source().hint"></div>
          </div>
        </div>

        <!-- Scan quality -->
        <div class="bg-white rounded-2xl border border-stone-200">
          <button @click="qOpen = !qOpen" class="w-full flex items-center justify-between gap-3 px-5 py-4 text-left">
            <span class="flex items-center gap-3">
              <span class="inline-block w-3 h-3 rounded-full" :class="quality().ok ? 'bg-[#15803D]' : 'bg-amber-500'"></span>
              <span class="font-semibold" x-text="quality().label"></span>
            </span>
            <span class="text-sm text-stone-400" x-text="qOpen ? 'Hide details' : 'Show details'"></span>
          </button>
          <div x-show="qOpen" class="px-5 pb-5 text-sm text-stone-600 space-y-3 border-t border-stone-100 pt-4">
            <p x-text="quality().frames"></p>
            <p x-text="quality().barcodes"></p>
            <p x-show="run.receipt_image_url">
              <button type="button" @click="openLightbox(run.receipt_image_url, 'Full uploaded receipt photo', null)"
                class="text-[#15803D] font-semibold underline">View full receipt photo</button>
            </p>
            <p x-show="run.cost" class="text-xs text-stone-500"
              x-text="'Scan AI cost: $' + Number((run.cost && run.cost.run_usd) || 0).toFixed(4) + ' \\u00B7 Monthly spend: $' + Number((run.cost && run.cost.monthly_usd) || 0).toFixed(4) + ' / $' + Number((run.cost && run.cost.max_monthly_usd) || 5).toFixed(2)"></p>
            <div x-show="(sq().unreadable_tags || []).length">
              <div class="font-semibold text-stone-700 mb-1">Tags we couldn't read</div>
              <ul class="list-disc pl-5 space-y-0.5">
                <template x-for="(t, i) in (sq().unreadable_tags || [])" :key="i">
                  <li><span x-text="reasonText(t.reason)"></span><span x-show="t.partial_text" x-text="' \\u2014 we could make out \\u201C' + t.partial_text + '\\u201D'"></span></li>
                </template>
              </ul>
            </div>
            <div x-show="uncertain().length">
              <div class="font-semibold text-stone-700 mb-1">Items we're not fully sure about</div>
              <ul class="list-disc pl-5 space-y-0.5">
                <template x-for="(r, i) in uncertain()" :key="i"><li x-text="name(r)"></li></template>
              </ul>
            </div>
          </div>
        </div>

        <!-- Comparison Table -->
        <div class="bg-white rounded-2xl border border-stone-200 p-5 space-y-3">
          <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
            <div class="flex items-center gap-3">
              <h2 class="text-lg font-semibold">In-store vs. online prices</h2>
              <button type="button" @click="toggleAdmin()"
                class="text-xs px-2.5 py-1 rounded-lg border transition"
                :class="adminUnlocked ? 'border-[#15803D] bg-green-50 text-[#15803D] font-semibold' : 'border-stone-300 text-stone-500 hover:text-stone-800'"
                x-text="adminUnlocked ? '\\u{1F513} Verify mode ON' : '\\u{1F512} Unlock verify'"></button>
            </div>
            <input x-model="q" type="search" placeholder="Search items&hellip;"
              class="w-full sm:w-64 rounded-xl border border-stone-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[#15803D]">
          </div>
          <p x-show="verifyMsg" class="text-xs text-[#15803D] font-medium" x-text="verifyMsg"></p>
          <div class="overflow-x-auto">
            <table class="w-full text-sm">
              <thead class="text-stone-500 border-b border-stone-200">
                <tr>
                  <th class="text-left font-medium py-2 pr-3 w-16">Photo</th>
                  <template x-for="c in cols" :key="c.k">
                    <th class="text-left font-medium py-2 pr-4 cursor-pointer select-none whitespace-nowrap hover:text-stone-800" @click="sortBy(c.k)">
                      <span x-text="c.l"></span><span x-show="sortKey === c.k" x-text="sortDir > 0 ? ' \\u25B2' : ' \\u25BC'"></span>
                    </th>
                  </template>
                  <th x-show="adminUnlocked" class="text-left font-medium py-2">Review</th>
                </tr>
              </thead>
              <tbody>
                <template x-for="(r, i) in rows()" :key="r._idx">
                  <tr class="border-b border-stone-100 hover:bg-stone-50" :class="r.flagged_wrong ? 'opacity-60 bg-red-50/40' : ''">
                    <td class="py-3 pr-3">
                      <template x-if="r.evidence_url">
                        <button type="button" @click="openLightbox(r.evidence_url, name(r) + ' (' + money(r.in_store_price) + ')', r)" class="block">
                          <img :src="r.evidence_url" loading="lazy" alt="Shelf tag crop"
                            class="w-14 h-11 object-cover rounded-lg border border-stone-200 hover:ring-2 hover:ring-[#15803D]">
                        </button>
                      </template>
                      <span x-show="!r.evidence_url" class="text-xs text-stone-300">-</span>
                    </td>
                    <td class="py-3 pr-4">
                      <div class="font-medium flex flex-wrap items-center gap-1.5">
                        <span x-text="name(r)"></span>
                        <span x-show="r.human_verified" class="rounded-full bg-green-100 text-green-800 px-2 py-0.5 text-[11px] font-semibold">&#10003; Verified</span>
                        <span x-show="r.flagged_wrong" class="rounded-full bg-red-100 text-red-700 px-2 py-0.5 text-[11px] font-semibold">&#9873; Flagged</span>
                      </div>
                      <div class="text-xs text-stone-400" x-show="r.receipt_raw_text" x-text="'Receipt: ' + r.receipt_raw_text"></div>
                      <div class="text-xs text-stone-400" x-show="r.upc_barcode" x-text="'UPC ' + r.upc_barcode"></div>
                      <div class="text-xs text-stone-500 mt-0.5" x-show="r.matched_catalog_item">
                        <span x-text="'Matched: ' + r.matched_catalog_item"></span>
                        <span class="text-stone-400" x-show="r.match_score" x-text="' (' + Math.round(r.match_score) + '%)'"></span>
                      </div>
                      <div class="flex flex-wrap gap-1 mt-1" x-show="r.matched_catalog_item">
                        <span x-show="r.store_match === 'exact_store'" class="rounded-full bg-green-100 text-green-800 px-2 py-0.5 text-[11px] font-medium">Same store</span>
                        <span x-show="r.store_match === 'instacart_store'" class="rounded-full bg-sky-100 text-sky-800 px-2 py-0.5 text-[11px] font-medium">Instacart</span>
                        <span x-show="r.store_match === 'other_retailer'" class="rounded-full bg-amber-100 text-amber-800 px-2 py-0.5 text-[11px] font-medium">Different retailer</span>
                        <span x-show="r.size_verified === false" class="rounded-full bg-amber-100 text-amber-800 px-2 py-0.5 text-[11px] font-medium">Check size</span>
                      </div>
                      <div class="text-xs text-stone-500 italic mt-0.5" x-show="r.human_note" x-text="'Note: ' + r.human_note"></div>
                      <div class="text-xs text-stone-400" x-show="!r.matched_catalog_item">No verified online match yet</div>
                    </td>
                    <td class="py-3 pr-4 whitespace-nowrap" x-text="r.size_text || '-'"></td>
                    <td class="py-3 pr-4 whitespace-nowrap font-medium" x-text="money(r.in_store_price)"></td>
                    <td class="py-3 pr-4 whitespace-nowrap">
                      <div class="font-medium" x-text="money(r.online_price)"></div>
                      <div class="text-xs text-emerald-700 font-medium" x-show="r.promo_price" x-text="'Sale ' + money(r.promo_price)"></div>
                      <template x-if="r.online_url">
                        <a :href="r.online_url" target="_blank" rel="noopener noreferrer"
                          class="text-xs text-[#15803D] underline hover:text-green-900 inline-flex items-center gap-0.5"
                          x-text="(r.online_merchant || 'View product') + ' \\u2197'"></a>
                      </template>
                      <div x-show="!r.online_url && r.online_merchant" class="text-xs text-stone-400" x-text="r.online_merchant"></div>
                    </td>
                    <td class="py-3 pr-4 whitespace-nowrap font-semibold text-[#15803D]" x-text="r.dollar_markup == null ? '-' : money(r.dollar_markup)"></td>
                    <td class="py-3 pr-4 whitespace-nowrap" x-text="r.pct_markup == null ? '-' : r.pct_markup.toFixed(1) + '%'"></td>
                    <td x-show="adminUnlocked" class="py-3 whitespace-nowrap space-x-1">
                      <button type="button" @click="quickVerify(r._idx, !r.human_verified, false)" title="Mark human-verified"
                        class="px-2 py-1 rounded-lg border text-xs" :class="r.human_verified ? 'bg-[#15803D] text-white border-[#15803D]' : 'border-stone-300 hover:bg-green-50'">&#10003;</button>
                      <button type="button" @click="openEdit(r)" title="Edit item / note"
                        class="px-2 py-1 rounded-lg border border-stone-300 text-xs hover:bg-stone-100">&#9998;</button>
                      <button type="button" @click="quickVerify(r._idx, false, !r.flagged_wrong)" title="Flag wrong OCR or match"
                        class="px-2 py-1 rounded-lg border text-xs" :class="r.flagged_wrong ? 'bg-red-600 text-white border-red-600' : 'border-stone-300 hover:bg-red-50'">&#9873;</button>
                    </td>
                  </tr>
                </template>
                <tr x-show="rows().length === 0"><td colspan="8" class="py-8 text-center text-stone-400">Nothing to show.</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </template>
  </section>

  <!-- TAB 2: Staple Basket (120+ items across US states & national stores) -->
  <section x-show="tab === 'basket'" class="space-y-5">

    <!-- DRILL-DOWN VIEW: Single State / Store Full Price Sheet -->
    <template x-if="selectedStore">
      <div class="space-y-4">
        <div class="bg-white rounded-2xl border border-[#15803D] p-5 space-y-3 shadow-sm">
          <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
            <div>
              <button type="button" @click="selectedStore = null; sqFilter = ''"
                class="text-xs font-semibold text-[#15803D] hover:underline mb-1 inline-block">&larr; Back to all states &amp; stores</button>
              <h2 class="text-xl font-bold text-stone-900"
                x-text="(selectedStore.state && selectedStore.state !== 'US' ? selectedStore.state + ' \\u00B7 ' : '') + selectedStore.store + ' (' + selectedStore.city + ')'"></h2>
              <p class="text-xs text-stone-500 mt-0.5" x-text="(selectedStore.store_name || '') + (selectedStore.address ? ' \\u00B7 ' + selectedStore.address : '')"></p>
            </div>
            <div class="flex flex-wrap gap-2 text-xs">
              <div class="rounded-xl bg-stone-50 border border-stone-200 px-3 py-2 text-center">
                <div class="text-stone-400">Items priced</div>
                <div class="text-base font-bold text-stone-900" x-text="selectedStore.items_priced"></div>
              </div>
              <div class="rounded-xl bg-green-50 border border-green-200 px-3 py-2 text-center" x-show="selectedStore.common_basket_total">
                <div class="text-green-700" x-text="selectedStore.common_basket_items + '-item basket'"></div>
                <div class="text-base font-bold text-[#15803D]" x-text="money(selectedStore.common_basket_total)"></div>
              </div>
              <div class="rounded-xl bg-stone-50 border border-stone-200 px-3 py-2 text-center">
                <div class="text-stone-400">Vs. national median</div>
                <div class="text-base font-bold" :class="selectedStore.avg_pct_vs_median <= 0 ? 'text-[#15803D]' : 'text-amber-700'"
                  x-text="(selectedStore.avg_pct_vs_median > 0 ? '+' : '') + Number(selectedStore.avg_pct_vs_median || 0).toFixed(1) + '%'"></div>
              </div>
            </div>
          </div>

          <div class="flex flex-col sm:flex-row gap-2.5 pt-2 border-t border-stone-100">
            <div class="inline-flex rounded-xl bg-stone-100 p-1 text-xs">
              <button type="button" @click="bType = 'all'" :class="bType === 'all' ? 'bg-white shadow-sm text-[#15803D] font-semibold' : 'text-stone-500'" class="px-3 py-1.5 rounded-lg">All</button>
              <button type="button" @click="bType = 'grocery'" :class="bType === 'grocery' ? 'bg-white shadow-sm text-[#15803D] font-semibold' : 'text-stone-500'" class="px-3 py-1.5 rounded-lg">Grocery</button>
              <button type="button" @click="bType = 'non_grocery'" :class="bType === 'non_grocery' ? 'bg-white shadow-sm text-[#15803D] font-semibold' : 'text-stone-500'" class="px-3 py-1.5 rounded-lg">Non-Grocery</button>
            </div>
            <select x-model="bCat" class="rounded-xl border border-stone-300 bg-white px-3 py-2 text-sm">
              <option value="">All categories</option>
              <template x-for="c in basketCategories()" :key="c"><option :value="c" x-text="c"></option></template>
            </select>
            <input x-model="sqFilter" type="search" placeholder="Search items at this store&hellip;"
              class="flex-1 rounded-xl border border-stone-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[#15803D]">
            <select x-model="sSort" class="rounded-xl border border-stone-300 bg-white px-3 py-2 text-sm">
              <option value="diff">Sort: Biggest diff vs. median</option>
              <option value="name">Sort: A &ndash; Z</option>
              <option value="price">Sort: Price low &ndash; high</option>
            </select>
          </div>
        </div>

        <div class="bg-white rounded-2xl border border-stone-200 p-4 sm:p-5 overflow-x-auto">
          <table class="w-full text-sm">
            <thead class="text-stone-500 border-b border-stone-200">
              <tr>
                <th class="text-left font-medium py-2 pr-3">Staple Item &amp; Exact Store SKU</th>
                <th class="text-left font-medium py-2 pr-3">Category</th>
                <th class="text-left font-medium py-2 pr-3">Store Price</th>
                <th class="text-left font-medium py-2 pr-3">National Median</th>
                <th class="text-left font-medium py-2 pr-3">Vs. Median</th>
                <th class="text-left font-medium py-2">Direct Link</th>
              </tr>
            </thead>
            <tbody>
              <template x-for="row in selectedStoreRows()" :key="row.id">
                <tr class="border-b border-stone-100 hover:bg-stone-50">
                  <td class="py-2.5 pr-3">
                    <div class="font-medium" x-text="row.name"></div>
                    <div class="text-xs text-stone-500">
                      <span x-text="row.p.product_title"></span>
                      <span x-show="row.p.same_upc" class="ml-1 rounded bg-green-50 text-green-700 px-1.5 py-0.5 text-[10px]">Exact UPC</span>
                    </div>
                  </td>
                  <td class="py-2.5 pr-3 text-xs text-stone-500 whitespace-nowrap" x-text="row.category"></td>
                  <td class="py-2.5 pr-3 whitespace-nowrap font-semibold">
                    <span x-text="money(row.p.price)"></span>
                    <div class="text-xs text-emerald-700 font-medium" x-show="row.p.promo_price" x-text="'Sale ' + money(row.p.promo_price)"></div>
                  </td>
                  <td class="py-2.5 pr-3 whitespace-nowrap text-stone-500" x-text="money(row.median_price)"></td>
                  <td class="py-2.5 pr-3 whitespace-nowrap text-xs font-semibold"
                    :class="row.diff <= 0 ? 'text-[#15803D]' : 'text-amber-700'"
                    x-text="row.diff == null ? '-' : (row.diff > 0 ? '+' : '') + money(row.diff) + ' (' + (row.diffPct > 0 ? '+' : '') + row.diffPct.toFixed(1) + '%)'"></td>
                  <td class="py-2.5 whitespace-nowrap text-xs">
                    <a x-show="row.p.url" :href="row.p.url" target="_blank" rel="noopener noreferrer"
                      class="text-[#15803D] underline hover:text-green-900">Open product &#8599;</a>
                  </td>
                </tr>
              </template>
              <tr x-show="selectedStoreRows().length === 0"><td colspan="6" class="py-8 text-center text-stone-400">No items match your filter.</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </template>

    <!-- OVERVIEW: State-by-State Summary + National Retailers + 130-Item Cross-Store Explorer -->
    <template x-if="!selectedStore">
      <div class="space-y-5">
        <div class="bg-white rounded-2xl border border-stone-200 p-5 space-y-4">
          <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
            <div>
              <h2 class="text-lg font-semibold">How Grocery &amp; Household Prices Vary by State</h2>
              <p class="text-xs text-stone-500"
                x-text="(basket.total_items || 120) + '-item basket (' + (basket.grocery_count || 100) + ' grocery + ' + (basket.non_grocery_count || 20) + ' non-grocery) across ' + (basket.states_count || 0) + ' US states. Click any state or store to see every item price and direct product link.'"></p>
            </div>
            <span class="text-xs text-stone-400" x-show="basket.generated_at" x-text="'Updated ' + when(basket.generated_at)"></span>
          </div>

          <!-- State Variation Headline Banner -->
          <div x-show="stateStores().length >= 2" class="grid grid-cols-1 sm:grid-cols-3 gap-3">
            <div class="rounded-xl bg-green-50 border border-green-200 p-3.5">
              <div class="text-xs text-green-800 font-medium">Cheapest State Basket</div>
              <div class="text-2xl font-bold text-[#15803D] mt-0.5" x-text="money(stateStores()[0].common_basket_total)"></div>
              <div class="text-xs text-stone-600" x-text="stateStores()[0].state + ' \\u00B7 ' + stateStores()[0].store + ' (' + stateStores()[0].city + ')'"></div>
            </div>
            <div class="rounded-xl bg-amber-50 border border-amber-200 p-3.5">
              <div class="text-xs text-amber-800 font-medium">Most Expensive State Basket</div>
              <div class="text-2xl font-bold text-amber-700 mt-0.5" x-text="money(stateStores()[stateStores().length - 1].common_basket_total)"></div>
              <div class="text-xs text-stone-600" x-text="stateStores()[stateStores().length - 1].state + ' \\u00B7 ' + stateStores()[stateStores().length - 1].store + ' (' + stateStores()[stateStores().length - 1].city + ')'"></div>
            </div>
            <div class="rounded-xl bg-stone-50 border border-stone-200 p-3.5">
              <div class="text-xs text-stone-500 font-medium">Same-Company State Spread</div>
              <div class="text-2xl font-bold text-stone-900 mt-0.5"
                x-text="'+' + money(stateStores()[stateStores().length - 1].dollars_above_cheapest_state)"></div>
              <div class="text-xs text-stone-500"
                x-text="'+' + Number(stateStores()[stateStores().length - 1].pct_above_cheapest_state || 0).toFixed(1) + '% across ' + (basket.common_kroger_items || 0) + ' identical shared items'"></div>
            </div>
          </div>

          <!-- Clickable State-by-State Cards with Relative Price Bar -->
          <div x-show="stateStores().length" class="space-y-2">
            <div class="text-xs font-semibold uppercase tracking-wide text-stone-500">State-by-State Ranking (Click any state to open full item list)</div>
            <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-2.5">
              <template x-for="(s, idx) in stateStores()" :key="s.key">
                <button type="button" @click="openStoreDetail(s)"
                  class="text-left rounded-xl border p-3 transition hover:border-[#15803D] hover:shadow-sm"
                  :class="idx === 0 ? 'border-[#15803D] bg-green-50/60' : 'border-stone-200 bg-stone-50/50'">
                  <div class="flex items-center justify-between gap-1">
                    <span class="font-bold text-sm text-stone-900" x-text="s.state + ' \\u00B7 ' + s.store"></span>
                    <span class="text-[11px] font-semibold px-1.5 py-0.5 rounded"
                      :class="idx === 0 ? 'bg-[#15803D] text-white' : (s.pct_above_cheapest_state > 15 ? 'bg-amber-100 text-amber-800' : 'bg-stone-200 text-stone-700')"
                      x-text="idx === 0 ? 'Lowest' : '+' + Number(s.pct_above_cheapest_state || 0).toFixed(1) + '%'"></span>
                  </div>
                  <div class="text-[11px] text-stone-500 truncate" x-text="s.city + ' ' + s.zip + ' \\u00B7 ' + s.items_priced + ' items'"></div>
                  <div class="mt-1.5 flex items-baseline justify-between">
                    <span class="text-lg font-bold text-stone-900" x-text="money(s.common_basket_total)"></span>
                    <span class="text-[11px] text-[#15803D] font-medium">View all &rarr;</span>
                  </div>
                  <div class="w-full bg-stone-200 rounded-full h-1.5 mt-1.5 overflow-hidden">
                    <div class="h-1.5 rounded-full" :class="idx === 0 ? 'bg-[#15803D]' : (s.pct_above_cheapest_state > 15 ? 'bg-amber-500' : 'bg-emerald-600')"
                      :style="'width:' + stateBarPct(s) + '%'"></div>
                  </div>
                  <div class="text-[10px] text-stone-400 mt-1" x-show="s.common_grocery_total"
                    x-text="'Grocery ' + money(s.common_grocery_total) + ' \\u00B7 Household ' + money(s.common_non_grocery_total)"></div>
                </button>
              </template>
            </div>
          </div>

          <!-- National First-Party Online Retailers (Target, Walmart) -->
          <div x-show="nationalStores().length" class="space-y-2 pt-2 border-t border-stone-100">
            <div class="text-xs font-semibold uppercase tracking-wide text-stone-500">National Online Retailers (First-Party Only &mdash; 3rd-Party Resellers &amp; Multipacks Excluded)</div>
            <div class="grid grid-cols-1 sm:grid-cols-3 gap-2.5">
              <template x-for="s in nationalStores()" :key="s.key">
                <button type="button" @click="openStoreDetail(s)"
                  class="text-left rounded-xl border border-stone-200 bg-white p-3 hover:border-[#15803D] hover:shadow-sm transition">
                  <div class="flex items-center justify-between">
                    <span class="font-bold text-sm" x-text="s.store + ' (Online)'"></span>
                    <span class="text-xs font-semibold" :class="s.avg_pct_vs_median <= 0 ? 'text-[#15803D]' : 'text-amber-700'"
                      x-text="(s.avg_pct_vs_median > 0 ? '+' : '') + Number(s.avg_pct_vs_median || 0).toFixed(1) + '% vs median'"></span>
                  </div>
                  <div class="text-xs text-stone-500 mt-1" x-text="s.items_priced + ' verified core items priced \\u00B7 View all \\u2192'"></div>
                </button>
              </template>
            </div>
          </div>

          <!-- Item Filters -->
          <div class="flex flex-col sm:flex-row gap-2.5 pt-2 border-t border-stone-100">
            <div class="inline-flex rounded-xl bg-stone-100 p-1 text-xs sm:text-sm">
              <button type="button" @click="bType = 'all'" :class="bType === 'all' ? 'bg-white shadow-sm text-[#15803D] font-semibold' : 'text-stone-500'"
                class="px-3 py-1.5 rounded-lg" x-text="'All (' + (basket.total_items || 130) + ')'"></button>
              <button type="button" @click="bType = 'grocery'" :class="bType === 'grocery' ? 'bg-white shadow-sm text-[#15803D] font-semibold' : 'text-stone-500'"
                class="px-3 py-1.5 rounded-lg" x-text="'Grocery (' + (basket.grocery_count || 108) + ')'"></button>
              <button type="button" @click="bType = 'non_grocery'" :class="bType === 'non_grocery' ? 'bg-white shadow-sm text-[#15803D] font-semibold' : 'text-stone-500'"
                class="px-3 py-1.5 rounded-lg" x-text="'Non-Grocery (' + (basket.non_grocery_count || 22) + ')'"></button>
            </div>
            <select x-model="bCat" class="rounded-xl border border-stone-300 bg-white px-3 py-2 text-sm">
              <option value="">All categories</option>
              <template x-for="c in basketCategories()" :key="c"><option :value="c" x-text="c"></option></template>
            </select>
            <input x-model="bq" type="search" placeholder="Search staple items&hellip;"
              class="flex-1 rounded-xl border border-stone-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[#15803D]">
            <select x-model="bSort" class="rounded-xl border border-stone-300 bg-white px-3 py-2 text-sm">
              <option value="spread">Sort: Biggest state/store spread</option>
              <option value="name">Sort: A &ndash; Z</option>
              <option value="low">Sort: Lowest price</option>
            </select>
          </div>
        </div>

        <div x-show="basketRows().length === 0" class="bg-white rounded-2xl border border-stone-200 p-8 text-center text-stone-500">
          No staple basket items match your filter yet.
        </div>

        <div class="space-y-2.5">
          <template x-for="it in basketRows()" :key="it.id">
            <details class="bg-white rounded-2xl border border-stone-200 p-4">
              <summary class="cursor-pointer list-none flex flex-col sm:flex-row sm:items-center justify-between gap-2">
                <div>
                  <div class="font-semibold flex flex-wrap items-center gap-2">
                    <span x-text="it.name"></span>
                    <span class="rounded-full bg-stone-100 text-stone-600 px-2 py-0.5 text-[11px] font-medium" x-text="it.category"></span>
                  </div>
                  <div class="text-xs text-stone-500 mt-0.5" x-show="it.stores_priced > 0"
                    x-text="it.stores_priced + ' stores/states \\u00B7 low ' + money(it.min_price) + ' (' + storeLabel(it.cheapest_store_key) + ') \\u00B7 high ' + money(it.max_price) + ' (' + storeLabel(it.priciest_store_key) + ')'"></div>
                </div>
                <div class="flex items-center gap-2 shrink-0">
                  <span x-show="it.dollar_spread > 0" class="rounded-full bg-amber-100 text-amber-800 px-2.5 py-1 text-xs font-semibold"
                    x-text="money(it.dollar_spread) + ' spread (+' + Number(it.pct_spread || 0).toFixed(0) + '%)'"></span>
                  <span x-show="it.stores_priced > 1 && it.dollar_spread === 0" class="rounded-full bg-green-100 text-green-800 px-2.5 py-1 text-xs font-semibold">Same everywhere</span>
                </div>
              </summary>
              <div class="overflow-x-auto mt-3 pt-3 border-t border-stone-100">
                <table class="w-full text-xs sm:text-sm">
                  <thead class="text-stone-500 border-b border-stone-200">
                    <tr>
                      <th class="text-left font-medium py-1.5 pr-3">State &amp; Store</th>
                      <th class="text-left font-medium py-1.5 pr-3">Exact Matched Product</th>
                      <th class="text-left font-medium py-1.5 pr-3">Price</th>
                      <th class="text-left font-medium py-1.5">Direct Link</th>
                    </tr>
                  </thead>
                  <tbody>
                    <template x-for="p in basketPriceList(it)" :key="p.key">
                      <tr class="border-b border-stone-100">
                        <td class="py-2 pr-3 font-medium" x-text="(p.state && p.state !== 'US' ? p.state + ' \\u00B7 ' : '') + p.store + ' (' + p.city + ')'"></td>
                        <td class="py-2 pr-3 text-stone-600">
                          <span x-text="p.product_title"></span>
                          <span x-show="p.same_upc" class="ml-1 rounded bg-green-50 text-green-700 px-1.5 py-0.5 text-[10px]">Exact UPC</span>
                        </td>
                        <td class="py-2 pr-3 whitespace-nowrap font-semibold">
                          <span x-text="money(p.price)"></span>
                          <span x-show="p.promo_price" class="text-xs text-emerald-700 ml-1" x-text="'(Sale ' + money(p.promo_price) + ')'"></span>
                        </td>
                        <td class="py-2 whitespace-nowrap">
                          <a x-show="p.url" :href="p.url" target="_blank" rel="noopener noreferrer"
                            class="text-[#15803D] underline hover:text-green-900">Open product &#8599;</a>
                        </td>
                      </tr>
                    </template>
                  </tbody>
                </table>
              </div>
            </details>
          </template>
        </div>
      </div>
    </template>
  </section>

  <!-- TAB 3: Past runs -->
  <section x-show="tab === 'runs'" class="space-y-3">
    <div x-show="runs.length === 0" class="bg-white rounded-2xl border border-stone-200 p-8 text-center text-stone-500">No earlier scans yet.</div>
    <template x-for="r in runs" :key="r.walk_id">
      <button @click="openRun(r.walk_id)" class="w-full text-left bg-white rounded-2xl border border-stone-200 p-4 sm:p-5 hover:border-[#15803D] transition flex flex-col sm:flex-row sm:items-center justify-between gap-2">
        <div>
          <div class="font-semibold flex items-center gap-2">
            <span x-text="r.store + (r.zip_code && r.zip_code !== '00000' ? ' \\u00B7 ZIP ' + r.zip_code : '')"></span>
            <span class="rounded-full bg-stone-100 text-stone-600 px-2 py-0.5 text-[11px]" x-text="r.scan_type === 'receipt' ? 'Receipt' : 'Video'"></span>
          </div>
          <div class="text-sm text-stone-500" x-text="when(r.timestamp)"></div>
        </div>
        <div class="flex items-center gap-3 text-sm">
          <span x-text="r.item_count + ' items'"></span>
          <span class="text-stone-400" x-text="'online +' + Number(r.avg_markup_pct || 0).toFixed(1) + '%'"></span>
          <span class="rounded-full px-2.5 py-0.5 text-xs font-semibold" :class="sourceOf(r.catalog_source).cls" x-text="sourceOf(r.catalog_source).label"></span>
        </div>
      </button>
    </template>
  </section>

  <!-- TAB 4: Price history -->
  <section x-show="tab === 'history'" class="space-y-4">
    <input x-model="hq" type="search" placeholder="Search for an item, like milk or eggs&hellip;"
      class="w-full rounded-xl border border-stone-300 bg-white px-4 py-3 focus:outline-none focus:ring-2 focus:ring-[#15803D]">
    <div x-show="historyList().length === 0" class="bg-white rounded-2xl border border-stone-200 p-8 text-center text-stone-500">
      No price history to show yet.
    </div>
    <template x-for="p in historyList()" :key="p.key">
      <details class="bg-white rounded-2xl border border-stone-200 p-4 sm:p-5">
        <summary class="cursor-pointer list-none flex flex-col sm:flex-row sm:items-center justify-between gap-2">
          <div>
            <div class="font-semibold capitalize flex flex-wrap items-center gap-2">
              <span x-text="p.name"></span>
              <span x-show="p.locations > 1 && p.spread > 0" class="rounded-full bg-amber-100 text-amber-800 px-2 py-0.5 text-xs font-medium"
                x-text="p.locations + ' locations \\u00B7 ' + money(p.spread) + ' spread'"></span>
              <span x-show="p.locations > 1 && p.spread === 0" class="rounded-full bg-green-100 text-green-800 px-2 py-0.5 text-xs font-medium"
                x-text="p.locations + ' locations \\u00B7 same price'"></span>
            </div>
            <div class="text-sm text-stone-500" x-text="p.points.length + ' recorded price' + (p.points.length === 1 ? '' : 's') + ' \\u00B7 low ' + money(p.min) + ' \\u00B7 high ' + money(p.max)"></div>
          </div>
          <svg width="120" height="30" viewBox="0 0 120 30" class="shrink-0">
            <polyline fill="none" stroke="#15803D" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" :points="spark(p.points)"></polyline>
          </svg>
        </summary>
        <div class="overflow-x-auto mt-3">
          <table class="w-full text-sm">
            <thead class="text-stone-500 border-b border-stone-200"><tr>
              <th class="text-left font-medium py-2 pr-4">Date</th><th class="text-left font-medium py-2 pr-4">Store</th>
              <th class="text-left font-medium py-2 pr-4">ZIP</th><th class="text-left font-medium py-2 pr-4">In-store</th>
              <th class="text-left font-medium py-2 pr-4">Online</th></tr></thead>
            <tbody>
              <template x-for="(pt, i) in p.points" :key="i">
                <tr class="border-b border-stone-100">
                  <td class="py-2 pr-4 whitespace-nowrap" x-text="pt.date"></td>
                  <td class="py-2 pr-4" x-text="pt.store"></td>
                  <td class="py-2 pr-4" x-text="pt.zip_code === '00000' ? '-' : pt.zip_code"></td>
                  <td class="py-2 pr-4 font-medium" x-text="money(pt.in_store_price)"></td>
                  <td class="py-2 pr-4" x-text="money(pt.online_price)"></td>
                </tr>
              </template>
            </tbody>
          </table>
        </div>
      </details>
    </template>
  </section>

  <footer class="text-center text-xs text-stone-400 pb-6">Evidence crops are stored for 60 days. Always verify shelf tags and direct product links when accuracy matters.</footer>
</div>

<!-- Evidence Lightbox Modal -->
<div x-show="lightbox.open" @click.self="lightbox.open = false" class="fixed inset-0 z-50 bg-black/70 flex items-center justify-center p-4">
  <div class="bg-white rounded-2xl max-w-2xl w-full p-4 sm:p-5 space-y-3 shadow-xl">
    <div class="flex items-center justify-between gap-2">
      <div class="font-semibold text-sm sm:text-base" x-text="lightbox.title"></div>
      <button type="button" @click="lightbox.open = false" class="text-stone-400 hover:text-stone-800 text-xl px-2">&times;</button>
    </div>
    <img :src="lightbox.src" alt="Evidence crop" class="w-full max-h-[65vh] object-contain rounded-xl bg-stone-100">
    <div class="flex items-center justify-between text-xs text-stone-500" x-show="lightbox.item">
      <span x-text="lightbox.item && lightbox.item.matched_catalog_item ? 'Online match: ' + lightbox.item.matched_catalog_item : 'No online match'"></span>
      <a x-show="lightbox.item && lightbox.item.online_url" :href="lightbox.item && lightbox.item.online_url" target="_blank" rel="noopener noreferrer"
        class="text-[#15803D] font-semibold underline">Open online product &#8599;</a>
    </div>
  </div>
</div>

<!-- Inline Edit / Human Verification Modal -->
<div x-show="editModal.open" @click.self="editModal.open = false" class="fixed inset-0 z-50 bg-black/60 flex items-center justify-center p-4">
  <div class="bg-white rounded-2xl max-w-md w-full p-5 space-y-3.5 shadow-xl">
    <div class="flex items-center justify-between">
      <h3 class="font-semibold">Verify &amp; correct item</h3>
      <button type="button" @click="editModal.open = false" class="text-stone-400 hover:text-stone-800 text-xl">&times;</button>
    </div>
    <label class="block text-sm"><span class="text-stone-600">Product name</span>
      <input x-model="editModal.name" class="mt-1 w-full rounded-xl border border-stone-300 px-3 py-2"></label>
    <div class="grid grid-cols-2 gap-3">
      <label class="block text-sm"><span class="text-stone-600">Size</span>
        <input x-model="editModal.size" class="mt-1 w-full rounded-xl border border-stone-300 px-3 py-2"></label>
      <label class="block text-sm"><span class="text-stone-600">In-store price ($)</span>
        <input x-model="editModal.price" type="number" step="0.01" class="mt-1 w-full rounded-xl border border-stone-300 px-3 py-2"></label>
    </div>
    <label class="block text-sm"><span class="text-stone-600">Reviewer note (optional)</span>
      <input x-model="editModal.note" placeholder="e.g. Confirmed shelf tag & size" class="mt-1 w-full rounded-xl border border-stone-300 px-3 py-2"></label>
    <label class="flex items-center gap-2 text-xs text-stone-600">
      <input type="checkbox" x-model="editModal.clearMatch"> Remove wrong online match
    </label>
    <div class="flex justify-end gap-2 pt-2">
      <button type="button" @click="editModal.open = false" class="px-4 py-2 rounded-xl border border-stone-300 text-sm">Cancel</button>
      <button type="button" @click="saveEdit()" class="px-4 py-2 rounded-xl bg-[#15803D] text-white text-sm font-semibold">Save &amp; Mark Verified</button>
    </div>
  </div>
</div>

<script>
function app() {
  return {
    krogerStores: ['Gerbes', 'Kroger', 'Harris Teeter', 'Dillons', 'Ralphs', 'Fred Meyer', 'King Soopers', "Fry's", "Smith's", "Mariano's", 'QFC', "Pick 'n Save", 'Metro Market', 'City Market', 'Ruler Foods'],
    otherStores: ['Aldi', 'Walmart', 'Target', 'Costco', "Sam's Club", 'Whole Foods', "Trader Joe's", 'Hy-Vee', 'Schnucks', 'Giant', 'Wegmans', 'Lidl', 'Safeway', 'Publix', 'H-E-B', 'Meijer', 'Food Lion', 'Sprouts', 'ShopRite', 'WinCo'],
    store: 'Gerbes', customStore: '', zip: '', mode: 'video', file: null, drag: false, helpOpen: false,
    uploading: false, progress: 0, uploadError: '',
    status: { stage: 'idle' },
    tab: 'latest',
    tabs: [
      { id: 'latest', label: 'Latest scan' },
      { id: 'basket', label: 'Staple basket (120)' },
      { id: 'runs', label: 'Past runs' },
      { id: 'history', label: 'Price history' }
    ],
    run: {}, latest: {}, viewingPast: false, knownWalk: null,
    runs: [], history: { products: {} }, hq: '',
    basket: { stores: [], items: [] }, bType: 'all', bCat: '', bq: '', bSort: 'spread',
    selectedStore: null, sqFilter: '', sSort: 'diff',
    q: '', sortKey: 'dollar_markup', sortDir: -1, qOpen: false,
    adminUnlocked: false, adminPin: '', verifyMsg: '',
    lightbox: { open: false, src: '', title: '', item: null },
    editModal: { open: false, idx: -1, name: '', size: '', price: '', note: '', clearMatch: false },
    cols: [
      { k: 'product_name', l: 'Item' }, { k: 'size_text', l: 'Size' },
      { k: 'in_store_price', l: 'In-store' }, { k: 'online_price', l: 'Online' },
      { k: 'dollar_markup', l: 'Savings' }, { k: 'pct_markup', l: 'Online +' }
    ],

    async init() {
      const savedPin = sessionStorage.getItem('gpf_pin');
      if (savedPin) { this.adminPin = savedPin; this.adminUnlocked = true; }
      await this.loadLatest();
      this.knownWalk = this.latest.walk_id || null;
      this.loadRuns();
      this.poll();
      setInterval(() => this.poll(), 3000);
    },

    setMode(m) { this.mode = m; this.file = null; this.uploadError = ''; },
    money(v) { return v == null ? '-' : '$' + Number(v).toFixed(2); },
    name(r) { return ((r.brand || '') + ' ' + (r.product_name || '')).trim(); },
    when(iso) { try { return new Date(iso).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }); } catch (e) { return ''; } },
    stageText(s) {
      return ({
        uploaded: 'Uploaded! Getting started\\u2026',
        slicing_frames: 'Preparing clear images\\u2026',
        analyzing_frames: 'Reading prices & saving evidence crops\\u2026',
        matching: 'Comparing with live online prices\\u2026',
        complete: 'All done! Your results are below.',
        error: 'Something went wrong. '
      })[s] || 'Working on it\\u2026';
    },
    reasonText(r) {
      return ({ blur: 'Too blurry to read', glare: 'Glare on the tag', cut_off: 'Tag was cut off', barcode_unreadable: "Barcode couldn't be read" })[r] || 'Hard to read';
    },
    sourceOf(s) {
      if (s === 'live_api') return { label: 'Live online prices', cls: 'bg-green-100 text-green-800', hint: 'Matched live online & cached for 7 days.' };
      if (s === 'zip_catalog') return { label: 'ZIP code catalog', cls: 'bg-green-100 text-green-800', hint: 'Online prices cached for your area.' };
      if (s === 'chain_catalog') return { label: 'Store-wide catalog', cls: 'bg-sky-100 text-sky-800', hint: 'Typical online prices for this store.' };
      if (s === 'mixed') return { label: 'Mixed live + sample', cls: 'bg-sky-100 text-sky-800', hint: 'Some items matched live online; others used sample prices.' };
      if (s === 'sample_fallback') return { label: 'Sample catalog', cls: 'bg-amber-100 text-amber-800', hint: 'Example prices only.' };
      return { label: 'Unknown', cls: 'bg-stone-100 text-stone-600', hint: '' };
    },
    source() { return this.sourceOf(this.run.catalog_source); },

    items() { return (this.run.items || []).map((it, idx) => Object.assign({ _idx: idx }, it)); },
    matched() { return this.items().filter(i => i.pct_markup != null && !i.flagged_wrong); },
    verifiedCount() { return this.items().filter(i => i.human_verified).length; },
    avgSavings() { const m = this.matched(); return m.length ? m.reduce((a, i) => a + (i.dollar_markup || 0), 0) / m.length : 0; },
    avgPct() { const m = this.matched(); return m.length ? m.reduce((a, i) => a + i.pct_markup, 0) / m.length : 0; },
    sq() { return this.run.scan_quality || {}; },
    uncertain() { return this.items().filter(i => String(i.confidence || '').toLowerCase() !== 'high'); },
    quality() {
      const s = this.sq();
      const tags = (s.unreadable_tags || []).length, bc = s.barcodes_unreadable || 0, low = this.uncertain().length;
      const ok = tags === 0 && bc === 0 && low === 0;
      const bits = [];
      if (tags) bits.push(tags + (tags === 1 ? ' unreadable tag' : ' unreadable tags'));
      if (bc) bits.push(bc + (bc === 1 ? ' unreadable barcode' : ' unreadable barcodes'));
      if (low) bits.push(low + (low === 1 ? ' uncertain item' : ' uncertain items'));
      const extras = [];
      if (s.dropped_blurry_frames) extras.push(s.dropped_blurry_frames + ' blurry');
      if (s.skipped_duplicate_frames) extras.push(s.skipped_duplicate_frames + ' duplicate');
      if (s.capped_frames) extras.push(s.capped_frames + ' capped for cost control');
      const frames = this.run.scan_type === 'receipt'
        ? 'Scanned from a single checkout receipt photo.'
        : (s.total_seconds
          ? 'We looked at ' + s.total_seconds + 's of video and used ' + s.kept_frames + ' clear frame' + (s.kept_frames === 1 ? '' : 's') + (extras.length ? ' (skipped ' + extras.join(', ') + ').' : '.')
          : 'Video details are not available for this scan.');
      const barcodes = (s.barcodes_read || 0) + ' barcode' + ((s.barcodes_read || 0) === 1 ? '' : 's') + ' read' + (bc ? ', ' + bc + " couldn't be read." : '.');
      return { ok: ok, label: ok ? 'All tags read clearly' : bits.join(' \\u00B7 '), frames: frames, barcodes: barcodes };
    },
    sortBy(k) { if (this.sortKey === k) this.sortDir *= -1; else { this.sortKey = k; this.sortDir = -1; } },
    rows() {
      const q = this.q.toLowerCase(), k = this.sortKey, d = this.sortDir;
      return this.items()
        .filter(i => !q || JSON.stringify(i).toLowerCase().indexOf(q) >= 0)
        .sort((a, b) => {
          const x = a[k], y = b[k];
          if (x == null && y == null) return 0;
          if (x == null) return 1;
          if (y == null) return -1;
          return (typeof x === 'number' ? x - y : String(x).localeCompare(String(y))) * d;
        });
    },

    // ---------- staple basket helpers ----------
    stateStores() { return (this.basket.stores || []).filter(s => s.common_basket_total != null); },
    nationalStores() { return (this.basket.stores || []).filter(s => s.common_basket_total == null); },
    stateBarPct(s) {
      const list = this.stateStores();
      if (!list.length) return 100;
      const hi = list[list.length - 1].common_basket_total || 1;
      const lo = list[0].common_basket_total || 0;
      if (hi <= lo) return 100;
      return Math.max(18, Math.round((s.common_basket_total / hi) * 100));
    },
    openStoreDetail(s) {
      this.selectedStore = s; this.sqFilter = '';
      window.scrollTo({ top: 0, behavior: 'smooth' });
    },
    selectedStoreRows() {
      if (!this.selectedStore) return [];
      const sk = this.selectedStore.key, q = this.sqFilter.toLowerCase().trim();
      let list = [];
      for (const it of (this.basket.items || [])) {
        const p = it.prices && it.prices[sk];
        if (!p) continue;
        if (this.bType !== 'all' && it.type !== this.bType) continue;
        if (this.bCat && it.category !== this.bCat) continue;
        if (q && (it.name + ' ' + it.category + ' ' + (p.product_title || '')).toLowerCase().indexOf(q) < 0) continue;
        const med = it.median_price;
        const diff = (med != null && p.price != null) ? Math.round((p.price - med) * 100) / 100 : null;
        const diffPct = (med && diff != null) ? (diff / med) * 100 : 0;
        list.push({ id: it.id, name: it.name, category: it.category, median_price: med, p: p, diff: diff, diffPct: diffPct });
      }
      if (this.sSort === 'diff') list.sort((a, b) => Math.abs(b.diff || 0) - Math.abs(a.diff || 0));
      else if (this.sSort === 'price') list.sort((a, b) => (a.p.price || 999) - (b.p.price || 999));
      else list.sort((a, b) => a.name.localeCompare(b.name));
      return list;
    },
    storeLabel(key) {
      const s = (this.basket.stores || []).find(x => x.key === key);
      return s ? ((s.state && s.state !== 'US' ? s.state + ' ' : '') + s.store) : (key || '');
    },
    basketCategories() {
      const set = new Set((this.basket.items || []).map(i => i.category).filter(Boolean));
      return Array.from(set).sort();
    },
    basketRows() {
      const q = this.bq.toLowerCase().trim();
      let list = (this.basket.items || []).filter(i => {
        if (this.bType !== 'all' && i.type !== this.bType) return false;
        if (this.bCat && i.category !== this.bCat) return false;
        if (q && (i.name + ' ' + i.category).toLowerCase().indexOf(q) < 0) return false;
        return true;
      });
      if (this.bSort === 'spread') list = list.slice().sort((a, b) => (b.dollar_spread || -1) - (a.dollar_spread || -1));
      else if (this.bSort === 'low') list = list.slice().sort((a, b) => (a.min_price || 999) - (b.min_price || 999));
      else list = list.slice().sort((a, b) => a.name.localeCompare(b.name));
      return list;
    },
    basketPriceList(it) {
      const pm = it.prices || {};
      return Object.keys(pm).map(k => Object.assign({ key: k }, pm[k])).sort((a, b) => (a.price || 999) - (b.price || 999));
    },

    // ---------- history ----------
    historyList() {
      const q = this.hq.toLowerCase().trim(), out = [];
      const prods = (this.history && this.history.products) || {};
      for (const key of Object.keys(prods)) {
        const p = prods[key];
        if (q && (p.product_name || key).toLowerCase().indexOf(q) < 0) continue;
        const pts = (p.points || []).slice().sort((a, b) => String(a.date).localeCompare(String(b.date)));
        const vals = pts.map(x => x.in_store_price).filter(v => v != null);
        const locs = new Set(pts.map(x => (x.store || '') + '|' + (x.zip_code || ''))).size;
        const min = vals.length ? Math.min.apply(null, vals) : null;
        const max = vals.length ? Math.max.apply(null, vals) : null;
        out.push({ key: key, name: p.product_name || key, points: pts, min: min, max: max, locations: locs, spread: (min != null && max != null) ? Math.round((max - min) * 100) / 100 : 0 });
      }
      out.sort((a, b) => b.points.length - a.points.length || a.name.localeCompare(b.name));
      return out.slice(0, 50);
    },
    spark(points) {
      const v = points.map(p => p.in_store_price).filter(x => x != null);
      if (!v.length) return '';
      const lo = Math.min.apply(null, v), hi = Math.max.apply(null, v), span = (hi - lo) || 1;
      if (v.length === 1) return '56,15 64,15';
      return v.map((y, i) => (4 + i * (112 / (v.length - 1))).toFixed(1) + ',' + (26 - (y - lo) / span * 22).toFixed(1)).join(' ');
    },

    // ---------- lightbox & human verification ----------
    openLightbox(src, title, item) { this.lightbox = { open: true, src: src, title: title, item: item }; },
    async toggleAdmin() {
      if (this.adminUnlocked) {
        this.adminUnlocked = false; this.adminPin = ''; sessionStorage.removeItem('gpf_pin'); this.verifyMsg = '';
        return;
      }
      const pin = prompt('Enter Admin PIN to unlock human verification & edits:');
      if (!pin) return;
      const r = await fetch('/grocery/verify', {
        method: 'POST', headers: { 'content-type': 'application/json', 'x-admin-pin': pin.trim() },
        body: JSON.stringify({ action: 'check_pin' })
      });
      if (r.ok) {
        this.adminPin = pin.trim(); this.adminUnlocked = true;
        sessionStorage.setItem('gpf_pin', this.adminPin);
        this.verifyMsg = 'Verification mode unlocked for this tab.';
      } else {
        const d = await r.json().catch(() => ({}));
        alert(d.error || 'Incorrect PIN.');
      }
    },
    async quickVerify(idx, verified, flagged) {
      const r = await fetch('/grocery/verify', {
        method: 'POST', headers: { 'content-type': 'application/json', 'x-admin-pin': this.adminPin },
        body: JSON.stringify({ walk_id: this.run.walk_id, item_index: idx, verified: verified, flagged_wrong: flagged })
      });
      const d = await r.json().catch(() => ({}));
      if (r.ok && d.item) {
        this.run.items[idx] = d.item;
        this.run.summary = d.summary;
        this.verifyMsg = verified ? 'Saved: marked human-verified.' : (flagged ? 'Saved: flagged as wrong.' : 'Updated.');
      } else { alert(d.error || 'Could not save verification.'); }
    },
    openEdit(r) {
      this.editModal = { open: true, idx: r._idx, name: this.name(r), size: r.size_text || '', price: r.in_store_price || '', note: r.human_note || '', clearMatch: false };
    },
    async saveEdit() {
      const m = this.editModal;
      const r = await fetch('/grocery/verify', {
        method: 'POST', headers: { 'content-type': 'application/json', 'x-admin-pin': this.adminPin },
        body: JSON.stringify({
          walk_id: this.run.walk_id, item_index: m.idx, verified: true, flagged_wrong: false,
          corrected_name: m.name, corrected_size: m.size, corrected_price: m.price,
          note: m.note, clear_online_match: m.clearMatch
        })
      });
      const d = await r.json().catch(() => ({}));
      if (r.ok && d.item) {
        this.run.items[m.idx] = d.item;
        this.run.summary = d.summary;
        this.editModal.open = false;
        this.verifyMsg = 'Saved correction & marked Verified.';
      } else { alert(d.error || 'Could not save edit.'); }
    },

    // ---------- data loading ----------
    async getJson(url) { try { const r = await fetch(url, { cache: 'no-store' }); return r.ok ? await r.json() : null; } catch (e) { return null; } },
    async loadLatest() {
      const p = await this.getJson('/grocery/prices');
      if (p && !p.error) { this.latest = p; if (!this.viewingPast) this.run = p; }
    },
    async loadRuns() { const r = await this.getJson('/grocery/runs'); if (r && r.runs) this.runs = r.runs; },
    async loadHistory() { const h = await this.getJson('/grocery/history'); if (h && h.products) this.history = h; },
    async loadBasket() { const b = await this.getJson('/grocery/basket'); if (b && b.items) this.basket = b; },
    async openTab(id) {
      this.tab = id;
      if (id === 'runs') this.loadRuns();
      if (id === 'history') this.loadHistory();
      if (id === 'basket') this.loadBasket();
    },
    async openRun(id) {
      const r = await this.getJson('/grocery/runs/' + encodeURIComponent(id));
      if (!r) return;
      this.run = r; this.viewingPast = true; this.tab = 'latest'; this.q = '';
      window.scrollTo({ top: 0, behavior: 'smooth' });
    },
    backToLatest() { this.viewingPast = false; this.run = this.latest; },
    async poll() {
      const s = await this.getJson('/grocery/status');
      if (!s || s.error) return;
      this.status = s;
      if (s.stage === 'complete' && s.walk_id && s.walk_id !== this.knownWalk) {
        this.knownWalk = s.walk_id;
        this.viewingPast = false;
        await this.loadLatest();
        this.run = this.latest;
        this.loadRuns();
        if (this.tab === 'history') this.loadHistory();
      }
    },

    // ---------- upload ----------
    setFile(f) { if (f) { this.file = f; this.uploadError = ''; } },
    onDrop(e) {
      this.drag = false;
      const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      const ok = f && (this.mode === 'receipt' ? /^image\\//.test(f.type || '') : /^video\\//.test(f.type || ''));
      if (ok) this.setFile(f); else this.uploadError = this.mode === 'receipt' ? 'Please choose a photo (JPG/PNG).' : 'Please choose a video file.';
    },
    async upload() {
      if (!this.file) return;
      const zip = (this.zip || '').trim();
      if (zip && !/^\\d{5}$/.test(zip)) { this.uploadError = 'Please enter a 5-digit ZIP code (or leave it blank).'; return; }
      this.uploadError = ''; this.uploading = true; this.progress = 0;
      try {
        const mExt = (this.file.name || '').match(/\\.([a-z0-9]+)$/i);
        const ext = mExt ? mExt[1].toLowerCase() : (this.mode === 'receipt' ? 'jpg' : 'mp4');
        const chosenStore = this.store === 'Other' ? ((this.customStore || '').trim() || 'Other') : this.store;
        const res = await fetch('/grocery/presign', {
          method: 'POST', headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ store: chosenStore, zip: zip || '00000', ext: ext, mode: this.mode })
        });
        const pre = await res.json();
        if (!pre.url) throw new Error(pre.error || 'Could not start the upload');
        await new Promise((resolve, reject) => {
          const xhr = new XMLHttpRequest();
          xhr.open('PUT', pre.url);
          xhr.upload.onprogress = (e) => { if (e.lengthComputable) this.progress = Math.round(e.loaded / e.total * 100); };
          xhr.onload = () => {
            if (xhr.status >= 200 && xhr.status < 300) return resolve();
            const t = xhr.responseText || '';
            const code = (t.match(/<Code>([^<]*)<\\/Code>/) || [])[1];
            const msg = (t.match(/<Message>([^<]*)<\\/Message>/) || [])[1];
            reject(new Error('S3 upload failed: ' + xhr.status + (code ? ' ' + code : '') + (msg ? ' - ' + msg : '')));
          };
          xhr.onerror = () => reject(new Error('Network error during upload'));
          xhr.send(this.file);
        });
        this.status = { stage: 'uploaded', walk_id: pre.walk_id };
        this.file = null;
      } catch (e) { this.uploadError = String(e.message || e); }
      this.uploading = false;
    }
  };
}
</script>
</body>
</html>`;
