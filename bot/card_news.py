"""⑦ 카드뉴스 — 하루 3번, 시간대별로 가장 많이 보도된 뉴스 1건을 1~3장 카드뉴스로 업로드
  07:05 → 00:00~07:00 / 16:05 → 07:01~16:00 / 19:35 → 16:01~19:30
- 뉴스: 구글 뉴스 RSS(경제·증시 키워드) → 시간대 안 기사만
- '이슈 크기': 같은 사건을 다룬 기사를 묶어 '다룬 언론사 수'가 가장 많은 사건 선택
- 요약: 대표 기사 본문 2~3건 + 헤드라인을 근거로 Gemini가 카드 구성(1~3장) 결정
- 이미지: 업종별 색·무늬 템플릿 (기사 사진은 저작권 문제로 사용하지 않음)
실행: python -m bot.card_news                 (시각으로 시간대 자동 판단)
      python -m bot.card_news --slot day --force
      DRY_RUN=1 python -m bot.card_news --slot day --force --save card
"""
import argparse
import calendar
import datetime as dt
import io
import json
import math
import random
import re
from urllib.parse import quote

import feedparser
import requests
from bs4 import BeautifulSoup

from bot.common import (KST, UA, add_history, esc, gemini, is_market_day, load_config, load_state, now,
                        save_state, send_album)
from bot.hot_issue import clean_title, font, is_junk, similar, vgradient

W, H = 1080, 1350
WEEK = "월화수목금토일"
SLOTS = {  # 이름: (시작, 끝, 표시)
    "night": ((0, 0), (7, 0), "밤사이"),
    "day": ((7, 1), (16, 0), "장중"),
    "evening": ((16, 1), (19, 30), "장마감 후"),
}
DEFAULT_KEYWORDS = ["증시", "코스피", "코스닥", "뉴욕증시", "경제", "금리", "환율", "반도체", "실적", "관세"]

# 업종별 테마: (위 색, 아래 색, 강조색, 무늬)
THEMES = {
    "반도체": ((10, 30, 80), (20, 70, 160), (90, 200, 255), "circuit"),
    "바이오": ((6, 60, 52), (14, 120, 96), (120, 240, 190), "molecule"),
    "자동차·2차전지": ((30, 30, 40), (60, 70, 90), (255, 200, 60), "lines"),
    "에너지·원자재": ((70, 30, 8), (150, 70, 20), (255, 190, 90), "waves"),
    "금리·환율·거시": ((20, 24, 60), (50, 40, 110), (190, 160, 255), "grid"),
    "해외증시": ((8, 20, 52), (18, 52, 112), (255, 214, 120), "globe"),
    "국내증시": ((90, 14, 24), (170, 30, 40), (255, 210, 120), "candles"),
    "기업·산업": ((24, 40, 60), (40, 80, 110), (120, 220, 255), "dots"),
}


# ---------------------------------------------------------------- 1) 시간대·뉴스 수집
def slot_range(slot, day):
    (h1, m1), (h2, m2), _ = SLOTS[slot]
    start = dt.datetime(day.year, day.month, day.day, h1, m1, tzinfo=KST)
    end = dt.datetime(day.year, day.month, day.day, h2, m2, 59, tzinfo=KST)
    return start, end


def detect_slot(t):
    return "night" if t.hour < 10 else ("day" if t.hour < 18 else "evening")


def collect(keywords, start, end):
    hours = max(2, math.ceil((now() - start).total_seconds() / 3600) + 1)
    items, seen = [], set()
    for kw in keywords:
        url = f"https://news.google.com/rss/search?q={quote(kw)}+when:{hours}h&hl=ko&gl=KR&ceid=KR:ko"
        try:
            feed = feedparser.parse(requests.get(url, headers=UA, timeout=15).content)
        except Exception as e:  # noqa
            print("뉴스 실패:", kw, e)
            continue
        for e in feed.entries[:40]:
            pub = e.get("published_parsed")
            if not pub:
                continue
            ts = dt.datetime.fromtimestamp(calendar.timegm(pub), KST)
            raw = e.get("title", "")
            if not (start <= ts <= end) or raw in seen:
                continue
            seen.add(raw)
            title = clean_title(raw)
            if is_junk(title):
                continue
            src = (e.get("source") or {}).get("title") or (raw.rsplit(" - ", 1)[-1] if " - " in raw else "")
            items.append({"title": title, "link": e.get("link", ""), "source": src.strip(), "ts": ts})
    print(f"[뉴스] {start:%H:%M}~{end:%H:%M} 기사 {len(items)}건")
    return items


