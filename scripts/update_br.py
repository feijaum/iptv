#!/usr/bin/env python3
import csv
import io
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from urllib.error import HTTPError

PRIMARY_SOURCE = "https://iptv-org.github.io/iptv/countries/br.m3u"

# Alternative public/free sources. They are used first as replacement pools for
# channels whose primary URL is broken. We do not blindly import paid/pirated feeds.
FALLBACK_SOURCES = [
    "https://iptv-org.github.io/iptv/sources/br.m3u",
    "https://iptv-org.github.io/iptv/sources/br_pluto.m3u",
    "https://iptv-org.github.io/iptv/sources/br_samsung.m3u",
    "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlist.m3u8",
    "https://raw.githubusercontent.com/freecasthub/public-iptv/main/playlist.m3u",
]

CHANNELS_DB = "https://raw.githubusercontent.com/iptv-org/database/master/data/channels.csv"
LOGOS_DB = "https://raw.githubusercontent.com/iptv-org/database/master/data/logos.csv"

# Compact categories requested for the player.
CATEGORY_MAP = {
    "movies": "Filmes e Series",
    "series": "Filmes e Series",
    "classic": "Filmes e Series",
    "comedy": "Filmes e Series",
    "animation": "Desenhos e Animes",
    "kids": "Desenhos e Animes",
    "sports": "Esportes",
    "news": "Noticias",
    "music": "Musica",
    "documentary": "Documentarios e Outros",
    "education": "Documentarios e Outros",
    "science": "Documentarios e Outros",
    "culture": "Documentarios e Outros",
    "travel": "Documentarios e Outros",
    "outdoor": "Documentarios e Outros",
    "lifestyle": "Documentarios e Outros",
    "entertainment": "Documentarios e Outros",
    "family": "Documentarios e Outros",
    "religious": "Documentarios e Outros",
    "shop": "Documentarios e Outros",
    "business": "Documentarios e Outros",
    "cooking": "Documentarios e Outros",
    "auto": "Documentarios e Outros",
    "weather": "Documentarios e Outros",
    "public": "Canais Abertos",
    "legislative": "Canais Abertos",
    "general": "Canais Abertos",
}

NAME_RULES = [
    ("Esportes", r"\b(sport|sports|esporte|futebol|football|soccer|combate|fight|mma|ufc|racing|corrida|caze|nsports|espn|poker|barca|real madrid)\b"),
    ("Noticias", r"\b(news|noticia|jornal|cnn|bandnews|globonews|jovem pan|record news|cnbc|bloomberg|reuters)\b"),
    ("Desenhos e Animes", r"\b(kids?|junior|baby|infantil|crianca|cartoon|animation|animacao|anime|toon|desenho|pokemon|naruto|one piece|gloob|nick|smurfs|popeye|super onze|yu-gi-oh|teletubbies)\b"),
    ("Filmes e Series", r"\b(movie|movies|cinema|cine|filme|series?|novela|drama|sitcom|megapix|axn|walking dead|rookie blue|star trek|z nation)\b"),
    ("Musica", r"\b(music|musica|mtv|kpop|trace|vevo|karaoke|radio)\b"),
    ("Documentarios e Outros", r"\b(documentary|documentario|history|historia|nature|natureza|discovery|science|ciencia|travel|viagem|turismo|food|culinaria|cozinha|chef|gospel|relig|igreja|church|canal rural|agro|fish tv)\b"),
]

OPEN_TV_RULE = r"\b(globo|sbt|record|recordtv|band|redetv|tv brasil|tv cultura|gazeta|cultura para|cultura par[aá]|aratu|amazon sat|tv bahia)\b"

def download(url, timeout=45):
    req = Request(url, headers={"User-Agent": "feijaum-iptv-updater/4.0"})
    with urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8-sig", errors="replace")

def entries(text):
    cur = []
    for raw in text.replace("\r", "").split("\n"):
        line = raw.strip()
        if not line or line == "#EXTM3U":
            continue
        if line.startswith("#EXTINF:"):
            if cur:
                yield cur
            cur = [line]
        elif cur and line.startswith("#"):
            cur.append(line)
        elif cur:
            cur.append(line)
            yield cur
            cur = []
    if cur:
        yield cur

