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
GROQ_API_KEY     = os.environ.get("GROQ_API_KEY", "")     # optional 3rd brain
OPENROUTER_KEY   = os.environ.get("OPENROUTER_KEY", "")
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
GROQ_MODELS = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
OPENROUTER_MODELS = ["meta-llama/llama-3.3-70b-instruct:free",
                     "mistralai/mistral-small-3.2-24b-instruct:free",
                     "google/gemma-3-27b-it:free"]

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
def _gemini(prompt):
    for m in GEMINI_MODELS:
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent",
                params={"key": GEMINI_API_KEY},
                json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=60)
            if r.status_code == 429:
                print(f"[info] Gemini {m} rate-limited -> next"); continue
            if r.status_code != 200:
                print(f"[debug] gemini {m} {r.status_code}: {r.text[:150]}"); continue
            print(f"[ok] text via Gemini {m}")
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            print(f"[warn] gemini {m}: {e}")
    return None


def _groq(prompt):
    for m in GROQ_MODELS:
        try:
            r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json={"model": m, "messages": [{"role": "user", "content": prompt}],
                      "temperature": 0.9}, timeout=60)
            if r.status_code != 200:
                print(f"[debug] groq {m} {r.status_code}: {r.text[:150]}"); continue
            print(f"[ok] text via Groq {m}")
            return r.json()["choices"][0]["message"]["content"]
        except Exception as e:
            print(f"[warn] groq {m}: {e}")
    return None


def _openrouter(prompt):
    for m in OPENROUTER_MODELS:
        try:
            r = requests.post("https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_KEY}"},
                json={"model": m, "messages": [{"role": "user", "content": prompt}],
                      "temperature": 0.9}, timeout=60)
            if r.status_code != 200:
                print(f"[debug] openrouter {m} {r.status_code}: {r.text[:150]}"); continue
            print(f"[ok] text via OpenRouter {m}")
            return r.json()["choices"][0]["message"]["content"]
        except Exception as e:
            print(f"[warn] openrouter {m}: {e}")
    return None


def get_text(prompt):
    for provider, key in ((_gemini, GEMINI_API_KEY), (_groq, GROQ_API_KEY),
                          (_openrouter, OPENROUTER_KEY)):
        if key:
            out = provider(prompt)
            if out:
                return out
    raise RuntimeError("All text providers failed - check keys/logs")


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


def download_image(url, out="raw.jpg"):
    r = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=60)
    r.raise_for_status()
    if "image" not in r.headers.get("Content-Type", "") or len(r.content) < 3000:
        raise ValueError("not an image")
    with open(out, "wb") as f:
        f.write(r.content)
    return out


def safe_download(url):
    try:
        return download_image(url)
    except Exception as e:
        print(f"[warn] download: {e}")
        return None


def generate_image(image_prompt, out="raw.jpg"):
    url = (f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}"
           f"/ai/run/@cf/black-forest-labs/flux-1-schnell")
    clean = re.sub(r"\s+", " ", image_prompt).strip()
    REAL = (" Hyper-realistic, ultra-detailed 4k commercial product photograph, "
            "shot on a DSLR 85mm lens, clean white studio background, soft natural "
            "lighting, true-to-life colours, realistic materials, subtle shadow, "
            "e-commerce catalog style, no text, no watermark, no logo, "
            "not a cartoon, not an illustration.")
    for attempt in (clean[:1100] + REAL, clean[:220] + REAL):
        r = requests.post(url, headers={"Authorization": f"Bearer {CF_API_TOKEN}"},
                          json={"prompt": attempt, "steps": 6}, timeout=120)
        if r.status_code == 200:
            with open(out, "wb") as f:
                f.write(base64.b64decode(r.json()["result"]["image"]))
            print("[ok] hyper-real AI image generated")
            return out
        print(f"[debug] cloudflare {r.status_code}: {r.text[:150]}")
    r.raise_for_status()


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
    # small tasteful pill badge, top-left (no big title text)
    bf = load_font(30)
    txt = f"★ {badge}"
    tw = draw.textlength(txt, font=bf)
    draw.rounded_rectangle([28, 28, 28 + tw + 34, 84], radius=26, fill=(17, 17, 17))
    draw.text((45, 40), txt, fill=(255, 255, 255), font=bf)
    # subtle brand mark, bottom-right
    sf = load_font(24)
    brand = "Trendy Tools Hub"
    draw.text((W - draw.textlength(brand, font=sf) - 28, H - 42), brand,
              fill=(165, 165, 165), font=sf)
    canvas.save(out, quality=92)
    print("[ok] clean 2:3 pin formatted")
    return out


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
    existing = ws.get_all_values()
    if not existing:
        ws.append_row(SHEET_HEADERS)
    elif existing[0] != SHEET_HEADERS:                 # auto-upgrade header row
        cells = ws.range(1, 1, 1, len(SHEET_HEADERS))
        for c, val in zip(cells, SHEET_HEADERS):
            c.value = val
        ws.update_cells(cells)
        print("[ok] sheet header upgraded")
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
def post_to_pinterest(image_path, title, description, link):
    if not (PIN_ACCESS_TOKEN and PIN_BOARD_ID):
        print("[skip] Pinterest write not set - semi-auto")
        return False
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    r = requests.post("https://api.pinterest.com/v5/pins",
                      headers={"Authorization": f"Bearer {PIN_ACCESS_TOKEN}"},
                      json={"board_id": PIN_BOARD_ID, "title": title[:100],
                            "description": description[:800], "link": link,
                            "media_source": {"source_type": "image_base64",
                                             "content_type": "image/jpeg", "data": b64}},
                      timeout=60)
    r.raise_for_status()
    print("[ok] posted to Pinterest")
    return True


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
        data = parse_json(get_text(prompt))
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
        upd = parse_json(get_text(prompt))
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
        upd = parse_json(get_text(prompt))
        data["pin_title"] = upd.get("pin_title", data["pin_title"])
        data["pin_description"] = upd.get("pin_description", data["pin_description"])
        return data


