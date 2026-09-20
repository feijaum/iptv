#!/usr/bin/env python3
import csv
import io
import re
from urllib.request import Request, urlopen
from pathlib import Path

SOURCES = [
    "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/br.m3u",
    "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/br_pluto.m3u",
    "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/br_samsung.m3u",
]
CHANNELS_DB = "https://raw.githubusercontent.com/iptv-org/database/master/data/channels.csv"
LOGOS_DB = "https://raw.githubusercontent.com/iptv-org/database/master/data/logos.csv"

CATEGORY_PT = {
    "general": "Canais Abertos / Geral",
    "movies": "Filmes",
    "series": "Series",
    "animation": "Desenhos",
    "kids": "Infantil",
    "news": "Noticias",
    "sports": "Esportes",
    "music": "Musica",
    "documentary": "Documentarios",
    "education": "Educacao",
    "lifestyle": "Variedades",
    "entertainment": "Entretenimento",
    "comedy": "Comedia",
    "family": "Familia",
    "religious": "Religiosos",
    "shop": "Compras",
    "business": "Negocios",
    "culture": "Cultura",
    "travel": "Viagens",
    "weather": "Tempo",
    "auto": "Automotivo",
    "cooking": "Culinaria",
    "outdoor": "Natureza / Outdoor",
    "science": "Ciencia",
    "classic": "Classicos",
}

def download(url):
    req = Request(url, headers={"User-Agent": "feijaum-iptv-updater/2.0"})
    with urlopen(req, timeout=120) as r:
        return r.read().decode("utf-8-sig")

def entries(text):
    current = []
    for raw in text.replace("\r", "").split("\n"):
        line = raw.strip()
        if not line or line == "#EXTM3U":
            continue
        if line.startswith("#EXTINF:"):
            current = [line]
        elif current and line.startswith("#"):
            current.append(line)
        elif current:
            current.append(line)
            yield current
            current = []

def base_tvg_id(extinf):
    m = re.search(r'tvg-id="([^"]+)"', extinf)
    if not m:
        return ""
    return m.group(1).split("@", 1)[0]

def set_attr(extinf, key, value):
    value = (value or "").replace('"', "'")
    pattern = rf'\s{re.escape(key)}="[^"]*"'
    if re.search(pattern, extinf):
        return re.sub(pattern, f' {key}="{value}"', extinf, count=1)
    comma = extinf.find(",")
    if comma >= 0:
        return extinf[:comma] + f' {key}="{value}"' + extinf[comma:]
    return extinf + f' {key}="{value}"'

def load_metadata():
    channels = {}
    for row in csv.DictReader(io.StringIO(download(CHANNELS_DB))):
        channels[row["id"]] = row

    logos = {}
    for row in csv.DictReader(io.StringIO(download(LOGOS_DB))):
        channel = row.get("channel", "")
        if not channel or row.get("in_use", "").upper() != "TRUE":
            continue
        url = row.get("url", "")
        if not url:
            continue
        current = logos.get(channel)
        # Prefer PNG/JPG because IPTV players generally handle raster logos best.
        score = 2 if row.get("format", "").upper() in ("PNG", "JPG", "JPEG", "WEBP") else 1
        if current is None or score > current[0]:
            logos[channel] = (score, url)
    return channels, {k: v[1] for k, v in logos.items()}

def category_for(channel):
    raw = (channel or {}).get("categories", "")
    cats = [x.strip() for x in raw.split(";") if x.strip()]
    if not cats:
        return "Outros"
    # IPTV-org categories are authoritative; use first category as primary group.
    return CATEGORY_PT.get(cats[0], cats[0].replace("-", " ").title())

def main():
    channels, logos = load_metadata()
    seen_urls = set()
    merged = []
    logo_count = 0
    categorized_count = 0

    for source in SOURCES:
        for entry in entries(download(source)):
            url = entry[-1]
            if url in seen_urls:
                continue
            seen_urls.add(url)

            extinf = entry[0]
            channel_id = base_tvg_id(extinf)
            metadata = channels.get(channel_id, {})
            group = category_for(metadata)
            extinf = set_attr(extinf, "group-title", group)
            if group != "Outros":
                categorized_count += 1

            logo = logos.get(channel_id)
            if logo:
                extinf = set_attr(extinf, "tvg-logo", logo)
                logo_count += 1

            entry[0] = extinf
            merged.append(entry)

    content = "#EXTM3U\n" + "\n".join("\n".join(e) for e in merged) + "\n"
    Path("br.m3u").write_text(content, encoding="utf-8")
    print(
        f"Generated br.m3u: {len(merged)} streams; "
        f"{categorized_count} categorized; {logo_count} with logos"
    )

if __name__ == "__main__":
    main()