def attr(line, key):
    m = re.search(rf'{re.escape(key)}="([^"]*)"', line)
    return m.group(1) if m else ""

def set_attr(line, key, value):
    value = (value or "").replace('"', "'")
    p = rf'\s{re.escape(key)}="[^"]*"'
    if re.search(p, line):
        return re.sub(p, f' {key}="{value}"', line, count=1)
    i = line.find(",")
    return line[:i] + f' {key}="{value}"' + line[i:] if i >= 0 else line + f' {key}="{value}"'

def base_id(line):
    return attr(line, "tvg-id").split("@", 1)[0]

def channel_name(line):
    return line.split(",", 1)[1].strip() if "," in line else base_id(line)

def normalize(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"\s*\([^)]*\)|\s*\[[^]]*\]", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())

def load_metadata():
    channels = {r["id"]: r for r in csv.DictReader(io.StringIO(download(CHANNELS_DB)))}
    logos = {}
    for r in csv.DictReader(io.StringIO(download(LOGOS_DB))):
        ch, url = r.get("channel", ""), r.get("url", "")
        if not ch or not url or r.get("in_use", "").upper() != "TRUE":
            continue
        score = 2 if r.get("format", "").upper() in ("PNG", "JPG", "JPEG", "WEBP") else 1
        if ch not in logos or score > logos[ch][0]:
            logos[ch] = (score, url)
    return channels, {k: v[1] for k, v in logos.items()}

def category_for(name, metadata, existing=""):
    n = normalize(name)
    if re.search(OPEN_TV_RULE, n, re.I):
        return "Canais Abertos"
    for group, pattern in NAME_RULES:
        if re.search(pattern, n, re.I):
            return group
    cats = [x.strip() for x in (metadata or {}).get("categories", "").split(";") if x.strip()]
    if cats:
        return CATEGORY_MAP.get(cats[0], "Documentarios e Outros")
    e = (existing or "").strip().lower()
    return CATEGORY_MAP.get(e, "Documentarios e Outros")

def headers_for(entry):
    h = {"User-Agent": "Mozilla/5.0", "Range": "bytes=0-4095"}
    for line in entry[1:-1]:
        if line.startswith("#EXTVLCOPT:http-referrer="):
            h["Referer"] = line.split("=", 1)[1]
        elif line.startswith("#EXTVLCOPT:http-user-agent="):
            h["User-Agent"] = line.split("=", 1)[1]
    return h

def _fetch_probe(url, headers, timeout):
    req = Request(url, headers=headers)
    with urlopen(req, timeout=timeout) as r:
        code = getattr(r, "status", 200)
        data = r.read(4096)
        ctype = (r.headers.get("Content-Type") or "").lower()
        return code, data, ctype, r.geturl()

def validate(entry, timeout=7):
    url = entry[-1]
    if not url.startswith(("http://", "https://")):
        return "OFF", "unsupported"
    headers = headers_for(entry)
    last = ""
    for attempt in range(2):
        try:
            code, data, ctype, final_url = _fetch_probe(url, headers, timeout)
            text = data.decode("utf-8", errors="ignore")
            if not (200 <= code < 400):
                return "OFF", str(code)

            # HLS: validate one child URI as well, so a dead master playlist does
            # not count as healthy just because the manifest itself returns 200.
            if "#EXTM3U" in text or "mpegurl" in ctype or url.lower().split("?")[0].endswith(".m3u8"):
                child = next((x.strip() for x in text.splitlines() if x.strip() and not x.startswith("#")), "")
                if child:
                    child_url = urljoin(final_url, child)
                    try:
                        c2, d2, t2, _ = _fetch_probe(child_url, headers, timeout)
                        if 200 <= c2 < 400 and (d2 or "mpegurl" in t2 or "video" in t2 or "octet-stream" in t2):
                            return "OK", f"{code}/{c2}"
                    except HTTPError as e:
                        if e.code in (401, 403, 451):
                            return "INCONCLUSIVO", f"geo/auth {e.code}"
                    except Exception as e:
                        last = type(e).__name__
                elif data:
                    return "OK", str(code)
            elif data or "video" in ctype or "octet-stream" in ctype:
                return "OK", str(code)
        except HTTPError as e:
            if e.code in (401, 403, 451):
                return "INCONCLUSIVO", f"geo/auth {e.code}"
            last = f"HTTP {e.code}"
        except Exception as e:
            last = type(e).__name__
        if attempt == 0:
            time.sleep(0.35)
    return "OFF", last or "failed"

