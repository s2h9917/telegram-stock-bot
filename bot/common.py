"""공통 기능: 설정/상태 파일, 텔레그램 전송, Gemini 요약, 이미지 카드 생성."""
import datetime as dt
import html
import io
import json
import os
import textwrap
import time
from zoneinfo import ZoneInfo

import requests

KST = ZoneInfo("Asia/Seoul")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
}
DRY_RUN = os.getenv("DRY_RUN") == "1"


# ---------------------------------------------------------------- 시간/설정
def now():
    return dt.datetime.now(KST)


def load_config():
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def is_market_day(cfg, day=None):
    """주말·휴장일(config.json의 holidays)이면 False"""
    day = day or now().date()
    return day.weekday() < 5 and day.isoformat() not in cfg.get("holidays", [])


def load_state(name):
    path = os.path.join(ROOT, "state", f"{name}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(name, state):
    os.makedirs(os.path.join(ROOT, "state"), exist_ok=True)
    with open(os.path.join(ROOT, "state", f"{name}.json"), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def add_history(state, title, link=""):
    """관리자 화면 '발송 이력'에 표시할 기록 (최근 50건 유지)"""
    hist = state.setdefault("history", [])
    hist.insert(0, {"time": now().strftime("%Y-%m-%d %H:%M"), "title": title, "link": link})
    del hist[50:]


def esc(s):
    return html.escape(str(s or ""), quote=False)


# ---------------------------------------------------------------- 텔레그램
def _tg(method, cfg, data=None, files=None):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID") or cfg["telegram"]["chat_id"]
    if DRY_RUN or not token:
        print(f"\n[DRY-RUN {method}] -> {chat_id}\n{(data or {}).get('text') or (data or {}).get('caption')}\n")
        return True
    payload = {"chat_id": chat_id, **(data or {})}
    for attempt in range(3):
        r = requests.post(f"https://api.telegram.org/bot{token}/{method}",
                          data=payload, files=files, timeout=30)
        if r.status_code == 429:  # 너무 빨리 보내면 텔레그램이 잠깐 기다리라고 함
            time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
            continue
        if not r.ok:
            print("Telegram error:", r.status_code, r.text)
        return r.ok
    return False


def split_text(text, limit=4000):
    parts, buf = [], ""
    for block in text.split("\n\n"):
        if len(buf) + len(block) + 2 > limit and buf:
            parts.append(buf)
            buf = ""
        buf = f"{buf}\n\n{block}" if buf else block
    if buf:
        parts.append(buf)
    return [p[:limit] for p in parts]


def send_text(text, cfg, preview=None):
    if preview is None:
        preview = not cfg["telegram"].get("disable_preview", True)
    ok = True
    for part in split_text(text):
        ok &= _tg("sendMessage", cfg, {
            "text": part, "parse_mode": "HTML",
            "disable_web_page_preview": "false" if preview else "true",
        })
        time.sleep(1.2)
    return ok


def send_photo(png_bytes, caption, cfg):
    return _tg("sendPhoto", cfg, {"caption": caption[:1024], "parse_mode": "HTML"},
               files={"photo": ("card.png", png_bytes, "image/png")})


# ---------------------------------------------------------------- Gemini (무료 AI 요약)
def gemini(prompt, cfg, want_json=False):
    """요약 실패 시 None 반환 → 각 스크립트가 AI 없이 보내는 방식으로 대체"""
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        print("GEMINI_API_KEY 없음 → AI 요약 생략")
        return None
    model = cfg.get("gemini", {}).get("model", "gemini-flash-latest")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.3}}
    if want_json:
        body["generationConfig"]["responseMimeType"] = "application/json"
    for attempt in range(4):
        try:
            r = requests.post(url, params={"key": key}, json=body, timeout=120)
            if r.status_code in (429, 500, 503):
                print("Gemini 대기 후 재시도:", r.status_code)
                time.sleep(20 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception as e:  # noqa
            print("Gemini 오류:", e)
            time.sleep(5)
    return None


# ---------------------------------------------------------------- 이미지 카드
FONT_DIRS = ["/usr/share/fonts/truetype/nanum", "C:/Windows/Fonts", "/Library/Fonts",
             "/System/Library/Fonts/Supplemental"]


def _font(size, bold=False):
    from PIL import ImageFont
    names = (["NanumGothicBold.ttf", "malgunbd.ttf", "AppleGothic.ttf"] if bold
             else ["NanumGothic.ttf", "malgun.ttf", "AppleGothic.ttf"])
    for d in FONT_DIRS:
        for n in names:
            p = os.path.join(d, n)
            if os.path.exists(p):
                return ImageFont.truetype(p, size)
    noto = f"/usr/share/fonts/opentype/noto/NotoSansCJK-{'Bold' if bold else 'Regular'}.ttc"
    if os.path.exists(noto):
        return ImageFont.truetype(noto, size, index=1)  # index 1 = 한국어
    return ImageFont.load_default(size)


def make_card(badge, title, lines, footer, accent=(220, 38, 38)):
    """1080x1080 정사각 카드 이미지 (PNG bytes)"""
    from PIL import Image, ImageDraw
    W = H = 1080
    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 16], fill=accent)
    d.text((70, 70), badge, font=_font(34, True), fill=accent)
    y = 130
    for t in textwrap.wrap(title, 22)[:3]:
        d.text((70, y), t, font=_font(54, True), fill=(17, 17, 17))
        y += 72
    y += 30
    d.line([70, y, W - 70, y], fill=(225, 225, 225), width=3)
    y += 40
    body = _font(38)
    for line in lines:
        for i, t in enumerate(textwrap.wrap(line, 26)):
            d.text((70, y), ("• " if i == 0 else "   ") + t, font=body, fill=(40, 40, 40))
            y += 56
            if y > H - 160:
                break
        y += 14
    d.text((70, H - 100), footer, font=_font(28), fill=(130, 130, 130))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
