"""③ 장전(08시)·장중(11시)·장마감(16시) 증시 브리핑
- 지수/환율: 네이버 증권 → 실패 시 야후 파이낸스(yfinance)
- 뉴스: 구글 뉴스 RSS + 네이버 금융 주요뉴스 헤드라인
- 요약: Gemini (키가 없으면 지수 + 헤드라인 목록만 발송)
실행: python -m bot.market_brief --slot pre|mid|close   (생략 시 현재 시각으로 자동 판단)
"""
import argparse
import json
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


def market_still_open(cfg):
    """장마감 브리핑 전에 '정말 장이 끝났는지' 확인.
    1) 관리자 화면에 등록한 '장마감 지연일'(예: 수능일 16:30 마감)
    2) 네이버 증권의 코스피 장 상태(OPEN/CLOSE) — 갑작스러운 지연도 자동 감지"""
    t = now()
    late = cfg["market"].get("late_close", {})          # {"2026-11-19": "16:30"}
    close_at = late.get(t.strftime("%Y-%m-%d"))
    if close_at:
        hh, mm = map(int, close_at.split(":"))
        if (t.hour, t.minute) < (hh, mm + 5 if mm < 55 else mm):
            print(f"[장 상태] 오늘은 {close_at} 마감일 → 아직 장중")
            return True
    try:
        r = requests.get("https://m.stock.naver.com/api/index/KOSPI/basic", headers=UA, timeout=15)
        status = str(r.json().get("marketStatus", "")).upper()
        print("[장 상태] 네이버 코스피:", status or "(정보 없음)")
        return status == "OPEN"
    except Exception as e:  # noqa
        print("[장 상태] 확인 실패 → 마감으로 간주:", e)
        return False


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


# 지수용 표시: 상승 빨간 삼각형 / 하락 파란 역삼각형 / 보합 대시
UP, DOWN, FLAT = "🔺", "🔽", "➖"
# 헤드라인용 작은 표시: 호재 주황(빨강 계열) / 악재 파랑 / 중립 검정 (텔레그램에 작은 원 이모지가 없어 작은 마름모·사각형 사용)
H_UP, H_DOWN, H_FLAT = "🔸", "🔹", "▪️"


def fmt_quote(name, q):
    try:
        v = round(float(str(q["pct"]).replace("%", "")), 2)
        mark = UP if v > 0 else DOWN if v < 0 else FLAT
        pct = f"{v:+.2f}" if v else "0.00"
    except ValueError:
        mark, pct = FLAT, str(q["pct"])
    return f"{mark} {name} {q['price']} ({pct}%)"


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
        "공통 규칙: 아래 데이터와 헤드라인에 있는 사실만 사용, 없는 수치·사실을 만들지 말 것, "
        "매수·매도 권유 금지, 한국어, 마크다운·별표·이모지 쓰지 말 것.\n\n"
        "[주요 헤드라인 작성 규칙]\n"
        "- 같은 사건을 다룬 헤드라인은 하나로 묶고, 서로 다른 이슈만 최대 5개 (중요한 순서)\n"
        "- 헤드라인을 그대로 옮기지 말고, 팩트(수치·고유명사)를 살려 흐름이 보이는 '스토리형 제목'으로 새로 쓰기\n"
        "  (원인 → 결과, 또는 '○○에도 △△' 처럼 인과가 드러나게. 예시 문장을 따라 쓰지 말고 그날 기사로 새로 쓸 것)\n"
        "- 언론사명, [말머리], (종합)·(속보) 같은 표기는 절대 넣지 말 것\n"
        "- 한 줄 40자 이내\n"
        "- tone: 시장·해당 자산에 상승/호재면 up, 하락/악재면 down, 중립이면 flat\n\n"
        "[핵심 요약] 2~3줄, 각 60자 이내, 지수 숫자를 나열하지 말고 '왜 그렇게 움직였는지' 중심\n"
        "[체크 포인트] 1~3줄, 앞으로 지켜볼 일정·변수·관전 포인트만 (위 지수 숫자를 다시 적지 말 것)\n\n"
        "출력은 JSON만:\n"
        '{"summary": ["핵심 요약 2~3줄"], '
        '"headlines": [{"title": "스토리형 제목", "tone": "up|down|flat"}], '
        '"checkpoints": ["체크 포인트 1~3줄"]}\n\n'
        f"[지수·환율]\n{q}\n\n[뉴스 헤드라인]\n{n}"
    )


TONE = {"up": H_UP, "down": H_DOWN, "flat": H_FLAT}
UP_WORDS = ("상승", "급등", "강세", "반등", "돌파", "최고", "호실적", "↑", "상향", "흑자")
DOWN_WORDS = ("하락", "급락", "약세", "후퇴", "부진", "최저", "↓", "하향", "적자", "우려")


def guess_tone(t):
    up, down = any(w in t for w in UP_WORDS), any(w in t for w in DOWN_WORDS)
    return "up" if up and not down else "down" if down and not up else "flat"


def build_body(slot, quotes, news, cfg):
    """AI 요약(JSON) → 텔레그램 본문. AI 실패 시 헤드라인 5개만 정리해서 사용"""
    from bot.hot_issue import clean_title, dedupe
    data = {}
    out = gemini(build_prompt(slot, quotes, news), cfg, want_json=True)
    if out:
        try:
            data = json.loads(re.sub(r"^```(json)?|```$", "", out.strip()).strip())
        except Exception as e:  # noqa
            print("⚠️ AI 응답 해석 실패:", e, out[:200])
    else:
        print("⚠️ AI 응답 없음 → 헤드라인만 발송")

    heads = []
    for h in data.get("headlines", []) if isinstance(data, dict) else []:
        t = clean_title(str(h.get("title", "") if isinstance(h, dict) else h))
        tone = h.get("tone") if isinstance(h, dict) else None
        if t:
            heads.append((t, tone if tone in TONE else guess_tone(t)))
    if not heads:  # AI 실패 → 원 헤드라인에서 말머리·언론사 지우고 중복 제거
        heads = [(t, guess_tone(t)) for t in dedupe([clean_title(x["title"]) for x in news])]
    titles = dedupe([t for t, _ in heads])
    heads = [(t, tone) for t, tone in heads if t in titles][:5]

    blocks = []
    if data.get("summary"):
        blocks.append("📌 <b>핵심 요약</b>\n" + "\n".join(f"• {esc(x)}" for x in data["summary"][:3]))
    blocks.append("📰 <b>주요 헤드라인</b>\n" + "\n".join(f"{TONE[tone]} {esc(t)}" for t, tone in heads))
    if data.get("checkpoints"):
        blocks.append("👀 <b>체크 포인트</b>\n" + "\n".join(f"• {esc(x)}" for x in data["checkpoints"][:3]))
    return "\n\n".join(blocks)


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
    if slot == "close" and not args.force and market_still_open(cfg):
        return print("아직 장이 열려 있음 → 장마감 브리핑 보류 (다음 실행 때 다시 확인)")

    quotes = get_quotes(slot)
    news = get_news(cfg)
    body = build_body(slot, quotes, news, cfg)

    title = f"{SLOT_NAME[slot]} | {now():%m/%d %H:%M}"
    parts = [f"<b>{esc(title)}</b>",
             "\n".join(esc(fmt_quote(n, q)) for n, q in quotes),
             body]
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
