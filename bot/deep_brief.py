"""⑩ 장전 키워드 심층 브리핑 (평일 07:50)
- 전일 장 마감(15:30) 이후 기사들을 사건별로 묶고, 많이 보도된 이슈 5~6개 선정
- 이슈마다 서로 다른 언론사 기사 2~3건의 '본문'을 읽혀 AI가 사실 중심으로 정리
- 이슈별: #키워드 제목 → 핵심 사실 5~7줄 → ▶ 시사점 한 줄 → 원문 링크
- 마지막: 핵심 변수 요약표(이미지) + 체크 포인트 (매매 권유 없음)
실행: python -m bot.deep_brief [--force]
"""
import argparse
import datetime as dt
import io
import json
import re
import time

import requests
from bs4 import BeautifulSoup

from bot.card_news import collect
from bot.common import (KST, UA, _font, add_history, esc, gemini, is_market_day, load_config,
                        load_state, now, save_state, send_photo, send_text)
from bot.hot_issue import similar

DEFAULT_KEYWORDS = ["증시", "코스피", "코스닥", "뉴욕증시", "반도체", "금리", "환율", "실적",
                    "수주", "금융당국", "유가", "AI", "바이오", "2차전지"]


# ---------------------------------------------------------------- 1) 이슈 선정
def window_start(t):
    """직전 거래일 장 마감(15:30) 이후 기사. 월요일이면 금요일 15:30부터"""
    d = t.date() - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return dt.datetime(d.year, d.month, d.day, 15, 30, tzinfo=KST)


def clusters_of(items):
    """같은 사건끼리 묶고, 보도한 언론사 수가 많은 순으로 정렬"""
    cl = []
    for it in sorted(items, key=lambda x: x["ts"], reverse=True):
        for c in cl:
            if similar(it["title"], c[0]["title"]):
                c.append(it)
                break
        else:
            cl.append([it])
    cl.sort(key=lambda c: (len({x["source"] for x in c}), len(c)), reverse=True)
    return cl


def article(link):
    """(실제 기사 주소, 본문 앞부분). 실패하면 (link, '')"""
    url = link
    try:
        if "news.google.com" in link:
            from googlenewsdecoder import gnewsdecoder
            r = gnewsdecoder(link, interval=1)
            if r.get("status") or r.get("success"):
                url = r["decoded_url"]
        r = requests.get(url, headers=UA, timeout=15)
        r.encoding = r.apparent_encoding or "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")
        for t in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
            t.decompose()
        paras = [p.get_text(" ", strip=True) for p in soup.find_all(["p", "div"])
                 if p.find(["p", "div"]) is None]
        body = "\n".join(p for p in paras if len(p) > 40 and "Copyright" not in p and "무단" not in p)
        return url, body[:2200]
    except Exception as e:  # noqa
        print("  본문 읽기 실패:", str(e)[:80])
        return url, ""


def gather(cfg, dc):
    t = now()
    items = collect(dc.get("keywords") or DEFAULT_KEYWORDS, window_start(t), t)
    cl = [c for c in clusters_of(items) if len({x["source"] for x in c}) >= 2]
    n = max(3, min(8, int(dc.get("issues", 6))))
    picked = cl[:n + 2]                 # AI가 겹치는 이슈를 빼도 n개가 되도록 여유 있게
    issues = []
    for i, c in enumerate(picked, 1):
        arts, used = [], set()
        for it in c:
            if it["source"] in used:
                continue
            url, body = article(it["link"])
            if len(body) < 200:
                continue
            used.add(it["source"])
            arts.append({"title": it["title"], "source": it["source"], "url": url, "body": body})
            if len(arts) >= 3:
                break
        print(f"[이슈{i}] 언론사 {len({x['source'] for x in c})}곳, 본문 {len(arts)}건: {c[0]['title'][:40]}")
        if arts:
            issues.append({"titles": [x["title"] for x in c[:6]], "articles": arts})
    return issues


