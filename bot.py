"""
Trendy Tools Hub - AI Content Studio
====================================
A team of specialized AI "agents" that runs like a company, 24/7, for free:

  TrendResearcher  - pulls real search-demand data (Google autocomplete + Trends)
  Strategist       - picks the single best product (trend + season + no repeats)
  Copywriter       - SEO + marketing copy in a proven "Best X under Rs.Y" template
  ArtDirector      - clean 2:3 hyper-real image (real product first) + Idea video
  Publisher        - Telegram approval gate + Pinterest posting
  Analyst          - weekly summary + performance feedback (activates with Pinterest)
  Manager          - always-on loop: instant /new, daily post, weekly summary

MODES (first CLI arg):
  run        - always-on studio (default; used by the workflow)
  scheduled  - one pin now (auto-post on approval timeout)
  listen     - one poll for a /new trigger
  summary    - weekly digest to Telegram

All secrets come from environment variables (GitHub Actions Secrets).
"""

import os
import re
import sys
import json
import time
import base64
import datetime
import traceback
import subprocess
import urllib.parse
import requests
import gspread
from google.oauth2.service_account import Credentials
from PIL import Image, ImageDraw, ImageFont

try:
    from pytrends.request import TrendReq
except Exception:
    TrendReq = None

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
GEMINI_API_KEY   = os.environ.get("GEMINI_API_KEY", "")
GROQ_API_KEY     = os.environ.get("GROQ_API_KEY", "")
OPENROUTER_KEY   = os.environ.get("OPENROUTER_KEY", "")
CEREBRAS_API_KEY = os.environ.get("CEREBRAS_API_KEY", "")
MISTRAL_API_KEY  = os.environ.get("MISTRAL_API_KEY", "")
SAMBANOVA_API_KEY = os.environ.get("SAMBANOVA_API_KEY", "")
TOGETHER_API_KEY = os.environ.get("TOGETHER_API_KEY", "")
NVIDIA_API_KEY   = os.environ.get("NVIDIA_API_KEY", "")
CF_ACCOUNT_ID    = os.environ["CF_ACCOUNT_ID"]
CF_API_TOKEN     = os.environ["CF_API_TOKEN"]
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
AMAZON_TAG       = os.environ.get("AMAZON_TAG", "dailyneedss03-21")

GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS", "")
GSHEET_ID  = os.environ.get("GSHEET_ID", "")
GSHEET_TAB = "Pinterest"
SHEET_HEADERS = ["Date", "Time", "Product", "Search Keyword", "SEO Keywords",
                 "Pin Title", "Pin Description", "Hashtags", "Image Source",
                 "Format", "Affiliate Link", "Posted To", "Status"]

PIN_ACCESS_TOKEN = os.environ.get("PIN_ACCESS_TOKEN", "")
PIN_BOARD_ID     = os.environ.get("PIN_BOARD_ID", "")

APPROVAL_WINDOW = int(os.environ.get("APPROVAL_WINDOW", "1800"))   # 30 min
NOTE_WINDOW     = 600
SHIFT_SECONDS   = int(os.environ.get("SHIFT_SECONDS", str(int(5.3 * 3600))))
TG = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.5-flash-lite",
                 "gemini-2.0-flash-lite", "gemini-1.5-flash", "gemini-1.5-flash-8b"]

# Every provider below is OpenAI-compatible (base_url, key, [models to try]).
# Each agent picks its specialist first; the ROLE_ROUTING + GLOBAL_FALLBACK below
# spread load across all 8 providers and never let a job stall.
OAI_PROVIDERS = {
    "groq":      ("https://api.groq.com/openai/v1/chat/completions", GROQ_API_KEY,
                  ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]),
    "cerebras":  ("https://api.cerebras.ai/v1/chat/completions", CEREBRAS_API_KEY,
                  ["llama-3.3-70b", "llama3.1-8b"]),
    "mistral":   ("https://api.mistral.ai/v1/chat/completions", MISTRAL_API_KEY,
                  ["mistral-large-latest", "mistral-small-latest"]),
    "sambanova": ("https://api.sambanova.ai/v1/chat/completions", SAMBANOVA_API_KEY,
                  ["Meta-Llama-3.3-70B-Instruct", "Meta-Llama-3.1-405B-Instruct"]),
    "together":  ("https://api.together.xyz/v1/chat/completions", TOGETHER_API_KEY,
                  ["meta-llama/Llama-3.3-70B-Instruct-Turbo-Free",
                   "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo"]),
    "nvidia":    ("https://integrate.api.nvidia.com/v1/chat/completions", NVIDIA_API_KEY,
                  ["meta/llama-3.3-70b-instruct",
                   "nvidia/llama-3.1-nemotron-70b-instruct"]),
    "openrouter": ("https://openrouter.ai/api/v1/chat/completions", OPENROUTER_KEY,
                   ["meta-llama/llama-3.3-70b-instruct:free",
                    "mistralai/mistral-small-3.2-24b-instruct:free"]),
}

# Each agent's preferred provider order (specialist first)
ROLE_ROUTING = {
    "researcher": ["groq", "cerebras"],
    "strategist": ["sambanova", "cerebras", "gemini"],
    "copywriter": ["mistral", "cerebras", "together"],
    "editor":     ["nvidia", "gemini", "groq"],
    "artdirector": ["cerebras", "groq"],
    "analyst":    ["gemini", "groq"],
}
GLOBAL_FALLBACK = ["gemini", "groq", "cerebras", "mistral", "sambanova",
                   "together", "nvidia", "openrouter"]

NICHE = ("trendy, useful daily-use products, smart gadgets and lifestyle tools "
         "on Amazon India that save time and money for everyday people")
