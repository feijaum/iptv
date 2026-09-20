let plutoBootCache = null;

const PLAYLIST_URL = "https://raw.githubusercontent.com/feijaum/iptv/main/br.m3u";
const UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36";

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
    h.endsWith(".paramount.tech") ||
    h.endsWith(".akamaized.net") ||
    h.endsWith(".akamaihd.net") ||
    h.endsWith(".cloudfront.net") ||
    h.endsWith(".fastly.net")
  );
}

async function getPlutoBoot(channelId = "") {
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
  if (channelId) u.searchParams.set("channelSlug", channelId);

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
    const p = new URL(provider === "pluto" ? "/pluto-proxy" : "/fast-child", proxyBase.origin);
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

  return new Response(r.body, { status: r.status, headers: outHeaders });
}

async function handlePluto(channelId, request) {
  if (!/^[0-9a-f]{24}$/i.test(channelId)) {
    return new Response("Invalid Pluto channel id", { status: 400 });
  }

  try {
    const boot = await getPlutoBoot(channelId);
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

    return await proxyHls(u.toString(), request, "pluto");
  } catch (e) {
    return new Response("Pluto session failed", {
      status: 502,
      headers: corsHeaders({ "Cache-Control": "no-store" })
    });
  }
}


async function plutoHealth(channelId) {
  const result = { channelId, boot: false, master: false, variant: false, segment: false, hosts: [] };
  try {
    const boot = await getPlutoBoot(channelId);
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

    if (url.pathname.startsWith("/health/pluto/")) {
      const channelId = url.pathname.split("/").pop();
      const data = await plutoHealth(channelId);
      return Response.json(data, { headers: corsHeaders({ "Cache-Control": "no-store" }) });
    }

    if (url.pathname.startsWith("/pluto/")) {
      return handlePluto(url.pathname.split("/").pop(), request);
    }

    if (url.pathname === "/pluto-proxy") {
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

    const r = await fetch(PLAYLIST_URL, { cf: { cacheTtl: 30, cacheEverything: true } });
    if (!r.ok) {
      return new Response("Playlist unavailable", { status: 502 });
    }

    return new Response(r.body, {
      status: 200,
      headers: corsHeaders({
        "Content-Type": "application/vnd.apple.mpegurl; charset=utf-8",
        "Content-Disposition": "inline; filename=br.m3u",
        "Cache-Control": "public, max-age=30, must-revalidate"
      })
    });
  }
};