def key_for(entry):
    cid = base_id(entry[0])
    if cid:
        return ("id", cid.lower())
    return ("name", normalize(channel_name(entry[0])))

def build_fallback_pool():
    by_id, by_name = {}, {}
    for src in FALLBACK_SOURCES:
        try:
            text = download(src)
        except Exception as e:
            print(f"WARN fallback source failed: {src}: {type(e).__name__}")
            continue
        for e in entries(text):
            if not e or not e[-1].startswith(("http://", "https://")):
                continue
            cid = base_id(e[0]).lower()
            name = normalize(channel_name(e[0]))
            if cid:
                by_id.setdefault(cid, []).append(e)
            if name:
                by_name.setdefault(name, []).append(e)
    return by_id, by_name

def enrich(entry, channels, logos):
    cid = base_id(entry[0])
    meta = channels.get(cid, {})
    old_group = attr(entry[0], "group-title")
    entry[0] = set_attr(entry[0], "group-title", category_for(channel_name(entry[0]), meta, old_group))
    if cid in logos:
        entry[0] = set_attr(entry[0], "tvg-logo", logos[cid])
    return entry

def candidate_replacements(entry, by_id, by_name):
    cid = base_id(entry[0]).lower()
    name = normalize(channel_name(entry[0]))
    seen = {entry[-1]}
    out = []
    for e in (by_id.get(cid, []) if cid else []) + by_name.get(name, []):
        if e[-1] not in seen:
            seen.add(e[-1])
            out.append(e)
    return out

def main():
    channels, logos = load_metadata()
    primary = [enrich(e, channels, logos) for e in entries(download(PRIMARY_SOURCE))]
    by_id, by_name = build_fallback_pool()

    # Deduplicate only exact stream URLs in the primary list.
    deduped, seen_urls = [], set()
    for e in primary:
        if e[-1] in seen_urls:
            continue
        seen_urls.add(e[-1])
        deduped.append(e)

    print(f"Primary channels: {len(deduped)}")
    results = [None] * len(deduped)
    with ThreadPoolExecutor(max_workers=18) as ex:
        futs = {ex.submit(validate, e): i for i, e in enumerate(deduped)}
        for fut in as_completed(futs):
            i = futs[fut]
            try:
                results[i] = fut.result()
            except Exception as e:
                results[i] = ("OFF", type(e).__name__)

    active, off, report = [], [], []
    replaced = 0

    for i, e in enumerate(deduped):
        status, detail = results[i]
        chosen = e

        if status == "OFF":
            for alt in candidate_replacements(e, by_id, by_name):
                alt = enrich(alt, channels, logos)
                astatus, adetail = validate(alt)
                if astatus in ("OK", "INCONCLUSIVO"):
                    # Keep original display metadata but swap option lines/url from
                    # the healthy alternative where needed.
                    chosen = [e[0]] + alt[1:]
                    status, detail = astatus, f"substituido: {adetail}"
                    replaced += 1
                    break

        if status == "OFF":
            off.append(e)
        else:
            active.append(chosen)

        report.append([
            channel_name(e[0]), base_id(e[0]), attr(e[0], "group-title"),
            e[-1], status, detail, chosen[-1] if chosen else ""
        ])

    Path("br.m3u").write_text(
        "#EXTM3U\n" + "\n".join("\n".join(e) for e in active) + "\n",
        encoding="utf-8"
    )
    Path("linksoff.m3u").write_text(
        "#EXTM3U\n" + ("\n".join("\n".join(e) for e in off) + "\n" if off else ""),
        encoding="utf-8"
    )
    with Path("stream-status.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["canal","tvg_id","categoria","url_original","status","resultado","url_final"])
        w.writerows(report)

    groups = {}
    for e in active:
        g = attr(e[0], "group-title") or "Documentarios e Outros"
        groups[g] = groups.get(g, 0) + 1

    print(f"ACTIVE={len(active)} OFF={len(off)} REPLACED={replaced}")
    print("GROUPS=" + ", ".join(f"{k}:{v}" for k,v in sorted(groups.items())))

if __name__ == "__main__":
    main()