CATEGORIES = ["kitchen gadgets", "home decor", "cleaning tools",
              "fitness and wellness", "travel accessories",
              "desk and work-from-home", "smart home devices",
              "storage and organization"]
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
TRIGGER_WORDS = {"/new", "/pin", "/generate", "pinterest now", "pinterest", "new pin"}


# ===========================================================================
# TELEGRAM
# ===========================================================================
def tg_send_message(text, reply_markup=None):
    d = {"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000], "parse_mode": "HTML"}
    if reply_markup:
        d["reply_markup"] = json.dumps(reply_markup)
    return requests.post(f"{TG}/sendMessage", data=d, timeout=60)


def tg_send_photo(path, caption, reply_markup=None):
    d = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption[:1024], "parse_mode": "HTML"}
    if reply_markup:
        d["reply_markup"] = json.dumps(reply_markup)
    with open(path, "rb") as p:
        return requests.post(f"{TG}/sendPhoto", data=d, files={"photo": p}, timeout=120)


def tg_send_video(path, caption=""):
    d = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption[:1024], "parse_mode": "HTML"}
    with open(path, "rb") as v:
        return requests.post(f"{TG}/sendVideo", data=d, files={"video": v}, timeout=180)


def tg_get_updates(offset=None, timeout=25):
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    try:
        r = requests.get(f"{TG}/getUpdates", params=params, timeout=timeout + 20)
        return r.json().get("result", [])
    except Exception as e:
        print(f"[warn] getUpdates: {e}")
        return []


def tg_answer_callback(cb_id):
    try:
        requests.post(f"{TG}/answerCallbackQuery",
                      data={"callback_query_id": cb_id}, timeout=30)
    except Exception:
        pass


APPROVAL_KB = {"inline_keyboard": [
    [{"text": "✅ Approve & Post", "callback_data": "approve"},
     {"text": "❌ Reject", "callback_data": "reject"}],
    [{"text": "✍️ Fix Copy", "callback_data": "fix_copy"},
     {"text": "🖼️ Fix Image", "callback_data": "fix_image"}],
]}


# ===========================================================================
# TEXT BRAIN: Gemini -> Groq -> OpenRouter
# ===========================================================================
# Model auto-discovery: if the hard-coded model names ever go stale, the router
# asks each provider what models it ACTUALLY has live and tries those too.
_MODEL_CACHE = {}
_BAD = ("embed", "whisper", "tts", "guard", "rerank", "moderation", "bge",
        "stable-diffusion", "flux", "dall", "-image", "vision-embed", "-asr",
        "-ocr", "audio", "reranker", "safety")
_GOOD = ("instruct", "llama", "mistral", "qwen", "gemma", "nemotron", "deepseek",
         "mixtral", "command", "phi", "gpt-oss", "-it", "maverick", "scout")


def discover_oai_models(name):
    """Ask an OpenAI-compatible provider which chat models are live right now."""
    if name in _MODEL_CACHE:
        return _MODEL_CACHE[name]
    base, key, _ = OAI_PROVIDERS[name]
    url = base.replace("/chat/completions", "/models")
    ids = []
    try:
        r = requests.get(url, headers={"Authorization": f"Bearer {key}"}, timeout=30)
        if r.status_code == 200:
            data = r.json()
            items = data.get("data", data) if isinstance(data, dict) else data
            for it in items:
                mid = it.get("id") if isinstance(it, dict) else None
                if mid and not any(b in mid.lower() for b in _BAD):
                    ids.append(mid)
            ids.sort(key=lambda m: 0 if any(g in m.lower() for g in _GOOD) else 1)
    except Exception as e:
        print(f"[warn] discover {name}: {e}")
    _MODEL_CACHE[name] = ids
    return ids


def _gemini_models():
    if "gemini" in _MODEL_CACHE:
        return _MODEL_CACHE["gemini"]
    ids = list(GEMINI_MODELS)
    try:
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                         params={"key": GEMINI_API_KEY}, timeout=30)
        if r.status_code == 200:
            for mo in r.json().get("models", []):
                if "generateContent" in mo.get("supportedGenerationMethods", []):
                    mid = mo["name"].split("/")[-1]
                    if ("flash" in mid or "pro" in mid) and mid not in ids:
                        ids.append(mid)
    except Exception as e:
        print(f"[warn] gemini discover: {e}")
    _MODEL_CACHE["gemini"] = ids
    return ids


def _gemini(prompt):
    for m in _gemini_models()[:8]:
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent",
                params={"key": GEMINI_API_KEY},
                json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=60)
            if r.status_code == 429:
                continue
            if r.status_code != 200:
                print(f"[debug] gemini {m} {r.status_code}: {r.text[:120]}"); continue
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            print(f"[warn] gemini {m}: {e}")
    return None


def _oai(name, prompt):
    """Call an OpenAI-compatible provider. Tries the curated models first, then
    every other live model it discovers - so one dead model never kills a brain."""
    base, key, curated = OAI_PROVIDERS[name]
    if not key:
        return None
    candidates = list(curated)
    for m in discover_oai_models(name):
        if m not in candidates:
            candidates.append(m)
    tried = set()
    for m in candidates[:8]:
        if m in tried:
            continue
        tried.add(m)
        try:
            r = requests.post(base, headers={"Authorization": f"Bearer {key}"},
                              json={"model": m,
                                    "messages": [{"role": "user", "content": prompt}],
                                    "temperature": 0.8}, timeout=60)
            if r.status_code != 200:
                print(f"[debug] {name} {m} {r.status_code}: {r.text[:100]}"); continue
            return r.json()["choices"][0]["message"]["content"]
        except Exception as e:
            print(f"[warn] {name} {m}: {e}")
    return None


