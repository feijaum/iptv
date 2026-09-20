let plutoBootCache = null;
let relayMapCache = null;

const PLAYLIST_URL = "https://raw.githubusercontent.com/feijaum/iptv/main/br.m3u";
const RELAY_MAP_URL = "https://raw.githubusercontent.com/feijaum/iptv/main/relay-map.json";
const UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36";
const ADULT_PIN_SHA256 = "79737ac46dad121166483e084a0727e5d6769fb47fa9b0b627eba4107e696078";


async function sha256Hex(text) {
  const data = new TextEncoder().encode(String(text || ""));
  const digest = await crypto.subtle.digest("SHA-256", data);
  return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, "0")).join("");
}

async function validAdultPin(pin) {
  if (!pin) return false;
  return (await sha256Hex(pin)) === ADULT_PIN_SHA256;
}

function splitM3uEntries(text) {
  const lines = String(text || "").replace(/\r/g, "").split("\n");
  const entries = [];
  let header = "#EXTM3U";
  let current = [];

  for (const line of lines) {
    if (!line) continue;
    if (line === "#EXTM3U") {
      header = line;
      continue;
    }
    if (line.startsWith("#EXTINF:")) {
      if (current.length) entries.push(current);
      current = [line];
      continue;
    }
    if (current.length) {
      current.push(line);
      if (!line.startsWith("#")) {
        entries.push(current);
        current = [];
      }
    }
  }
  if (current.length) entries.push(current);
  return { header, entries };
}

function isAdultEntry(entry) {
  return !!(entry && entry[0] && /group-title="Adultos"/i.test(entry[0]));
}

function buildFilteredPlaylist(text, mode) {
  const parsed = splitM3uEntries(text);
  let entries = parsed.entries;
  if (mode === "safe") entries = entries.filter(e => !isAdultEntry(e));
  if (mode === "adult-only") entries = entries.filter(isAdultEntry);
  return parsed.header + "\n" + entries.map(e => e.join("\n")).join("\n") + (entries.length ? "\n" : "");
}

function corsHeaders(extra = {}) {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "*",
    ...extra
  };
}

function isPrivateHost(hostname) {
  const h = (hostname || "").toLowerCase();
  if (h === "localhost" || h.endsWith(".local")) return true;
  if (/^127\./.test(h) || /^10\./.test(h) || /^192\.168\./.test(h)) return true;
  const m = h.match(/^172\.(\d+)\./);
  if (m && Number(m[1]) >= 16 && Number(m[1]) <= 31) return true;
  return h === "::1" || h.startsWith("fc") || h.startsWith("fd");
}

function isPlutoHost(hostname) {
  const h = (hostname || "").toLowerCase();
  if (isPrivateHost(h)) return false;
  return (
    h === "pluto.tv" ||
    h.endsWith(".pluto.tv") ||
    h === "plutotv.net" ||
    h.endsWith(".plutotv.net") ||
    h.endsWith(".paramount.tech") ||
    h.endsWith(".akamaized.net") ||
    h.endsWith(".akamaihd.net") ||
    h.endsWith(".cloudfront.net") ||
    h.endsWith(".fastly.net")
  );
}


function relaySiteKey(hostname) {
  const h = (hostname || "").toLowerCase().replace(/\.$/, "");
  const parts = h.split(".").filter(Boolean);
  if (parts.length <= 2) return h;
  const last2 = parts.slice(-2).join(".");
  const brSecond = new Set(["com.br", "net.br", "org.br", "tv.br", "edu.br", "gov.br"]);
  return brSecond.has(last2) && parts.length >= 3 ? parts.slice(-3).join(".") : last2;
}

function relayCdnHost(hostname) {
  const h = (hostname || "").toLowerCase();
  const suffixes = [
    ".akamaized.net", ".akamaihd.net", ".cloudfront.net", ".amazonaws.com",
    ".googlevideo.com", ".googleusercontent.com", ".gvt1.com", ".edgekey.net",
    ".edgesuite.net", ".fastly.net", ".cdn77.org", ".ottera.tv", ".jwplayer.com",
    ".maissbt.com", ".redbull.com", ".stingray.com"
  ];
  return suffixes.some(s => h.endsWith(s));
}

