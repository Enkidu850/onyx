
# Nécessite deux variables d'environnement (dans le .env) :
# GOOGLE_API_KEY : clef d'API (Google Cloud > APIs & Services)
# GOOGLE_CSE_ID : identifiant du moteur (cx) créé dans Programmable Search Engine

from __future__ import annotations

import os
import re
import time
import html
import requests
from dotenv import load_dotenv
from typing import Any, Dict, Tuple
from urllib.parse import urlparse, unquote
from flask import Flask, request, render_template, redirect, url_for

load_dotenv()

API_KEY = os.getenv("GOOGLE_API_KEY")
CX = os.getenv("GOOGLE_CSE_ID")
MAIL = os.getenv("MAIL")

app = Flask(__name__)

# Cache de la mémoire
CacheKey = Tuple[str, int, str]
_cache: Dict[CacheKey, Tuple[float, Dict[str, Any]]] = {}
CACHE_TTL_SEC = 60  # 1mn

# Compteur de requêtes par jour
_request_counter: Dict[str, int] = {"date": time.strftime("%Y-%m-%d"), "count": 0}

def _cache_get(key: CacheKey):
    item = _cache.get(key)
    if not item:
        return None
    ts, payload = item
    if time.time() - ts > CACHE_TTL_SEC:
        _cache.pop(key, None)
        return None
    return payload


def _cache_set(key: CacheKey, value: Dict[str, Any]):
    _cache[key] = (time.time(), value)


# Appel de l'API Google Search
GOOGLE_SEARCH_ENDPOINT = "https://customsearch.googleapis.com/customsearch/v1"