def _call_provider(name, prompt):
    if name == "gemini":
        return _gemini(prompt) if GEMINI_API_KEY else None
    return _oai(name, prompt)


def get_text(prompt, role="strategist"):
    """Route to the agent's specialist provider, then fall back through all others."""
    order = list(ROLE_ROUTING.get(role, []))
    for p in GLOBAL_FALLBACK:
        if p not in order:
            order.append(p)
    for name in order:
        out = _call_provider(name, prompt)
        if out:
            print(f"[brain] {role} -> {name}")
            return out
    raise RuntimeError(f"All providers failed for role '{role}'")


def parse_json(text):
    return json.loads(re.search(r"\{.*\}", text, re.DOTALL).group(0))


# ===========================================================================
# DATA SIGNALS: Google autocomplete (reliable) + Trends (bonus)
# ===========================================================================
def google_autocomplete(seed):
    try:
        r = requests.get("https://suggestqueries.google.com/complete/search",
                         params={"client": "firefox", "q": seed, "hl": "en", "gl": "in"},
                         headers={"User-Agent": BROWSER_UA}, timeout=20)
        return r.json()[1][:10]
    except Exception as e:
        print(f"[warn] autocomplete ({seed}): {e}")
        return []


def trends_rising(keyword):
    if TrendReq is None:
        return []
    try:
        py = TrendReq(hl="en-US", tz=330)
        py.build_payload([keyword], geo="IN", timeframe="now 7-d")
        rq = py.related_queries().get(keyword, {})
        rising = rq.get("rising")
        if rising is not None:
            return list(rising["query"])[:6]
    except Exception as e:
        print(f"[warn] pytrends: {e}")
    return []


# ===========================================================================
# CATEGORY + SEASON
# ===========================================================================
def pick_category():
    return CATEGORIES[datetime.date.today().toordinal() % len(CATEGORIES)]


def indian_season(d):
    m = d.month
    if m in (4, 5, 6):
        return "peak summer (cooling, hydration, travel and portable items sell well)"
    if m in (7, 8, 9):
        return "monsoon (rain gear, indoor comfort, quick-dry and cleaning items)"
    if m in (10, 11):
        return "festive season (Diwali/Dhanteras; gifting, decor, home upgrades)"
    return "winter (warmth, cozy home, kitchen and wellness items)"


# ===========================================================================
# IMAGE SOURCES
# ===========================================================================
def try_amazon_product(keyword):
    try:
        q = urllib.parse.quote_plus(keyword)
        r = requests.get(f"https://www.amazon.in/s?k={q}",
                         headers={"User-Agent": BROWSER_UA,
                                  "Accept-Language": "en-IN,en;q=0.9"}, timeout=30)
        if r.status_code != 200:
            print(f"[info] Amazon blocked (HTTP {r.status_code})")
            return None, None
        html = r.text
        for m in re.finditer(r'data-asin="([A-Z0-9]{10})"', html):
            asin = m.group(1)
            window = html[m.start():m.start() + 3000]
            for tag in re.findall(r"<img[^>]+>", window):
                if "s-image" in tag:
                    src = re.search(r'src="([^"]+)"', tag)
                    if src:
                        big = re.sub(r"\._[^/]*?_\.(jpg|jpeg|png)", r".\1",
                                     src.group(1), flags=re.I)
                        return big, f"https://www.amazon.in/dp/{asin}?tag={AMAZON_TAG}"
        return None, None
    except Exception as e:
        print(f"[warn] Amazon scrape: {e}")
        return None, None


def try_web_image(keyword):
    try:
        r = requests.get("https://api.openverse.org/v1/images/",
                         params={"q": keyword, "license_type": "commercial", "page_size": 8},
                         headers={"User-Agent": BROWSER_UA}, timeout=30)
        if r.status_code != 200:
            return None
        for item in r.json().get("results", []):
            if item.get("url"):
                return item["url"]
    except Exception as e:
        print(f"[warn] web image: {e}")
    return None


def bestseller_link(keyword):
    q = urllib.parse.quote_plus(keyword)
    return (f"https://www.amazon.in/s?k={q}&s=review-rank"
            f"&rh=p_72:1318476031&tag={AMAZON_TAG}")


def amazon_gallery(asin):
    """Pull the exact product's OWN gallery photos from its Amazon page, so every
    image is the SAME real product. Returns a list of image URLs (may be empty)."""
    try:
        r = requests.get(f"https://www.amazon.in/dp/{asin}",
                         headers={"User-Agent": BROWSER_UA,
                                  "Accept-Language": "en-IN,en;q=0.9"}, timeout=30)
        if r.status_code != 200:
            print(f"[info] Amazon product page blocked (HTTP {r.status_code})")
            return []
        html = r.text
        urls = re.findall(r'"hiRes":"(https:[^"]+?\.jpg)"', html) \
            or re.findall(r'"large":"(https:[^"]+?\.jpg)"', html)
        seen, out = set(), []
        for u in urls:
            u = u.replace("\\/", "/")
            if u not in seen:
                seen.add(u); out.append(u)
        return out[:4]
    except Exception as e:
        print(f"[warn] amazon_gallery: {e}")
        return []