function relayChildAllowed(rootUrl, childUrl) {
  try {
    const root = new URL(rootUrl);
    const child = new URL(childUrl);
    if (!["http:", "https:"].includes(child.protocol)) return false;
    if (isPrivateHost(child.hostname)) return false;
    if (child.hostname.toLowerCase() === root.hostname.toLowerCase()) return true;
    if (relaySiteKey(child.hostname) === relaySiteKey(root.hostname)) return true;
    return relayCdnHost(child.hostname);
  } catch (_) {
    return false;
  }
}

async function getRelayMap() {
  const now = Date.now();
  if (relayMapCache && relayMapCache.expiresAt > now) return relayMapCache.data;
  const r = await fetch(RELAY_MAP_URL, { cf: { cacheTtl: 20, cacheEverything: true } });
  if (!r.ok) throw new Error("relay map " + r.status);
  const data = await r.json();
  relayMapCache = { data, expiresAt: now + 20000 };
  return data;
}

function relayProxyUrl(abs, requestUrl, relayId) {
  const pathname = new URL(abs).pathname.toLowerCase();
  let ext = ".ts";
  if (pathname.endsWith(".m3u8")) ext = ".m3u8";
  else if (pathname.endsWith(".m4s")) ext = ".m4s";
  else if (pathname.endsWith(".mp4")) ext = ".mp4";
  else if (pathname.endsWith(".aac")) ext = ".aac";
  else if (pathname.endsWith(".vtt")) ext = ".vtt";
  else if (pathname.endsWith(".key")) ext = ".key";
  const p = new URL("/relay/" + relayId + ext, new URL(requestUrl).origin);
  p.searchParams.set("u", abs);
  return p.toString();
}

function rewriteRelayManifest(text, finalUrl, requestUrl, relayId) {
  const base = new URL(finalUrl);
  const makeProxy = raw => relayProxyUrl(new URL(raw, base).toString(), requestUrl, relayId);
  return text.split(/\r?\n/).map(line => {
    const trimmed = line.trim();
    if (!trimmed) return line;
    if (!trimmed.startsWith("#")) return makeProxy(trimmed);
    return line.replace(/URI="([^"]+)"/g, (_, uri) => 'URI="' + makeProxy(uri) + '"');
  }).join("\n");
}

async function proxyRelay(upstream, request, relayId, rootEntry, initial = false) {
  if (!initial && !relayChildAllowed(rootEntry.url, upstream)) {
    return new Response("Blocked relay upstream", {
      status: 403,
      headers: corsHeaders({ "Cache-Control": "no-store" })
    });
  }

  const headers = {
    "User-Agent": (rootEntry.headers && rootEntry.headers["User-Agent"]) || UA,
    "Accept": "*/*"
  };
  if (rootEntry.headers && rootEntry.headers["Referer"]) {
    headers["Referer"] = rootEntry.headers["Referer"];
  }
  const range = request.headers.get("Range");
  if (range) headers["Range"] = range;

  let r;
  try {
    r = await fetch(upstream, { headers, redirect: "follow" });
  } catch (_) {
    return new Response("Relay upstream fetch failed", {
      status: 502,
      headers: corsHeaders({ "Cache-Control": "no-store" })
    });
  }

  if (!r.ok) {
    return new Response("Relay upstream error " + r.status, {
      status: 502,
      headers: corsHeaders({ "Cache-Control": "no-store" })
    });
  }

  const ct = (r.headers.get("Content-Type") || "").toLowerCase();
  const finalUrl = r.url || upstream;
  const looksManifest =
    ct.includes("mpegurl") ||
    new URL(finalUrl).pathname.toLowerCase().endsWith(".m3u8");

  if (looksManifest) {
    const text = await r.text();
    const rewritten = rewriteRelayManifest(text, finalUrl, request.url, relayId);
    return new Response(rewritten, {
      status: 200,
      headers: corsHeaders({
        "Content-Type": "application/vnd.apple.mpegurl; charset=utf-8",
        "Cache-Control": "no-store"
      })
    });
  }

  const outHeaders = corsHeaders({
    "Content-Type": r.headers.get("Content-Type") || "application/octet-stream",
    "Cache-Control": "no-store"
  });
  for (const h of ["Content-Range", "Accept-Ranges", "Content-Length", "ETag", "Last-Modified"]) {
    const v = r.headers.get(h);
    if (v) outHeaders[h] = v;
  }
  return new Response(r.body, { status: r.status, headers: outHeaders });
}

