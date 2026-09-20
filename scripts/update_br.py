#!/usr/bin/env python3
import csv, io, re, socket
from pathlib import Path
from urllib.request import Request, urlopen

SOURCES = [
 "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/br.m3u",
 "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/br_pluto.m3u",
 "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/br_samsung.m3u",
]
CHANNELS_DB="https://raw.githubusercontent.com/iptv-org/database/master/data/channels.csv"
LOGOS_DB="https://raw.githubusercontent.com/iptv-org/database/master/data/logos.csv"
CATEGORY_PT={"general":"Canais Abertos / Geral","movies":"Filmes","series":"Series","animation":"Desenhos","kids":"Infantil","news":"Noticias","sports":"Esportes","music":"Musica","documentary":"Documentarios","education":"Educacao","lifestyle":"Variedades","entertainment":"Entretenimento","comedy":"Comedia","family":"Familia","religious":"Religiosos","shop":"Compras","business":"Negocios","culture":"Cultura","travel":"Viagens","weather":"Tempo","auto":"Automotivo","cooking":"Culinaria","outdoor":"Natureza / Outdoor","science":"Ciencia","classic":"Classicos"}

# Strong name signals override generic/ambiguous database categories.
NAME_RULES=[
 ("Esportes",r"\b(sport|sports|esporte|futebol|football|soccer|combate|fight|mma|ufc|racing|corrida|motor|surf|skate|poker)\b"),
 ("Noticias",r"\b(news|noticias?|jornal|jovem pan|cnn|record news|bandnews|euronews|bloomberg|reuters)\b"),
 ("Infantil",r"\b(kids?|junior|baby|infantil|crianca|nick jr|discovery kids)\b"),
 ("Desenhos",r"\b(cartoon|animation|animacao|anime|toon|desenhos?|naruto|pokemon)\b"),
 ("Filmes",r"\b(movie|movies|cinema|cine|filmes?|film|action movies|horror|terror)\b"),
 ("Series",r"\b(series?|novelas?|drama|sitcom|soap)\b"),
 ("Documentarios",r"\b(documentary|documentario|history|historia|nature|natureza|discovery|science|ciencia)\b"),
 ("Musica",r"\b(music|musica|mtv|radio|sertanejo|rock|pop|trace|vevo)\b"),
 ("Religiosos",r"\b(gospel|relig|igreja|church|catolic|crista|cristao|biblia|fe\b|canção nova|cancao nova|rede vida)\b"),
 ("Culinaria",r"\b(food|comida|culinaria|cozinha|cooking|receita)\b"),
 ("Automotivo",r"\b(auto|carros?|motor|automot|garage)\b"),
 ("Viagens",r"\b(travel|viagem|turismo|trip)\b"),
 ("Compras",r"\b(shop|shopping|vendas?|ofertas?)\b"),
]

def download(url,timeout=120):
 req=Request(url,headers={"User-Agent":"feijaum-iptv-updater/3.0"})
 with urlopen(req,timeout=timeout) as r:return r.read().decode("utf-8-sig")

def entries(text):
 cur=[]
 for raw in text.replace("\r","").split("\n"):
  line=raw.strip()
  if not line or line=="#EXTM3U":continue
  if line.startswith("#EXTINF:"):cur=[line]
  elif cur and line.startswith("#"):cur.append(line)
  elif cur:cur.append(line);yield cur;cur=[]

def attr(line,key):
 m=re.search(rf'{re.escape(key)}="([^"]*)"',line);return m.group(1) if m else ""

def base_id(line):return attr(line,"tvg-id").split("@",1)[0]

def channel_name(line):
 return line.split(",",1)[1].strip() if "," in line else base_id(line)

def clean_name(name):
 return re.sub(r"\s*\([^)]*\)|\s*\[[^]]*\]"," ",name).lower()

def set_attr(line,key,value):
 value=(value or "").replace('"',"'"); p=rf'\s{re.escape(key)}="[^"]*"'
 if re.search(p,line):return re.sub(p,f' {key}="{value}"',line,count=1)
 i=line.find(",");return line[:i]+f' {key}="{value}"'+line[i:] if i>=0 else line+f' {key}="{value}"'

def load_metadata():
 channels={r["id"]:r for r in csv.DictReader(io.StringIO(download(CHANNELS_DB)))}
 logos={}
 for r in csv.DictReader(io.StringIO(download(LOGOS_DB))):
  ch=r.get("channel",""); url=r.get("url","")
  if not ch or not url or r.get("in_use","").upper()!="TRUE":continue
  score=2 if r.get("format","").upper() in ("PNG","JPG","JPEG","WEBP") else 1
  if ch not in logos or score>logos[ch][0]:logos[ch]=(score,url)
 return channels,{k:v[1] for k,v in logos.items()}

def category_for(name,metadata):
 n=clean_name(name)
 for group,pattern in NAME_RULES:
  if re.search(pattern,n,re.I):return group
 cats=[x.strip() for x in (metadata or {}).get("categories","").split(";") if x.strip()]
 return CATEGORY_PT.get(cats[0],cats[0].replace("-"," ").title()) if cats else "Outros"

def headers_for(entry):
 h={"User-Agent":"Mozilla/5.0","Range":"bytes=0-2047"}
 for line in entry[1:-1]:
  if line.startswith("#EXTVLCOPT:http-referrer="):h["Referer"]=line.split("=",1)[1]
  elif line.startswith("#EXTVLCOPT:http-user-agent="):h["User-Agent"]=line.split("=",1)[1]
 return h

def validate(entry,timeout=8):
 url=entry[-1]
 if not url.startswith(("http://","https://")):return False,"unsupported"
 try:
  req=Request(url,headers=headers_for(entry))
  with urlopen(req,timeout=timeout) as r:
   code=getattr(r,"status",200); data=r.read(2048)
   ctype=(r.headers.get("Content-Type") or "").lower()
   ok=200<=code<400 and (data or "mpegurl" in ctype or "video" in ctype or "octet-stream" in ctype)
   return ok,str(code)
 except Exception as e:
  return False,type(e).__name__

def main():
 channels,logos=load_metadata();seen=set();merged=[]
 for src in SOURCES:
  for e in entries(download(src)):
   if e[-1] in seen:continue
   seen.add(e[-1]); cid=base_id(e[0]); meta=channels.get(cid,{})
   e[0]=set_attr(e[0],"group-title",category_for(channel_name(e[0]),meta))
   if cid in logos:e[0]=set_attr(e[0],"tvg-logo",logos[cid])
   merged.append(e)

 # Validate but do NOT delete failing streams automatically: temporary geo/CDN/network
 # failures are common. A report is generated for review, preserving every channel.
 rows=[];working=0
 for i,e in enumerate(merged,1):
  ok,status=validate(e)
  working+=int(ok)
  rows.append([channel_name(e[0]),base_id(e[0]),attr(e[0],"group-title"),e[-1],"OK" if ok else "FALHOU",status])
  print(f"[{i}/{len(merged)}] {'OK' if ok else 'FAIL'} {channel_name(e[0])}")

 Path("br.m3u").write_text("#EXTM3U\n"+"\n".join("\n".join(e) for e in merged)+"\n",encoding="utf-8")
 with Path("stream-status.csv").open("w",encoding="utf-8",newline="") as f:
  w=csv.writer(f);w.writerow(["canal","tvg_id","categoria","url","status","resultado"]);w.writerows(rows)
 print(f"Generated {len(merged)} streams; validation: {working} OK, {len(merged)-working} failed. No failed stream was removed.")

if __name__=="__main__":main()