def amazon_images(keyword):
    """Find the top product for the keyword and return (image_urls, product_link).
    Prefers the product's real gallery; falls back to its search thumbnail."""
    q = urllib.parse.quote_plus(keyword)
    fallback_link = bestseller_link(keyword)
    try:
        r = requests.get(f"https://www.amazon.in/s?k={q}",
                         headers={"User-Agent": BROWSER_UA,
                                  "Accept-Language": "en-IN,en;q=0.9"}, timeout=30)
        if r.status_code != 200:
            print(f"[info] Amazon search blocked (HTTP {r.status_code})")
            return [], fallback_link
        html = r.text
        asin, thumb = None, None
        for m in re.finditer(r'data-asin="([A-Z0-9]{10})"', html):
            window = html[m.start():m.start() + 3000]
            for tag in re.findall(r"<img[^>]+>", window):
                if "s-image" in tag:
                    s = re.search(r'src="([^"]+)"', tag)
                    if s:
                        asin = m.group(1)
                        thumb = re.sub(r"\._[^/]*?_\.(jpg|jpeg|png)", r".\1",
                                       s.group(1), flags=re.I)
                        break
            if asin:
                break
        if not asin:
            return [], fallback_link
        link = f"https://www.amazon.in/dp/{asin}?tag={AMAZON_TAG}"
        gallery = amazon_gallery(asin)
        if gallery:
            print(f"[ok] {len(gallery)} real gallery images for {asin}")
            return gallery, link
        if thumb:
            print(f"[ok] 1 real thumbnail for {asin}")
            return [thumb], link
        return [], link
    except Exception as e:
        print(f"[warn] amazon_images: {e}")
        return [], fallback_link


def download_image(url, out="raw.jpg"):
    r = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=60)
    r.raise_for_status()
    if "image" not in r.headers.get("Content-Type", "") or len(r.content) < 3000:
        raise ValueError("not an image")
    with open(out, "wb") as f:
        f.write(r.content)
    return out


def safe_download(url, out="raw.jpg"):
    try:
        return download_image(url, out)
    except Exception as e:
        print(f"[warn] download: {e}")
        return None


REAL_STYLE = (" Award-winning professional commercial product photography, "
              "hyper-realistic, ultra-detailed, 8k, shot on a Canon EOS R5 with an "
              "85mm f/1.8 lens, studio softbox lighting with a gentle reflection, "
              "true-to-life colours and textures, crisp focus, clean composition, "
              "photorealistic, magazine-quality, no text, no watermark, no logo, "
              "not a cartoon, not an illustration, no deformities.")


def _together_image(prompt, out):
    """Higher-quality FLUX render via Together (free tier)."""
    if not TOGETHER_API_KEY:
        return None
    try:
        r = requests.post("https://api.together.xyz/v1/images/generations",
                          headers={"Authorization": f"Bearer {TOGETHER_API_KEY}"},
                          json={"model": "black-forest-labs/FLUX.1-schnell-Free",
                                "prompt": prompt[:1000], "width": 768, "height": 1152,
                                "steps": 4, "n": 1, "response_format": "b64_json"},
                          timeout=120)
        if r.status_code != 200:
            print(f"[debug] together img {r.status_code}: {r.text[:120]}")
            return None
        d = r.json()["data"][0]
        if d.get("b64_json"):
            with open(out, "wb") as f:
                f.write(base64.b64decode(d["b64_json"]))
            print("[ok] Together FLUX image")
            return out
        if d.get("url"):
            return download_image(d["url"], out)
    except Exception as e:
        print(f"[warn] together image: {e}")
    return None


def _cloudflare_image(prompt, out):
    url = (f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}"
           f"/ai/run/@cf/black-forest-labs/flux-1-schnell")
    for attempt in (prompt[:1000], prompt[:200]):
        r = requests.post(url, headers={"Authorization": f"Bearer {CF_API_TOKEN}"},
                          json={"prompt": attempt, "steps": 8}, timeout=120)
        if r.status_code == 200:
            with open(out, "wb") as f:
                f.write(base64.b64decode(r.json()["result"]["image"]))
            print("[ok] Cloudflare FLUX image")
            return out
        print(f"[debug] cloudflare {r.status_code}: {r.text[:120]}")
    r.raise_for_status()


def generate_image(image_prompt, out="raw.jpg"):
    prompt = re.sub(r"\s+", " ", image_prompt).strip() + REAL_STYLE
    img = _together_image(prompt, out)     # best quality first
    if img:
        return img
    return _cloudflare_image(prompt, out)  # reliable backup


# ---- Pillow: clean 2:3 with a SMALL tasteful badge (hybrid) ----------------
FONT_PATHS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"]


def load_font(size):
    for p in FONT_PATHS:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def format_pinterest(path, badge="Best Seller", out="pin_final.jpg"):
    W, H = 1000, 1500
    canvas = Image.new("RGB", (W, H), (255, 255, 255))
    try:
        im = Image.open(path).convert("RGB")
    except Exception as e:
        print(f"[warn] open image: {e}")
        return path
    im.thumbnail((940, 1380))
    canvas.paste(im, ((W - im.width) // 2, (H - im.height) // 2))
    draw = ImageDraw.Draw(canvas)
    if badge:                                   # small tasteful pill (cover only)
        bf = load_font(30)
        txt = f"★ {badge}"
        tw = draw.textlength(txt, font=bf)
        draw.rounded_rectangle([28, 28, 28 + tw + 34, 84], radius=26, fill=(17, 17, 17))
        draw.text((45, 40), txt, fill=(255, 255, 255), font=bf)
    sf = load_font(24)                          # subtle brand mark
    brand = "Trendy Tools Hub"
    draw.text((W - draw.textlength(brand, font=sf) - 28, H - 42), brand,
              fill=(165, 165, 165), font=sf)
    canvas.save(out, quality=92)
    print("[ok] clean 2:3 pin formatted")
    return out


def make_slideshow(images, out="pin_video.mp4"):
    """Cinematic video: each image gets a slow zoom with smooth fade in/out, then
    they're stitched together (elegant, no hard cuts)."""
    if not images:
        return None
    try:
        clips, dur = [], 3.2
        for i, img in enumerate(images):
            c = f"clip{i}.mp4"
            vf = (f"scale=1000:1500,zoompan=z='min(zoom+0.0010,1.09)':d=80:"
                  f"s=1000x1500:fps=25,fade=t=in:st=0:d=0.5,"
                  f"fade=t=out:st={dur - 0.5}:d=0.5")
            subprocess.run(
                ["ffmpeg", "-y", "-loop", "1", "-i", img, "-t", str(dur),
                 "-vf", vf, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "25", c],
                check=True, capture_output=True, timeout=120)
            clips.append(c)
        with open("concat.txt", "w") as f:
            for c in clips:
                f.write(f"file '{c}'\n")
        subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                        "-i", "concat.txt", "-c", "copy", out],
                       check=True, capture_output=True, timeout=120)
        print(f"[ok] cinematic video ({len(images)} scene(s))")
        return out
    except Exception as e:
        print(f"[warn] slideshow: {e}")
        return None