class ArtDirector:
    """Sources a clean, hyper-real image and an Idea-Pin video."""
    def create(self, data):
        kw = data["search_keyword"]
        link = self._bestseller_link(kw)
        source, raw = "AI (hyper-real)", None
        a_url, a_link = try_amazon_product(kw)
        if a_url:
            raw = safe_download(a_url)
            if raw:
                link, source = a_link, "Amazon (real)"
        if raw is None:
            w = try_web_image(kw)
            if w:
                raw = safe_download(w)
                if raw:
                    source = "Web (real)"
        if raw is None:
            raw = generate_image(data["image_prompt"])
        img = format_pinterest(raw, badge="Best Seller")
        print(f"[ArtDirector] image ready ({source})")
        return img, link, source

    def video(self, img):
        return make_video(img)

    @staticmethod
    def _bestseller_link(keyword):
        q = urllib.parse.quote_plus(keyword)
        return (f"https://www.amazon.in/s?k={q}&s=review-rank"
                f"&rh=p_72:1318476031&tag={AMAZON_TAG}")


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

    def approval(self, cw, data, img, link, source, video, off):
        hint = "\n\n👆 Approve &amp; Post, Reject, or ask me to Fix the copy/image."
        tg_send_photo(img, self._caption(data, link) + hint, APPROVAL_KB)
        if video:
            tg_send_video(video, "🎬 Idea-Pin video version")
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
                    return "approved", data, img, link, source
                if action == "reject":
                    return "rejected", data, img, link, source
                if action in ("fix_copy", "fix_image"):
                    what = "the copy" if action == "fix_copy" else "the image"
                    tg_send_message(f"✍️ What should I improve about {what}? Reply once.")
                    note = self._wait_text(off, NOTE_WINDOW)
                    if note:
                        try:
                            if action == "fix_copy":
                                data = cw.revise(data, note)
                            else:
                                img = format_pinterest(
                                    generate_image(data["image_prompt"] + ". " + note))
                                source = "AI (hyper-real)"
                                video = make_video(img)
                        except Exception as e:
                            tg_send_message(f"⚠️ Regenerate failed ({e}); keeping current.")
                    tg_send_photo(img, self._caption(data, link) + hint, APPROVAL_KB)
                    if video:
                        tg_send_video(video, "🎬 Updated Idea-Pin video")
                    end = time.time() + APPROVAL_WINDOW
        return "timeout", data, img, link, source

    def finalize(self, result, ws, data, img, link, source, fmt):
        post, status = False, ""
        if result == "approved":
            post, status = True, "Approved"
        elif result == "rejected":
            status = "Rejected"
            tg_send_message("❌ Rejected - not posted. Logged in your sheet.")
        else:
            post, status = True, "Auto-approved (timeout)"
            tg_send_message("⏰ No reply - auto-approving.")
        posted_to = "Telegram (semi-auto)"
        if post:
            try:
                if post_to_pinterest(img, data["pin_title"], data["pin_description"], link):
                    posted_to = "Pinterest"
                    tg_send_message("✅ Posted to Pinterest!")
                else:
                    tg_send_message("✅ Approved. Post it from the image above until "
                                    "Pinterest write access is live - then it auto-posts.")
            except Exception as e:
                tg_send_message(f"⚠️ Pinterest post failed ({e}).")
        log_post(ws, data, link, posted_to, source, fmt, status)


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
        self.art = ArtDirector()
        self.publisher = Publisher()
        self.analyst = Analyst()
        self._summary_week = None

    def make_pin(self, mode, off):
        drained = tg_get_updates(off[0], timeout=0)
        if drained:
            off[0] = drained[-1]["update_id"] + 1
        ws = get_worksheet()
        recent = load_history(ws)
        category, season = pick_category(), indian_season(datetime.date.today())
        signals = self.trend.research(category)
        data = self.strategist.choose(signals, recent, category, season)
        data = self.copywriter.write(data)
        img, link, source = self.art.create(data)
        video = self.art.video(img)
        fmt = "Image + Video" if video else "Image"
        result, data, img, link, source = self.publisher.approval(
            self.copywriter, data, img, link, source, video, off)
        self.publisher.finalize(result, ws, data, img, link, source, fmt)
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
