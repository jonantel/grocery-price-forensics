// Cloudflare Worker: grocery price forensics dashboard (route: discourseforensics.com/grocery*)
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

function s3Host(env) {
  const region = env.AWS_REGION || "us-east-1";
  return { region, host: `${env.S3_BUCKET}.s3.${region}.amazonaws.com` };
}

// Presigned URL (query-string SigV4) for a PUT of a given object key.
async function presignPut(env, key, expires = 900) {
  const { region, host } = s3Host(env);
  const { amzDate, dateStamp } = amzDates();
  const scope = `${dateStamp}/${region}/s3/aws4_request`;
  const params = {
    "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
    "X-Amz-Credential": `${env.AWS_ACCESS_KEY_ID}/${scope}`,
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
  const sig = toHex(await hmac(await signingKey(env.AWS_SECRET_ACCESS_KEY, dateStamp, region), stringToSign));
  return `https://${host}${canonicalUri}?${canonicalQuery}&X-Amz-Signature=${sig}`;
}

// Header-signed GET of an S3 object.
async function s3Get(env, key) {
  const { region, host } = s3Host(env);
  const { amzDate, dateStamp } = amzDates();
  const scope = `${dateStamp}/${region}/s3/aws4_request`;
  const payloadHash = await sha256Hex("");
  const canonicalUri = "/" + encodeKey(key);
  const signedHeaders = "host;x-amz-content-sha256;x-amz-date";
  const canonicalHeaders = `host:${host}\nx-amz-content-sha256:${payloadHash}\nx-amz-date:${amzDate}\n`;
  const canonicalRequest = ["GET", canonicalUri, "", canonicalHeaders, signedHeaders, payloadHash].join("\n");
  const stringToSign = ["AWS4-HMAC-SHA256", amzDate, scope, await sha256Hex(canonicalRequest)].join("\n");
  const sig = toHex(await hmac(await signingKey(env.AWS_SECRET_ACCESS_KEY, dateStamp, region), stringToSign));
  const auth = `AWS4-HMAC-SHA256 Credential=${env.AWS_ACCESS_KEY_ID}/${scope}, SignedHeaders=${signedHeaders}, Signature=${sig}`;
  return fetch(`https://${host}${canonicalUri}`, {
    headers: { "x-amz-content-sha256": payloadHash, "x-amz-date": amzDate, Authorization: auth },
  });
}

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });

async function proxyJson(env, key, fallback) {
  const r = await s3Get(env, key);
  if (r.status === 404 || r.status === 403 && fallback) return json(fallback);
  if (!r.ok) return json({ error: "s3_error", status: r.status }, 502);
  return new Response(r.body, { headers: { "content-type": "application/json", "cache-control": "no-store" } });
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
        try {
          const body = await request.json();
          if (body && /^(mp4|mov)$/i.test(body.ext || "")) ext = body.ext.toLowerCase();
        } catch (_) {}
        const key = `raw-videos/walk_${Date.now()}.${ext}`;
        return json({ url: await presignPut(env, key), key, walk_id: key.split("/")[1].replace(/\.[^.]+$/, "") });
      }
      if (path === "/grocery/status" && request.method === "GET") {
        return proxyJson(env, "public/status.json", { stage: "idle", message: "Waiting for upload." });
      }
      if (path === "/grocery/prices" && request.method === "GET") {
        return proxyJson(env, "public/latest_prices.json", { summary: {}, items: [] });
      }
      return json({ error: "not_found" }, 404);
    } catch (e) {
      return json({ error: String(e && e.message || e) }, 500);
    }
  },
};

