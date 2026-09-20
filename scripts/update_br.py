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

# Sources are ordered by preference. The updater keeps one healthy stream per
# channel and uses later sources as automatic backups.
SOURCES = [
    ("iptv-org BR", "https://iptv-org.github.io/iptv/countries/br.m3u", None),
    ("dearbulut BR working", "https://dearbulut.github.io/iptv/playlists/country/br.m3u", None),
    ("iptv-com BR", "https://raw.githubusercontent.com/iptv-com/iptv/main/lists/brazil.m3u", None),
    ("Free-TV", "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlist.m3u8", None),
    ("FreeCastHub", "https://raw.githubusercontent.com/freecasthub/public-iptv/main/playlist.m3u", None),

    # FAST providers. These repositories regenerate their playlists frequently.
    ("Pluto BR Buddy", "https://raw.githubusercontent.com/BuddyChewChew/app-m3u-generator/main/playlists/plutotv_br.m3u", None),
    ("Pluto BR OwnerPlugins", "https://raw.githubusercontent.com/OwnerPlugins/pluto-tv-m3u/main/pluto-live-BR.m3u", None),
    ("Samsung TV Plus", "https://raw.githubusercontent.com/BuddyChewChew/app-m3u-generator/main/playlists/samsungtvplus_all.m3u", None),
    ("Plex FAST", "https://raw.githubusercontent.com/BuddyChewChew/app-m3u-generator/main/playlists/plex_all.m3u", None),
    ("Roku FAST", "https://raw.githubusercontent.com/BuddyChewChew/app-m3u-generator/main/playlists/roku_all.m3u", None),
    ("Tubi FAST", "https://raw.githubusercontent.com/BuddyChewChew/app-m3u-generator/main/playlists/tubi_all.m3u", None),

    # Adult list is kept isolated by category and only entries that pass the
    # same health check are published.
    ("IPTVJS Adult", "https://raw.githubusercontent.com/iptvjs/iptv/main/adultiptv_all.m3u", "Adultos"),
]

CHANNELS_DB = "https://raw.githubusercontent.com/iptv-org/database/master/data/channels.csv"
LOGOS_DB = "https://raw.githubusercontent.com/iptv-org/database/master/data/logos.csv"

CATEGORY_MAP = {
    "movies": "Filmes e Series", "series": "Filmes e Series",
    "classic": "Filmes e Series", "comedy": "Filmes e Series",
    "animation": "Desenhos e Animes", "kids": "Desenhos e Animes",
    "sports": "Esportes", "news": "Noticias", "music": "Musica",
    "documentary": "Documentarios e Outros", "education": "Documentarios e Outros",
    "science": "Documentarios e Outros", "culture": "Documentarios e Outros",
    "travel": "Documentarios e Outros", "outdoor": "Documentarios e Outros",
    "lifestyle": "Documentarios e Outros", "entertainment": "Documentarios e Outros",
    "family": "Documentarios e Outros", "religious": "Documentarios e Outros",
    "shop": "Documentarios e Outros", "business": "Documentarios e Outros",
    "cooking": "Documentarios e Outros", "auto": "Documentarios e Outros",
    "weather": "Documentarios e Outros", "public": "Canais Abertos",
    "legislative": "Canais Abertos", "general": "Canais Abertos",
}

NAME_RULES = [
    ("Esportes", r"\b(sport|sports|esporte|futebol|football|soccer|combate|fight|mma|ufc|racing|corrida|caze|nsports|espn|poker|barca|real madrid|wrestling|boxing)\b"),
    ("Noticias", r"\b(news|noticia|jornal|cnn|bandnews|globonews|jovem pan|record news|cnbc|bloomberg|reuters)\b"),
    ("Desenhos e Animes", r"\b(kids?|junior|baby|infantil|crianca|cartoon|animation|animacao|anime|toon|desenho|pokemon|naruto|one piece|gloob|nick|smurfs|popeye|super onze|yu gi oh|teletubbies)\b"),
    ("Filmes e Series", r"\b(movie|movies|cinema|cine|filme|series?|novela|drama|sitcom|megapix|axn|walking dead|rookie blue|star trek|z nation|thriller|horror|romance)\b"),
    ("Musica", r"\b(music|musica|mtv|kpop|trace|vevo|karaoke|radio|concert)\b"),
    ("Documentarios e Outros", r"\b(documentary|documentario|history|historia|nature|natureza|discovery|science|ciencia|travel|viagem|turismo|food|culinaria|cozinha|chef|gospel|relig|igreja|church|canal rural|agro|fish tv|lifestyle)\b"),
]
OPEN_TV_RULE = r"\b(globo|sbt|record|recordtv|band|redetv|tv brasil|tv cultura|gazeta|cultura para|cultura para|aratu|amazon sat|tv bahia)\b"

