"""
Trendy Tools Hub - Auto Pin Bot
--------------------------------
Free, zero-cost automation:
  1. Research a trending daily-use product (OpenRouter free LLM)
  2. Write a Pinterest title + description with a CTA (same LLM)
  3. Generate a high-quality image (Cloudflare Workers AI - FLUX, free)
  4. Build your Amazon India affiliate search link (Option A)
  5. Send the finished pin to your Telegram (semi-auto mode)
  6. (Optional) Auto-post to Pinterest once your app is approved

Every secret is read from an environment variable so nothing sensitive
lives in the code. On GitHub these come from "Actions Secrets".
"""

import os
import re
import json
import base64
import datetime
import urllib.parse
import requests
import gspread
from google.oauth2.service_account import Credentials

# Google Sheet log settings
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS", "")  # service-account JSON
GSHEET_ID  = os.environ.get("GSHEET_ID", "")                   # your sheet's ID
GSHEET_TAB = "Pinterest"                                        # the tab it writes to
SHEET_HEADERS = ["Date", "Time", "Product", "Search Keyword", "Pin Title",
                 "Pin Description", "Hashtags", "Image Source", "Affiliate Link",
                 "Posted To", "Status"]

# ---------------------------------------------------------------------------
# CONFIG - all values come from GitHub Secrets (never hard-code keys here)
# ---------------------------------------------------------------------------
GEMINI_API_KEY   = os.environ.get("GEMINI_API_KEY", "")   # primary text brain
OPENROUTER_KEY   = os.environ.get("OPENROUTER_KEY", "")   # backup text brain
CF_ACCOUNT_ID    = os.environ["CF_ACCOUNT_ID"]
CF_API_TOKEN     = os.environ["CF_API_TOKEN"]
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
AMAZON_TAG       = os.environ.get("AMAZON_TAG", "dailyneedss03-21")

# Optional - only needed once Pinterest approves your app
PIN_ACCESS_TOKEN = os.environ.get("PIN_ACCESS_TOKEN", "")
PIN_BOARD_ID     = os.environ.get("PIN_BOARD_ID", "")

# OpenRouter backup models (only used if Gemini is unavailable)
OPENROUTER_MODELS = [
    "meta-llama/llama-3.3-70b-instruct:free",
    "mistralai/mistral-small-3.2-24b-instruct:free",
    "google/gemma-3-27b-it:free",
]

# Gemini free models to try IN ORDER. Each model has its OWN separate daily
# quota, so when one hits its rate limit the bot simply moves to the next one -
# multiplying your total free capacity before it ever needs OpenRouter.
GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash-lite",
    "gemini-1.5-flash",
    "gemini-1.5-flash-8b",
]

# The niche/vibe for Trendy Tools Hub
NICHE = (
    "trendy, useful daily-use products, smart gadgets and lifestyle tools "
    "available on Amazon India that save time and money for everyday people"
)


# ---------------------------------------------------------------------------
# TEXT BRAIN - tries Gemini first, then OpenRouter. Returns raw text.
# ---------------------------------------------------------------------------
def gemini_complete(prompt):
    for model in GEMINI_MODELS:
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                params={"key": GEMINI_API_KEY},
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=60,
            )
            if r.status_code == 429:
                print(f"[info] Gemini {model} rate-limited today -> trying next Gemini model")
                continue
            if r.status_code != 200:
                print(f"[debug] gemini {model} -> HTTP {r.status_code}: {r.text[:300]}")
                continue
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            print(f"[ok] text via Gemini {model}")
            return text
        except Exception as e:
            print(f"[warn] gemini {model} failed: {e}")
    print("[info] all Gemini models exhausted -> falling back to OpenRouter")
    return None


def openrouter_complete(prompt):
    for model in OPENROUTER_MODELS:
        try:
            r = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.9,
                },
                timeout=60,
            )
            if r.status_code != 200:
                print(f"[debug] openrouter {model} -> HTTP {r.status_code}: {r.text[:300]}")
                continue
            print(f"[ok] text via OpenRouter {model}")
            return r.json()["choices"][0]["message"]["content"]
        except Exception as e:
            print(f"[warn] openrouter {model} failed: {e}")
    return None


