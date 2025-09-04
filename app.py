
from flask import Flask, request, jsonify
import os, requests, html, re

app = Flask(__name__)

RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY")
SEARCH_ENDPOINT = "https://aliexpress-datahub.p.rapidapi.com/item_search"
DETAIL_ENDPOINT = "https://aliexpress-datahub.p.rapidapi.com/item_detail"

HEADERS = {
    "X-RapidAPI-Key": RAPIDAPI_KEY,
    "X-RapidAPI-Host": "aliexpress-datahub.p.rapidapi.com"  # update if you switch providers
}

def clean_html(text):
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

def score_result(result, query):
    title = (result.get("title") or "").lower()
    q = (query or "").lower()
    score = 0.0
    if q and q in title:
        score += 5.0
    try:
        orders = float(result.get("orders", 0))
        score += orders / 1000.0
    except:
        pass
    try:
        rating = float(result.get("rating", 0))
        score += rating
    except:
        pass
    return score

def normalize_candidate(r):
    return {
        "title": r.get("title"),
        "price": r.get("price"),
        "url": r.get("url") or r.get("product_url"),
        "image": r.get("image") or r.get("thumbnail")
    }

@app.route("/search", methods=["POST"])
def search():
    data = request.get_json(force=True)
    query = (data.get("query") or "").strip()
    target_price = data.get("target_price")
    if not query:
        return jsonify({"status": "error", "message": "Missing query"}), 400

    params = {"q": query, "page": 1}
    res = requests.get(SEARCH_ENDPOINT, headers=HEADERS, params=params, timeout=20)
    if res.status_code != 200:
        return jsonify({"status": "error", "message": f"Search failed: {res.status_code}"}), 502

    payload = res.json() or {}
    results = payload.get("data") or payload.get("result") or []
    if not results:
        return jsonify({"status": "needs_review", "message": "No results", "candidates": []}), 200

    scored = [(score_result(r, query), r) for r in results[:15]]
    scored.sort(key=lambda x: x[0], reverse=True)
    top_score, top = scored[0]

    CONFIDENCE_MIN = 6.0
    if top_score < CONFIDENCE_MIN:
        candidates = [normalize_candidate(r) for _, r in scored[:3]]
        return jsonify({"status": "needs_review", "candidates": candidates}), 200

    product_id = top.get("product_id") or top.get("item_id") or top.get("id")
    images = []
    description_html = ""
    vendor = top.get("store_name") or top.get("seller_name") or "Supplier"

    try:
        if product_id:
            dparams = {"item_id": product_id}
            dres = requests.get(DETAIL_ENDPOINT, headers=HEADERS, params=dparams, timeout=20)
            if dres.status_code == 200:
                det = dres.json() or {}
                images = det.get("images") or det.get("gallery") or []
                description_html = det.get("description") or det.get("desc") or ""
    except Exception:
        pass

    thumb = top.get("image") or top.get("thumbnail")
    if thumb and thumb not in images:
        images = [thumb] + images

    try:
        cost = float(top.get("price") or 0.0)
    except:
        cost = 0.0

    if target_price:
        try:
            price = float(target_price)
        except:
            price = round(cost * 2.2, 2) if cost else 9.99
    else:
        price = round(cost * 2.2, 2) if cost else 9.99
    compare_at = round(price * 1.25, 2)

    normalized = {
        "status": "ok",
        "title": top.get("title") or query,
        "body_html": clean_html(description_html) or f"<p>{clean_html(top.get('title') or query)}</p>",
        "price": price,
        "compare_at_price": compare_at,
        "inventory_quantity": 100,
        "image_srcs": [u for u in images if isinstance(u, str)][:8],
        "vendor": vendor,
        "tags": ["caregiver","autosourced"],
        "supplier_url": top.get("url") or top.get("product_url"),
        "cost": cost
    }
    return jsonify(normalized), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
