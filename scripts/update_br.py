#!/usr/bin/env python3
from urllib.request import Request, urlopen
from pathlib import Path

SOURCES = [
    "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/br.m3u",
    "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/br_pluto.m3u",
    "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/br_samsung.m3u",
]

def download(url):
    req = Request(url, headers={"User-Agent": "feijaum-iptv-updater/1.0"})
    with urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8-sig")

def entries(text):
    lines = text.replace("\r", "").split("\n")
    current = []
    for line in lines:
        line = line.strip()
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

def main():
    # Deduplicate ONLY identical final stream URLs.
    # Same channel/tvg-id with different URLs is intentionally preserved.
    seen_urls = set()
    merged = []
    for source in SOURCES:
        for entry in entries(download(source)):
            url = entry[-1]
            if url in seen_urls:
                continue
            seen_urls.add(url)
            merged.append(entry)

    content = "#EXTM3U\n" + "\n".join("\n".join(e) for e in merged) + "\n"
    Path("br.m3u").write_text(content, encoding="utf-8")
    print(f"Generated br.m3u with {len(merged)} stream entries")

if __name__ == "__main__":
    main()