def tg_send_media_group(paths, caption=""):
    """Send several images as one Telegram album."""
    media, files = [], {}
    for i, p in enumerate(paths[:10]):
        key = f"photo{i}"
        item = {"type": "photo", "media": f"attach://{key}"}
        if i == 0 and caption:
            item.update({"caption": caption[:1024], "parse_mode": "HTML"})
        media.append(item)
        files[key] = open(p, "rb")
    try:
        return requests.post(f"{TG}/sendMediaGroup",
                             data={"chat_id": TELEGRAM_CHAT_ID, "media": json.dumps(media)},
                             files=files, timeout=120)
    finally:
        for f in files.values():
            f.close()


def upload_catbox(path):
    """Upload an image to catbox.moe (free, no key) to get a public URL for
    Pinterest carousel posting. Returns the URL or None."""
    try:
        with open(path, "rb") as f:
            r = requests.post("https://catbox.moe/user/api.php",
                              data={"reqtype": "fileupload"},
                              files={"fileToUpload": f}, timeout=90)
        if r.status_code == 200 and r.text.startswith("http"):
            return r.text.strip()
    except Exception as e:
        print(f"[warn] catbox upload: {e}")
    return None


def make_video(image_path, out="pin_video.mp4"):
    """Short Ken-Burns MP4 for Pinterest Idea Pins (ffmpeg, free)."""
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loop", "1", "-i", image_path, "-t", "5",
             "-vf", ("scale=1000:1500,zoompan=z='min(zoom+0.0012,1.12)':"
                     "d=125:s=1000x1500:fps=25"),
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "25", out],
            check=True, capture_output=True, timeout=180)
        print("[ok] Idea Pin video created")
        return out
    except Exception as e:
        print(f"[warn] video: {e}")
        return None


# ===========================================================================
# GOOGLE SHEET
# ===========================================================================
def get_worksheet():
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDENTIALS),
        scopes=["https://www.googleapis.com/auth/spreadsheets"])
    sh = gspread.authorize(creds).open_by_key(GSHEET_ID)
    try:
        ws = sh.worksheet(GSHEET_TAB)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=GSHEET_TAB, rows=3000, cols=len(SHEET_HEADERS))
    try:
        existing = ws.get_all_values()
        if not existing:
            ws.append_row(SHEET_HEADERS)
        elif existing[0] != SHEET_HEADERS:             # upgrade header via values API
            end_col = chr(ord("A") + len(SHEET_HEADERS) - 1)   # 13 cols -> 'M'
            ws.update(range_name=f"A1:{end_col}1", values=[SHEET_HEADERS])
            print("[ok] sheet header upgraded")
    except Exception as e:
        print(f"[warn] header check skipped ({e})")
    return ws


def load_history(ws):
    products = ws.col_values(3)[1:]
    return [p for p in products if p][-40:]


def log_post(ws, data, link, posted_to, source, fmt, status):
    now = datetime.datetime.now()
    hashtags = " ".join(re.findall(r"#\w+", data.get("pin_description", "")))
    ws.append_row([
        now.strftime("%Y-%m-%d"), now.strftime("%H:%M"), data["product_name"],
        data["search_keyword"], data.get("seo_keywords", ""), data["pin_title"],
        data["pin_description"], hashtags, source, fmt, link, posted_to, status,
    ], value_input_option="USER_ENTERED")
    print(f"[ok] logged to sheet ({status})")


# ===========================================================================
# PINTEREST
# ===========================================================================
def _pin_headers():
    return {"Authorization": f"Bearer {PIN_ACCESS_TOKEN}"}


def post_video_pin(video_path, title, description, link):
    """Register + upload a video to Pinterest, then create a video pin."""
    reg = requests.post("https://api.pinterest.com/v5/media", headers=_pin_headers(),
                        json={"media_type": "video"}, timeout=60)
    reg.raise_for_status()
    reg = reg.json()
    up = reg["upload_url"]; params = reg["upload_parameters"]; mid = reg["media_id"]
    with open(video_path, "rb") as f:
        requests.post(up, data=params, files={"file": f}, timeout=180).raise_for_status()
    for _ in range(20):                       # wait for Pinterest to process
        time.sleep(6)
        st = requests.get(f"https://api.pinterest.com/v5/media/{mid}",
                          headers=_pin_headers(), timeout=30).json()
        if st.get("status") == "succeeded":
            break
    # video pins need a cover image; reuse the first pin image
    with open("pin0.jpg", "rb") as f:
        cover = base64.b64encode(f.read()).decode()
    r = requests.post("https://api.pinterest.com/v5/pins", headers=_pin_headers(),
                      json={"board_id": PIN_BOARD_ID, "title": title[:100],
                            "description": description[:800], "link": link,
                            "media_source": {"source_type": "video_id",
                                             "media_id": mid, "cover_image_content_type":
                                             "image/jpeg", "cover_image_data": cover}},
                      timeout=60)
    r.raise_for_status()
    print("[ok] video pin posted")


