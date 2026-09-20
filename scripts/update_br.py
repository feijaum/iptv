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
    ("iptv-org BR", "https://iptv-org.github.io/iptv/countries/br.m3u", None, True, False),
    ("dearbulut BR working", "https://dearbulut.github.io/iptv/playlists/country/br.m3u", None, True, False),
    ("iptv-com BR", "https://raw.githubusercontent.com/iptv-com/iptv/main/lists/brazil.m3u", None, True, False),
    ("Free-TV", "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlist.m3u8", None, False, False),
    ("FreeCastHub", "https://raw.githubusercontent.com/freecasthub/public-iptv/main/playlist.m3u", None, False, False),

    # FAST providers. Brazilian feeds are accepted in full; global feeds are
    # filtered to Brazil/Portuguese before validation.
    ("Pluto BR Buddy", "https://raw.githubusercontent.com/BuddyChewChew/app-m3u-generator/main/playlists/plutotv_br.m3u", None, True, False),
    ("Pluto BR OwnerPlugins", "https://raw.githubusercontent.com/OwnerPlugins/pluto-tv-m3u/main/pluto-live-BR.m3u", None, True, False),
    ("Samsung TV Plus", "https://raw.githubusercontent.com/BuddyChewChew/app-m3u-generator/main/playlists/samsungtvplus_all.m3u", None, False, False),
    ("Plex FAST", "https://raw.githubusercontent.com/BuddyChewChew/app-m3u-generator/main/playlists/plex_all.m3u", None, False, False),
    ("Roku FAST", "https://raw.githubusercontent.com/BuddyChewChew/app-m3u-generator/main/playlists/roku_all.m3u", None, False, False),
    ("Tubi FAST", "https://raw.githubusercontent.com/BuddyChewChew/app-m3u-generator/main/playlists/tubi_all.m3u", None, False, False),

    # Adult source is also filtered to Portuguese/Brazil signals.
    ("IPTVJS Adult", "https://raw.githubusercontent.com/iptvjs/iptv/main/adultiptv_all.m3u", "Adultos", False, True),
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

def _looks_like_media(data, ctype):
    if not data:
        return False
    ct = (ctype or "").lower()
    if "text/html" in ct or data[:64].lstrip().lower().startswith((b"<html", b"<!doctype html")):
        return False
    return (
        "video" in ct or "audio" in ct or "octet-stream" in ct or
        data.startswith((b"\x47", b"ID3")) or len(data) >= 1024
    )

def _validate_hls_url(url, headers, timeout, depth=0):
    if depth > 3:
        return "OFF", "hls depth"
    try:
        code, data, ctype, final_url = _fetch_probe(url, headers, timeout)
    except HTTPError as e:
        if e.code in (401, 403, 451):
            return "INCONCLUSIVO", f"geo/auth {e.code}"
        return "OFF", f"HTTP {e.code}"
    except Exception as e:
        return "OFF", type(e).__name__

    if not (200 <= code < 400):
        return "OFF", str(code)

    text = data.decode("utf-8", errors="ignore")
    is_manifest = "#EXTM3U" in text or "mpegurl" in ctype or url.lower().split("?")[0].endswith(".m3u8")
    if not is_manifest:
        return ("OK", str(code)) if _looks_like_media(data, ctype) else ("OFF", "non-media response")

    children = [x.strip() for x in text.splitlines() if x.strip() and not x.startswith("#")]
    if not children:
        return "OFF", "empty hls"

    # Try more than one child because the first rendition/segment can be temporarily unavailable.
    last_status, last_detail = "OFF", "no playable child"
    for child in children[:3]:
        child_url = urljoin(final_url, child)
        child_is_playlist = child.lower().split("?")[0].endswith(".m3u8")
        if child_is_playlist:
            st, detail = _validate_hls_url(child_url, headers, timeout, depth + 1)
        else:
            try:
                c2, d2, t2, _ = _fetch_probe(child_url, headers, timeout)
                st = "OK" if 200 <= c2 < 400 and _looks_like_media(d2, t2) else "OFF"
                detail = f"{code}/{c2}" if st == "OK" else f"segment {c2}"
            except HTTPError as e:
                st = "INCONCLUSIVO" if e.code in (401, 403, 451) else "OFF"
                detail = f"geo/auth {e.code}" if st == "INCONCLUSIVO" else f"HTTP {e.code}"
            except Exception as e:
                st, detail = "OFF", type(e).__name__
        if st == "OK":
            return st, detail
        if st == "INCONCLUSIVO":
            last_status, last_detail = st, detail
        elif last_status != "INCONCLUSIVO":
            last_status, last_detail = st, detail
    return last_status, last_detail

def validate(entry, timeout=7):
    url = entry[-1]
    if not url.startswith(("http://", "https://")):
        return "OFF", "unsupported"
    headers = headers_for(entry)
    last = ("OFF", "failed")
    for attempt in range(2):
        last = _validate_hls_url(url, headers, timeout)
        if last[0] == "OK":
            return last
        if attempt == 0:
            time.sleep(0.25)
    return last

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


def is_ptbr_entry(entry, source_is_br=False):
    if source_is_br:
        return True
    line = entry[0]
    name = channel_name(line)
    hay = " ".join([
        attr(line, "tvg-id"),
        attr(line, "channel-id"),
        attr(line, "tvg-language"),
        attr(line, "group-title"),
        attr(line, "tvg-country"),
        name,
    ])
    n = normalize(hay)

    # Strong Brazil / Brazilian Portuguese signals used by FAST playlists.
    if re.search(r"(^| )(br|brazil|brasil|brazilian|portuguese|portugues|pt br|ptbr)( |$)", n):
        return True

    # Common FAST ids use a regional suffix such as "-br" or "@BR".
    raw_id = " ".join([attr(line, "tvg-id"), attr(line, "channel-id")]).lower()
    if re.search(r"(@br|@brazil|[-_.]br)(\b|$)", raw_id):
        return True

    # Portuguese-language labels occasionally use locale notation.
    raw_lang = attr(line, "tvg-language").lower()
    if raw_lang in ("pt", "pt-br", "por", "portuguese", "português"):
        return True

    return False

def main():
    channels, logos = load_metadata()
    pool = {}
    source_errors = []

    for source_name, url, override, source_is_br, nsfw_unrestricted in SOURCES:
        try:
            txt = download(url)
            count = 0
            skipped_language = 0
            for e in entries(txt):
                if not e or not e[-1].startswith(("http://", "https://")):
                    continue
                if not nsfw_unrestricted and not is_ptbr_entry(e, source_is_br):
                    skipped_language += 1
                    continue
                e = enrich(e, channels, logos, override)
                key = key_for(e)
                # Avoid exact duplicate URL for same logical channel.
                bucket = pool.setdefault(key, [])
                if all(x[0][-1] != e[-1] for x in bucket):
                    bucket.append((e, source_name))
                    count += 1
            mode = "NSFW unrestricted" if nsfw_unrestricted else ("BR source" if source_is_br else "PT-BR filtered")
            print(f"SOURCE {source_name}: {count} candidates; skipped_non_ptbr={skipped_language}; mode={mode}")
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

        if status != "OK":
            for alt_e, alt_source in candidates[1:]:
                astatus, adetail = validate(alt_e)
                if astatus == "OK":
                    chosen_e, chosen_source = alt_e, alt_source
                    status, detail = astatus, f"fallback {alt_source}: {adetail}"
                    replaced += 1
                    break

        if status != "OK":
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
