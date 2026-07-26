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
import urllib.parse
import requests

# ---------------------------------------------------------------------------
# CONFIG - all values come from GitHub Secrets (never hard-code keys here)
# ---------------------------------------------------------------------------
OPENROUTER_KEY   = os.environ["OPENROUTER_KEY"]
CF_ACCOUNT_ID    = os.environ["CF_ACCOUNT_ID"]
CF_API_TOKEN     = os.environ["CF_API_TOKEN"]
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
AMAZON_TAG       = os.environ.get("AMAZON_TAG", "dailyneedss03-21")

# Optional - only needed once Pinterest approves your app
PIN_ACCESS_TOKEN = os.environ.get("PIN_ACCESS_TOKEN", "")
PIN_BOARD_ID     = os.environ.get("PIN_BOARD_ID", "")

# Free LLM models on OpenRouter. Bot tries them top-to-bottom until one works,
# so it never stops if one is rate-limited.
LLM_MODELS = [
    "meta-llama/llama-3.3-70b-instruct:free",
    "qwen/qwen3-235b-a22b:free",
    "google/gemma-3-27b-it:free",
]

# The niche/vibe for Trendy Tools Hub
NICHE = (
    "trendy, useful daily-use products, smart gadgets and lifestyle tools "
    "available on Amazon India that save time and money for everyday people"
)


# ---------------------------------------------------------------------------
# 1 + 2.  RESEARCH A PRODUCT AND WRITE THE COPY (one LLM call, JSON out)
# ---------------------------------------------------------------------------
def research_and_write():
    prompt = f"""You are a Pinterest affiliate marketer for an account called
"Trendy Tools Hub" that promotes {NICHE}.

Pick ONE specific trending product to feature today. Reply with ONLY a JSON
object (no markdown, no extra text) with exactly these keys:

{{
  "product_name": "short product name",
  "search_keyword": "the words a shopper would type into Amazon to find it",
  "pin_title": "catchy Pinterest title under 95 characters, benefit-driven",
  "pin_description": "2-3 sentences selling the product with an emotional hook, then a clear call to action like 'Tap the link to grab yours', ending with 5-7 relevant hashtags",
  "image_prompt": "a detailed prompt to generate a clean, bright, professional product-hero photo of this item on a soft studio background, no text, no watermark"
}}"""

    last_error = None
    for model in LLM_MODELS:
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
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            # Pull the JSON object out even if the model wrapped it in text
            match = re.search(r"\{.*\}", content, re.DOTALL)
            data = json.loads(match.group(0))
            print(f"[ok] research via {model}: {data['product_name']}")
            return data
        except Exception as e:
            last_error = e
            print(f"[warn] model {model} failed: {e}")
            continue
    raise RuntimeError(f"All LLM models failed. Last error: {last_error}")


# ---------------------------------------------------------------------------
# 3.  GENERATE THE IMAGE (Cloudflare Workers AI - FLUX, free)
# ---------------------------------------------------------------------------
def generate_image(image_prompt, out_path="pin.jpg"):
    url = (
        f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}"
        f"/ai/run/@cf/black-forest-labs/flux-1-schnell"
    )
    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {CF_API_TOKEN}"},
        json={"prompt": image_prompt, "steps": 6},
        timeout=120,
    )
    r.raise_for_status()
    b64 = r.json()["result"]["image"]
    with open(out_path, "wb") as f:
        f.write(base64.b64decode(b64))
    print(f"[ok] image saved -> {out_path}")
    return out_path


# ---------------------------------------------------------------------------
# 4.  BUILD THE AMAZON AFFILIATE LINK (Option A - search link)
# ---------------------------------------------------------------------------
def affiliate_link(keyword):
    q = urllib.parse.quote_plus(keyword)
    return f"https://www.amazon.in/s?k={q}&tag={AMAZON_TAG}"


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
# MAIN
# ---------------------------------------------------------------------------
def main():
    data = research_and_write()
    link = affiliate_link(data["search_keyword"])
    img = generate_image(data["image_prompt"])
    notify_telegram(img, data["pin_title"], data["pin_description"], link)
    post_to_pinterest(img, data["pin_title"], data["pin_description"], link)
    print("[done] pin cycle complete")


if __name__ == "__main__":
    main()