def download(url, timeout=60):
    req = Request(url, headers={"User-Agent": "feijaum-iptv-updater/5.0"})
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

def category_for(name, metadata, existing="", override=None):
    if override:
        return override
    n = normalize(name)
    if re.search(OPEN_TV_RULE, n, re.I):
        return "Canais Abertos"
    for group, pattern in NAME_RULES:
        if re.search(pattern, n, re.I):
            return group
    cats = [x.strip() for x in (metadata or {}).get("categories", "").split(";") if x.strip()]
    if cats:
        return CATEGORY_MAP.get(cats[0], "Documentarios e Outros")
    e = normalize(existing).replace(" ", "-")
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
        return getattr(r, "status", 200), r.read(4096), (r.headers.get("Content-Type") or "").lower(), r.geturl()

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
            if "#EXTM3U" in text or "mpegurl" in ctype or url.lower().split("?")[0].endswith(".m3u8"):
                child = next((x.strip() for x in text.splitlines() if x.strip() and not x.startswith("#")), "")
                if child:
                    try:
                        c2, d2, t2, _ = _fetch_probe(urljoin(final_url, child), headers, timeout)
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
            time.sleep(0.25)
    return "OFF", last or "failed"

def key_for(entry):
    cid = base_id(entry[0])
    if cid:
        return "id:" + cid.lower()
    return "name:" + normalize(channel_name(entry[0]))

def enrich(entry, channels, logos, override=None):
    cid = base_id(entry[0])
    meta = channels.get(cid, {})
    entry = list(entry)
    entry[0] = set_attr(entry[0], "group-title", category_for(channel_name(entry[0]), meta, attr(entry[0], "group-title"), override))
    if cid in logos and not attr(entry[0], "tvg-logo"):
        entry[0] = set_attr(entry[0], "tvg-logo", logos[cid])
    return entry

def main():
    channels, logos = load_metadata()
    pool = {}
    source_errors = []

    for source_name, url, override in SOURCES:
        try:
            txt = download(url)
            count = 0
            for e in entries(txt):
                if not e or not e[-1].startswith(("http://", "https://")):
                    continue
                e = enrich(e, channels, logos, override)
                key = key_for(e)
                # Avoid exact duplicate URL for same logical channel.
                bucket = pool.setdefault(key, [])
                if all(x[0][-1] != e[-1] for x in bucket):
                    bucket.append((e, source_name))
                    count += 1
            print(f"SOURCE {source_name}: {count} candidates")
        except Exception as exc:
            source_errors.append((source_name, url, type(exc).__name__))
            print(f"WARN source failed: {source_name}: {type(exc).__name__}")

    # Probe the preferred candidate for every channel in parallel.
    keys = list(pool)
    initial = {}
    with ThreadPoolExecutor(max_workers=24) as ex:
        futures = {ex.submit(validate, pool[k][0][0]): k for k in keys}
        for fut in as_completed(futures):
            k = futures[fut]
            try:
                initial[k] = fut.result()
            except Exception as exc:
                initial[k] = ("OFF", type(exc).__name__)

    active, off, report = [], [], []
    seen_urls = set()
    replaced = 0

    for k in keys:
        candidates = pool[k]
        chosen_e, chosen_source = candidates[0]
        status, detail = initial[k]
        original_url = chosen_e[-1]
        original_source = chosen_source

        if status == "OFF":
            for alt_e, alt_source in candidates[1:]:
                astatus, adetail = validate(alt_e)
                if astatus in ("OK", "INCONCLUSIVO"):
                    chosen_e, chosen_source = alt_e, alt_source
                    status, detail = astatus, f"fallback {alt_source}: {adetail}"
                    replaced += 1
                    break

        if status == "OFF":
            off.append(chosen_e)
        else:
            if chosen_e[-1] not in seen_urls:
                seen_urls.add(chosen_e[-1])
                active.append(chosen_e)

        report.append([
            channel_name(chosen_e[0]), base_id(chosen_e[0]), attr(chosen_e[0], "group-title"),
            original_source, original_url, status, detail, chosen_source, chosen_e[-1]
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
        w.writerow(["canal","tvg_id","categoria","fonte_original","url_original","status","resultado","fonte_final","url_final"])
        w.writerows(report)

    with Path("source-status.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["fonte","url","resultado"])
        for n,u,e in source_errors:
            w.writerow([n,u,e])

    groups = {}
    for e in active:
        g = attr(e[0], "group-title") or "Documentarios e Outros"
        groups[g] = groups.get(g, 0) + 1

    print(f"ACTIVE={len(active)} OFF={len(off)} REPLACED={replaced} SOURCES={len(SOURCES)} SOURCE_ERRORS={len(source_errors)}")
    print("GROUPS=" + ", ".join(f"{k}:{v}" for k,v in sorted(groups.items())))

if __name__ == "__main__":
    main()
