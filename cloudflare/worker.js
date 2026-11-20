// Cloudflare Worker: grocery price forensics app (route: discourseforensics.com/grocery*)
// Secrets/vars required: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, S3_BUCKET  (optional: AWS_REGION, default us-east-1)

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
  const iso = now.toISOString().replace(/[:-]|\.\d{3}/g, ""); // YYYYMMDDTHHMMSSZ
  return { amzDate: iso, dateStamp: iso.slice(0, 8) };
}

// Secrets piped from Windows PowerShell often carry a trailing \r\n; trim everything before signing.
function cfg(env) {
  return {
    accessKeyId: String(env.AWS_ACCESS_KEY_ID || "").trim(),
    secretAccessKey: String(env.AWS_SECRET_ACCESS_KEY || "").trim(),
    bucket: String(env.S3_BUCKET || "").trim(),
    region: String(env.AWS_REGION || "us-east-1").trim(),
  };
}

function s3Host(env) {
  const { bucket, region } = cfg(env);
  return { region, host: `${bucket}.s3.${region}.amazonaws.com` };
}

// Presigned URL (query-string SigV4) for a PUT of a given object key.
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
  // Keys sorted alphabetically; each key/value RFC-3986 encoded exactly once.
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

// Header-signed GET of an S3 object.
async function s3Get(env, key) {
  const { accessKeyId, secretAccessKey } = cfg(env);
  const { region, host } = s3Host(env);
  const { amzDate, dateStamp } = amzDates();
  const scope = `${dateStamp}/${region}/s3/aws4_request`;
  const payloadHash = await sha256Hex("");
  const canonicalUri = "/" + encodeKey(key);
  const signedHeaders = "host;x-amz-content-sha256;x-amz-date";
  const canonicalHeaders = `host:${host}\nx-amz-content-sha256:${payloadHash}\nx-amz-date:${amzDate}\n`;
  const canonicalRequest = ["GET", canonicalUri, "", canonicalHeaders, signedHeaders, payloadHash].join("\n");
  const stringToSign = ["AWS4-HMAC-SHA256", amzDate, scope, await sha256Hex(canonicalRequest)].join("\n");
  const sig = toHex(await hmac(await signingKey(secretAccessKey, dateStamp, region), stringToSign));
  const auth = `AWS4-HMAC-SHA256 Credential=${accessKeyId}/${scope}, SignedHeaders=${signedHeaders}, Signature=${sig}`;
  return fetch(`https://${host}${canonicalUri}`, {
    headers: { "x-amz-content-sha256": payloadHash, "x-amz-date": amzDate, Authorization: auth },
  });
}

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });

// S3 answers 403 (not 404) for missing keys when the caller lacks ListBucket, so treat both as "not found".
async function proxyJson(env, key, fallback) {
  const r = await s3Get(env, key);
  if (r.status === 404 || r.status === 403) {
    return fallback === null ? json({ error: "not_found" }, 404) : json(fallback);
  }
  if (!r.ok) return json({ error: "s3_error", status: r.status }, 502);
  return new Response(r.body, { headers: { "content-type": "application/json", "cache-control": "no-store" } });
}