def top_cluster(items):
    """같은 사건끼리 묶고, 다룬 언론사 수가 가장 많은 묶음 반환"""
    clusters = []
    for it in sorted(items, key=lambda x: x["ts"], reverse=True):
        for c in clusters:
            if similar(it["title"], c[0]["title"]):
                c.append(it)
                break
        else:
            clusters.append([it])
    if not clusters:
        return []
    score = lambda c: (len({x["source"] for x in c}), len(c), max(x["ts"] for x in c))
    clusters.sort(key=score, reverse=True)
    for c in clusters[:3]:
        print(f"  · 언론사 {len({x['source'] for x in c})}곳 / 기사 {len(c)}건: {c[0]['title'][:40]}")
    return clusters[0]


def article_text(link):
    """대표 기사 본문 (사실 확인용, 앞부분만)"""
    try:
        if "news.google.com" in link:
            from googlenewsdecoder import gnewsdecoder
            r = gnewsdecoder(link, interval=1)
            if r.get("success") or r.get("status"):
                link = r["decoded_url"]
        r = requests.get(link, headers=UA, timeout=15)
        r.encoding = r.apparent_encoding or "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")
        for t in soup(["script", "style", "nav", "header", "footer", "aside"]):
            t.decompose()
        paras = [p.get_text(" ", strip=True) for p in soup.find_all(["p", "div"]) if p.find(["p", "div"]) is None]
        body = "\n".join(p for p in paras if len(p) > 40)
        return body[:2500]
    except Exception as e:  # noqa
        print("  본문 읽기 실패:", str(e)[:80])
        return ""


# ---------------------------------------------------------------- 2) AI 카드 구성
def plan_cards(cluster, cfg):
    heads = list(dict.fromkeys(x["title"] for x in cluster))[:12]
    bodies = [b for b in (article_text(x["link"]) for x in cluster[:3]) if b]
    print(f"  [근거] 헤드라인 {len(heads)}개, 본문 {len(bodies)}건")
    prompt = (
        "너는 경제 미디어의 인스타그램 카드뉴스 에디터야. 아래 한 사건에 대한 기사들로 카드뉴스를 구성해.\n"
        "규칙:\n"
        "- 기사에 있는 사실·숫자만 사용, 없는 내용 추가 금지, 투자 권유 금지, 한국어\n"
        "- 언론사명·기자명·[말머리] 넣지 말 것\n"
        "- pages: 내용이 단순하면 1, 보통이면 2, 숫자·맥락이 풍부하면 3\n"
        "- title: 표지 제목 22자 이내, 핵심이 한눈에 / subtitle: 30자 이내 보충 설명\n"
        "- key_number: 가장 중요한 숫자 하나 {\"value\": \"1.5조원\", \"label\": \"기술수출 규모\"} (없으면 null)\n"
        "- summary: 3~4줄, 각 40자 이내 핵심 요약 (육하원칙·숫자 중심)\n"
        "- points: 1~2줄, 각 40자 이내 '왜 중요한가/앞으로 볼 점' (pages가 3일 때 사용)\n"
        f"- category: {', '.join(THEMES)} 중 하나\n"
        '출력 JSON만: {"pages": 2, "title": "", "subtitle": "", "key_number": null, '
        '"summary": [], "points": [], "category": ""}\n\n'
        "[헤드라인]\n" + "\n".join(f"- {h}" for h in heads)
        + ("\n\n[기사 본문]\n" + "\n---\n".join(bodies) if bodies else "")
    )
    out = gemini(prompt, cfg, want_json=True)
    plan = {}
    if out:
        try:
            plan = json.loads(re.sub(r"^```(json)?|```$", "", out.strip()).strip())
        except Exception as e:  # noqa
            print("⚠️ AI 응답 해석 실패:", e)
    if not plan.get("title") or not plan.get("summary"):
        print("AI 구성 실패 → 헤드라인으로 1장 구성")
        others = [h for h in heads[1:] if not similar(h, heads[0], 0.5)][:3]
        plan = {"pages": 1, "title": heads[0][:40], "subtitle": "", "key_number": None,
                "summary": others or heads[1:4], "points": [], "category": "기업·산업"}
    plan["pages"] = max(1, min(3, int(plan.get("pages") or 1)))
    if plan["pages"] == 3 and not plan.get("points"):
        plan["pages"] = 2
    if plan.get("category") not in THEMES:
        plan["category"] = "기업·산업"
    return plan