async function handleLive(relayId, request) {
  relayId = String(relayId || "").replace(/\.[a-z0-9]+$/i, "");
  if (!/^[0-9a-f]{16}$/i.test(relayId)) {
    return new Response("Invalid relay id", { status: 400, headers: corsHeaders() });
  }
  try {
    const map = await getRelayMap();
    const entry = map && map.streams && map.streams[relayId];
    if (!entry || !entry.url) {
      return new Response("Unknown relay id", { status: 404, headers: corsHeaders() });
    }
    return await proxyRelay(entry.url, request, relayId, entry, true);
  } catch (_) {
    return new Response("Relay unavailable", {
      status: 502,
      headers: corsHeaders({ "Cache-Control": "no-store" })
    });
  }
}

async function handleRelay(relayId, request, url) {
  relayId = String(relayId || "").replace(/\.[a-z0-9]+$/i, "");
  if (!/^[0-9a-f]{16}$/i.test(relayId)) {
    return new Response("Invalid relay id", { status: 400, headers: corsHeaders() });
  }
  const upstream = url.searchParams.get("u");
  if (!upstream) return new Response("Missing upstream", { status: 400, headers: corsHeaders() });
  try {
    const map = await getRelayMap();
    const entry = map && map.streams && map.streams[relayId];
    if (!entry || !entry.url) {
      return new Response("Unknown relay id", { status: 404, headers: corsHeaders() });
    }
    return await proxyRelay(upstream, request, relayId, entry, false);
  } catch (_) {
    return new Response("Relay unavailable", {
      status: 502,
      headers: corsHeaders({ "Cache-Control": "no-store" })
    });
  }
}

async function getPlutoBoot() {
  const now = Math.floor(Date.now() / 1000);
  if (plutoBootCache && plutoBootCache.expiresAt > now + 60) {
    return plutoBootCache.data;
  }

  const clientID = crypto.randomUUID();
  const u = new URL("https://boot.pluto.tv/v4/start");
  const p = {
    appName: "web",
    appVersion: "8.0.0-111b2b9dc00bd0bea9030b30662159ed9e7c8bc6",
    deviceVersion: "122.0.0",
    deviceModel: "web",
    deviceMake: "chrome",
    deviceType: "web",
    clientID,
    clientModelNumber: "1.0.0",
    serverSideAds: "false",
    drmCapabilities: "widevine:L3",
    blockingMode: ""
  };
  for (const [k, v] of Object.entries(p)) u.searchParams.set(k, v);

  const r = await fetch(u.toString(), {
    headers: {
      "Accept": "*/*",
      "Origin": "https://pluto.tv",
      "Referer": "https://pluto.tv/",
      "User-Agent": UA
    }
  });
  if (!r.ok) throw new Error("Pluto boot " + r.status);
  const data = await r.json();
  if (!data.sessionToken) throw new Error("Pluto session token missing");

  let exp = now + 600;
  try {
    const payload = data.sessionToken.split(".")[1]
      .replace(/-/g, "+").replace(/_/g, "/");
    const padded = payload + "=".repeat((4 - payload.length % 4) % 4);
    exp = JSON.parse(atob(padded)).exp || exp;
  } catch (_) {}

  plutoBootCache = { data, expiresAt: exp };
  return data;
}

