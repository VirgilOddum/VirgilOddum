from flask import Flask, request, jsonify
import os, requests, html, re, time

app = Flask(__name__)

# === Read from Render environment (with safe defaults) ===
RAPIDAPI_KEY   = os.environ.get("RAPIDAPI_KEY", "")
RAPIDAPI_HOST  = os.environ.get("RAPIDAPI_HOST", "aliexpress-datahub.p.rapidapi.com")
SEARCH_ENDPOINT = os.environ.get("SEARCH_ENDPOINT",
                                 "https://aliexpress-datahub.p.rapidapi.com/item_search")
DETAIL_ENDPOINT = os.environ.get("DETAIL_ENDPOINT",
                                 "https://aliexpress-datahub.p.rapidapi.com/item_detail")
DETAIL_ENDPOINT_ALT = os.environ.get("DETAIL_ENDPOINT_ALT",
                                     "https://aliexpress-datahub.p.rapidapi.com/item_detail2")

HEADERS = {
    "X-RapidAPI-Key": RAPIDAPI_KEY,
    "X-RapidAPI-Host": RAPIDAPI_HOST,
}

def clean_html(text):
    if not text: return ""
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

@app.route("/", methods=["GET"])
def root():
    return "CaretakerTools autosourcing service", 200

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200

def get_with_retries(url, *, params=None, headers=None, tries=3, backoff=2):
    last = None
    for i in range(tries):
        last = requests.get(url, headers=headers, params=params, timeout=25)
        if last.status_code != 429:
            return last
        time.sleep(backoff * (i + 1))  # 2s, 4s, 6s
    return last

def try_search(query):
    """Try common param names used by AliExpress providers and return (results, debug)."""
    attempts = [
        {"keyword": query, "page": 1},
        {"keywords": query, "page": 1},
        {"q": query, "page": 1},
    ]
    last_status = None
    last_text = None
    last_params = None
    for params in attempts:
        try:
            r = get_with_retries(SEARCH_ENDPOINT, headers=HEADERS, params=params)
            last_status = r.status_code
            last_params = params
            last_text = (r.text or "")[:400]
            if r.status_code == 200:
                j = r.json() or {}
                results = j.get("data") or j.get("result") or j.get("items") or []
                if results:
                    return results, {"attempt": params, "status": r.status_code}
            # keep looping; we’ll return debug below if none succeeded
        except Exception as e:
            last_text = f"exception: {str(e)[:200]}"
            continue
    return [], {"last_status": last_status, "last_text": last_text, "last_params": last_params}

def get_detail(product_id):
    for endpoint in (DETAIL_ENDPOINT, DETAIL_ENDPOINT_ALT):
        for key in ("itemId", "item_id"):
            try:
                r = get_with_retries(endpoint, headers=HEADERS, params={key: product_id})
                if r.status_code == 200:
                    return r.json() or {}
            except Exception:
                pass
    return {}

def score_result(result, query):
    title = (result.get("title") or "").lower()
    q = (query or "").lower()
    score = 0.0
    if q and q in title: score += 5.0
    try: score += float(result.get("orders", 0)) / 1000.0
    except: pass
    try: score += float(result.get("rating", 0))
    except: pass
    return score

def normalize_candidate(r):
    return {
        "title": r.get("title"),
        "price": r.get("price"),
        "url": r.get("url") or r.get("product_url"),
        "image": r.get("image") or r.get("thumbnail"),
    }

@app.route("/search", methods=["POST"])
def search():
    try:
        if not RAPIDAPI_KEY:
            return jsonify({"status": "config_error", "message": "RAPIDAPI_KEY missing"}), 500

        data = request.get_json(force=True)
        query = (data.get("query") or "").strip()
        target_price = data.get("target_price")
        if not query:
            return jsonify({"status": "error", "message": "Missing query"}), 400

        results, debug = try_search(query)

        # Friendly rate-limit signal (Zapier can retry)
        if not results and debug and debug.get("last_status") == 429:
            return jsonify({
                "status": "rate_limited",
                "message": "Upstream API rate limit (RapidAPI). Please retry shortly.",
                "retry_hint_seconds": 60,
                "debug": debug
            }), 200

        # Bubble up non-200s as debug instead of 500
        if not results and (debug.get("last_status") and debug.get("last_status") != 200):
            return jsonify({"status": "needs_review", "message": "No results", "candidates": [], "debug": debug}), 200

        if not results:
            return jsonify({"status": "needs_review", "message": "No results", "candidates": [], "debug": debug}), 200

        scored = sorted(((score_result(r, query), r) for r in results[:15]), key=lambda x: x[0], reverse=True)
        top_score, top = scored[0]
        if top_score < 6.0:
            return jsonify({"status": "needs_review", "candidates": [normalize_candidate(r) for _, r in scored[:3]]}), 200

        pid = top.get("product_id") or top.get("item_id") or top.get("id")
        det = get_detail(pid) if pid else {}
        images = det.get("images") or det.get("gallery") or []
        description_html = det.get("description") or det.get("desc") or ""
        thumb = top.get("image") or top.get("thumbnail")
        if thumb and thumb not in images:
            images = [thumb] + images

        try: cost = float(top.get("price") or 0.0)
        except: cost = 0.0
        if target_price:
            try: price = float(target_price)
            except: price = round(cost * 2.2, 2) if cost else 9.99
        else:
            price = round(cost * 2.2, 2) if cost else 9.99
        compare_at = round(price * 1.25, 2)

        vendor = top.get("store_name") or top.get("seller_name") or "Supplier"
        return jsonify({
            "status": "ok",
            "title": top.get("title") or query,
            "body_html": clean_html(description_html) or f"<p>{clean_html(top.get('title') or query)}</p>",
            "price": price,
            "compare_at_price": compare_at,
            "inventory_quantity": 100,
            "image_srcs": [u for u in images if isinstance(u, str)][:8],
            "vendor": vendor,
            "tags": ["caregiver", "autosourced"],
            "supplier_url": top.get("url") or top.get("product_url"),
            "cost": cost
        }), 200

    except Exception as e:
        # Return a JSON payload so you can see it from PowerShell/Postman
        return jsonify({"status": "server_error", "message": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