# ---------------------------------------------------------------- 3) 이미지
def pattern(img, kind, accent, seed):
    from PIL import Image, ImageDraw, ImageFilter
    rnd = random.Random(seed)
    lay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    a = (*accent, 46)
    if kind == "circuit":
        for _ in range(26):
            x, y = rnd.randint(0, W), rnd.randint(0, H)
            for _ in range(rnd.randint(2, 4)):
                nx, ny = (x + rnd.choice([-1, 1]) * rnd.randint(60, 200), y) if rnd.random() < .5 else (x, y + rnd.choice([-1, 1]) * rnd.randint(60, 200))
                d.line([x, y, nx, ny], fill=a, width=3)
                x, y = nx, ny
            d.ellipse([x - 7, y - 7, x + 7, y + 7], outline=a, width=3)
    elif kind == "molecule":
        pts = [(rnd.randint(0, W), rnd.randint(0, H)) for _ in range(22)]
        for i, p in enumerate(pts):
            for q in pts[i + 1:]:
                if math.dist(p, q) < 230:
                    d.line([p, q], fill=a, width=3)
            r = rnd.randint(10, 22)
            d.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], outline=a, width=3)
    elif kind == "candles":
        x = 30
        y = H * 0.55
        while x < W:
            y += rnd.randint(-60, 60)
            h = rnd.randint(40, 160)
            d.rectangle([x, y - h / 2, x + 26, y + h / 2], outline=a, width=3)
            d.line([x + 13, y - h / 2 - 30, x + 13, y + h / 2 + 30], fill=a, width=2)
            x += 52
    elif kind == "waves":
        for k in range(9):
            pts = [(x, H * 0.15 + k * 120 + 40 * math.sin(x / 120 + k)) for x in range(0, W + 20, 20)]
            d.line(pts, fill=a, width=3)
    elif kind == "grid":
        for x in range(0, W, 90):
            d.line([x, 0, x, H], fill=(*accent, 22), width=2)
        for y in range(0, H, 90):
            d.line([0, y, W, y], fill=(*accent, 22), width=2)
        pts = [(x, H * 0.6 - 300 * math.sin(x / 300) - x * 0.15) for x in range(0, W + 20, 30)]
        d.line(pts, fill=a, width=5)
    elif kind == "globe":
        cx, cy, r = W * 0.78, H * 0.22, 380
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=a, width=3)
        for k in range(1, 6):
            w = r * k / 6
            d.ellipse([cx - w, cy - r, cx + w, cy + r], outline=a, width=2)
            yy = cy - r + 2 * r * k / 6
            half = math.sqrt(max(0, r * r - (yy - cy) ** 2))
            d.line([cx - half, yy, cx + half, yy], fill=a, width=2)
    elif kind == "lines":
        for k in range(-H, W, 70):
            d.line([k, 0, k + H, H], fill=(*accent, 30), width=6)
    else:  # dots
        for _ in range(140):
            x, y, r = rnd.randint(0, W), rnd.randint(0, H), rnd.randint(3, 9)
            d.ellipse([x - r, y - r, x + r, y + r], fill=a)
    img.alpha_composite(lay.filter(ImageFilter.GaussianBlur(1)))


def wrap(d, text, fnt, max_w, max_lines=9):
    """단어(띄어쓰기) 단위로 줄바꿈, 한 단어가 너무 길 때만 글자 단위"""
    lines, cur = [], ""
    for word in str(text).split(" "):
        cand = (cur + " " + word).strip()
        if d.textlength(cand, font=fnt) <= max_w:
            cur = cand
            continue
        if cur:
            lines.append(cur)
        cur = ""
        for ch in word:
            if d.textlength(cur + ch, font=fnt) > max_w and cur:
                lines.append(cur)
                cur = ch
            else:
                cur += ch
    lines += [cur] if cur else []
    return lines[:max_lines]


