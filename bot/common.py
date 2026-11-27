"""공통 기능: 설정/상태 파일, 텔레그램 전송, Gemini 요약, 이미지 카드 생성."""
import datetime as dt
import html
import io
import json
import os
import re
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


def tg_call(method, cfg, data=None):
    """텔레그램 API 호출 후 결과 JSON 반환 (고정 메시지 수정 등 결과값이 필요할 때)"""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID") or cfg["telegram"]["chat_id"]
    if DRY_RUN or not token:
        print(f"[DRY-RUN {method}] {json.dumps(data or {}, ensure_ascii=False)[:300]}")
        return {"ok": True, "result": {"message_id": 999}}
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}",
                      data={"chat_id": chat_id, **(data or {})}, timeout=30)
    j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"ok": False}
    if not j.get("ok"):
        print(f"Telegram {method} 실패:", r.status_code, r.text[:200])
    return j


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


def send_album(pngs, caption, cfg):
    """이미지 여러 장을 앨범(넘겨 보기)으로 한 번에 발송. 1장이면 일반 사진으로"""
    if len(pngs) == 1:
        return send_photo(pngs[0], caption, cfg)
    media = [{"type": "photo", "media": f"attach://p{i}"} for i in range(len(pngs))]
    media[0].update(caption=caption[:1024], parse_mode="HTML")
    files = {f"p{i}": (f"card{i}.png", b, "image/png") for i, b in enumerate(pngs)}
    return _tg("sendMediaGroup", cfg, {"media": json.dumps(media, ensure_ascii=False), "caption": caption},
               files=files)


def send_photo(png_bytes, caption, cfg, above=False):
    """above=True: 글을 사진 위에 표시 (글 → 사진 순서)"""
    data = {"caption": caption if plain_len(caption) <= 1024 else caption[:1024], "parse_mode": "HTML"}
    if above:
        data["show_caption_above_media"] = "true"
    return _tg("sendPhoto", cfg, data, files={"photo": ("card.png", png_bytes, "image/png")})


def plain_len(html_text):
    """텔레그램이 세는 글자 수 (HTML 태그 제외) — 사진 설명글은 1,024자 제한"""
    return len(html.unescape(re.sub(r"<[^>]+>", "", html_text)))


# ---------------------------------------------------------------- Gemini (무료 AI 요약)
# 설정한 모델이 안 되면 차례로 시도할 무료 모델들
GEMINI_FALLBACKS = ["gemini-flash-latest", "gemini-flash-lite-latest", "gemini-2.5-flash",
                    "gemini-2.5-flash-lite"]


# 모든 AI 작성물(브리핑·리포트 요약·핫이슈·종목뉴스·카드뉴스 등)에 공통으로 적용되는 작성자 관점.
# 관리자 화면 > 설정 > 'AI 작성 관점'에서 바꿀 수 있음 (비우면 이 기본값 사용)
DEFAULT_PERSONA = """너는 국내 증권사 리서치센터장과 경제신문 증권부장을 두루 거친 30년 경력의 증권·경제 전문가다.
글을 쓸 때 다음을 지킨다.
1. 사실 우선: 주어진 자료에 있는 수치·사실만 쓰고, 없는 숫자·날짜·발언은 절대 만들지 않는다.
2. 인과 연결: 단순 나열 대신 '무엇이 → 왜 → 시장(업종·종목)에 어떤 의미인지'를 짧게 연결한다.
   (예: 금리·환율·유가·수급·실적 사이의 연결고리, 외국인·기관 수급의 방향)
3. 전문가의 어휘: 밸류에이션, 컨센서스 대비, 수급, 리레이팅, 실적 가시성 등 업계 용어를 쓰되
   초보 투자자도 이해할 수 있게 한 문장 안에서 풀어 쓴다.
4. 균형: 호재와 함께 리스크·변수도 짚는다. 과장·선동 표현('폭등 확정', '무조건')은 쓰지 않는다.
5. 매수·매도 권유, 목표가 제시, 수익 보장 표현은 하지 않는다. 판단은 독자에게 맡긴다.
6. 간결함: 군더더기 없이 짧고 단단한 문장. 자기소개나 '전문가로서' 같은 표현은 쓰지 않는다.
7. 요청된 출력 형식(JSON 등)과 글자 수 제한은 반드시 지킨다."""


_EXHAUSTED = set()
_DISCOVERED = None


def flash_models(key):
    """이 키로 쓸 수 있는 'X.Y Flash' 정식 모델 목록 (최신 버전 순). 예: gemini-3.8-flash, gemini-3.6-flash
    기본 모델이 혼잡(503)·시간 초과일 때, 품질이 비슷한 이전 Flash로 먼저 넘어가기 위해 사용"""
    global _DISCOVERED
    if _DISCOVERED is not None:
        return _DISCOVERED
    _DISCOVERED = []
    try:
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                         params={"key": key, "pageSize": 200}, timeout=15)
        found = []
        for m in r.json().get("models", []):
            name = m.get("name", "").split("/")[-1]
            mt = re.fullmatch(r"gemini-(\d+(?:\.\d+)?)-flash", name)
            if mt and "generateContent" in m.get("supportedGenerationMethods", []):
                found.append((float(mt.group(1)), name))
        _DISCOVERED = [n for _, n in sorted(found, reverse=True)][:3]
        print(f"[AI] 사용 가능한 Flash 모델: {', '.join(_DISCOVERED) or '없음'}")
    except Exception as e:  # noqa
        print("[AI] 모델 목록 조회 실패:", str(e)[:100])
    return _DISCOVERED   # 이번 실행 중 '하루 한도'가 끝난 모델 → 다시 부르지 않음