def get_text(prompt):
    """Gemini first (stable, 1500/day free), OpenRouter as backup."""
    text = None
    if GEMINI_API_KEY:
        text = gemini_complete(prompt)
    if text is None and OPENROUTER_KEY:
        text = openrouter_complete(prompt)
    if text is None:
        raise RuntimeError("All text providers failed - check keys and logs above")
    return text


# ---------------------------------------------------------------------------
# 1 + 2.  RESEARCH A PRODUCT AND WRITE THE COPY (one LLM call, JSON out)
# ---------------------------------------------------------------------------
def research_and_write(avoid_products):
    avoid_text = ", ".join(avoid_products) if avoid_products else "none yet"
    prompt = f"""You are BOTH a Pinterest SEO specialist and a direct-response
marketing copywriter for an account called "Trendy Tools Hub" that promotes
{NICHE}.

Pick ONE specific, currently trending product to feature today.
IMPORTANT: Do NOT pick any of these already-featured products:
[{avoid_text}]
Choose something genuinely different from that list.

Follow Pinterest SEO + algorithm best practices:
- Front-load the main keyword in the title (people search Pinterest like Google).
- Title: benefit-driven, keyword-rich, under 95 characters.
- Description: 2-4 sentences. Natural keywords early, one strong emotional hook,
  and a clear call to action (e.g. "Tap the link to grab yours"). Keep it under
  480 characters INCLUDING hashtags.
- Use EXACTLY 2 to 4 relevant, specific hashtags (never more than 4).

Reply with ONLY a JSON object (no markdown, no extra text) with these keys:

{{
  "product_name": "short product name",
  "search_keyword": "the words a shopper would type into Amazon to find it",
  "pin_title": "keyword-first, benefit-driven, under 95 chars",
  "pin_description": "SEO + persuasive copy with CTA, ending in 2-4 hashtags, under 480 chars total",
  "image_prompt": "a detailed prompt for a clean, bright, professional product-hero photo of this exact item on a soft studio background, no text, no watermark"
}}"""
    content = get_text(prompt)
    match = re.search(r"\{.*\}", content, re.DOTALL)
    data = json.loads(match.group(0))
    print(f"[ok] research complete: {data['product_name']}")
    return data


# ---------------------------------------------------------------------------
# 3.  GENERATE THE IMAGE (Cloudflare Workers AI - FLUX, free)
# ---------------------------------------------------------------------------
def generate_image(image_prompt, out_path="pin.jpg"):
    url = (
        f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}"
        f"/ai/run/@cf/black-forest-labs/flux-1-schnell"
    )
    # FLUX rejects very long prompts, so clean whitespace and cap the length.
    clean = re.sub(r"\s+", " ", image_prompt).strip()
    # Realism modifiers so the AI image looks like a genuine product photo.
    REALISM = (" Photorealistic commercial product photograph, shot on a DSLR "
               "with an 85mm lens, natural soft studio lighting, sharp focus, "
               "realistic materials and reflections, high detail, e-commerce "
               "catalog style, no text, no watermark, no logo, not a cartoon, "
               "not an illustration.")
    # Try the full (capped) prompt, then a shorter version if it's refused.
    for attempt in (clean[:1200] + REALISM, clean[:250] + REALISM):
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {CF_API_TOKEN}"},
            json={"prompt": attempt, "steps": 6},
            timeout=120,
        )
        if r.status_code == 200:
            b64 = r.json()["result"]["image"]
            with open(out_path, "wb") as f:
                f.write(base64.b64decode(b64))
            print(f"[ok] image saved -> {out_path}")
            return out_path
        print(f"[debug] cloudflare HTTP {r.status_code}: {r.text[:300]}")
    r.raise_for_status()


