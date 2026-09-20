#!/usr/bin/env python3
import csv
import gzip
import io
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

PLAYLIST = Path("br.m3u")
OUT_GZ = Path("guide.xml.gz")
STATUS = Path("epg-status.csv")

SOURCES = [
    ("EPG.pw Brasil", "https://epg.pw/xmltv/epg_BR.xml"),
    ("Pluto TV Brasil", "https://i.mjh.nz/PlutoTV/br.xml"),
    ("EPGShare Brasil", "https://epgshare01.online/epgshare01/epg_ripper_BR1.xml.gz"),
]

def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"\([^)]*\)|\[[^]]*\]", " ", s)
    s = re.sub(r"\b(2160p|1080[pi]|720p|576p|480p|360p|hd|fhd|uhd|4k|brasil|brazil|geo blocked|not 24 7)\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())

def download(url):
    req = Request(url, headers={"User-Agent": "feijaum-iptv-epg/1.0", "Accept": "*/*"})
    with urlopen(req, timeout=90) as r:
        data = r.read()
        enc = (r.headers.get("Content-Encoding") or "").lower()
    if url.endswith(".gz") or enc == "gzip" or data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data

def playlist_channels():
    out = []
    for line in PLAYLIST.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("#EXTINF:"):
            continue
        mid = re.search(r'tvg-id="([^"]+)"', line)
        if not mid:
            continue
        tvg_id = mid.group(1).strip()
        if not tvg_id:
            continue
        quoted = False
        name = ""
        for i, ch in enumerate(line):
            if ch == '"':
                quoted = not quoted
            elif ch == "," and not quoted:
                name = line[i+1:].strip()
                break
        out.append({
            "id": tvg_id,
            "base": tvg_id.split("@", 1)[0],
            "name": name or tvg_id,
            "norm": norm(name or tvg_id),
        })
    return out

def source_index(root):
    by_id = {}
    by_norm = {}
    for ch in root.findall("channel"):
        sid = ch.get("id", "").strip()
        if not sid:
            continue
        names = [(d.text or "").strip() for d in ch.findall("display-name") if (d.text or "").strip()]
        by_id.setdefault(sid.lower(), (ch, names))
        for n in names + [sid]:
            nn = norm(n)
            if nn:
                by_norm.setdefault(nn, []).append((sid, ch, names))
    return by_id, by_norm

def best_name_match(target, by_norm):
    wanted = target["norm"]
    aliases = {wanted}
    manual = {
        "sbt nacional": {"sbt", "sbt nacional"},
        "record": {"record", "record tv", "recordtv"},
        "band": {"band", "band rede"},
        "rede tv": {"rede tv", "redetv"},
        "tv bahia": {"tv bahia"},
        "tv aratu": {"tv aratu", "aratu"},
        "recordtv itapoan": {"recordtv itapoan", "record tv itapoan", "record bahia"},
        "tve bahia": {"tve bahia"},
    }
    aliases |= manual.get(wanted, set())
    for a in aliases:
        if a in by_norm:
            return by_norm[a][0]
    best = None
    best_score = 0.0
    for n, rows in by_norm.items():
        score = SequenceMatcher(None, wanted, n).ratio()
        if score > best_score:
            best_score, best = score, rows[0]
    return best if best_score >= 0.90 else None

def clone_with_channel(elem, new_id=None):
    data = ET.tostring(elem, encoding="utf-8")
    c = ET.fromstring(data)
    if new_id is not None:
        c.set("id", new_id)
    return c

def main():
    targets = playlist_channels()
    unmatched = {t["id"]: t for t in targets}
    matched = {}
    selected_channels = {}
    selected_programmes = []

    for source_name, url in SOURCES:
        if not unmatched:
            break
        try:
            root = ET.fromstring(download(url))
        except Exception as e:
            print(f"EPG source failed: {source_name}: {e}")
            continue

        by_id, by_norm = source_index(root)
        sid_to_targets = {}

        for tid, t in list(unmatched.items()):
            hit = None
            for key in (t["id"].lower(), t["base"].lower()):
                if key in by_id:
                    ch, names = by_id[key]
                    hit = (ch.get("id", ""), ch, names)
                    break
            if hit is None:
                hit = best_name_match(t, by_norm)
            if hit is None:
                continue

            sid, ch, names = hit
            sid_to_targets.setdefault(sid, []).append(tid)
            selected_channels[tid] = clone_with_channel(ch, tid)
            matched[tid] = (source_name, sid)
            unmatched.pop(tid, None)

        if sid_to_targets:
            for p in root.findall("programme"):
                sid = p.get("channel", "")
                tids = sid_to_targets.get(sid)
                if not tids:
                    continue
                for tid in tids:
                    cp = ET.fromstring(ET.tostring(p, encoding="utf-8"))
                    cp.set("channel", tid)
                    selected_programmes.append(cp)

    tv = ET.Element("tv", {
        "generator-info-name": "feijaum/iptv EPG merger",
        "source-info-name": "Public XMLTV sources",
    })
    for t in targets:
        ch = selected_channels.get(t["id"])
        if ch is not None:
            tv.append(ch)
    selected_programmes.sort(key=lambda p: (p.get("start", ""), p.get("channel", "")))
    for p in selected_programmes:
        tv.append(p)

    xml = ET.tostring(tv, encoding="utf-8", xml_declaration=True)
    OUT_GZ.write_bytes(gzip.compress(xml, compresslevel=6))

    with STATUS.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["tvg_id", "name", "status", "source", "source_channel_id"])
        for t in targets:
            if t["id"] in matched:
                src, sid = matched[t["id"]]
                w.writerow([t["id"], t["name"], "OK", src, sid])
            else:
                w.writerow([t["id"], t["name"], "SEM_EPG", "", ""])

    print(f"EPG_MATCHED={len(matched)} EPG_UNMATCHED={len(unmatched)} PROGRAMMES={len(selected_programmes)}")

if __name__ == "__main__":
    main()