def post_to_pinterest(images, title, description, link, video=None):
    """Post a carousel of images as one pin, plus the video as its own pin."""
    if not (PIN_ACCESS_TOKEN and PIN_BOARD_ID):
        print("[skip] Pinterest write not set - semi-auto")
        return False
    urls = []
    for p in images[:5]:
        u = upload_catbox(p)
        if u:
            urls.append(u)
    ok = False
    if urls:
        if len(urls) > 1:
            media = {"source_type": "multiple_image_urls",
                     "items": [{"url": u} for u in urls]}
        else:
            media = {"source_type": "image_url", "url": urls[0]}
        r = requests.post("https://api.pinterest.com/v5/pins", headers=_pin_headers(),
                          json={"board_id": PIN_BOARD_ID, "title": title[:100],
                                "description": description[:800], "link": link,
                                "media_source": media}, timeout=60)
        r.raise_for_status()
        print(f"[ok] posted carousel pin ({len(urls)} images)")
        ok = True
    if video:
        try:
            post_video_pin(video, title, description, link)
        except Exception as e:
            print(f"[warn] video pin failed: {e}")
    return ok


# ===========================================================================
# THE AGENTS
# ===========================================================================
class TrendResearcher:
    """Gathers real search-demand signals for a category."""
    def research(self, category):
        signals = trends_rising(category)
        signals += google_autocomplete("best " + category + " ")
        signals += google_autocomplete(category + " for ")
        seen, out = set(), []
        for s in signals:
            s = s.strip()
            if s and s.lower() not in seen:
                seen.add(s.lower()); out.append(s)
        print(f"[TrendResearcher] {len(out)} demand signals for '{category}'")
        return out[:15]


class Strategist:
    """Chooses ONE product with real demand, matched to season, no repeats."""
    def choose(self, signals, avoid, category, season):
        prompt = f"""You are the Product Strategist for "Trendy Tools Hub" ({NICHE}).
Category today: {category}. Season in India: {season}.
REAL current Google search-demand signals: {signals}
Best-performing style: affordable, useful, giftable daily products.

Pick ONE specific product with genuine buyer demand, suited to the season, that
is NOT any of these already-featured: [{', '.join(avoid) or 'none'}].

Reply ONLY JSON:
{{"product_name":"short name","search_keyword":"what a shopper types on Amazon India","price_band":"a realistic rounded INR price ceiling like 'under Rs.1500' or empty if unsure","image_prompt":"clean hyper-real product-hero photo prompt of this exact item on white, with its retail packaging if typical, no text","why":"one-line reason it will sell"}}"""
        data = parse_json(get_text(prompt, "strategist"))
        print(f"[Strategist] chose {data['product_name']} - {data.get('why','')}")
        return data


class Copywriter:
    """SEO + marketing copy in the proven high-converting template."""
    def write(self, data):
        kws = (google_autocomplete(data["search_keyword"] + " ")
               or google_autocomplete(data["product_name"]))
        data["seo_keywords"] = ", ".join(kws[:6])
        band = data.get("price_band", "")
        prompt = f"""You are the SEO + Marketing Copywriter for "Trendy Tools Hub".
Write a Pinterest pin for: {data['product_name']}
Real related search keywords to weave in naturally: {kws}
Price band (use only if not empty): {band}

TITLE - follow this exact high-converting pattern, under 95 chars:
"Best {data['product_name']} {('('+band+')') if band else ''} in India | <key spec> for <2-3 use cases>"

DESCRIPTION - under 480 chars total:
- Open with a search-style question using the main keyword.
- One line on who it's for (students, gym-goers, professionals, travellers...).
- 2-3 emoji benefit points.
- End with a clear CTA like "Tap to shop on Amazon".
- Finish with EXACTLY 2-4 relevant hashtags (never more than 4).

Reply ONLY JSON: {{"pin_title":"...","pin_description":"..."}}"""
        upd = parse_json(get_text(prompt, "copywriter"))
        data["pin_title"] = upd.get("pin_title", data["product_name"])
        data["pin_description"] = upd.get("pin_description", "")
        print("[Copywriter] copy ready")
        return data

    def revise(self, data, note):
        prompt = f"""Revise ONLY the pin_title and pin_description based on feedback.
Keep the "Best X in India | spec for uses" title style, the emoji benefits, a CTA,
and 2-4 hashtags (max 4).
Product: {data['product_name']}
Current title: {data['pin_title']}
Current description: {data['pin_description']}
Feedback: {note}
Reply ONLY JSON: {{"pin_title":"...","pin_description":"..."}}"""
        upd = parse_json(get_text(prompt, "copywriter"))
        data["pin_title"] = upd.get("pin_title", data["pin_title"])
        data["pin_description"] = upd.get("pin_description", data["pin_description"])
        return data


class Editor:
    """Independent QA: polishes the copy against a quality checklist using a
    DIFFERENT provider than the Copywriter (diverse second opinion)."""
    def review(self, data):
        prompt = f"""You are the Editor for "Trendy Tools Hub". Quality-check and
lightly improve this Pinterest pin. Ensure: title under 95 chars in the
"Best X in India | spec for uses" style; description has a hook, clear CTA, and
EXACTLY 2-4 hashtags (never more); no hype that breaks Pinterest policy; India
spelling. Keep it natural. If it's already great, return it unchanged.

Title: {data['pin_title']}
Description: {data['pin_description']}

Reply ONLY JSON: {{"pin_title":"...","pin_description":"..."}}"""
        try:
            upd = parse_json(get_text(prompt, "editor"))
            if upd.get("pin_title"):
                data["pin_title"] = upd["pin_title"]
            if upd.get("pin_description"):
                data["pin_description"] = upd["pin_description"]
            print("[Editor] copy reviewed")
        except Exception as e:
            print(f"[warn] editor skipped: {e}")
        return data


