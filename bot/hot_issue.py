"""④ 오늘의 핫 이슈 — 매일 아침 이슈 타이틀 3~5개를 골라 이미지 카드로 텔레그램 업로드
- 뉴스: 구글 뉴스 RSS + 네이버 금융 주요뉴스 (market_brief와 같은 수집기 사용)
- 선정·제목 다듬기: Gemini (키가 없으면 최신 헤드라인 상위 N개)
실행: python -m bot.hot_issue            (평소, 하루 1회만 발송)
      python -m bot.hot_issue --force    (중복·휴장일 체크 무시)
      DRY_RUN=1 python -m bot.hot_issue --force --save card.png   (이미지 파일로도 저장)
"""
import argparse
import io
import json
import math
import os
import random
import re
from urllib.parse import quote

import feedparser
import requests

from bot.common import (UA, add_history, esc, gemini, is_market_day, load_config, load_state,
                        now, save_state, send_photo)

W, H = 1080, 1350          # 텔레그램에서 크게 보이는 4:5 세로 비율
WEEK = "월화수목금토일"

DEFAULT_KEYWORDS = ["뉴욕증시", "코스피", "환율", "국제유가", "반도체", "금리", "관세", "비트코인"]


def clean_title(t):
    t = re.sub(r"\s+-\s+[^-]+$", "", t)                 # 끝의 '- 언론사'
    t = re.sub(r"^\s*[\[【(<][^\]】)>]{1,20}[\]】)>]\s*", "", t)  # 앞의 [뉴욕증시] 같은 말머리
    return re.sub(r"\s+", " ", t).strip()


def _bigrams(t):
    t = re.sub(r"[^\w가-힣]", "", t)
    return {t[i:i + 2] for i in range(len(t) - 1)}


GENERIC = {"증시", "주가", "마감", "상승", "하락", "전망", "투자", "시장", "급등", "급락", "속보", "종합",
           "오늘", "기대", "우려", "발표", "지속", "확대", "영향", "단독"}
SUFFIX = re.compile(r"(으로|에서|에도|에는|까지|부터|은|는|이|가|을|를|에|의|도|로|와|과|세|만)$")


def _keywords(t):
    words = [SUFFIX.sub("", w) for w in re.findall(r"[A-Za-z가-힣&]{2,}", t)]
    return {w.lower() for w in words if len(w) >= 2 and w not in GENERIC}


# 같은 '주제'로 볼 단어 묶음 (예: 뉴욕증시·3대지수·나스닥·혼조 → 모두 '미국 증시' 이슈)
TOPICS = {
    "미국증시": ["뉴욕증시", "뉴욕증권", "美증시", "미국증시", "미증시", "3대지수", "다우", "나스닥", "s&p", "혼조"],
    "국내증시": ["코스피", "코스닥", "마감시황", "개장시황"],
    "환율": ["환율", "원달러", "원·달러", "원/달러", "달러인덱스"],
    "유가": ["유가", "wti", "브렌트", "원유"],
    "금리": ["금리", "국채", "연준", "fomc", "기준금리"],
}


def topic_of(t):
    flat = re.sub(r"\s", "", t).lower()
    return {k for k, words in TOPICS.items() if any(w in flat for w in words)}


def similar(a, b, th=0.28):
    """같은 이슈인지 판단: 글자 겹침이 크거나 / 같은 주제 묶음이거나 / 핵심 단어가 2개 이상 겹치면"""
    ta, tb = topic_of(a), topic_of(b)
    if len(ta) == 1 and ta == tb:
        return True
    A, B = _bigrams(a), _bigrams(b)
    if A and B and len(A & B) / len(A | B) >= th:
        return True
    ka, kb = _keywords(a), _keywords(b)
    flat_a, flat_b = re.sub(r"\s", "", a).lower(), re.sub(r"\s", "", b).lower()
    hits = {w for w in ka if w in flat_b} | {w for w in kb if w in flat_a}
    return len(hits) >= 2


def dedupe(titles):
    out = []
    for t in titles:
        if t and not any(similar(t, o) for o in out):
            out.append(t)
    return out