def google_search(query: str, start_index: int = 1, num: int = 10, search_type: str | None = None) -> Dict[str, Any]:
    if not API_KEY or not CX:
        raise RuntimeError(
            "Variables d'environnement manquantes: GOOGLE_API_KEY et GOOGLE_CSE_ID"
        )

    start_index = max(1, min(start_index, 91)) # Google limite start à 91 pour num = 10
    num = max(1, min(num, 10))  # max 10 par appel

    key: CacheKey = (query + f"|{num}", start_index, search_type or "web")
    cached = _cache_get(key)
    if cached:
        return cached

    params = {
        "key": API_KEY,
        "cx": CX,
        "q": query,
        "num": num,
        "start": start_index
    }

    if search_type:
        params["searchType"] = search_type
    r = requests.get(GOOGLE_SEARCH_ENDPOINT, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()

    # Incrémente le compteur
    today = time.strftime("%Y-%m-%d")
    if _request_counter["date"] != today:
        _request_counter["date"] = today
        _request_counter["count"] = 0
    _request_counter["count"] += 1
    _cache_set(key, data)
    return data

def osm_search(query: str) -> Dict[str, Any] | None:
    """
        Recherche un lieu avec Nominatim (OpenStreetMap)
        Retourne des infos basiques et les coordonnées ou tout simplement rien du tout si c'est pas pertinent
    """

    url = "https://nominatim.openstreetmap.org/search"
    params = {
        "q": query,
        "format": "json",
        "addressdetails": 1,
        "limit": 1,
        "extratags": 1
    }
    headers = {"User-Agent": "MiniMetaSearch/1.0 (contact: "+MAIL+")"}

    try:
        r = requests.get(url, params=params, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()

        if not data:
            return None

        result = data[0]

        # Filtrage par type
        allowed_classes = {
            "place", "boundary", "building",
            "amenity", "tourism", "highway", "shop",
            "leisure", "natural", "historic"
        }
        if result.get("class") not in allowed_classes:
            return None

        # Filtrage par importance
        if float(result.get("importance", 0)) < 0.3:
            return None

        return result

    except Exception:
        return None

@app.route("/")
def home():
    q = request.args.get("q", "").strip()
    start = request.args.get("start", "1")
    try:
        start_index = int(start)
    except ValueError:
        start_index = 1

    results = []
    info = None
    error = None
    prev_start = None
    next_start = None
    wiki_box = None
    osm_box = None

    if q:
        try:
            data = google_search(q, start_index=start_index, num=10)
            info = data.get("searchInformation") or {}
            items = data.get("items") or []
            for it in items:
                link = it.get("link", "#")
                domain = urlparse(link).netloc
                favicon = "https://www.google.com/s2/favicons?sz=32&domain="+domain
                results.append({
                    "title": it.get("title", "(sans titre)"),
                    "link": link,
                    "displayLink": it.get("displayLink", ""),
                    "snippet": it.get("snippet", ""),
                    "favicon": favicon,
                })
                
            def shorten_extract(text, max_sentences=3):
                # Garde seulement les 3 premières phrases de wikipedia
                sentences = re.split(r'(?<=[.!?])\s+', text)
                return " ".join(sentences[:max_sentences])

            # Encadré Wikipedia
            if results and "wikipedia.org" in results[0]["link"]:
                path = urlparse(results[0]["link"]).path
                title = path.split("/")[-1]  # /wiki/Albert_Einstein
                title = unquote(title)

                # Détecte la langue à partir du domaine
                domain = urlparse(results[0]["link"]).netloc
                lang = domain.split(".")[0] if domain else "en"

                wiki_api = "https://"+lang+".wikipedia.org/api/rest_v1/page/summary/"+title
                w = requests.get(wiki_api).json()
                
                raw_extract = w.get("extract", results[0]["snippet"])
                short_extract = shorten_extract(raw_extract, 3)

                wiki_box = {
                    "title": w.get("title", results[0]["title"]),
                    "extract": short_extract,
                    "thumbnail": w.get("thumbnail", {}).get("source"),
                    "link": results[0]["link"]
                }

            # Encadré OSM
            # Sous Wikipédia s'il existe sinon seul
            try:
                place = osm_search(q)
            except Exception:
                place = None
            if place:
                addr = place.get("address", {}) or {}
                extratags = place.get("extratags", {}) or {}
                osm_box = {
                    "display_name": place.get("display_name"),
                    "lat": place.get("lat"),
                    "lon": place.get("lon"),
                    "type": place.get("type"),
                    "address": {
                        "road": addr.get("road"),
                        "postcode": addr.get("postcode"),
                        "city": addr.get("city") or addr.get("town") or addr.get("village"),
                        "country": addr.get("country"),
                    },
                    "opening_hours": extratags.get("opening_hours"),
                }

            queries = data.get("queries", {})
            if isinstance(queries, dict):
                next_list = queries.get("nextPage") or []
                prev_list = queries.get("previousPage") or []
                if next_list:
                    next_start = next_list[0].get("startIndex")
                if prev_list:
                    prev_start = prev_list[0].get("startIndex")

        except requests.HTTPError as e:
            error = "HTTP "+e.response.status_code+": "+e.response.text[:200]+"…"
        except Exception as e:
            error = str(e)

    return render_template(
        "links.html",
        q=q,
        results=results,
        info=info,
        error=error,
        prev_start=prev_start,
        next_start=next_start,
        mode="web",
        count=_request_counter["count"],
        wiki_box=wiki_box,
        osm_box=osm_box
    )


@app.route("/images")
def images():
    q = request.args.get("q", "").strip()
    start = request.args.get("start", "1")
    try:
        start_index = int(start)
    except ValueError:
        start_index = 1

    results = []
    error = None
    prev_start = None
    next_start = None

    if q:
        try:
            data = google_search(q, start_index=start_index, num=10, search_type="image")
            items = data.get("items") or []
            for it in items:
                results.append({
                    "link": it.get("link", "#"),
                    "thumbnail": it.get("image", {}).get("thumbnailLink", it.get("link", "")),
                    "context": it.get("image", {}).get("contextLink", it.get("link", "")),
                })

            queries = data.get("queries", {})
            if isinstance(queries, dict):
                next_list = queries.get("nextPage") or []
                prev_list = queries.get("previousPage") or []
                if next_list:
                    next_start = next_list[0].get("startIndex")
                if prev_list:
                    prev_start = prev_list[0].get("startIndex")

        except requests.HTTPError as e:
            error = "HTTP "+e.response.status_code+": "+e.response.text[:200]+"…"
        except Exception as e:
            error = str(e)

    return render_template(
        "images.html",
        q=q,
        results=results,
        error=error,
        prev_start=prev_start,
        next_start=next_start,
        mode="images",
        count=_request_counter["count"]
    )

if __name__ == "__main__":
    app.run(debug=True)
