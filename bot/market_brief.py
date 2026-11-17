"""③ 장전(08시)·장중(11시)·장마감(16시) 증시 브리핑
- 지수/환율: 네이버 증권 → 실패 시 야후 파이낸스(yfinance)
- 뉴스: 구글 뉴스 RSS + 네이버 금융 주요뉴스 헤드라인
- 요약: Gemini (키가 없으면 지수 + 헤드라인 목록만 발송)
실행: python -m bot.market_brief --slot pre|mid|close   (생략 시 현재 시각으로 자동 판단)
"""
import argparse
import re
from urllib.parse import quote

import feedparser
import requests
from bs4 import BeautifulSoup

from bot.common import (UA, add_history, esc, gemini, is_market_day, load_config,
                        load_state, now, save_state, send_text)

SLOT_NAME = {"pre": "🌅 장전 브리핑", "mid": "☀️ 장중 브리핑", "close": "🌙 장마감 브리핑"}

# 표시이름, 네이버 코드(국내지수), 야후 티커
DOMESTIC = [("코스피", "KOSPI", "^KS11"), ("코스닥", "KOSDAQ", "^KQ11")]
OVERSEAS = [("다우", "^DJI"), ("S&P500", "^GSPC"), ("나스닥", "^IXIC"),
            ("필라델피아 반도체", "^SOX"), ("WTI", "CL=F"), ("미 10년물 금리", "^TNX")]
FX = [("원/달러", "KRW=X")]


def naver_index(code):
    r = requests.get(f"https://m.stock.naver.com/api/index/{code}/basic", headers=UA, timeout=15)
    j = r.json()
    return {"price": j["closePrice"], "chg": j["compareToPreviousClosePrice"],
            "pct": j["fluctuationsRatio"]}


def yahoo(ticker):
    import yfinance as yf
    h = yf.Ticker(ticker).history(period="7d")["Close"].dropna()
    last, prev = float(h.iloc[-1]), float(h.iloc[-2])
    return {"price": f"{last:,.2f}", "chg": f"{last - prev:+,.2f}",
            "pct": f"{(last / prev - 1) * 100:+.2f}"}


def get_quotes(slot):
    rows = []
    targets = []
    if slot == "pre":
        targets += [(n, None, t) for n, t in OVERSEAS]
    targets += DOMESTIC
    targets += [(n, None, t) for n, t in FX]
    for name, ncode, yt in targets:
        q = None
        if ncode:
            try:
                q = naver_index(ncode)
            except Exception as e:  # noqa
                print("네이버 지수 실패:", name, e)
        if q is None:
            try:
                q = yahoo(yt)
            except Exception as e:  # noqa
                print("야후 실패:", name, e)
                continue
        rows.append((name, q))
    return rows


def fmt_quote(name, q):
    try:
        v = float(str(q["pct"]).replace("%", ""))
        arrow, pct = ("🔺" if v > 0 else "🔻" if v < 0 else "▪️"), f"{v:+.2f}"
    except ValueError:
        arrow, pct = "▪️", str(q["pct"])
    return f"{arrow} {name} {q['price']} ({pct}%)"


def get_news(cfg, limit=30):
    heads, seen = [], set()
    for kw in cfg["market"].get("keywords", ["증시"]):
        url = (f"https://news.google.com/rss/search?q={quote(kw)}+when:1d"
               "&hl=ko&gl=KR&ceid=KR:ko")
        for e in feedparser.parse(url).entries[:10]:
            t = re.sub(r"\s+-\s+[^-]+$", "", e.title)  # 끝의 '- 언론사' 제거
            if t not in seen:
                seen.add(t)
                heads.append({"title": t, "link": e.link})
    try:
        r = requests.get("https://finance.naver.com/news/mainnews.naver", headers=UA, timeout=15)
        r.encoding = "euc-kr"
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.select(".articleSubject a")[:15]:
            t = a.get_text(strip=True)
            if t and t not in seen:
                seen.add(t)
                heads.append({"title": t, "link": "https://finance.naver.com" + a["href"]})
    except Exception as e:  # noqa
        print("네이버 뉴스 실패:", e)
    return heads[:limit]


def build_prompt(slot, quotes, news):
    focus = {
        "pre": "간밤 미국 증시와 환율·금리를 정리하고, 오늘 국내 증시에서 주목할 이슈와 섹터를 짚어줘.",
        "mid": "오전 장 흐름(지수 방향, 강세/약세 업종과 이슈)을 정리해줘.",
        "close": "오늘 장 마감 결과와 상승/하락 요인, 내일 체크할 일정·이슈를 정리해줘.",
    }[slot]
    q = "\n".join(fmt_quote(n, v) for n, v in quotes)
    n = "\n".join(f"- {x['title']}" for x in news)
    return (
        f"너는 증권 방송 작가야. {focus}\n"
        "규칙: 아래 데이터와 헤드라인에 있는 내용만 사용, 없는 수치를 만들지 말 것, "
        "매수·매도 권유 금지, 한국어, 텔레그램용 짧은 문장.\n"
        "형식(마크다운·별표 쓰지 말 것):\n"
        "📌 핵심 요약\n• (2~3줄)\n\n🔥 주요 이슈\n• (4~6줄, 각 1줄)\n\n👀 체크 포인트\n• (1~3줄)\n\n"
        f"[지수·환율]\n{q}\n\n[뉴스 헤드라인]\n{n}"
    )


def detect_slot(h):
    return "pre" if h < 10 else ("mid" if h < 14 else "close")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", choices=["pre", "mid", "close", "auto"], default="auto")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    mc = cfg["market"]
    slot = detect_slot(now().hour) if args.slot == "auto" else args.slot
    if not mc.get("enabled", True) or not mc.get("slots", {}).get(slot, True):
        return print("브리핑 꺼짐:", slot)
    if not args.force and not is_market_day(cfg):
        return print("휴장일 → 건너뜀")

    state = load_state("market")
    key = f"{now():%Y-%m-%d}-{slot}"
    if not args.force and key in state.get("done", []):
        return print("이미 발송함:", key)

    quotes = get_quotes(slot)
    news = get_news(cfg)
    summary = gemini(build_prompt(slot, quotes, news), cfg)
    if not summary:  # AI 없이 보내는 기본 형식
        summary = "📰 주요 헤드라인\n" + "\n".join(f"• {x['title']}" for x in news[:8])
    summary = summary.replace("**", "")

    title = f"{SLOT_NAME[slot]} | {now():%m/%d %H:%M}"
    parts = [f"<b>{esc(title)}</b>",
             "\n".join(esc(fmt_quote(n, q)) for n, q in quotes),
             esc(summary)]
    if cfg.get("footer"):
        parts.append(f"<i>{esc(cfg['footer'])}</i>")
    send_text("\n\n".join(parts), cfg)

    state.setdefault("done", []).append(key)
    state["done"] = state["done"][-60:]
    add_history(state, f"[브리핑] {title}")
    state["last_run"] = now().strftime("%Y-%m-%d %H:%M")
    save_state("market", state)


if __name__ == "__main__":
    main()