def collect_news(keywords, per_kw=4):
    """키워드마다 최신 헤드라인 몇 개씩만 모아 한 주제가 독식하지 않게 함"""
    groups = []
    for kw in keywords:
        url = f"https://news.google.com/rss/search?q={quote(kw)}+when:1d&hl=ko&gl=KR&ceid=KR:ko"
        try:
            feed = feedparser.parse(requests.get(url, headers=UA, timeout=15).content)
        except Exception as e:  # noqa
            print("뉴스 실패:", kw, e)
            continue
        heads = dedupe([clean_title(e.title) for e in feed.entries[:15]])[:per_kw]
        if heads:
            groups.append((kw, heads))
    return groups


def pick_issues(cfg, hc):
    n = max(3, min(5, int(hc.get("count", 5))))
    groups = collect_news(hc.get("keywords") or DEFAULT_KEYWORDS)
    all_heads = dedupe([h for _, hs in groups for h in hs])
    print(f"[뉴스] 키워드 {len(groups)}개, 서로 다른 헤드라인 {len(all_heads)}개")
    if not all_heads:
        return []

    prompt = (
        "너는 경제방송 뉴스 편집자야. 아래 헤드라인으로 '오늘의 핫 이슈'를 만든다.\n"
        "1단계: 같은 사건·같은 주제를 다룬 헤드라인은 하나의 이슈로 묶는다. "
        "(예: '뉴욕증시 혼조', '엔비디아 호실적에도 증시 하락', '3대지수 혼조세'는 모두 같은 이슈)\n"
        f"2단계: 서로 완전히 다른 이슈만 골라 시장 영향이 큰 순서로 최대 {n}개. "
        "주제가 겹치는 이슈를 억지로 채우지 말고, 뚜렷하게 다른 이슈가 적으면 3개만.\n"
        "3단계: 각 이슈를 방송 자막처럼 한 줄 제목으로 다듬는다.\n"
        "- 공백 포함 22자 이내, 핵심 수치·고유명사 유지, [말머리]·언론사명 제거\n"
        "- 미국은 美, 중국은 中, 일본은 日 표기 가능, 말줄임은 '…'\n"
        "- 헤드라인에 없는 사실·수치 추가 금지, 매수·매도 권유 금지\n"
        '출력은 JSON만: {"issues": ["제목1", "제목2", "제목3"]}\n\n'
        + "\n".join(f"[{kw}] " + " / ".join(hs) for kw, hs in groups)
    )
    out = gemini(prompt, cfg, want_json=True)
    issues = []
    if out:
        try:
            data = json.loads(re.sub(r"^```(json)?|```$", "", out.strip()).strip())
            items = data.get("issues", []) if isinstance(data, dict) else data
            issues = [clean_title(str(x)) for x in items if str(x).strip()]
        except Exception as e:  # noqa
            print("⚠️ AI 응답 해석 실패:", e, "| 응답:", out[:300])
    else:
        print("⚠️ AI 응답 없음 (GEMINI_API_KEY·무료 한도 확인)")
    issues = dedupe(issues)

    if len(issues) < 3:  # AI 결과가 부족하면 → 키워드별 대표 헤드라인 중 겹치지 않는 것으로 채움
        print("AI 선정 부족 → 키워드별 대표 헤드라인으로 보충")
        issues = dedupe(issues + [hs[0] for _, hs in groups] + all_heads)
    distinct = len(issues)
    issues = issues[: n if distinct >= n else 3]
    return issues


# ---------------------------------------------------------------- 2) 이미지 만들기
FONT_DIRS = ["/usr/share/fonts/truetype/nanum", "C:/Windows/Fonts", "/Library/Fonts"]
FONT_FILES = {
    "regular": (["NanumGothic.ttf", "malgun.ttf"], "NotoSansCJK-Regular.ttc"),
    "bold": (["NanumGothicBold.ttf", "malgunbd.ttf"], "NotoSansCJK-Bold.ttc"),
    "heavy": (["NanumGothicExtraBold.ttf", "NanumGothicBold.ttf", "malgunbd.ttf"], "NotoSansCJK-Black.ttc"),
}