# ---------------------------------------------------------------------------
# 4.  BUILD THE AMAZON AFFILIATE LINK (Option A - search link)
# ---------------------------------------------------------------------------
def affiliate_link(keyword):
    q = urllib.parse.quote_plus(keyword)
    return f"https://www.amazon.in/s?k={q}&tag={AMAZON_TAG}"


# ---------------------------------------------------------------------------
# 4b.  HYBRID IMAGE - best-effort grab of the REAL Amazon product photo +
#      exact product link. If Amazon blocks the server (common), return None
#      so main() falls back to the AI image. Never crashes the run.
# ---------------------------------------------------------------------------
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")


def try_amazon_product(keyword):
    """Return (image_url, product_link) from the first real Amazon result,
    or (None, None) if Amazon blocks or nothing is found."""
    try:
        q = urllib.parse.quote_plus(keyword)
        r = requests.get(
            f"https://www.amazon.in/s?k={q}",
            headers={"User-Agent": BROWSER_UA, "Accept-Language": "en-IN,en;q=0.9"},
            timeout=30,
        )
        if r.status_code != 200:
            print(f"[info] Amazon blocked scrape (HTTP {r.status_code}) -> AI image")
            return None, None
        html = r.text
        # Pair each product's ASIN with a nearby product image
        for m in re.finditer(r'data-asin="([A-Z0-9]{10})"', html):
            asin = m.group(1)
            window = html[m.start():m.start() + 3000]
            for tag in re.findall(r"<img[^>]+>", window):
                if "s-image" in tag:
                    src = re.search(r'src="([^"]+)"', tag)
                    if src:
                        # strip the size token (e.g. ._AC_UL320_.) for full-res
                        big = re.sub(r"\._[^/]*?_\.(jpg|jpeg|png)", r".\1",
                                     src.group(1), flags=re.I)
                        link = f"https://www.amazon.in/dp/{asin}?tag={AMAZON_TAG}"
                        print(f"[ok] real Amazon product found: {asin}")
                        return big, link
        print("[info] no product image parsed -> AI image")
        return None, None
    except Exception as e:
        print(f"[warn] Amazon scrape failed ({e}) -> AI image")
        return None, None


def download_image(url, out_path="pin.jpg"):
    r = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=60)
    r.raise_for_status()
    ctype = r.headers.get("Content-Type", "")
    if "image" not in ctype or len(r.content) < 3000:
        raise ValueError(f"not a real image (type={ctype}, {len(r.content)} bytes)")
    with open(out_path, "wb") as f:
        f.write(r.content)
    print(f"[ok] downloaded real image -> {out_path}")
    return out_path


def safe_download(url):
    """Download an image, returning the path or None (never raises)."""
    try:
        return download_image(url)
    except Exception as e:
        print(f"[warn] image download failed ({e})")
        return None


def try_web_image(keyword):
    """Find a real photo of a similar product on the open web (Openverse,
    free, no API key, commercially licensed). Returns an image URL or None."""
    try:
        r = requests.get(
            "https://api.openverse.org/v1/images/",
            params={"q": keyword, "license_type": "commercial", "page_size": 8},
            headers={"User-Agent": BROWSER_UA},
            timeout=30,
        )
        if r.status_code != 200:
            print(f"[info] web image search HTTP {r.status_code} -> AI image")
            return None
        for item in r.json().get("results", []):
            u = item.get("url")
            if u:
                print("[ok] found a similar real product photo on the web")
                return u
        print("[info] no web image found -> AI image")
        return None
    except Exception as e:
        print(f"[warn] web image search failed ({e}) -> AI image")
        return None


# ---------------------------------------------------------------------------
# 5.  SEND THE FINISHED PIN TO TELEGRAM (semi-auto mode)
# ---------------------------------------------------------------------------
def notify_telegram(image_path, title, description, link):
    caption = f"<b>{title}</b>\n\n{description}\n\n🔗 {link}"
    with open(image_path, "rb") as photo:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "caption": caption[:1024],
                "parse_mode": "HTML",
            },
            files={"photo": photo},
            timeout=60,
        )
    r.raise_for_status()
    print("[ok] sent to Telegram")