// "Giant Eagle!" -> "giant-eagle"; never contains "_" so the upload key stays parseable.
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

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";

    if (request.method === "OPTIONS") {
      return new Response(null, {
        headers: {
          "access-control-allow-origin": "*",
          "access-control-allow-methods": "GET,POST,OPTIONS",
          "access-control-allow-headers": "content-type",
        },
      });
    }

    try {
      if (path === "/grocery" && request.method === "GET") {
        return new Response(PAGE, { headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" } });
      }
      if (path === "/grocery/presign" && request.method === "POST") {
        let ext = "mp4";
        let body = {};
        try {
          body = (await request.json()) || {};
        } catch (_) {}
        if (/^(mp4|mov)$/i.test(body.ext || "")) ext = body.ext.toLowerCase();
        const store = slugify(body.store || "other");
        const zip = cleanZip(body.zip);
        const walkId = `walk_${Date.now()}_${store}_${zip}`;
        const key = `raw-videos/${walkId}.${ext}`;
        return json({ url: await presignPut(env, key), key, walk_id: walkId, store, zip });
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
      if (path === "/grocery/history" && request.method === "GET") {
        return proxyJson(env, "public/price_history.json", { products: {} });
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

  <header class="text-center space-y-1">
    <div class="text-4xl">&#128722;</div>
    <h1 class="text-2xl sm:text-3xl font-bold text-[#15803D]">Grocery Price Forensics</h1>
    <p class="text-stone-500 text-sm sm:text-base">Film a quick walk down the aisle and see how in-store prices compare with online prices.</p>
  </header>

  <!-- Upload -->
  <section class="bg-white rounded-2xl border border-stone-200 shadow-sm p-5 sm:p-6 space-y-4">
    <h2 class="text-lg font-semibold">Check a new store visit</h2>
    <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
      <label class="block">
        <span class="text-sm text-stone-600">Which store?</span>
        <select x-model="store" class="mt-1 w-full rounded-xl border border-stone-300 bg-white px-3 py-2.5 focus:outline-none focus:ring-2 focus:ring-[#15803D]">
          <template x-for="s in stores" :key="s"><option :value="s" x-text="s"></option></template>
        </select>
      </label>
      <label class="block">
        <span class="text-sm text-stone-600">ZIP code</span>
        <input x-model="zip" inputmode="numeric" maxlength="5" placeholder="e.g. 20176"
          class="mt-1 w-full rounded-xl border border-stone-300 px-3 py-2.5 focus:outline-none focus:ring-2 focus:ring-[#15803D]">
      </label>
    </div>

    <div @dragover.prevent="drag = true" @dragleave.prevent="drag = false" @drop.prevent="onDrop($event)"
      :class="drag ? 'border-[#15803D] bg-green-50' : 'border-stone-300 bg-stone-50'"
      class="rounded-2xl border-2 border-dashed p-6 text-center space-y-3 transition-colors">
      <div class="text-3xl">&#127909;</div>
      <p class="text-stone-600 text-sm" x-text="file ? 'Ready: ' + file.name : 'Drag a video here, or pick one below'"></p>
      <div class="flex flex-col sm:flex-row gap-3 justify-center">
        <button type="button" @click="$refs.pick.click()"
          class="px-5 py-3 rounded-xl bg-white border border-stone-300 font-medium hover:bg-stone-100">Choose a video</button>
        <button type="button" @click="$refs.cam.click()"
          class="px-5 py-3 rounded-xl bg-white border border-stone-300 font-medium hover:bg-stone-100">Use phone camera</button>
      </div>
      <input x-ref="pick" type="file" accept="video/*" class="hidden" @change="setFile($event.target.files[0])">
      <input x-ref="cam" type="file" accept="video/*" capture="environment" class="hidden" @change="setFile($event.target.files[0])">
    </div>

    <button @click="upload()" :disabled="!file || uploading"
      class="w-full sm:w-auto px-8 py-3.5 rounded-xl bg-[#15803D] text-white font-semibold text-base shadow-sm hover:bg-green-800 disabled:opacity-40 disabled:cursor-not-allowed">
      <span x-text="uploading ? 'Uploading ' + progress + '%' : 'Upload and compare prices'"></span>
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
  <nav class="flex gap-2 bg-stone-100 rounded-2xl p-1.5">
    <template x-for="t in tabs" :key="t.id">
      <button @click="openTab(t.id)" x-text="t.label"
        :class="tab === t.id ? 'bg-white shadow-sm text-[#15803D]' : 'text-stone-500 hover:text-stone-700'"
        class="flex-1 rounded-xl py-2.5 text-sm sm:text-base font-semibold transition"></button>
    </template>
  </nav>

  <!-- Latest scan -->
  <section x-show="tab === 'latest'" class="space-y-5">
    <div x-show="viewingPast" class="rounded-xl bg-amber-50 border border-amber-200 px-4 py-3 text-sm flex items-center justify-between gap-3">
      <span>Viewing an earlier scan from <b x-text="when(run.timestamp || run.generated_at)"></b>.</span>
      <button @click="backToLatest()" class="font-semibold text-[#15803D] underline">Back to latest</button>
    </div>

    <div x-show="!run.walk_id" class="bg-white rounded-2xl border border-stone-200 p-8 text-center text-stone-500">
      No scans yet. Upload a video above to get started.
    </div>

    <template x-if="run.walk_id">
      <div class="space-y-5">
        <div class="grid grid-cols-1 sm:grid-cols-3 gap-4">
          <div class="bg-white rounded-2xl border border-stone-200 p-5">
            <div class="text-sm text-stone-500">Items found</div>
            <div class="text-4xl font-bold mt-1" x-text="items().length"></div>
            <div class="text-xs text-stone-400 mt-1" x-text="(run.store || 'Store') + (run.zip_code && run.zip_code !== '00000' ? ' \\u00B7 ZIP ' + run.zip_code : '')"></div>
          </div>
          <div class="bg-white rounded-2xl border border-stone-200 p-5">
            <div class="text-sm text-stone-500">Average savings in-store</div>
            <div class="text-4xl font-bold mt-1 text-[#15803D]" x-text="'$' + avgSavings().toFixed(2)"></div>
            <div class="text-xs text-stone-400 mt-1" x-text="'per item \\u00B7 online runs ' + avgPct().toFixed(1) + '% higher'"></div>
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
            <p x-show="quality().ok" class="text-stone-500">Nothing needs a second look. For the best results, hold the camera steady for a second on each shelf tag.</p>
          </div>
        </div>

        <!-- Table -->
        <div class="bg-white rounded-2xl border border-stone-200 p-5 space-y-3">
          <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
            <h2 class="text-lg font-semibold">In-store vs. online prices</h2>
            <input x-model="q" type="search" placeholder="Search items&hellip;"
              class="w-full sm:w-64 rounded-xl border border-stone-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[#15803D]">
          </div>
          <div class="overflow-x-auto">
            <table class="w-full text-sm">
              <thead class="text-stone-500 border-b border-stone-200">
                <tr>
                  <template x-for="c in cols" :key="c.k">
                    <th class="text-left font-medium py-2 pr-4 cursor-pointer select-none whitespace-nowrap hover:text-stone-800" @click="sortBy(c.k)">
                      <span x-text="c.l"></span><span x-show="sortKey === c.k" x-text="sortDir > 0 ? ' \\u25B2' : ' \\u25BC'"></span>
                    </th>
                  </template>
                </tr>
              </thead>
              <tbody>
                <template x-for="(r, i) in rows()" :key="i">
                  <tr class="border-b border-stone-100 hover:bg-stone-50">
                    <td class="py-3 pr-4">
                      <div class="font-medium" x-text="name(r)"></div>
                      <div class="text-xs text-stone-400" x-show="r.upc_barcode" x-text="'Barcode ' + r.upc_barcode"></div>
                      <div class="text-xs text-stone-400" x-show="!r.matched_catalog_item">No online match found</div>
                    </td>
                    <td class="py-3 pr-4 whitespace-nowrap" x-text="r.size_text"></td>
                    <td class="py-3 pr-4 whitespace-nowrap" x-text="money(r.in_store_price)"></td>
                    <td class="py-3 pr-4 whitespace-nowrap" x-text="money(r.online_price)"></td>
                    <td class="py-3 pr-4 whitespace-nowrap font-semibold text-[#15803D]" x-text="r.dollar_markup == null ? '-' : money(r.dollar_markup)"></td>
                    <td class="py-3 pr-4 whitespace-nowrap" x-text="r.pct_markup == null ? '-' : r.pct_markup.toFixed(1) + '%'"></td>
                  </tr>
                </template>
                <tr x-show="rows().length === 0"><td colspan="6" class="py-8 text-center text-stone-400">Nothing to show.</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </template>
  </section>

  <!-- Past runs -->
  <section x-show="tab === 'runs'" class="space-y-3">
    <div x-show="runs.length === 0" class="bg-white rounded-2xl border border-stone-200 p-8 text-center text-stone-500">No earlier scans yet.</div>
    <template x-for="r in runs" :key="r.walk_id">
      <button @click="openRun(r.walk_id)" class="w-full text-left bg-white rounded-2xl border border-stone-200 p-4 sm:p-5 hover:border-[#15803D] transition flex flex-col sm:flex-row sm:items-center justify-between gap-2">
        <div>
          <div class="font-semibold" x-text="r.store + (r.zip_code && r.zip_code !== '00000' ? ' \\u00B7 ZIP ' + r.zip_code : '')"></div>
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

  <!-- Price history -->
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
            <div class="font-semibold capitalize" x-text="p.name"></div>
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

  <footer class="text-center text-xs text-stone-400 pb-6">Prices are read from your video and may contain mistakes. Check the shelf tag when it matters.</footer>
</div>

<script>
function app() {
  return {
    stores: ['Aldi', 'Giant', 'Wegmans', 'Schnucks', 'Gerbes', 'Lidl', 'Other'],
    store: 'Aldi', zip: '', file: null, drag: false,
    uploading: false, progress: 0, uploadError: '',
    status: { stage: 'idle' },
    tab: 'latest',
    tabs: [{ id: 'latest', label: 'Latest scan' }, { id: 'runs', label: 'Past runs' }, { id: 'history', label: 'Price history' }],
    run: {}, latest: {}, viewingPast: false, knownWalk: null,
    runs: [], history: { products: {} }, hq: '',
    q: '', sortKey: 'dollar_markup', sortDir: -1, qOpen: false,
    cols: [
      { k: 'product_name', l: 'Item' }, { k: 'size_text', l: 'Size' },
      { k: 'in_store_price', l: 'In-store price' }, { k: 'online_price', l: 'Online price' },
      { k: 'dollar_markup', l: 'You save in-store' }, { k: 'pct_markup', l: 'Online is higher by' }
    ],

    async init() {
      await this.loadLatest();
      this.knownWalk = this.latest.walk_id || null;
      this.loadRuns();
      this.poll();
      setInterval(() => this.poll(), 3000);
    },

    // ---------- formatting ----------
    money(v) { return v == null ? '-' : '$' + Number(v).toFixed(2); },
    name(r) { return ((r.brand || '') + ' ' + (r.product_name || '')).trim(); },
    when(iso) { try { return new Date(iso).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }); } catch (e) { return ''; } },
    stageText(s) {
      return ({
        uploaded: 'Video uploaded. Getting started\\u2026',
        slicing_frames: 'Picking the clearest moments from your video\\u2026',
        analyzing_frames: 'Reading the price tags\\u2026',
        matching: 'Comparing with online prices\\u2026',
        complete: 'All done! Your results are below.',
        error: 'Something went wrong. '
      })[s] || 'Working on it\\u2026';
    },
    reasonText(r) {
      return ({ blur: 'Too blurry to read', glare: 'Glare on the tag', cut_off: 'Tag was cut off', barcode_unreadable: "Barcode couldn't be read" })[r] || 'Hard to read';
    },
    sourceOf(s) {
      if (s === 'zip_catalog') return { label: 'ZIP code catalog', cls: 'bg-green-100 text-green-800', hint: 'Online prices for your area.' };
      if (s === 'chain_catalog') return { label: 'Store-wide catalog', cls: 'bg-sky-100 text-sky-800', hint: 'Typical online prices for this store, not specific to your ZIP.' };
      if (s === 'sample_fallback') return { label: 'Sample catalog', cls: 'bg-amber-100 text-amber-800', hint: 'Example prices only \\u2014 not live online prices.' };
      return { label: 'Unknown', cls: 'bg-stone-100 text-stone-600', hint: '' };
    },
    source() { return this.sourceOf(this.run.catalog_source); },

    // ---------- derived data ----------
    items() { return this.run.items || []; },
    matched() { return this.items().filter(i => i.pct_markup != null); },
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
      const frames = s.total_seconds
        ? 'We looked at ' + s.total_seconds + ' second' + (s.total_seconds === 1 ? '' : 's') + ' of video and used ' + s.kept_frames + ' clear frame' + (s.kept_frames === 1 ? '' : 's') + (s.dropped_blurry_frames ? ' (' + s.dropped_blurry_frames + ' too blurry to use).' : '.')
        : 'Video details are not available for this scan.';
      const barcodes = (s.barcodes_read || 0) + ' barcode' + ((s.barcodes_read || 0) === 1 ? '' : 's') + ' read' + (bc ? ', ' + bc + " couldn't be read." : '.');
      return { ok: ok, label: ok ? 'All tags read clearly' : bits.join(' \\u00B7 '), frames: frames, barcodes: barcodes };
    },
    sortBy(k) { if (this.sortKey === k) this.sortDir *= -1; else { this.sortKey = k; this.sortDir = -1; } },
    rows() {
      const q = this.q.toLowerCase(), k = this.sortKey, d = this.sortDir;
      return this.items()
        .filter(i => !q || JSON.stringify(i).toLowerCase().indexOf(q) >= 0)
        .slice()
        .sort((a, b) => {
          const x = a[k], y = b[k];
          if (x == null && y == null) return 0;
          if (x == null) return 1;
          if (y == null) return -1;
          return (typeof x === 'number' ? x - y : String(x).localeCompare(String(y))) * d;
        });
    },
    historyList() {
      const q = this.hq.toLowerCase().trim(), out = [];
      const prods = (this.history && this.history.products) || {};
      for (const key of Object.keys(prods)) {
        const p = prods[key];
        if (q && (p.product_name || key).toLowerCase().indexOf(q) < 0) continue;
        const pts = (p.points || []).slice().sort((a, b) => String(a.date).localeCompare(String(b.date)));
        const vals = pts.map(x => x.in_store_price).filter(v => v != null);
        out.push({ key: key, name: p.product_name || key, points: pts, min: vals.length ? Math.min.apply(null, vals) : null, max: vals.length ? Math.max.apply(null, vals) : null });
      }
      out.sort((a, b) => b.points.length - a.points.length || a.name.localeCompare(b.name));
      return out.slice(0, 40);
    },
    spark(points) {
      const v = points.map(p => p.in_store_price).filter(x => x != null);
      if (!v.length) return '';
      const lo = Math.min.apply(null, v), hi = Math.max.apply(null, v), span = (hi - lo) || 1;
      if (v.length === 1) return '56,15 64,15';
      return v.map((y, i) => (4 + i * (112 / (v.length - 1))).toFixed(1) + ',' + (26 - (y - lo) / span * 22).toFixed(1)).join(' ');
    },

    // ---------- data loading ----------
    async getJson(url) { try { const r = await fetch(url, { cache: 'no-store' }); return r.ok ? await r.json() : null; } catch (e) { return null; } },
    async loadLatest() {
      const p = await this.getJson('/grocery/prices');
      if (p && !p.error) { this.latest = p; if (!this.viewingPast) this.run = p; }
    },
    async loadRuns() { const r = await this.getJson('/grocery/runs'); if (r && r.runs) this.runs = r.runs; },
    async loadHistory() { const h = await this.getJson('/grocery/history'); if (h && h.products) this.history = h; },
    async openTab(id) {
      this.tab = id;
      if (id === 'runs') this.loadRuns();
      if (id === 'history') this.loadHistory();
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
      if (f && /^video\\//.test(f.type || '')) this.setFile(f); else this.uploadError = 'Please choose a video file.';
    },
    async upload() {
      if (!this.file) return;
      const zip = (this.zip || '').trim();
      if (zip && !/^\\d{5}$/.test(zip)) { this.uploadError = 'Please enter a 5-digit ZIP code (or leave it blank).'; return; }
      this.uploadError = ''; this.uploading = true; this.progress = 0;
      try {
        const ext = /\\.mov$/i.test(this.file.name) ? 'mov' : 'mp4';
        const res = await fetch('/grocery/presign', {
          method: 'POST', headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ store: this.store, zip: zip || '00000', ext: ext })
        });
        const pre = await res.json();
        if (!pre.url) throw new Error(pre.error || 'Could not start the upload');
        await new Promise((resolve, reject) => {
          const xhr = new XMLHttpRequest();
          xhr.open('PUT', pre.url);
          // No custom headers: any x-amz-* header here would be unsigned and rejected by S3.
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