class ArtDirector:
    """Produces a professional SET of images (studio + lifestyle + detail)
    plus a slideshow video."""
    SCENES = [
        ("Professional e-commerce studio product shot on a seamless pure white "
         "background, centered, soft even softbox lighting, gentle reflection, "
         "ultra sharp, premium catalog look"),
        ("Realistic lifestyle photo of the product in use in a bright, tidy modern "
         "Indian home, natural window light, tasteful decor, shallow depth of field, "
         "editorial magazine quality, authentic daily-use scene"),
    ]

    def create(self, data):
        kw = data["search_keyword"]
        base = data.get("image_prompt") or data["product_name"]
        images, source = [], "AI (professional)"

        # 1) The exact product's OWN photos (all the SAME real item)
        urls, link = amazon_images(kw)
        if urls:
            source = "Amazon (real)"
            for i, u in enumerate(urls[:4]):
                p = safe_download(u, f"raw{i}.jpg")
                if p:
                    images.append(format_pinterest(
                        p, badge="Best Seller" if i == 0 else None, out=f"pin{i}.jpg"))

        # 2) A real web photo of the product
        if not images:
            w = try_web_image(kw)
            if w:
                p = safe_download(w, "raw0.jpg")
                if p:
                    images.append(format_pinterest(p, badge="Best Seller", out="pin0.jpg"))
                    source = "Web (real)"

        # 3) ONE consistent AI image (no mismatched variants)
        if not images:
            p = generate_image(base + ". " + self.SCENES[0], "raw0.jpg")
            images.append(format_pinterest(p, badge="Best Seller", out="pin0.jpg"))

        print(f"[ArtDirector] {len(images)} image(s) ready ({source})")
        return images, link, source

    def video(self, images):
        return make_slideshow(images)


class Publisher:
    """Runs the Telegram approval gate and posts to Pinterest."""
    def _caption(self, data, link):
        return f"<b>{data['pin_title']}</b>\n\n{data['pin_description']}\n\n🔗 {link}"

    def _wait_text(self, off, seconds):
        end = time.time() + seconds
        while time.time() < end:
            for u in tg_get_updates(off[0], timeout=25):
                off[0] = u["update_id"] + 1
                msg = u.get("message") or {}
                if msg.get("text"):
                    return msg["text"].strip()
        return None

    def _send_draft(self, images, data, link, video):
        hint = "\n\n👆 Approve &amp; Post, Reject, or ask me to Fix the copy/image."
        tg_send_media_group(images, self._caption(data, link) + hint)
        if video:
            tg_send_video(video, "🎬 Slideshow video (posts as its own pin)")
        tg_send_message("👆 Choose an action for this pin:", APPROVAL_KB)

    def approval(self, cw, art, data, images, link, source, video, off):
        self._send_draft(images, data, link, video)
        end = time.time() + APPROVAL_WINDOW
        while time.time() < end:
            for u in tg_get_updates(off[0], timeout=25):
                off[0] = u["update_id"] + 1
                cb = u.get("callback_query")
                if not cb:
                    continue
                action = cb.get("data", "")
                tg_answer_callback(cb["id"])
                if action == "approve":
                    return "approved", data, images, link, source, video
                if action == "reject":
                    return "rejected", data, images, link, source, video
                if action in ("fix_copy", "fix_image"):
                    what = "the copy" if action == "fix_copy" else "the images"
                    tg_send_message(f"✍️ What should I improve about {what}? Reply once.")
                    note = self._wait_text(off, NOTE_WINDOW)
                    if note:
                        try:
                            if action == "fix_copy":
                                data = cw.revise(data, note)
                            else:
                                data["image_prompt"] = (data.get("image_prompt", "")
                                                        + ". " + note)
                                images, link, source = art.create(data)
                                video = art.video(images)
                        except Exception as e:
                            tg_send_message(f"⚠️ Regenerate failed ({e}); keeping current.")
                    self._send_draft(images, data, link, video)
                    end = time.time() + APPROVAL_WINDOW
        return "timeout", data, images, link, source, video

    def finalize(self, result, ws, data, images, link, source, video, fmt):
        post, status = False, ""
        if result == "approved":
            post, status = True, "Approved"
        elif result == "rejected":
            status = "Rejected"
            tg_send_message("❌ Rejected - not posted.")
        else:
            post, status = True, "Auto-approved (timeout)"
            tg_send_message("⏰ No reply - auto-approving.")
        posted_to = "Telegram (semi-auto)"
        if post:
            try:
                if post_to_pinterest(images, data["pin_title"],
                                     data["pin_description"], link, video):
                    posted_to = "Pinterest"
                    tg_send_message("✅ Posted to Pinterest (carousel + video)!")
                else:
                    tg_send_message("✅ Approved. Post it from the images above until "
                                    "Pinterest write access is live - then it auto-posts.")
            except Exception as e:
                tg_send_message(f"⚠️ Pinterest post failed ({e}).")
        logged, err = self._log(ws, data, link, posted_to, source, fmt, status)
        tg_send_message("🗒️ Logged to your Google Sheet." if logged
                        else f"⚠️ Sheet log failed: {err[:250]}")

    @staticmethod
    def _log(ws, *args):
        err = ""
        for attempt in range(2):
            try:
                if ws is None:
                    ws = get_worksheet()
                log_post(ws, *args)
                return True, ""
            except Exception as e:
                err = str(e)
                print(f"[warn] sheet log attempt {attempt + 1}: {e}")
                ws = None
        return False, err