const PAGE = `<!doctype html>
<html lang="en" class="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Grocery Price Forensics</title>
<script src="https://cdn.tailwindcss.com"></script>
<script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js"></script>
<style>body{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}</style>
</head>
<body class="bg-zinc-950 text-zinc-200 min-h-screen" x-data="app()" x-init="init()">
<div class="max-w-6xl mx-auto p-4 sm:p-8 space-y-6">
  <header class="flex items-center justify-between border-b border-zinc-800 pb-4">
    <div>
      <h1 class="text-xl sm:text-2xl font-bold tracking-tight text-emerald-400">GROCERY PRICE FORENSICS</h1>
      <p class="text-xs text-zinc-500">In-store shelf price vs. Instacart markup &middot; evidence workbench</p>
    </div>
    <span class="text-xs px-2 py-1 rounded border border-zinc-700 text-zinc-400" x-text="status.walk_id || 'no active walk'"></span>
  </header>

  <section class="rounded-lg border border-zinc-800 bg-zinc-900 p-4 space-y-3">
    <h2 class="text-sm font-semibold text-zinc-300 uppercase tracking-wider">1 &middot; Upload store walk video</h2>
    <div class="flex flex-col sm:flex-row gap-3 sm:items-center">
      <input type="file" accept="video/mp4,video/quicktime,video/*" @change="file = $event.target.files[0]"
        class="text-sm file:mr-3 file:py-2 file:px-4 file:rounded file:border-0 file:bg-emerald-600 file:text-white hover:file:bg-emerald-500 file:cursor-pointer">
      <button @click="upload()" :disabled="!file || uploading"
        class="px-4 py-2 rounded bg-emerald-600 text-white text-sm disabled:opacity-40 hover:bg-emerald-500">
        <span x-text="uploading ? 'Uploading...' : 'Upload & analyze'"></span>
      </button>
    </div>
    <div x-show="uploading || progress > 0" class="w-full bg-zinc-800 rounded h-2 overflow-hidden">
      <div class="bg-emerald-500 h-2 transition-all" :style="'width:' + progress + '%'"></div>
    </div>
    <p x-show="uploadError" class="text-sm text-red-400" x-text="uploadError"></p>
  </section>

  <section class="rounded-lg border border-zinc-800 bg-zinc-900 p-4">
    <h2 class="text-sm font-semibold text-zinc-300 uppercase tracking-wider mb-2">2 &middot; Pipeline status</h2>
    <div class="flex items-center gap-3 text-sm">
      <span class="inline-block w-2 h-2 rounded-full"
        :class="status.stage === 'complete' ? 'bg-emerald-400' : status.stage === 'error' ? 'bg-red-500' : status.stage === 'idle' ? 'bg-zinc-600' : 'bg-amber-400 animate-pulse'"></span>
      <span class="uppercase text-zinc-400" x-text="status.stage || 'idle'"></span>
      <span class="text-zinc-300" x-text="status.message"></span>
    </div>
  </section>

  <section class="grid grid-cols-1 sm:grid-cols-3 gap-4">
    <div class="rounded-lg border border-zinc-800 bg-zinc-900 p-4">
      <div class="text-xs text-zinc-500 uppercase">Total items</div>
      <div class="text-3xl font-bold text-zinc-100" x-text="items.length"></div>
    </div>
    <div class="rounded-lg border border-zinc-800 bg-zinc-900 p-4">
      <div class="text-xs text-zinc-500 uppercase">Avg Instacart markup</div>
      <div class="text-3xl font-bold text-red-400" x-text="avgMarkup().toFixed(1) + '%'"></div>
    </div>
    <div class="rounded-lg border border-zinc-800 bg-zinc-900 p-4">
      <div class="text-xs text-zinc-500 uppercase">Total basket savings (in-store)</div>
      <div class="text-3xl font-bold text-emerald-400" x-text="'$' + totalSavings().toFixed(2)"></div>
    </div>
  </section>

  <section class="rounded-lg border border-zinc-800 bg-zinc-900 p-4 space-y-3">
    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
      <h2 class="text-sm font-semibold text-zinc-300 uppercase tracking-wider">3 &middot; In-store vs. Instacart</h2>
      <input x-model="q" type="search" placeholder="Search items..."
        class="bg-zinc-950 border border-zinc-700 rounded px-3 py-1.5 text-sm w-full sm:w-64 focus:outline-none focus:border-emerald-500">
    </div>
    <div class="overflow-x-auto">
      <table class="w-full text-sm">
        <thead class="text-xs uppercase text-zinc-500 border-b border-zinc-800">
          <tr>
            <template x-for="c in cols" :key="c.k">
              <th class="text-left py-2 pr-4 cursor-pointer select-none hover:text-zinc-300" @click="sortBy(c.k)">
                <span x-text="c.l"></span><span x-show="sortKey === c.k" x-text="sortDir > 0 ? ' \\u25B2' : ' \\u25BC'"></span>
              </th>
            </template>
          </tr>
        </thead>
        <tbody>
          <template x-for="r in rows()" :key="r.brand + r.product_name + r.size_text">
            <tr class="border-b border-zinc-800/60 hover:bg-zinc-800/40">
              <td class="py-2 pr-4"><div class="text-zinc-100" x-text="(r.brand || '') + ' ' + (r.product_name || '')"></div>
                <div class="text-xs text-zinc-500" x-text="r.matched_catalog_item ? 'matched: ' + r.matched_catalog_item : 'no catalog match'"></div></td>
              <td class="py-2 pr-4" x-text="r.size_text"></td>
              <td class="py-2 pr-4" x-text="money(r.in_store_price)"></td>
              <td class="py-2 pr-4" x-text="money(r.online_price)"></td>
              <td class="py-2 pr-4 text-red-300" x-text="r.dollar_markup == null ? '-' : '+' + money(r.dollar_markup)"></td>
              <td class="py-2 pr-4 font-semibold" :class="(r.pct_markup || 0) > 15 ? 'text-red-400' : 'text-amber-300'" x-text="r.pct_markup == null ? '-' : r.pct_markup.toFixed(1) + '%'"></td>
              <td class="py-2 pr-4 text-xs" :class="r.confidence === 'high' ? 'text-emerald-400' : 'text-amber-400'" x-text="r.confidence"></td>
            </tr>
          </template>
          <tr x-show="rows().length === 0"><td colspan="7" class="py-6 text-center text-zinc-500">No results yet. Upload a video to begin.</td></tr>
        </tbody>
      </table>
    </div>
  </section>
</div>

<script>
function app() {
  return {
    file: null, uploading: false, progress: 0, uploadError: '',
    status: { stage: 'idle', message: 'Waiting for upload.' },
    items: [], q: '', sortKey: 'pct_markup', sortDir: -1, timer: null,
    cols: [
      { k: 'product_name', l: 'Item' }, { k: 'size_text', l: 'Size' },
      { k: 'in_store_price', l: 'In-store' }, { k: 'online_price', l: 'Instacart' },
      { k: 'dollar_markup', l: 'Markup $' }, { k: 'pct_markup', l: 'Markup %' }, { k: 'confidence', l: 'Conf.' }
    ],
    init() { this.poll(); this.timer = setInterval(() => this.poll(), 3000); },
    money(v) { return v == null ? '-' : '$' + Number(v).toFixed(2); },
    matched() { return this.items.filter(i => i.pct_markup != null); },
    avgMarkup() { const m = this.matched(); return m.length ? m.reduce((a, i) => a + i.pct_markup, 0) / m.length : 0; },
    totalSavings() { return this.matched().reduce((a, i) => a + (i.dollar_markup || 0), 0); },
    sortBy(k) { if (this.sortKey === k) this.sortDir *= -1; else { this.sortKey = k; this.sortDir = -1; } },
    rows() {
      const q = this.q.toLowerCase();
      const k = this.sortKey, d = this.sortDir;
      return this.items
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
    async poll() {
      try {
        const s = await (await fetch('/grocery/status', { cache: 'no-store' })).json();
        if (!s.error) this.status = s;
        const p = await (await fetch('/grocery/prices', { cache: 'no-store' })).json();
        if (p && p.items) this.items = p.items;
      } catch (e) {}
    },
    async upload() {
      if (!this.file) return;
      this.uploadError = ''; this.uploading = true; this.progress = 0;
      try {
        const ext = /\\.mov$/i.test(this.file.name) ? 'mov' : 'mp4';
        const pre = await (await fetch('/grocery/presign', {
          method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ ext: ext })
        })).json();
        if (!pre.url) throw new Error(pre.error || 'presign failed');
        await new Promise((resolve, reject) => {
          const xhr = new XMLHttpRequest();
          xhr.open('PUT', pre.url);
          xhr.upload.onprogress = (e) => { if (e.lengthComputable) this.progress = Math.round(e.loaded / e.total * 100); };
          xhr.onload = () => (xhr.status >= 200 && xhr.status < 300) ? resolve() : reject(new Error('S3 upload failed: ' + xhr.status));
          xhr.onerror = () => reject(new Error('Network error during upload'));
          xhr.send(this.file);
        });
        this.status = { stage: 'uploaded', message: 'Upload complete. Waiting for pipeline...', walk_id: pre.walk_id };
        this.items = [];
      } catch (e) { this.uploadError = String(e.message || e); }
      this.uploading = false;
    }
  };
}
</script>
</body>
</html>`;