function rewriteManifest(text, finalUrl, requestUrl, provider) {
  const base = new URL(finalUrl);
  const proxyBase = new URL(requestUrl);
  const makeProxy = (raw) => {
    const abs = new URL(raw, base).toString();
    let route;
    if (provider === "pluto") {
      const pathname = new URL(abs).pathname.toLowerCase();
      if (pathname.endsWith(".m3u8")) route = "/pluto-proxy.m3u8";
      else if (pathname.endsWith(".m4s")) route = "/pluto-media.m4s";
      else if (pathname.endsWith(".mp4")) route = "/pluto-media.mp4";
      else if (pathname.endsWith(".aac")) route = "/pluto-media.aac";
      else route = "/pluto-media.ts";
    } else {
      route = "/fast-child";
    }
    const p = new URL(route, proxyBase.origin);
    p.searchParams.set("u", abs);
    return p.toString();
  };

  return text.split(/\r?\n/).map(line => {
    const trimmed = line.trim();
    if (!trimmed) return line;
    if (!trimmed.startsWith("#")) return makeProxy(trimmed);

    // Rewrite URI="..." attributes used by AES keys, maps and media tags.
    return line.replace(/URI="([^"]+)"/g, (_, uri) => 'URI="' + makeProxy(uri) + '"');
  }).join("\n");
}


function absolutizeManifest(text, finalUrl) {
  const base = new URL(finalUrl);
  const absolute = raw => new URL(raw, base).toString();
  return text.split(/\r?\n/).map(line => {
    const trimmed = line.trim();
    if (!trimmed) return line;
    if (!trimmed.startsWith("#")) return absolute(trimmed);
    return line.replace(/URI="([^"]+)"/g, (_, uri) => 'URI="' + absolute(uri) + '"');
  }).join("\n");
}

async function proxyPlutoMaster(upstream) {
  const r = await fetch(upstream, {
    headers: {
      "User-Agent": UA,
      "Accept": "*/*",
      "Origin": "https://pluto.tv",
      "Referer": "https://pluto.tv/"
    },
    redirect: "follow"
  });
  if (!r.ok) {
    return new Response("Pluto master upstream error " + r.status, {
      status: 502,
      headers: corsHeaders({ "Cache-Control": "no-store" })
    });
  }
  const text = await r.text();
  const manifest = absolutizeManifest(text, r.url || upstream);
  return new Response(manifest, {
    status: 200,
    headers: corsHeaders({
      "Content-Type": "application/vnd.apple.mpegurl; charset=utf-8",
      "Cache-Control": "no-store"
    })
  });
}

async function proxyHls(upstream, request, provider) {
  const headers = {
    "User-Agent": UA,
    "Accept": "*/*"
  };
  if (provider === "pluto") {
    headers["Origin"] = "https://pluto.tv";
    headers["Referer"] = "https://pluto.tv/";
  }

  const range = request.headers.get("Range");
  if (range) headers["Range"] = range;

  const r = await fetch(upstream, { headers, redirect: "follow" });
  if (!r.ok) {
    return new Response("Upstream error " + r.status, {
      status: 502,
      headers: corsHeaders({ "Cache-Control": "no-store" })
    });
  }

  const ct = (r.headers.get("Content-Type") || "").toLowerCase();
  const finalUrl = r.url || upstream;
  const looksManifest =
    ct.includes("mpegurl") ||
    new URL(finalUrl).pathname.toLowerCase().endsWith(".m3u8");

  if (looksManifest) {
    const text = await r.text();
    const rewritten = rewriteManifest(text, finalUrl, request.url, provider);
    return new Response(rewritten, {
      status: 200,
      headers: corsHeaders({
        "Content-Type": "application/vnd.apple.mpegurl; charset=utf-8",
        "Cache-Control": provider === "pluto" ? "no-store" : "public, max-age=10"
      })
    });
  }

  const outHeaders = corsHeaders({
    "Content-Type": r.headers.get("Content-Type") || "application/octet-stream",
    "Cache-Control": "no-store"
  });
  const contentRange = r.headers.get("Content-Range");
  if (contentRange) outHeaders["Content-Range"] = contentRange;
  const acceptRanges = r.headers.get("Accept-Ranges");
  if (acceptRanges) outHeaders["Accept-Ranges"] = acceptRanges;
  const contentLength = r.headers.get("Content-Length");
  if (contentLength) outHeaders["Content-Length"] = contentLength;
  const etag = r.headers.get("ETag");
  if (etag) outHeaders["ETag"] = etag;
  const lastModified = r.headers.get("Last-Modified");
  if (lastModified) outHeaders["Last-Modified"] = lastModified;

  return new Response(r.body, { status: r.status, headers: outHeaders });
}