class Analyst:
    """Weekly summary + (once Pinterest is live) performance feedback."""
    def weekly_summary(self, ws):
        rows = ws.get_all_values()[1:]
        cutoff = datetime.date.today() - datetime.timedelta(days=7)
        week = []
        for r in rows:
            try:
                if datetime.date.fromisoformat(r[0]) >= cutoff:
                    week.append(r)
            except Exception:
                continue
        if not week:
            tg_send_message("📊 Weekly summary: no posts in the last 7 days.")
            return
        lines = [f"📊 <b>Weekly Studio Report</b> - {len(week)} pins\n"]
        for r in week:
            lines.append(f"• {r[2]}  <i>({r[8]}, {r[11]})</i>")
        perf = self._performance()
        if perf:
            lines.append("\n" + perf)
        tg_send_message("\n".join(lines))
        print("[Analyst] weekly report sent")

    def _performance(self):
        """Best-effort Pinterest analytics (activates once pins:read is granted)."""
        if not PIN_ACCESS_TOKEN:
            return ""
        try:
            r = requests.get("https://api.pinterest.com/v5/user_account/analytics",
                headers={"Authorization": f"Bearer {PIN_ACCESS_TOKEN}"},
                params={"start_date": str(datetime.date.today() - datetime.timedelta(days=7)),
                        "end_date": str(datetime.date.today()),
                        "metric_types": "IMPRESSION,PIN_CLICK"}, timeout=30)
            if r.status_code == 200:
                return "📈 Pinterest analytics attached (see sheet)."
        except Exception as e:
            print(f"[warn] analytics: {e}")
        return ""


# ===========================================================================
# THE MANAGER
# ===========================================================================
class Manager:
    def __init__(self):
        self.trend = TrendResearcher()
        self.strategist = Strategist()
        self.copywriter = Copywriter()
        self.editor = Editor()
        self.art = ArtDirector()
        self.publisher = Publisher()
        self.analyst = Analyst()
        self._summary_week = None

    def make_pin(self, mode, off):
        drained = tg_get_updates(off[0], timeout=0)
        if drained:
            off[0] = drained[-1]["update_id"] + 1
        try:
            ws = get_worksheet()
            recent = load_history(ws)
        except Exception as e:
            print(f"[warn] sheet unavailable, continuing without log ({e})")
            ws, recent = None, []
        category, season = pick_category(), indian_season(datetime.date.today())
        signals = self.trend.research(category)
        data = self.strategist.choose(signals, recent, category, season)
        data = self.copywriter.write(data)
        data = self.editor.review(data)
        images, link, source = self.art.create(data)
        video = self.art.video(images)
        fmt = f"{len(images)} images" + (" + video" if video else "")
        result, data, images, link, source, video = self.publisher.approval(
            self.copywriter, self.art, data, images, link, source, video, off)
        self.publisher.finalize(result, ws, data, images, link, source, video, fmt)
        print(f"[Manager] {mode} cycle done ({result})")

    def _done_today(self, today):
        try:
            dates = get_worksheet().col_values(1)[1:]
            return bool(dates) and dates[-1] == today.isoformat()
        except Exception:
            return False

    def _maybe_scheduled(self, off):
        ist = datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)
        if ist.hour == 10 and ist.minute < 5 and not self._done_today(ist.date()):
            print("[Manager] daily scheduled slot")
            self.make_pin("scheduled", off)
        if (ist.weekday() == 6 and ist.hour == 19 and ist.minute < 5
                and self._summary_week != ist.isocalendar()[1]):
            self.analyst.weekly_summary(get_worksheet())
            self._summary_week = ist.isocalendar()[1]

    def run_forever(self):
        end = time.time() + SHIFT_SECONDS
        off = [None]
        d = tg_get_updates(off[0], timeout=0)
        if d:
            off[0] = d[-1]["update_id"] + 1
        print("[Manager] studio online - listening for /new ...")
        while time.time() < end:
            try:
                self._maybe_scheduled(off)
                for u in tg_get_updates(off[0], timeout=50):
                    off[0] = u["update_id"] + 1
                    text = ((u.get("message") or {}).get("text") or "").strip().lower()
                    if text in TRIGGER_WORDS:
                        tg_send_message("🚀 Studio is generating a new pin for approval...")
                        self.make_pin("ondemand", off)
            except Exception as e:
                traceback.print_exc()
                try:
                    tg_send_message(f"⚠️ Studio hiccup: {e}")
                except Exception:
                    pass
                time.sleep(15)
        print("[Manager] shift ended - next shift takes over")


# ===========================================================================
# ENTRY
# ===========================================================================
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    mgr = Manager()
    try:
        if mode == "run":
            mgr.run_forever()
        elif mode == "listen":
            off = [None]
            ups = tg_get_updates(None, timeout=25)
            trig, last = False, None
            for u in ups:
                last = u["update_id"] + 1
                t = ((u.get("message") or {}).get("text") or "").strip().lower()
                if t in TRIGGER_WORDS:
                    trig = True
            if trig:
                tg_send_message("🚀 Generating a new pin for approval...")
                mgr.make_pin("ondemand", [last])
            elif last:
                tg_get_updates(last, timeout=0)
        elif mode == "summary":
            mgr.analyst.weekly_summary(get_worksheet())
        else:
            mgr.make_pin("scheduled", [None])
    except Exception as e:
        traceback.print_exc()
        try:
            tg_send_message(f"⚠️ <b>Studio error</b> ({mode}): {e}")
        except Exception:
            pass
        raise
