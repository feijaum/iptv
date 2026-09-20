#!/usr/bin/env python3
import csv
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

PLAYLIST = Path("br.m3u")
RELAY_MAP = Path("relay-map.json")
REPORT = Path("playback-audit.csv")
TIMEOUT = 14
WORKERS = 12
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/122 Safari/537.36"

def parse_entries(text):
    cur = []
    for raw in text.replace("\r", "").split("\n"):
        line = raw.strip()
        if not line or line == "#EXTM3U":
            continue
        if line.startswith("#EXTINF:"):
            if cur:
                yield cur
            cur = [line]
        elif cur:
            cur.append(line)
            if not line.startswith("#"):
                yield cur
                cur = []
    if cur:
        yield cur

def channel_name(extinf):
    return extinf.split(",", 1)[1].strip() if "," in extinf else extinf

def probe(url):
    last = ("OFF", "ffprobe failed", "", "")
    for attempt in range(2):
        try:
            cmd = [
                "ffprobe", "-v", "error",
                "-rw_timeout", "8000000",
                "-user_agent", UA,
                "-show_entries", "stream=codec_type,codec_name",
                "-of", "json",
                url,
            ]
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
            if p.returncode == 0:
                try:
                    data = json.loads(p.stdout or "{}")
                except Exception:
                    data = {}
                streams = data.get("streams") or []
                kinds = sorted({s.get("codec_type", "") for s in streams if s.get("codec_type")})
                codecs = sorted({s.get("codec_name", "") for s in streams if s.get("codec_name")})
                if any(k in ("video", "audio") for k in kinds):
                    return "OK", "decoded", ";".join(kinds), ";".join(codecs)
                last = ("OFF", "no audio/video stream", ";".join(kinds), ";".join(codecs))
            else:
                err = (p.stderr or "").strip().replace("\n", " ")
                last = ("OFF", err[-300:] if err else f"ffprobe exit {p.returncode}", "", "")
        except subprocess.TimeoutExpired:
            last = ("OFF", "timeout", "", "")
        except Exception as e:
            last = ("OFF", type(e).__name__, "", "")
        if attempt == 0:
            time.sleep(0.7)
    return last

def main():
    entries = list(parse_entries(PLAYLIST.read_text(encoding="utf-8", errors="replace")))
    relay_map = {}
    if RELAY_MAP.exists():
        try:
            relay_map = (json.loads(RELAY_MAP.read_text(encoding="utf-8")) or {}).get("streams", {})
        except Exception:
            relay_map = {}

    jobs = {}
    results = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for i, e in enumerate(entries):
            if not e or not e[-1].startswith(("http://", "https://")):
                results[i] = ("OFF", "unsupported URL", "", "")
            else:
                jobs[ex.submit(probe, e[-1])] = i
        for fut in as_completed(jobs):
            i = jobs[fut]
            try:
                results[i] = fut.result()
            except Exception as e:
                results[i] = ("OFF", type(e).__name__, "", "")

    # If a Worker relay fails, also probe its validated origin directly. This
    # tells the updater whether the failure is the channel or Cloudflare egress.
    upstream_jobs = {}
    upstream_results = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for i, e in enumerate(entries):
            status = results.get(i, ("OFF", "", "", ""))[0]
            if status == "OK":
                continue
            m = __import__("re").search(r"/live/([0-9a-f]{16})\.m3u8", e[-1], __import__("re").I)
            if not m:
                continue
            item = relay_map.get(m.group(1)) or {}
            upstream = item.get("url")
            if upstream:
                upstream_jobs[ex.submit(probe, upstream)] = (i, upstream)
        for fut in as_completed(upstream_jobs):
            i, upstream = upstream_jobs[fut]
            try:
                upstream_results[i] = (upstream,) + fut.result()
            except Exception as e:
                upstream_results[i] = (upstream, "OFF", type(e).__name__, "", "")

    rows = []
    ok = 0
    for i, e in enumerate(entries):
        status, detail, kinds, codecs = results.get(i, ("OFF", "missing result", "", ""))
        if status == "OK":
            ok += 1
        up = upstream_results.get(i, ("", "", "", "", ""))
        rows.append([
            channel_name(e[0]), e[-1], status, detail, kinds, codecs,
            up[0], up[1], up[2], up[3], up[4]
        ])

    with REPORT.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "canal", "url", "status", "resultado", "tipos", "codecs",
            "upstream_url", "upstream_status", "upstream_resultado", "upstream_tipos", "upstream_codecs"
        ])
        w.writerows(rows)

    print(f"PLAYBACK_OK={ok} PLAYBACK_OFF={len(entries)-ok} TOTAL={len(entries)}")

if __name__ == "__main__":
    main()
