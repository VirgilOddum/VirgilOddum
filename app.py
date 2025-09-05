# app.py
from flask import Flask, request, jsonify
import os, requests, html, re, time

app = Flask(__name__)

# ---- Environment (can be overridden in Render) ----
RAPIDAPI_KEY    = os.environ.get("RAPIDAPI_KEY", "")
RAPIDAPI_HOST   = os.environ.get("RAPIDAPI_HOST", "aliexpress-datahub.p.rapidapi.com")
SEARCH_ENDPOINT = os.environ.get("SEARCH_ENDPOINT", "https://aliexpress-datahub.p.rapidapi.com/item_search")
DETAIL_ENDPOINT = os.environ.get("DETAIL_ENDPOINT", "https://aliexpress-datahub.p.rapidapi.com/item_detail")

HEADERS = {
    "X-RapidAPI-Key": RAPIDAPI_KEY,
    "X-RapidAPI-Host": RAPIDAPI_HOST,
}

# ---- Helpers ----
def clean_html(text: str) -> str:
    if not text:
        return ""
    text = html.unescape(str(text))
    text = re.sub(r"\s+", " ", text).strip()
    return text

def score_result(result: dict, query: str) -> float:
    """Simple heuristic score: query match, orders, rating."""
    title = (result.get("title") or "").lower()
    q = (query or "").lower()
    score = 0.0
    if q and q in title:
        score += 5.0
    try:
        orders = float(result.get("orders", 0))
        score += orders / 1000.0
    except Exception:
        pass
    try:
        rating = float(result.get("rating", 0))
        score += rating
    except Exception:
        pass
    return score

def normalize_candidate(r: dict) -> dict:
    return {
        "title": r.get("title") or r.get("name"),
        "price": r.get("price") or r.get("sale_price") or r.get("min_price"),
        "url": r.get("url") or r.get("product_url"),
        "image": r.get("image") or r.get("thumbnail") or r.get("img"),
    }

def get_with_retries(url, headers=None, params=None, timeout=20, retries=2, backoff=1.0):
    last_exc = None
    for i in range(retries + 1):
        try:
            return requests.get(url, headers=headers, params=params, timeout=timeout)
        except Exception as e:
            last_exc = e
            time.sleep(backoff)
    raise last_exc

def try_search(query: str):
    """
    Try common param names; return (results_list, debug_dict).
    We normalize across different provider response shapes.
    """
    attempts = [
        {"keyword": query, "page": 1},
        {"keywords": query, "page": 1},
        {"q": query, "page": 1},
    ]

    last_status = None
    last_text = None
    last_params = None
    last_shape = None

    def extract_list(j):
        """Extract a list of products from a variety of shapes."""
        if not isinstance(j, dict):
            return []

        # Track top-level shape for debugging
        nonlocal last_shape
        last_shape = list(j.keys())[:8]

        # Very common shapes
        data = j.get("data")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for k in ("items", "products", "result", "list"):
                v = data.get(k)
                if isinstance(v, list):
                    return v

        # Other top-level shapes
        for k in ("result", "items", "products", "list"):
            v = j.get(k)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                for kk in ("items", "products", "list"):
                    vv = v.get(kk)
                    if isinstance(vv, list):
                        return vv
        return []

    for params in attempts:
        try:
            r = get_with_retries(SEARCH_ENDPOINT, headers=HEADERS, params=params, timeout=20)
            last_status = r.status_code
            last_params = params
            last_text = (r.text or "")[:500]
            if r.status_code == 200:
                j = {}
                try:
                    j = r.json() or {}
                except Exception:
                    # If JSON fails, treat as empty
                    j = {}
                results_list = extract_list(j)
                if results_list:
                    return results_list, {
                        "attempt": params,
                        "status": r.status_code,
                        "shape_hint": last_shape,
                    }
        except Exception as e:
            last_text = f"exception: {str(e)[:200]}"

    # No luck
    return [], {
        "last_status": last_status,
        "last_text": last_text,
        "last_params": last_params,
        "shape_hint": last_shape,
    }

# ---- Routes ----
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200

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
        if (not results) and debug and debug.get("last_status") == 429:
            return jsonify({
                "status": "rate_limited",
                "message": "Upstream API rate limit (RapidAPI). Please retry shortly.",
                "retry_hint_seconds": 60,
                "debug": debug
            }), 200

        # Bubble up non-200s as 'needs_review' instead of 500
        if (not results) and (debug and debug.get("last_status") != 200):
            return jsonify({
                "status": "needs_review",
                "message": "No results",
                "candidates": [],
                "debug": debug
            }), 200

        # Ensure results is a list before slicing
        if not isinstance(results, list):
            try:
                results = list(results)  # if it's something iterable
            except Exception:
                results = []

        # Score top N
        top_n = results[:15]
        scored = []
        for r in top_n:
            try:
                scored.append((score_result(r, query), r))
            except Exception:
                continue

        scored.sort(key=lambda x: x[0], reverse=True)

        if not scored:
            return jsonify({
                "status": "needs_review",
                "message": "No results",
                "candidates": [],
                "debug": {"note": "empty_after_parse", **(debug or {})}
            }), 200

        top_score, top = scored[0]
        # Confidence threshold
        if top_score < 6.0:
            return jsonify({
                "status": "needs_review",
                "candidates": [normalize_candidate(r) for _, r in scored[:3]],
                "debug": {"note": "low_confidence", **(debug or {})}
            }), 200

        # Pull images/description best-effort (optional; ignore failures)
        images = []
        description_html = ""
        vendor = top.get("store_name") or top.get("seller_name") or "Supplier"
        product_id = top.get("product_id") or top.get("item_id") or top.get("id")
        try:
            if product_id:
                dres = requests.get(
                    DETAIL_ENDPOINT, headers=HEADERS,
                    params={"item_id": product_id}, timeout=20
                )
                if dres.status_code == 200:
                    det = dres.json() or {}
                    images = (det.get("images") or det.get("gallery") or []) or []
                    description_html = det.get("description") or det.get("desc") or ""
        except Exception:
            pass

        thumb = top.get("image") or top.get("thumbnail")
        if thumb and thumb not in images:
            images = [thumb] + images

        # Pricing
        try:
            cost = float(top.get("price") or top.get("sale_price") or 0.0)
        except Exception:
            cost = 0.0

        if target_price:
            try:
                price = float(target_price)
            except Exception:
                price = round(cost * 2.2, 2) if cost else 9.99
        else:
            price = round(cost * 2.2, 2) if cost else 9.99

        compare_at = round(price * 1.25, 2)

        normalized = {
            "status": "ok",
            "title": top.get("title") or top.get("name") or query,
            "body_html": clean_html(description_html) or f"<p>{clean_html(top.get('title') or query)}</p>",
            "price": price,
            "compare_at_price": compare_at,
            "inventory_quantity": 100,
            "image_srcs": [u for u in images if isinstance(u, str)][:8],
            "vendor": vendor,
            "tags": ["caregiver", "autosourced"],
            "supplier_url": top.get("url") or top.get("product_url"),
            "cost": cost
        }
        return jsonify(normalized), 200

    except Exception as e:
        # Never crash the service; return clear error JSON
        return jsonify({"status": "server_error", "message": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
