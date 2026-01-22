import os
import time
import re
import base64
import urllib.parse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import feedparser
import requests
from bs4 import BeautifulSoup

from flask import Flask, jsonify, send_file, request, Response
from flask_cors import CORS

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)

# -------------------- İstanbul İlçeleri (Filtre için) --------------------
IST_DISTRICTS = [
    "adalar","arnavutköy","ataşehir","avcılar","bağcılar","bahçelievler","bakırköy",
    "başakşehir","bayrampaşa","beşiktaş","beykoz","beylikdüzü","beyoğlu","büyükçekmece",
    "çatalca","çekmeköy","esenler","esenyurt","eyüpsultan","fatih","gaziosmanpaşa",
    "güngören","kadıköy","kağıthane","kartal","küçükçekmece","maltepe","pendik",
    "sancaktepe","sarıyer","silivri","sultanbeyli","sultangazi","şile","şişli",
    "tuzla","ümraniye","üsküdar","zeytinburnu"
]

def is_istanbul_related(title: str, link: str = "") -> bool:
    t = (title or "").lower()
    l = (link or "").lower()

    # link içinde de istanbul geçiyorsa kabul
    if "istanbul" in t or "i̇stanbul" in t or "istanbul" in l:
        return True

    # ilçe adı title içinde geçiyorsa kabul
    return any(d in t for d in IST_DISTRICTS)

# -------------------- HOME (INDEX) --------------------
@app.route("/")
def home():
    return send_file(os.path.join(BASE_DIR, "index.html"))

# -------------------- HTTP SESSION (Connection Pooling) --------------------
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
})

# -------------------- IMAGE PROXY (Hotlink/CORS azaltır) --------------------
@app.route("/img")
def img_proxy():
    u = request.args.get("u", "")
    if not u:
        return ("", 400)
    if not (u.startswith("http://") or u.startswith("https://")):
        return ("", 400)

    try:
        r = SESSION.get(u, timeout=6, stream=True, allow_redirects=True)
        if r.status_code >= 400:
            return ("", 404)

        ctype = r.headers.get("Content-Type", "image/jpeg")
        return Response(r.content, headers={
            "Content-Type": ctype,
            "Cache-Control": "public, max-age=86400"
        })
    except:
        return ("", 404)

# -------------------- SIMPLE TTL CACHE --------------------
CACHE = {}
CACHE_TTL = 180  # 3 dk

def cache_get(key):
    now = time.time()
    item = CACHE.get(key)
    if not item:
        return None
    exp, data = item
    if now > exp:
        CACHE.pop(key, None)
        return None
    return data

def cache_set(key, data, ttl=CACHE_TTL):
    CACHE[key] = (time.time() + ttl, data)

# -------------------- HELPERS --------------------
def decode_url(source_url):
    """Google News maskeli linkleri çözmeyi dener."""
    try:
        if "news.google.com" not in source_url or "articles/" not in source_url:
            return source_url
        token = source_url.split("articles/")[1].split("?")[0]
        decoded = base64.b64decode(token + "===")
        text = decoded.decode("utf-8", errors="ignore")
        match = re.search(r"(https?://[^\s|\"'>]+)", text)
        return match.group(1) if match else source_url
    except:
        return source_url

def pick_image_from_entry(entry):
    """
    RSS içinden görsel yakala:
    - media_content / media_thumbnail
    - enclosure
    - summary içindeki img (src/data-src)
    """
    try:
        mc = entry.get("media_content") or getattr(entry, "media_content", None)
        if mc:
            u = mc[0].get("url")
            if u:
                return u

        mt = entry.get("media_thumbnail") or getattr(entry, "media_thumbnail", None)
        if mt:
            u = mt[0].get("url")
            if u:
                return u

        links = entry.get("links") or getattr(entry, "links", None)
        if links:
            for l in links:
                if l.get("rel") == "enclosure" and (l.get("type","").startswith("image/")) and l.get("href"):
                    return l["href"]

        summary = entry.get("summary") or getattr(entry, "summary", "") or ""
        if summary and "<img" in summary:
            soup = BeautifulSoup(summary, "html.parser")
            img = soup.find("img")
            if img:
                return img.get("src") or img.get("data-src") or img.get("data-lazy-src")
    except:
        pass

    return None