def gemini(prompt, cfg, want_json=False, light=False, timeout=60):
    """light=True: 자주 부르는 작업(종목뉴스 등)은 하루 한도가 넉넉한 가벼운 모델 우선.
    AI 호출. 모델이 없거나(404) 무료 한도가 없으면(429) 다음 모델로 자동 전환.
    모두 실패하면 None 반환 → 각 스크립트가 AI 없이 보내는 방식으로 대체"""
    key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if not key:
        print("❌ [AI] GEMINI_API_KEY가 비어 있음 → GitHub Settings > Secrets 확인")
        return None
    g = cfg.get("gemini", {})
    first = (g.get("light_model") or "gemini-flash-lite-latest") if light else (g.get("model") or "gemini-flash-latest")
    chain = GEMINI_FALLBACKS[1:] + GEMINI_FALLBACKS[:1] if light else GEMINI_FALLBACKS
    if not light:   # 기본 → (혼잡 시) 다른 정식 Flash 버전들 → 가벼운 모델 순
        chain = [chain[0]] + flash_models(key) + chain[1:]
    models = [m for m in dict.fromkeys([first] + chain) if m not in _EXHAUSTED]
    persona = (cfg.get("gemini", {}).get("persona") or "").strip() or DEFAULT_PERSONA
    body = {"systemInstruction": {"parts": [{"text": persona}]},
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.4}}
    if want_json:
        body["generationConfig"]["responseMimeType"] = "application/json"

    for model in list(models):
        if model in _EXHAUSTED:
            continue
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        # 최신 모델(Gemini 3 이상)은 답하기 전에 오래 '생각'해서 시간 초과가 남 → 생각 단계를 짧게
        mbody = json.loads(json.dumps(body))
        if not re.search(r"gemini-(1|2)\.", model):
            mbody["generationConfig"]["thinkingConfig"] = {"thinkingLevel": "low"}
        for attempt in range(3):
            try:
                r = requests.post(url, params={"key": key}, json=mbody, timeout=timeout)
            except Exception as e:  # noqa  시간 초과·접속 오류 → 같은 모델 재시도 없이 다음 모델로
                print(f"⚠️ [AI] {model} 응답 지연/접속 오류 → 다음 모델: {str(e)[:120]}")
                break
            if r.ok:
                try:
                    parts = r.json()["candidates"][0]["content"]["parts"]
                    text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
                except Exception:  # noqa
                    text = ""
                if text:
                    print(f"✅ [AI] {model} 응답 성공")
                    return text
                print(f"⚠️ [AI] {model} 빈 응답: {r.text[:200]}")
                break
            msg = r.text[:300].replace("\n", " ")
            print(f"⚠️ [AI] {model} → HTTP {r.status_code}: {msg}")
            if r.status_code == 400 and "thinking" in r.text.lower() and "thinkingConfig" in mbody["generationConfig"]:
                mbody["generationConfig"].pop("thinkingConfig")   # 이 모델은 생각 설정 미지원 → 빼고 재시도
                continue
            if r.status_code in (400, 401, 403) and "API key" in r.text:
                print("❌ [AI] API 키가 잘못됐거나 사용 중지됨 → aistudio.google.com 에서 키 재확인")
                return None
            if r.status_code == 429 and re.search(r"per ?day|PerDay|daily", r.text, re.I):
                print(f"⚠️ [AI] {model} 오늘 무료 한도 소진 → 다음 모델")
                _EXHAUSTED.add(model)
                break
            if r.status_code == 429 and "limit: 0" not in r.text and attempt < 2:
                time.sleep(25)       # 일시적인 분당 한도 → 잠깐 쉬고 같은 모델 재시도
                continue
            if r.status_code == 500 and attempt == 0:
                time.sleep(10)
                continue
            if r.status_code == 503:
                # 사용량 폭주는 보통 몇 분 이상 감 → 이번 실행 동안 이 모델은 건너뜀.
                # '-latest'(별칭)가 혼잡이면 같은 실체인 최신 버전도 같이 건너뜀
                _EXHAUSTED.add(model)
                if model.endswith("-latest") and "lite" not in model and flash_models(key):
                    _EXHAUSTED.add(flash_models(key)[0])
                models[:] = [m for m in models if m not in _EXHAUSTED or m == model]
            break                    # 404(모델 없음)·무료 한도 0 등 → 다음 모델로
    print("❌ [AI] 모든 모델 실패 → AI 없이 발송")
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


def ai_json(out, list_key=None):
    """AI 응답(JSON 문자열)을 dict로. 코드블록 표시 제거.
    AI가 객체 대신 목록([...])만 돌려준 경우 list_key가 있으면 {list_key: 목록}으로 감싸 줌"""
    import json as _json
    if not out:
        return {}
    txt = re.sub(r"^```(json)?|```$", "", str(out).strip()).strip()
    try:
        data = _json.loads(txt)
    except Exception:  # noqa
        m = re.search(r"[\[{].*[\]}]", txt, re.S)
        if not m:
            return {}
        try:
            data = _json.loads(m.group(0))
        except Exception:  # noqa
            return {}
    if isinstance(data, list):
        if len(data) == 1 and isinstance(data[0], dict) and any(isinstance(v, list) for v in data[0].values()):
            return data[0]
        return {list_key: data} if list_key else {}
    return data if isinstance(data, dict) else {}