def base(plan, seed):
    top, bottom, accent, kind = THEMES[plan["category"]]
    img = vgradient(W, H, top, bottom).convert("RGBA")
    pattern(img, kind, accent, seed)
    from PIL import Image
    shade = vgradient(W, H, (0, 0, 0), (0, 0, 0)).convert("RGBA")
    shade.putalpha(70)
    img.alpha_composite(shade)
    return img, accent


def header(d, plan, label, page, total, accent, brand):
    d.rounded_rectangle([70, 70, 70 + d.textlength(plan["category"], font=font(30, "bold")) + 44, 122], 26,
                        fill=(*accent, 255))
    d.text((92, 96), plan["category"], font=font(30, "bold"), fill=(15, 20, 35), anchor="lm")
    d.text((W - 70, 96), f"{page}/{total}" if total > 1 else label, font=font(30, "bold"),
           fill=(230, 236, 250), anchor="rm")
    if brand:
        d.text((70, H - 70), brand, font=font(28, "bold"), fill=(210, 220, 240), anchor="lm")
    d.text((W - 70, H - 70), label, font=font(26, "regular"), fill=(190, 200, 225), anchor="rm")


def key_badge(d, kn, accent, x, y):
    if not kn or not kn.get("value"):
        return y
    f1, f2 = font(96, "heavy"), font(32, "bold")
    d.text((x, y), str(kn["value"]), font=f1, fill=accent)
    d.text((x, y + 118), str(kn.get("label", "")), font=f2, fill=(220, 228, 245))
    return y + 180


