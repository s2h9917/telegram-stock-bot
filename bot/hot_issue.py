"""④ 오늘의 핫 이슈 — 매일 아침 이슈 타이틀 3~5개를 골라 이미지 카드로 텔레그램 업로드
- 뉴스: 구글 뉴스 RSS + 네이버 금융 주요뉴스 (market_brief와 같은 수집기 사용)
- 선정·제목 다듬기: Gemini (키가 없으면 최신 헤드라인 상위 N개)
실행: python -m bot.hot_issue            (평소, 하루 1회만 발송)
      python -m bot.hot_issue --force    (중복·휴장일 체크 무시)
      DRY_RUN=1 python -m bot.hot_issue --force --save card.png   (이미지 파일로만 저장)
"""
import argparse
import io
import json
import math
import os
import random
import re

from bot.common import (add_history, esc, gemini, is_market_day, load_config, load_state,
                        now, save_state, send_photo)
from bot.market_brief import get_news

W, H = 1080, 1350          # 텔레그램에서 크게 보이는 4:5 세로 비율
WEEK = "월화수목금토일"

DEFAULT_KEYWORDS = ["뉴욕증시", "미국 증시 마감", "코스피 전망", "환율", "국제유가", "반도체 주가", "트럼프 관세"]


# ---------------------------------------------------------------- 1) 이슈 선정
def pick_issues(cfg, hc):
    n = max(3, min(5, int(hc.get("count", 5))))
    news_cfg = {**cfg, "market": {**cfg.get("market", {}),
                                  "keywords": hc.get("keywords") or DEFAULT_KEYWORDS}}
    news = get_news(news_cfg, limit=40)
    print(f"[뉴스] 헤드라인 {len(news)}개 수집")
    if not news:
        return []
    prompt = (
        f"너는 경제방송 뉴스 편집자야. 아래 헤드라인 중 오늘 아침 주식 투자자가 꼭 알아야 할 핫 이슈 {n}개를 골라.\n"
        "규칙:\n"
        "- 서로 겹치지 않는 이슈로, 시장 영향이 큰 순서대로\n"
        "- 각 제목은 방송 자막처럼 짧게 다듬기: 공백 포함 24자 이내, 핵심 수치·고유명사 유지\n"
        "- 미국은 美, 중국은 中, 일본은 日 처럼 한 글자 표기 가능, 말줄임은 '…' 사용\n"
        "- 헤드라인에 없는 사실·수치 추가 금지, 매수·매도 권유 금지\n"
        '- 출력은 JSON만: {"issues": ["제목1", "제목2", ...]}\n\n'
        + "\n".join(f"- {x['title']}" for x in news)
    )
    out = gemini(prompt, cfg, want_json=True)
    issues = []
    try:
        issues = [str(t).strip() for t in json.loads(out)["issues"] if str(t).strip()]
    except Exception:  # noqa  AI 실패 → 최신 헤드라인 그대로 사용
        print("AI 선정 실패 → 최신 헤드라인 사용")
    if len(issues) < 3:
        issues = [x["title"] for x in news[:n]]
    return issues[:n]


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


def render(issues, title, brand, footer):
    from PIL import Image, ImageDraw, ImageFilter
    t = now()
    img = background(t.strftime("%Y%m%d"))
    d = ImageDraw.Draw(img)

    # 상단: 날짜 · 브랜드
    date_txt = f"{t:%Y.%m.%d} ({WEEK[t.weekday()]}) 07:30"
    d.text((70, 62), date_txt, font=font(30, "bold"), fill=(205, 222, 255))
    if brand:
        d.text((W - 70, 62), brand, font=font(30, "bold"), fill=(205, 222, 255), anchor="ra")
    d.line([70, 112, W - 70, 112], fill=(120, 160, 230), width=2)

    # 제목: HOT 배지 + 금색 타이틀
    d.rounded_rectangle([W // 2 - 70, 150, W // 2 + 70, 198], radius=24, fill=(220, 38, 38))
    d.text((W // 2, 174), "HOT", font=font(30, "heavy"), fill="white", anchor="mm")
    gold_text(img, (W // 2, 290), title, font(118, "heavy"))
    d = ImageDraw.Draw(img)
    d.text((W // 2, 385), "TODAY'S HOT ISSUE", font=font(26, "bold"), fill=(170, 200, 255), anchor="mm")

    # 이슈 바
    n = len(issues)
    top, bottom = 450, H - 150
    gap = 26
    bar_h = min(140, (bottom - top - gap * (n - 1)) // n)
    y = top + ((bottom - top) - (bar_h * n + gap * (n - 1))) // 2
    text_w = (W - 140) - 118 - 60
    size = min(fit_font(d, t, text_w)[0].size for t in issues)   # 모든 줄 같은 크기
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

    # 하단 고지 (도시 불빛 위에 어두운 띠)
    band = Image.new("RGBA", (W, 130), (6, 14, 38, 215))
    img.alpha_composite(band, (0, H - 130))
    d = ImageDraw.Draw(img)
    d.text((W // 2, H - 92), "출처: 국내외 언론 보도 종합", font=font(24, "regular"), fill=(190, 205, 235), anchor="mm")
    if footer:
        d.text((W // 2, H - 56), footer[:60], font=font(22, "regular"), fill=(160, 178, 215), anchor="mm")

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
    png = render(issues, title, hc.get("brand", ""), cfg.get("footer", ""))
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