async function handlePluto(channelId, request) {
  channelId = String(channelId || "").replace(/\.m3u8$/i, "");
  if (!/^[0-9a-f]{24}$/i.test(channelId)) {
    return new Response("Invalid Pluto channel id", { status: 400 });
  }

  try {
    const boot = await getPlutoBoot();
    const stitcher = (boot.servers && boot.servers.stitcher) ||
      "https://cfd-v4-service-channel-stitcher-use1-1.prd.pluto.tv";
    const u = new URL("/v2/stitch/hls/channel/" + channelId + "/master.m3u8", stitcher);

    if (boot.stitcherParams) {
      const extras = new URLSearchParams(boot.stitcherParams);
      for (const [k, v] of extras.entries()) {
        if (!u.searchParams.has(k)) u.searchParams.set(k, v);
      }
    }
    u.searchParams.set("jwt", boot.sessionToken);
    u.searchParams.set("includeExtendedEvents", "true");
    u.searchParams.set("masterJWTPassthrough", "true");

    // Only the dynamic master is served by our Worker. Child playlists,
    // audio renditions, encryption keys and media segments remain on Pluto's
    // own CDN. Native IPTV players handle those HLS relationships more
    // reliably than when every child request is re-proxied through Workers.
    return await proxyPlutoMaster(u.toString());
  } catch (e) {
    return new Response("Pluto session failed", {
      status: 502,
      headers: corsHeaders({ "Cache-Control": "no-store" })
    });
  }
}



async function plutoProxyHealth(channelId, request) {
  const result = {
    channelId,
    routeMaster: false,
    rewrittenVariant: false,
    proxiedVariant: false,
    rewrittenSegment: false,
    proxiedSegment: false
  };
  try {
    const origin = new URL(request.url).origin;
    const masterReq = new Request(origin + "/pluto/" + channelId, {
      headers: { "User-Agent": UA, "Accept": "*/*" }
    });
    const masterResp = await handlePluto(channelId, masterReq);
    result.routeMasterStatus = masterResp.status;
    if (!masterResp.ok) return result;
    result.routeMaster = true;
    const masterText = await masterResp.text();

    const variantLine = masterText.split(/\r?\n/).find(x => x.trim() && !x.startsWith("#"));
    if (!variantLine) {
      result.error = "no variant URL in rewritten master";
      return result;
    }
    result.rewrittenVariant = variantLine.includes("/pluto-");
    const variantURL = new URL(variantLine.trim());
    result.variantProxyHost = variantURL.hostname;
    const variantUpstream = variantURL.searchParams.get("u");
    if (!variantUpstream) {
      result.error = "rewritten variant missing upstream";
      return result;
    }

    const variantReq = new Request(variantURL.toString(), {
      headers: { "User-Agent": UA, "Accept": "*/*" }
    });
    const variantResp = await proxyHls(variantUpstream, variantReq, "pluto");
    result.proxiedVariantStatus = variantResp.status;
    if (!variantResp.ok) return result;
    result.proxiedVariant = true;
    const variantText = await variantResp.text();

    const segmentLine = variantText.split(/\r?\n/).find(x => x.trim() && !x.startsWith("#"));
    if (!segmentLine) {
      result.error = "no media URL in rewritten variant";
      return result;
    }
    result.rewrittenSegment = segmentLine.includes("/pluto-");
    const segmentURL = new URL(segmentLine.trim());
    result.segmentProxyHost = segmentURL.hostname;
    const segmentUpstream = segmentURL.searchParams.get("u");
    if (!segmentUpstream) {
      result.error = "rewritten segment missing upstream";
      return result;
    }
    result.segmentUpstreamHost = new URL(segmentUpstream).hostname;
    result.segmentAllowed = isPlutoHost(new URL(segmentUpstream).hostname);

    const segmentReq = new Request(segmentURL.toString(), {
      headers: { "User-Agent": UA, "Accept": "*/*", "Range": "bytes=0-1023" }
    });
    const segmentResp = await proxyHls(segmentUpstream, segmentReq, "pluto");
    result.proxiedSegmentStatus = segmentResp.status;
    result.proxiedSegmentContentType = segmentResp.headers.get("Content-Type") || "";
    result.proxiedSegmentContentRange = segmentResp.headers.get("Content-Range") || "";
    result.proxiedSegmentContentLength = segmentResp.headers.get("Content-Length") || "";
    if (!segmentResp.ok) return result;
    const bytes = new Uint8Array(await segmentResp.arrayBuffer());
    result.proxiedSegmentBytes = bytes.byteLength;
    result.proxiedSegment = bytes.byteLength > 0;
    return result;
  } catch (e) {
    result.error = String(e && e.message ? e.message : e);
    return result;
  }
}