# ---------------------------------------------------------------- 2) AI 정리
def build_prompt(issues, n, quotes):
    blocks = []
    for i, iss in enumerate(issues, 1):
        arts = "\n\n".join(f"<기사 {j}> {a['title']} ({a['source']})\n{a['body']}"
                           for j, a in enumerate(iss["articles"], 1))
        blocks.append(f"=== 이슈 {i} ===\n관련 제목: " + " / ".join(iss["titles"]) + f"\n\n{arts}")
    return (
        "오늘 아침 텔레그램 채널에 올릴 '장전 키워드 심층 브리핑'을 쓴다.\n"
        f"아래 이슈 묶음 중 오늘 국내 증시에 의미가 큰 것을 골라 서로 겹치지 않게 {n}개를 중요한 순서로 정리해.\n\n"
        "[이슈별 작성 규칙]\n"
        "- keyword: 해시태그용 핵심어 1개 (예: 코스피, 엔비디아, 증시안정펀드). 공백 없이\n"
        "- title: 팩트가 드러나는 40자 이내 제목 (말머리·언론사명 금지)\n"
        "- points: 기사 본문에 있는 구체적 사실 5~7개. 숫자·날짜·기관명·기업명을 살리고 각 70자 이내.\n"
        "  배경 → 핵심 사실 → 시장 해석 → 불확실성 순서로. 기사 문장을 그대로 베끼지 말고 짧게 다시 쓸 것\n"
        "- insight: 이 이슈가 국내 증시·업종에 주는 의미를 한 문장(80자 이내). 매수·매도 권유 금지\n"
        "- source: 가장 많이 참고한 기사 번호 (이슈 안의 <기사 N> 숫자)\n\n"
        "[전체]\n"
        "- headline: 오늘 브리핑 전체를 꿰는 제목 45자 이내\n"
        "- table: 이슈마다 {구분(6자 이내), 핵심 변수(18자 이내), 확인 사항(18자 이내)}\n"
        "- checkpoints: 오늘·이번 주 확인할 일정·지표·변수 3~5개 (각 60자 이내, 매매 조언 금지)\n"
        "- 기사에 없는 수치·날짜·발언은 절대 만들지 말 것. 한국어. 마크다운·이모지 금지\n\n"
        "출력은 JSON만:\n"
        '{"headline": "", "issues": [{"issue_no": 1, "keyword": "", "title": "", "points": [""], '
        '"insight": "", "source": 1}], "table": [{"구분": "", "핵심 변수": "", "확인 사항": ""}], '
        '"checkpoints": [""]}\n\n'
        f"[참고 지표]\n{quotes}\n\n" + "\n\n".join(blocks)
    )


def plan(issues, cfg, n):
    try:
        from bot.market_brief import fmt_quote, get_quotes
        quotes = "\n".join(fmt_quote(nm, q) for nm, q in get_quotes("pre"))
    except Exception as e:  # noqa
        print("지표 수집 실패:", e)
        quotes = "(없음)"
    out = gemini(build_prompt(issues, n, quotes), cfg, want_json=True, timeout=150)
    if not out:
        return None
    try:
        data = json.loads(re.sub(r"^```(json)?|```$", "", out.strip()).strip())
    except Exception as e:  # noqa
        print("⚠️ AI 응답 해석 실패:", e, out[:200])
        return None
    return data if data.get("issues") else None


