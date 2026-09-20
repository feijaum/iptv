export default {
  async fetch() {
    const url = "https://raw.githubusercontent.com/feijaum/iptv/main/br.m3u";
    const r = await fetch(url);

    if (!r.ok) {
      return new Response("Playlist unavailable", { status: 502 });
    }

    return new Response(r.body, {
      status: 200,
      headers: {
        "Content-Type": "application/vnd.apple.mpegurl; charset=utf-8",
        "Content-Disposition": "inline; filename=br.m3u",
        "Cache-Control": "public, max-age=30, must-revalidate",
        "Access-Control-Allow-Origin": "*"
      }
    });
  }
};