async function plutoHealth(channelId) {
  const result = { channelId, boot: false, master: false, variant: false, segment: false, hosts: [] };
  try {
    const boot = await getPlutoBoot();
    result.boot = !!boot.sessionToken;
    result.stitcher = new URL((boot.servers && boot.servers.stitcher) ||
      "https://cfd-v4-service-channel-stitcher-use1-1.prd.pluto.tv").hostname;

    const u = new URL("/v2/stitch/hls/channel/" + channelId + "/master.m3u8",
      (boot.servers && boot.servers.stitcher) ||
      "https://cfd-v4-service-channel-stitcher-use1-1.prd.pluto.tv");
    if (boot.stitcherParams) {
      for (const [k, v] of new URLSearchParams(boot.stitcherParams).entries()) u.searchParams.set(k, v);
    }
    u.searchParams.set("jwt", boot.sessionToken);
    u.searchParams.set("includeExtendedEvents", "true");
    u.searchParams.set("masterJWTPassthrough", "true");

    const h = { "User-Agent": UA, "Accept": "*/*", "Origin": "https://pluto.tv", "Referer": "https://pluto.tv/" };
    const r1 = await fetch(u.toString(), { headers: h, redirect: "follow" });
    result.masterStatus = r1.status;
    if (!r1.ok) return result;
    result.master = true;
    const t1 = await r1.text();
    const child = t1.split(/\r?\n/).find(x => x.trim() && !x.startsWith("#"));
    if (!child) return result;
    const childUrl = new URL(child.trim(), r1.url).toString();
    result.hosts.push(new URL(childUrl).hostname);

    const r2 = await fetch(childUrl, { headers: h, redirect: "follow" });
    result.variantStatus = r2.status;
    if (!r2.ok) return result;
    result.variant = true;
    const t2 = await r2.text();
    const seg = t2.split(/\r?\n/).find(x => x.trim() && !x.startsWith("#"));
    if (!seg) return result;
    const segUrl = new URL(seg.trim(), r2.url).toString();
    result.hosts.push(new URL(segUrl).hostname);

    const r3 = await fetch(segUrl, { headers: { ...h, "Range": "bytes=0-1023" }, redirect: "follow" });
    result.segmentStatus = r3.status;
    result.segment = r3.ok;
    return result;
  } catch (e) {
    result.error = String(e && e.message ? e.message : e);
    return result;
  }
}