# ---------------------------------------------------------------- 3) 요약표 이미지
def table_image(rows):
    from PIL import Image, ImageDraw
    cols = ["구분", "핵심 변수", "확인 사항"]
    widths = [210, 440, 430]
    W, pad, rh, hh = sum(widths) + 60, 30, 74, 80
    H = pad * 2 + hh + rh * len(rows) + 70
    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)
    f_t, f_h, f_b = _font(34, True), _font(28, True), _font(27)
    d.text((pad, pad - 4), "오늘의 핵심 변수", font=f_t, fill=(20, 20, 20))
    y = pad + 60
    d.rectangle([pad, y, W - pad, y + hh], fill=(242, 244, 247))
    x = pad
    for c, w in zip(cols, widths):
        d.text((x + 18, y + 24), c, font=f_h, fill=(60, 60, 60))
        x += w
    y += hh
    for r in rows:
        d.line([pad, y, W - pad, y], fill=(228, 228, 228), width=2)
        x = pad
        for c, w in zip(cols, widths):
            txt = str(r.get(c, ""))
            while d.textlength(txt, font=f_b) > w - 30 and len(txt) > 1:
                txt = txt[:-2] + "…"
            d.text((x + 18, y + 22), txt, font=f_b if c != "구분" else f_h, fill=(30, 30, 30))
            x += w
        y += rh
    d.line([pad, y, W - pad, y], fill=(228, 228, 228), width=2)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ---------------------------------------------------------------- 4) 발송
def compose(data, issues, n, show_link=False):
    t = now()
    wd = "월화수목금토일"[t.weekday()]
    head = f"✅ <b>[장전 키워드] {esc(data.get('headline', '오늘의 증시 키워드'))}</b>\n{t:%m/%d}({wd}) {t:%H:%M} 기준"
    blocks = [head]
    for k, it in enumerate(data["issues"][:n], 1):
        pts = "\n".join(f"• {esc(p)}" for p in (it.get("points") or [])[:7])
        b = f"<b>{k}. #{esc(str(it.get('keyword', '')).replace(' ', ''))} {esc(it.get('title', ''))}</b>\n{pts}"
        if it.get("insight"):
            b += f"\n▶️ <b>{esc(it['insight'])}</b>"
        if not show_link:
            blocks.append(b)
            continue
        try:   # 원문 링크 (AI가 가장 많이 참고한 기사) — 관리자 화면에서 켰을 때만
            iss = issues[int(it.get("issue_no", k)) - 1]
            a = iss["articles"][max(0, int(it.get("source", 1)) - 1)]
            urls = [a["url"]] + [x["url"] for x in iss["articles"]]
            url = next((u for u in urls if "news.google.com" not in u and len(u) <= 90), "")
            if url:                       # 한 줄에 들어가는 실제 기사 주소만 표시
                b += f"\n🔗 {esc(url)}"
        except Exception:  # noqa
            pass
        blocks.append(b)
    return blocks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    dc = cfg.get("deep_brief", {})
    if not dc.get("enabled", True) and not args.force:
        return print("⑩ 사용 안 함")
    if not args.force and not is_market_day(cfg):
        return print("휴장일 → 건너뜀")
    state = load_state("deep_brief")
    day = now().strftime("%Y-%m-%d")
    if state.get("last_date") == day and not args.force:
        return print("오늘 이미 발송함")

    n = max(3, min(8, int(dc.get("issues", 6))))
    issues = gather(cfg, dc)
    if len(issues) < 2:
        return print("❌ 본문을 읽은 이슈가 부족해 발송 안 함")
    data = plan(issues, cfg, n)
    if not data:
        return print("❌ AI 정리 실패 → 발송 안 함 (부실한 브리핑 대신 건너뜀)")

    blocks = compose(data, issues, n, dc.get("show_link", False))
    send_text("\n\n".join(blocks), cfg, preview=False)
    time.sleep(1)
    tail = []
    if data.get("checkpoints"):
        tail.append("👀 <b>오늘 체크 포인트</b>\n" + "\n".join(f"• {esc(x)}" for x in data["checkpoints"][:5]))
    if cfg.get("footer"):
        tail.append(f"<i>{esc(cfg['footer'])}</i>")
    rows = [r for r in (data.get("table") or []) if isinstance(r, dict)][:n]
    if rows:
        send_photo(table_image(rows), "\n\n".join(tail) or "📋 오늘의 핵심 변수", cfg)
    elif tail:
        send_text("\n\n".join(tail), cfg, preview=False)

    state["last_date"] = day
    add_history(state, f"⑩ 장전 키워드: {data.get('headline', '')}")
    save_state("deep_brief", state)


if __name__ == "__main__":
    main()
