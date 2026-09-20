#!/usr/bin/env python3
import csv
from pathlib import Path

PLAYLIST = Path("br.m3u")
OFFLIST = Path("linksoff.m3u")
AUDIT = Path("playback-audit.csv")

def parse_m3u(text):
    blocks = []
    cur = []
    for raw in text.replace("\r", "").split("\n"):
        line = raw.strip()
        if not line or line == "#EXTM3U":
            continue
        if line.startswith("#EXTINF:"):
            if cur:
                blocks.append(cur)
            cur = [line]
        elif cur:
            cur.append(line)
            if not line.startswith("#"):
                blocks.append(cur)
                cur = []
    if cur:
        blocks.append(cur)
    return blocks

def load_audit():
    with AUDIT.open("r", encoding="utf-8", newline="") as f:
        return {r["url"]: r for r in csv.DictReader(f)}

def write_m3u(path, blocks):
    body = "\n".join("\n".join(b) for b in blocks)
    header = '#EXTM3U url-tvg="https://iptv.jvleite7.workers.dev/epg.xml.gz" x-tvg-url="https://iptv.jvleite7.workers.dev/epg.xml.gz"'
    path.write_text(header + "\n" + (body + "\n" if body else ""), encoding="utf-8")

def main():
    if not PLAYLIST.exists() or not AUDIT.exists():
        raise SystemExit("playlist/audit missing")

    audit = load_audit()
    active = parse_m3u(PLAYLIST.read_text(encoding="utf-8", errors="replace"))
    existing_off = parse_m3u(OFFLIST.read_text(encoding="utf-8", errors="replace")) if OFFLIST.exists() else []

    kept = []
    removed = []
    direct_fallbacks = 0

    for block in active:

        url = block[-1]
        row = audit.get(url)
        if not row or row.get("status") == "OK":
            kept.append(block)
            continue

        upstream = (row.get("upstream_url") or "").strip()
        if upstream and row.get("upstream_status") == "OK":
            new_block = list(block)
            new_block[-1] = upstream
            kept.append(new_block)
            direct_fallbacks += 1
        else:
            removed.append(block)

    # Avoid duplicating exactly identical failed blocks.
    existing_keys = {b[0] + "\n" + b[-1] for b in existing_off if b}
    for b in removed:
        key = b[0] + "\n" + b[-1]
        if key not in existing_keys:
            existing_off.append(b)
            existing_keys.add(key)

    write_m3u(PLAYLIST, kept)
    write_m3u(OFFLIST, existing_off)
    print(f"REPAIR_KEPT={len(kept)} REPAIR_REMOVED={len(removed)} DIRECT_FALLBACKS={direct_fallbacks}")

if __name__ == "__main__":
    main()