export default {
  async fetch(request) {
    const url = new URL(request.url);

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders() });
    }

    if (url.pathname.startsWith("/live/")) {
      return handleLive(url.pathname.split("/").pop(), request);
    }

    if (url.pathname.startsWith("/relay/")) {
      return handleRelay(url.pathname.split("/").pop(), request, url);
    }

    if (url.pathname.startsWith("/health/player/")) {
      const channelId = url.pathname.split("/").pop();
      const data = await plutoProxyHealth(channelId, request);
      return Response.json(data, { headers: corsHeaders({ "Cache-Control": "no-store" }) });
    }

    if (url.pathname.startsWith("/health/pluto/")) {
      const channelId = url.pathname.split("/").pop();
      const data = await plutoHealth(channelId);
      return Response.json(data, { headers: corsHeaders({ "Cache-Control": "no-store" }) });
    }

    if (url.pathname.startsWith("/pluto/")) {
      return handlePluto(url.pathname.split("/").pop(), request);
    }

    if (
      url.pathname === "/pluto-proxy" ||
      url.pathname === "/pluto-proxy.m3u8" ||
      url.pathname === "/pluto-media.ts" ||
      url.pathname === "/pluto-media.m4s" ||
      url.pathname === "/pluto-media.mp4" ||
      url.pathname === "/pluto-media.aac"
    ) {
      const upstream = url.searchParams.get("u");
      if (!upstream) return new Response("Missing upstream", { status: 400 });
      let parsed;
      try { parsed = new URL(upstream); } catch (_) {
        return new Response("Invalid upstream", { status: 400 });
      }
      if (!isPlutoHost(parsed.hostname)) {
        return new Response("Blocked upstream", { status: 403 });
      }
      return proxyHls(parsed.toString(), request, "pluto");
    }

    if (url.pathname === "/fast") {
      const upstream = url.searchParams.get("u");
      if (!upstream) return new Response("Missing upstream", { status: 400 });
      let parsed;
      try { parsed = new URL(upstream); } catch (_) {
        return new Response("Invalid upstream", { status: 400 });
      }
      if (parsed.hostname !== "jmp2.uk") {
        return new Response("Blocked upstream", { status: 403 });
      }
      return proxyHls(parsed.toString(), request, "fast");
    }

    if (url.pathname === "/fast-child") {
      // Child URLs are only useful as a continuation of an HLS manifest.
      // Limit to HTTP(S) and do not expose cookies or authorization headers.
      const upstream = url.searchParams.get("u");
      if (!upstream) return new Response("Missing upstream", { status: 400 });
      let parsed;
      try { parsed = new URL(upstream); } catch (_) {
        return new Response("Invalid upstream", { status: 400 });
      }
      if (!["http:", "https:"].includes(parsed.protocol)) {
        return new Response("Blocked upstream", { status: 403 });
      }
      return proxyHls(parsed.toString(), request, "fast");
    }

    const playlistPaths = new Set(["/", "/br.m3u", "/playlist.m3u", "/adult.m3u"]);
    if (!playlistPaths.has(url.pathname)) {
      return new Response("Not found", { status: 404, headers: corsHeaders() });
    }

    const suppliedPin = url.searchParams.get("pin") || "";
    const pinOk = await validAdultPin(suppliedPin);

    if (url.pathname === "/adult.m3u" && !pinOk) {
      return new Response("PIN required", {
        status: 401,
        headers: corsHeaders({
          "Content-Type": "text/plain; charset=utf-8",
          "Cache-Control": "no-store"
        })
      });
    }

    if (suppliedPin && !pinOk) {
      return new Response("Invalid PIN", {
        status: 401,
        headers: corsHeaders({
          "Content-Type": "text/plain; charset=utf-8",
          "Cache-Control": "no-store"
        })
      });
    }

    const freshUrl = PLAYLIST_URL + "?v=" + Math.floor(Date.now() / 15000);
    const r = await fetch(freshUrl, {
      headers: { "Cache-Control": "no-cache" },
      cf: { cacheTtl: 0, cacheEverything: false }
    });
    if (!r.ok) {
      return new Response("Playlist unavailable", { status: 502 });
    }

    const rawPlaylist = await r.text();
    const mode =
      url.pathname === "/adult.m3u" ? "adult-only" :
      pinOk ? "full" :
      "safe";
    const playlist = buildFilteredPlaylist(rawPlaylist, mode);
    const filename = url.pathname === "/adult.m3u" ? "adult.m3u" : "br.m3u";

    return new Response(playlist, {
      status: 200,
      headers: corsHeaders({
        "Content-Type": "application/vnd.apple.mpegurl; charset=utf-8",
        "Content-Disposition": "inline; filename=" + filename,
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0"
      })
    });
  }
};