def render(plan, slot_label, brand):
    from PIL import ImageDraw
    seed = plan["title"]
    total = plan["pages"]
    pngs = []

    def save(img):
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "PNG", optimize=True)
        pngs.append(buf.getvalue())

    # 1장: 표지 (1장 구성이면 요약까지)
    img, accent = base(plan, seed)
    d = ImageDraw.Draw(img)
    header(d, plan, slot_label, 1, total, accent, brand)
    tf = font(100 if total > 1 else 84, "heavy")
    title_lines = wrap(d, plan["title"], tf, W - 140, 4)
    y = 320 if total > 1 else 210
    for ln in title_lines:
        d.text((70, y), ln, font=tf, fill="white")
        y += 128 if total > 1 else 108
    d.rectangle([70, y + 16, 190, y + 24], fill=accent)
    y += 60
    if plan.get("subtitle"):
        for ln in wrap(d, plan["subtitle"], font(46, "bold"), W - 140, 3):
            d.text((70, y), ln, font=font(46, "bold"), fill=(225, 232, 248))
            y += 64
        y += 30
    if total > 1:
        key_badge(d, plan.get("key_number"), accent, 70, max(y + 40, 900))
        d.text((W - 70, H - 150), "넘겨서 보기 →", font=font(32, "bold"), fill=accent, anchor="rm")
    else:
        y = key_badge(d, plan.get("key_number"), accent, 70, y + 10) + 10
        fsum = font(46, "bold")
        blocks = [wrap(d, s, fsum, W - 140 - 90, 3) for s in plan["summary"][:4]]
        box_h = sum(len(b) * 64 + 36 for b in blocks) + 40
        y = y + max(40, (H - 150 - y - box_h) // 2)          # 남는 공간 가운데에 요약 상자
        from PIL import Image
        box = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(box).rounded_rectangle([50, y - 30, W - 50, y - 30 + box_h], 28, fill=(0, 0, 0, 95))
        img.alpha_composite(box)
        d = ImageDraw.Draw(img)
        for i, lines in enumerate(blocks, 1):
            d.ellipse([80, y + 6, 134, y + 60], fill=accent)
            d.text((107, y + 33), str(i), font=font(32, "heavy"), fill=(15, 20, 35), anchor="mm")
            for ln in lines:
                d.text((160, y), ln, font=font(46, "bold"), fill="white")
                y += 64
            y += 36
    save(img)
    if total == 1:
        return pngs

    # 2장: 핵심 요약
    img, accent = base(plan, seed + "2")
    d = ImageDraw.Draw(img)
    header(d, plan, slot_label, 2, total, accent, brand)
    d.text((70, 200), "핵심 요약", font=font(72, "heavy"), fill="white")
    d.rectangle([70, 300, 190, 308], fill=accent)
    y = 400
    if total == 2:
        y = key_badge(d, plan.get("key_number"), accent, 70, y) + 10
    for i, s in enumerate(plan["summary"][:4], 1):
        lines = wrap(d, s, font(52, "bold"), W - 140 - 100, 3)
        d.ellipse([70, y + 6, 132, y + 68], fill=accent)
        d.text((101, y + 37), str(i), font=font(36, "heavy"), fill=(15, 20, 35), anchor="mm")
        for ln in lines:
            d.text((160, y), ln, font=font(52, "bold"), fill="white")
            y += 72
        y += 56
    if total == 2 and plan.get("points"):
        y += 10
        for p in plan["points"][:2]:
            for ln in wrap(d, "→ " + p, font(36, "bold"), W - 140, 2):
                d.text((70, y), ln, font=font(36, "bold"), fill=accent)
                y += 50
    save(img)
    if total == 2:
        return pngs

    # 3장: 숫자와 관전 포인트
    img, accent = base(plan, seed + "3")
    d = ImageDraw.Draw(img)
    header(d, plan, slot_label, 3, total, accent, brand)
    d.text((70, 200), "체크 포인트", font=font(72, "heavy"), fill="white")
    d.rectangle([70, 300, 190, 308], fill=accent)
    y = key_badge(d, plan.get("key_number"), accent, 70, 400) + 60
    for p in plan.get("points", [])[:2]:
        d.rounded_rectangle([70, y, W - 70, y + 6], 3, fill=accent)
        y += 30
        for ln in wrap(d, p, font(52, "bold"), W - 140, 3):
            d.text((70, y), ln, font=font(52, "bold"), fill="white")
            y += 72
        y += 60
    d.text((70, H - 140), "※ 투자 권유가 아닌 정보 제공 목적입니다", font=font(26, "regular"), fill=(190, 200, 225))
    save(img)
    return pngs


# ---------------------------------------------------------------- 실행
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", choices=["auto", *SLOTS], default="auto")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--save")
    args = ap.parse_args()

    cfg = load_config()
    cc = cfg.get("card_news", {})
    if not cc.get("enabled", True):
        return print("카드뉴스 기능 꺼짐")
    t = now()
    slot = detect_slot(t) if args.slot == "auto" else args.slot
    if not args.force and cc.get("weekdays_only") and not is_market_day(cfg):
        return print("휴장일 → 건너뜀")
    state = load_state("card_news")
    key = f"{t:%Y-%m-%d}-{slot}"
    if not args.force and key in state.get("done", []):
        return print("이미 발송함:", key)

    start, end = slot_range(slot, t)
    cluster = top_cluster(collect(cc.get("keywords") or DEFAULT_KEYWORDS, start, end))
    if not cluster:
        return print("해당 시간대 뉴스 없음 → 발송 안 함")
    plan = plan_cards(cluster, cfg)
    print(f"[구성] {plan['pages']}장 · {plan['category']} · {plan['title']}")

    label = f"{t:%m.%d}({WEEK[t.weekday()]}) {SLOTS[slot][2]} 이슈"
    pngs = render(plan, label, cc.get("brand", ""))
    if args.save:
        for i, b in enumerate(pngs, 1):
            open(f"{args.save}_{i}.png", "wb").write(b)
    cap = f"🗞 <b>{esc(plan['title'])}</b>\n{esc(SLOTS[slot][2])} 가장 많이 보도된 이슈 | {t:%m/%d}"
    send_album(pngs, cap, cfg)

    state.setdefault("done", []).append(key)
    state["done"] = state["done"][-40:]
    add_history(state, f"[카드뉴스] {plan['title']} ({plan['pages']}장)")
    state["last_run"] = t.strftime("%Y-%m-%d %H:%M")
    save_state("card_news", state)


if __name__ == "__main__":
    main()