# ---------------------------------------------------------------------------
# 6.  (OPTIONAL) AUTO-POST TO PINTEREST - only runs if a token is set
# ---------------------------------------------------------------------------
def post_to_pinterest(image_path, title, description, link):
    if not (PIN_ACCESS_TOKEN and PIN_BOARD_ID):
        print("[skip] Pinterest not configured yet - staying in semi-auto mode")
        return
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    r = requests.post(
        "https://api.pinterest.com/v5/pins",
        headers={"Authorization": f"Bearer {PIN_ACCESS_TOKEN}"},
        json={
            "board_id": PIN_BOARD_ID,
            "title": title[:100],
            "description": description[:800],
            "link": link,
            "media_source": {
                "source_type": "image_base64",
                "content_type": "image/jpeg",
                "data": b64,
            },
        },
        timeout=60,
    )
    r.raise_for_status()
    print("[ok] posted to Pinterest")


# ---------------------------------------------------------------------------
# 7.  GOOGLE SHEET LOG - writes to the "Pinterest" tab of your master sheet,
#     and reads past products so it never repeats one.
# ---------------------------------------------------------------------------
def get_worksheet():
    """Authorize as the service account and return the 'Pinterest' worksheet,
    creating it (with headers) if it doesn't exist yet."""
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDENTIALS),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(GSHEET_ID)
    try:
        ws = sh.worksheet(GSHEET_TAB)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=GSHEET_TAB, rows=2000, cols=len(SHEET_HEADERS))
    if not ws.get_all_values():           # empty tab -> add header row
        ws.append_row(SHEET_HEADERS)
    return ws


def load_history(ws):
    """Return the most recent 40 product names already in the sheet."""
    products = ws.col_values(3)[1:]       # column 3 = Product, skip header
    return [p for p in products if p][-40:]


def log_post(ws, data, link, posted_to, image_source, status="Success"):
    now = datetime.datetime.now()
    hashtags = " ".join(re.findall(r"#\w+", data.get("pin_description", "")))
    ws.append_row([
        now.strftime("%Y-%m-%d"),
        now.strftime("%H:%M"),
        data["product_name"],
        data["search_keyword"],
        data["pin_title"],
        data["pin_description"],
        hashtags,
        image_source,
        link,
        posted_to,
        status,
    ], value_input_option="USER_ENTERED")
    print("[ok] logged to Google Sheet")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    ws = get_worksheet()
    recent = load_history(ws)
    data = research_and_write(recent)

    # IMAGE PRIORITY:
    #   1) exact real Amazon product photo (+ direct product link)
    #   2) a real photo of a similar product from the open web
    #   3) authentic-looking AI image (last resort)
    kw = data["search_keyword"]
    link = affiliate_link(kw)          # default: Amazon search link
    img = None
    image_source = "AI (authentic)"

    a_url, a_link = try_amazon_product(kw)
    if a_url:
        img = safe_download(a_url)
        if img:
            link = a_link              # upgrade to exact product link
            image_source = "Amazon (real)"

    if img is None:
        w_url = try_web_image(kw)
        if w_url:
            img = safe_download(w_url)
            if img:
                image_source = "Web (real)"

    if img is None:
        img = generate_image(data["image_prompt"])

    notify_telegram(img, data["pin_title"], data["pin_description"], link)

    posted_to = "Telegram"
    if PIN_ACCESS_TOKEN and PIN_BOARD_ID:
        post_to_pinterest(img, data["pin_title"], data["pin_description"], link)
        posted_to = "Pinterest"
    else:
        print("[skip] Pinterest not configured yet - staying in semi-auto mode")

    log_post(ws, data, link, posted_to, image_source)
    print("[done] pin cycle complete")


if __name__ == "__main__":
    main()