def font(size, weight="bold"):
    from PIL import ImageFont
    names, noto = FONT_FILES[weight]
    for d in FONT_DIRS:
        for n in names:
            if os.path.exists(os.path.join(d, n)):
                return ImageFont.truetype(os.path.join(d, n), size)
    p = f"/usr/share/fonts/opentype/noto/{noto}"
    if os.path.exists(p):
        return ImageFont.truetype(p, size, index=1)  # index 1 = 한국어
    return ImageFont.load_default(size)


def vgradient(w, h, top, bottom):
    from PIL import Image
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        t = y / max(1, h - 1)
        c = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        for x in range(w):
            px[x, y] = c
    return img


def background(seed):
    """짙은 남색 그라데이션 + 빛줄기 + 보케 + 도시 실루엣 (매일 조금씩 다르게)"""
    from PIL import Image, ImageDraw, ImageFilter
    rnd = random.Random(seed)
    bg = vgradient(W, H, (8, 20, 52), (18, 52, 112)).convert("RGBA")

    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    g = ImageDraw.Draw(glow)
    for i in range(7):  # 위에서 내려오는 빛줄기
        x = rnd.randint(-200, W)
        g.polygon([(x, -50), (x + 60, -50), (x + 420, H), (x + 200, H)], fill=(120, 170, 255, 22))
    for _ in range(28):  # 보케(흐릿한 빛 방울)
        r = rnd.randint(8, 46)
        x, y = rnd.randint(0, W), rnd.randint(0, H)
        g.ellipse([x - r, y - r, x + r, y + r], fill=(150, 200, 255, rnd.randint(18, 45)))
    g.ellipse([W // 2 - 460, 120, W // 2 + 460, 460], fill=(90, 150, 255, 55))  # 제목 뒤 푸른 빛
    bg = Image.alpha_composite(bg, glow.filter(ImageFilter.GaussianBlur(18)))

    city = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    c = ImageDraw.Draw(city)
    x = 0
    while x < W:  # 하단 도시 실루엣 + 창문 불빛
        bw, bh = rnd.randint(50, 120), rnd.randint(90, 260)
        c.rectangle([x, H - bh, x + bw, H], fill=(10, 24, 58, 235))
        for wy in range(H - bh + 14, H - 10, 22):
            for wx in range(x + 10, x + bw - 10, 18):
                if rnd.random() < 0.28:
                    c.rectangle([wx, wy, wx + 7, wy + 9], fill=(255, 214, 120, 150))
        x += bw + rnd.randint(0, 8)
    return Image.alpha_composite(bg, city)


def gold_text(base, xy, text, fnt, anchor="mm"):
    """금색 그라데이션 + 주황 글로우 + 그림자 제목"""
    from PIL import Image, ImageDraw, ImageFilter
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).text(xy, text, font=fnt, fill=255, anchor=anchor)
    glow = Image.new("RGBA", (W, H), (255, 150, 30, 0))
    glow.putalpha(mask.filter(ImageFilter.GaussianBlur(16)).point(lambda v: min(255, v * 2)))
    base.alpha_composite(glow)
    shadow = Image.new("RGBA", (W, H), (60, 25, 0, 0))
    shadow.putalpha(mask.filter(ImageFilter.GaussianBlur(3)))
    base.alpha_composite(shadow, (4, 6))
    gold = vgradient(W, H, (255, 246, 196), (232, 160, 30)).convert("RGBA")
    gold.putalpha(mask)
    base.alpha_composite(gold)


def fit_font(draw, text, max_w, start=52, minimum=34):
    size = start
    while size > minimum and draw.textlength(text, font=font(size, "bold")) > max_w:
        size -= 2
    f = font(size, "bold")
    while draw.textlength(text, font=f) > max_w and len(text) > 4:
        text = text[:-2] + "…"
    return f, text


def render(issues, title, brand):
    from PIL import Image, ImageDraw, ImageFilter
    t = now()
    img = background(t.strftime("%Y%m%d"))
    d = ImageDraw.Draw(img)

    # 우측 상단 채널명 (설정했을 때만)
    if brand:
        d.text((W - 70, 62), brand, font=font(30, "bold"), fill=(205, 222, 255), anchor="ra")

    # 제목: HOT 배지 + 금색 타이틀
    d.rounded_rectangle([W // 2 - 70, 110, W // 2 + 70, 158], radius=24, fill=(220, 38, 38))
    d.text((W // 2, 134), "HOT", font=font(30, "heavy"), fill="white", anchor="mm")
    gold_text(img, (W // 2, 255), title, font(122, "heavy"))
    d = ImageDraw.Draw(img)
    d.text((W // 2, 352), "TODAY'S HOT ISSUE", font=font(26, "bold"), fill=(170, 200, 255), anchor="mm")

    # 이슈 바
    n = len(issues)
    top, bottom = 420, H - 90
    gap = 30
    bar_h = min(150 if n <= 3 else 140, (bottom - top - gap * (n - 1)) // n)
    y = top + ((bottom - top) - (bar_h * n + gap * (n - 1))) // 2
    text_w = (W - 140) - 118 - 60
    size = min(fit_font(d, t, text_w, start=56 if n <= 3 else 52)[0].size for t in issues)   # 모든 줄 같은 크기
    for i, text in enumerate(issues):
        x0, x1 = 70, W - 70
        sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))  # 그림자
        ImageDraw.Draw(sh).rounded_rectangle([x0 + 6, y + 10, x1 + 6, y + bar_h + 10], 16, fill=(0, 0, 20, 140))
        img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(8)))
        bar = vgradient(x1 - x0, bar_h, (255, 255, 255), (214, 224, 242)).convert("RGBA")
        m = Image.new("L", bar.size, 0)
        ImageDraw.Draw(m).rounded_rectangle([0, 0, bar.size[0] - 1, bar_h - 1], 16, fill=255)
        img.paste(bar, (x0, y), m)
        d = ImageDraw.Draw(img)
        accent = (214, 40, 40) if i % 2 == 0 else (30, 78, 190)
        d.rounded_rectangle([x0, y, x0 + 118, y + bar_h], 16, fill=accent)
        d.rectangle([x0 + 100, y, x0 + 118, y + bar_h], fill=accent)
        d.text((x0 + 59, y + bar_h // 2), f"{i + 1:02d}", font=font(46, "heavy"), fill="white", anchor="mm")
        f, txt = fit_font(d, text, text_w, start=size, minimum=size)
        d.text((x0 + 118 + 30, y + bar_h // 2), txt, font=f, fill=(12, 26, 66), anchor="lm")
        y += bar_h + gap

    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG", optimize=True)
    return buf.getvalue()


# ---------------------------------------------------------------- 실행
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--save", help="이미지를 파일로도 저장 (테스트용)")
    args = ap.parse_args()

    cfg = load_config()
    hc = cfg.get("hot_issue", {})
    if not hc.get("enabled", True):
        return print("핫 이슈 기능 꺼짐")
    if not args.force and hc.get("weekdays_only") and not is_market_day(cfg):
        return print("휴장일 → 건너뜀")

    state = load_state("hot_issue")
    key = now().strftime("%Y-%m-%d")
    if not args.force and key in state.get("done", []):
        return print("오늘 이미 발송함:", key)

    issues = pick_issues(cfg, hc)
    print("[선정]", issues)
    if not issues:
        return print("뉴스를 가져오지 못해 발송하지 않음")

    title = hc.get("title", "오늘의 핫 이슈")
    png = render(issues, title, hc.get("brand", ""))
    if args.save:
        open(args.save, "wb").write(png)
    cap = (f"🔥 <b>{esc(title)}</b> | {now():%m/%d}({WEEK[now().weekday()]})\n\n"
           + "\n".join(f"{i}. {esc(t)}" for i, t in enumerate(issues, 1)))
    send_photo(png, cap, cfg)

    state.setdefault("done", []).append(key)
    state["done"] = state["done"][-30:]
    add_history(state, f"[핫이슈] {' / '.join(issues)[:80]}")
    state["last_run"] = now().strftime("%Y-%m-%d %H:%M")
    save_state("hot_issue", state)


if __name__ == "__main__":
    main()