def get_real_image(url):
    """Sayfaya gidip og:image / twitter:image kazır (gerektiğinde)."""
    try:
        if not url:
            return None

        r = SESSION.get(url, timeout=4.0, allow_redirects=True)
        if r.status_code >= 400:
            return None

        html = r.text[:140000]
        soup = BeautifulSoup(html, "html.parser")

        og = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "twitter:image"})
        if og and og.get("content"):
            return og["content"]

        img = soup.find("img")
        if img:
            return img.get("src") or img.get("data-src") or img.get("data-lazy-src")
    except:
        return None

    return None

def format_time(entry):
    try:
        pp = entry.get("published_parsed") or getattr(entry, "published_parsed", None)
        if pp:
            return datetime(*pp[:6]).strftime("%H:%M")
    except:
        pass
    return "--:--"

# -------------------- CORE --------------------
def fetch_google_news(query, district, limit=30, strict_istanbul=False):
    cache_key = f"{district}:{query}:{limit}:{strict_istanbul}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    encoded = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={encoded}&hl=tr&gl=TR&ceid=TR:tr"
    feed = feedparser.parse(url)

    entries = sorted(
        getattr(feed, "entries", []),
        key=lambda x: x.get("published_parsed") or getattr(x, "published_parsed", 0),
        reverse=True
    )[:limit]

    results = []
    scrape_queue = []

    for e in entries:
        raw_title = (e.get("title") or getattr(e, "title", "") or "")
        title = raw_title.rsplit(" - ", 1)[0]

        raw_link = e.get("link") or getattr(e, "link", "") or ""
        real_link = decode_url(raw_link)

        # İstanbul strict filtresi (title + link)
        if strict_istanbul and (not is_istanbul_related(title, real_link)):
            continue

        img = pick_image_from_entry(e)

        source = "Haber"
        src_obj = e.get("source") or getattr(e, "source", None)
        if src_obj:
            try:
                source = src_obj.get("title", "Haber")
            except:
                pass

        results.append({
            "title": title,
            "link": real_link,
            "image": img,
            "source": source,
            "date": format_time(e),
            "district": district
        })

    # Görsel scrape: sadece ilk 12 haber
    for idx, item in enumerate(results[:12]):
        if not item.get("image"):
            scrape_queue.append((idx, item.get("link")))

    if scrape_queue:
        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = {ex.submit(get_real_image, link): idx for idx, link in scrape_queue}
            for fut in as_completed(futures):
                idx = futures[fut]
                try:
                    img = fut.result()
                    if img:
                        if img.startswith("http://"):
                            img = img.replace("http://", "https://", 1)
                        results[idx]["image"] = img
                except:
                    pass

    cache_set(cache_key, results)
    return results

# -------------------- ROUTES --------------------
@app.route("/get-news/<district>")
def district_news(district):
    query = f'"{district}" İstanbul yerel haberleri'
    if district == "Beşiktaş":
        query += " -transfer -maç -stadyum -futbol"
    if district == "Avcılar":
        query += " -avcılık -avcı -tüfek"

    return jsonify(fetch_google_news(query, district, 30, strict_istanbul=True))

@app.route("/get-breaking")
def breaking_news():
    # İstanbul'a kilitle + şehirleri negatifle (Google bazen sapıtıyor)
    q = (
        '"İstanbul" (son dakika OR belediye OR asayiş OR kaza OR trafik OR yangın OR operasyon OR gözaltı) '
        '-Bursa -Ankara -İzmir -Antalya -Adana -Konya -Kayseri -Gaziantep -Sakarya -Kocaeli -Edirne -Tekirdağ -Eskişehir'
    )

    data = fetch_google_news(q, "İstanbul", 70, strict_istanbul=True)

    # ekstra güvenlik: yine de kaçarsa temizle
    data = [x for x in data if is_istanbul_related(x.get("title"), x.get("link"))]

    return jsonify(data)

if __name__ == "__main__":
    app.run(port=5000, debug=False)
