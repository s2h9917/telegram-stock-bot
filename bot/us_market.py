"""⑪ 미국시장 섹터별 마감 + 국내 관련 테마 (평일 07:05)
1) 미국 주요 종목(관심 목록) 등락 → 급등/강세/급락/약세   … 실제 시세 (야후 파이낸스)
2) 섹터·테마 ETF 등락 → 섹터 강세/약세                        … 실제 시세
3) 간밤 뉴스 + 위 등락으로 AI가 '오늘의 핫 이슈'·'개별종목 이슈'를 고르고
   네이버 증권 테마 이름 중 해당 테마를 지정 → 테마의 대표 종목을 '관련 종목(참고)'로 표시
   (종목은 AI가 지어내지 않고, 네이버 테마 목록 + 관리자가 추가한 종목에서만 가져옴)
실행: python -m bot.us_market [--force]
"""
import argparse
import datetime as dt
import json
import re

import requests

from bot.card_news import collect
from bot.common import (UA, add_history, esc, gemini, is_market_day, load_config, load_state, now,
                        save_state, send_text)

# 관심 종목 (티커, 한글 이름) — 관리자 화면에서 더하거나 뺄 수 있음
DEFAULT_WATCH = [
    ("NVDA", "엔비디아"), ("AMD", "AMD"), ("AVGO", "브로드컴"), ("TSM", "TSMC"), ("MU", "마이크론"),
    ("INTC", "인텔"), ("QCOM", "퀄컴"), ("ARM", "ARM"), ("MRVL", "마벨테크"), ("NXPI", "NXP세미"),
    ("ON", "온세미컨덕터"), ("TXN", "텍사스인스트루먼트"), ("AMAT", "어플라이드머티리얼즈"),
    ("LRCX", "램리서치"), ("KLAC", "KLA"), ("ASML", "ASML"), ("SMCI", "슈퍼마이크로"),
    ("DELL", "델테크"), ("HPE", "HPE"), ("WDC", "웨스턴디지털"), ("STX", "시게이트"),
    ("SNDK", "샌디스크"), ("ANET", "아리스타"), ("CRDO", "크레도"), ("ALAB", "아스테라랩스"),
    ("COHR", "코히런트"), ("LITE", "루멘텀"), ("CIEN", "시에나"), ("GLW", "코닝"), ("VRT", "버티브"),
    ("MSFT", "마이크로소프트"), ("GOOGL", "알파벳"), ("AMZN", "아마존"), ("META", "메타"),
    ("AAPL", "애플"), ("ORCL", "오라클"), ("CRM", "세일즈포스"), ("PLTR", "팔란티어"),
    ("NOW", "서비스나우"), ("SNOW", "스노우플레이크"), ("CRWV", "코어위브"), ("NBIS", "네비우스"),
    ("IBM", "IBM"), ("ADBE", "어도비"), ("PANW", "팰로앨토"), ("CRWD", "크라우드스트라이크"),
    ("ZS", "지스케일러"), ("FTNT", "포티넷"), ("NET", "클라우드플레어"), ("IONQ", "아이온큐"),
    ("RGTI", "리게티"), ("QBTS", "디웨이브"), ("TSLA", "테슬라"), ("GM", "GM"), ("F", "포드"),
    ("RIVN", "리비안"), ("RKLB", "로켓랩"), ("ASTS", "AST스페이스모바일"), ("LMT", "록히드마틴"),
    ("NOC", "노스롭그루만"), ("RTX", "RTX"), ("GE", "GE에어로"), ("HWM", "하우멧에어로"),
    ("BA", "보잉"), ("CEG", "컨스텔레이션"), ("VST", "비스트라"), ("OKLO", "오클로"),
    ("SMR", "뉴스케일"), ("CCJ", "카메코"), ("GEV", "GE버노바"), ("FSLR", "퍼스트솔라"),
    ("ENPH", "엔페이즈"), ("RUN", "선런"), ("SHLS", "숄스테크"), ("BE", "블룸에너지"),
    ("XOM", "엑슨모빌"), ("LLY", "일라이릴리"), ("NVO", "노보노디스크"), ("MRNA", "모더나"),
    ("PFE", "화이자"), ("AMGN", "암젠"), ("REGN", "리제네론파마"), ("BMY", "브리스톨마이어"),
    ("VRTX", "버텍스"), ("RVMD", "레볼루션메디신"), ("ISRG", "인튜이티브써지컬"),
    ("UNH", "유나이티드헬스"), ("HUM", "휴마나"), ("NFLX", "넷플릭스"), ("SE", "SEA"),
    ("HAS", "해즈브로"), ("EFX", "에퀴팩스"), ("MMM", "3M"), ("COST", "코스트코"),
    ("WMT", "월마트"), ("NKE", "나이키"), ("DIS", "디즈니"), ("UBER", "우버"),
    ("COIN", "코인베이스"), ("MSTR", "스트래티지"), ("HOOD", "로빈후드"), ("JPM", "JP모건"),
    ("GS", "골드만삭스"), ("ALB", "앨버말"), ("FCX", "프리포트맥모란"),
]
# 섹터·테마 ETF (이름, 티커)
DEFAULT_ETFS = [
    ("반도체", "SOXX"), ("AI", "AIQ"), ("클라우드", "SKYY"), ("보안", "CIBR"), ("양자", "QTUM"),
    ("제약바이오", "XBI"), ("헬스케어", "XLV"), ("의료기기", "IHI"), ("2차전지", "LIT"),
    ("자동차", "CARZ"), ("로봇", "BOTZ"), ("우주", "UFO"), ("방산", "ITA"), ("원전", "NLR"),
    ("우라늄", "URA"), ("신재생E", "ICLN"), ("태양광", "TAN"), ("전력·유틸리티", "XLU"),
    ("에너지", "XLE"), ("비철금속", "COPX"), ("암호화폐", "BITQ"), ("금융", "XLF"),
    ("소비재", "XLY"), ("리츠", "VNQ"),
]
THEME_API = "https://stock.naver.com/api/stockSecurity/rankings/v2/domestic/themes"


# ---------------------------------------------------------------- 1) 미국 시세
def changes(tickers):
    """{티커: (등락률, 마지막 거래일)}"""
    import yfinance as yf
    out = {}
    try:
        df = yf.download(tickers, period="7d", group_by="ticker", progress=False,
                         auto_adjust=False, threads=True)
    except Exception as e:  # noqa
        print("⚠️ 미국 시세 실패:", e)
        return out
    for t in tickers:
        try:
            s = (df[t]["Close"] if len(tickers) > 1 else df["Close"]).dropna()
            if len(s) >= 2:
                out[t] = ((float(s.iloc[-1]) / float(s.iloc[-2]) - 1) * 100, s.index[-1].date())
        except Exception:  # noqa
            continue
    print(f"[미국 시세] {len(out)}/{len(tickers)}개")
    return out


def classify(watch, ch, uc):
    big, mid = float(uc.get("big_move", 5)), float(uc.get("move", 2))
    rows = sorted(((ch[t][0], name) for t, name in watch if t in ch), reverse=True)
    pick = lambda f: [n for r, n in rows if f(r)]  # noqa: E731
    cap = int(uc.get("max_names", 12))
    return {"급등": pick(lambda r: r >= big)[:cap], "강세": pick(lambda r: mid <= r < big)[:cap],
            "급락": list(reversed(pick(lambda r: r <= -big)))[:cap],
            "약세": list(reversed(pick(lambda r: -big < r <= -mid)))[:cap],
            "_rows": rows}


# ---------------------------------------------------------------- 2) 네이버 테마
MEMBER_API = "https://stock.naver.com/api/domestic/market/theme/{no}/stocklist"
NHEAD = {**UA, "Referer": "https://stock.naver.com/"}


def _get(url, params):
    r = requests.get(url, params=params, timeout=15, headers=NHEAD)
    r.raise_for_status()
    return r.json()


def theme_index():
    """{테마명: 테마번호} — 네이버 테마 전체(약 265개). 100개씩 이어받기"""
    index, cursor = {}, None
    for _ in range(8):
        params = {"sortType": "changeRate", "size": 100, "period": "daily"}
        if cursor:
            params["cursor"] = cursor
        try:
            d = _get(THEME_API, params)
        except Exception as e:  # noqa
            print("⚠️ 네이버 테마 목록 실패:", str(e)[:100])
            break
        for t in d.get("items", []):
            index[t["name"]] = str(t["code"])
        cursor = d.get("cursor")
        if not d.get("hasNext") or not cursor:
            break
    print(f"[네이버 테마] 목록 {len(index)}개")
    return index


def members(no, size=100):
    """테마 소속 전체 종목 (전일·당일 거래대금 큰 순) [{name, value, rate}]"""
    d = _get(MEMBER_API.format(no=no), {"sortType": "tradingValue", "sortDirection": "desc", "size": size})
    out = []
    for x in d.get("items", []):
        k = x.get("krx") or x.get("nxt") or {}
        out.append({"name": x.get("itemName", ""), "value": float(k.get("tradingValue") or 0),
                    "rate": float(k.get("changeRate") or 0)})
    return out


def load_themes(state, force=False, days=30):
    """테마 목록 + 테마별 전체 종목을 30일(한 달)마다 새로 받아 state에 저장 (매일 265번 요청하지 않도록)"""
    import time
    cache = state.get("themes") or {}
    fresh = cache.get("updated") and (now().date() - dt.date.fromisoformat(cache["updated"])).days < days
    if fresh and not force and cache.get("index"):
        return cache
    index = theme_index()
    if not index:
        return cache
    mem = {}
    for name, no in index.items():
        try:
            mem[name] = [m["name"] for m in members(no)]
        except Exception as e:  # noqa
            print(f"⚠️ 테마 종목 실패 {name}: {str(e)[:60]}")
            mem[name] = (cache.get("members") or {}).get(name, [])
        time.sleep(0.2)
    sizes = sorted(len(v) for v in mem.values())
    print(f"[네이버 테마] 종목 목록 갱신: 테마 {len(mem)}개, 테마당 {sizes[0]}~{sizes[-1]}종목")
    cache = {"updated": now().date().isoformat(), "index": index, "members": mem}
    state["themes"] = cache
    return cache


def hubs(cache):
    """여러 테마에 두루 끼는 대형주 (테마 4% 이상에 소속) → 관련 종목에서 뒤로 미룸"""
    freq = {}
    for v in (cache.get("members") or {}).values():
        for n in set(v):
            freq[n] = freq.get(n, 0) + 1
    th = max(6, int(len(cache.get("members") or {}) * 0.04))
    return {n for n, c in freq.items() if c >= th}


def match_theme(name, themes):
    """AI가 쓴 테마명을 네이버 테마명에 맞춤 (정확히 → 포함 관계)"""
    name = str(name).strip()
    if name in themes:
        return name
    key = re.sub(r"[\s()（）/·]", "", name)
    for t in themes:
        tk = re.sub(r"[\s()（）/·]", "", t)
        if key and (key in tk or tk.startswith(key)):
            return t
    return None


def related(theme_names, cache, uc, limit=6):
    """선택된 테마의 전체 종목 중 거래대금 큰 순으로 대표 종목을 번갈아 뽑음.
    관리자가 넣은 종목 우선, '-종목'·제외 종목 빼기, 여러 테마에 두루 끼는 대형주는 뒤로"""
    index = cache.get("index") or {}
    hub = hubs(cache)
    add = {k: [x.strip() for x in str(v).split(",") if x.strip()] for k, v in (uc.get("theme_add") or {}).items()}
    excl = set(uc.get("exclude_names") or [])
    used, lists = [], []
    for tn in theme_names:
        t = match_theme(tn, index)
        if not t or t in used:
            continue
        used.append(t)
        try:
            live = [m["name"] for m in members(index[t])]          # 오늘(전일) 거래대금 순
        except Exception:  # noqa
            live = (cache.get("members") or {}).get(t, [])
        plus = [x for x in add.get(t, []) if not x.startswith("-")]
        minus = {x[1:].strip() for x in add.get(t, []) if x.startswith("-")}
        live = [x for x in live if x not in hub] + [x for x in live if x in hub]
        lists.append([x for x in dict.fromkeys(plus + live) if x not in excl and x not in minus])
    out = []
    for i in range(30):
        for lst in lists:
            if i < len(lst) and lst[i] not in out:
                out.append(lst[i])
    return used, out[:limit]


# ---------------------------------------------------------------- 3) AI 이슈 선정
def pick_issues(cls, etf_rows, news, themes, cfg):
    movers = "\n".join(f"{n} {r:+.1f}%" for r, n in cls["_rows"] if abs(r) >= 2) or "(큰 변동 없음)"
    etfs = ", ".join(f"{n} {r:+.1f}%" for r, n in etf_rows)
    heads = "\n".join(f"- {x['title']} ({x['source']})" for x in news[:60])
    prompt = (
        "간밤 마감한 뉴욕증시를 바탕으로, 오늘 아침 국내 투자자가 주목할 이슈를 고른다.\n"
        "1) hot_issues: 뉴스 중 국내 증시에 파급이 큰 이슈 3개\n"
        "2) stock_issues: 아래 '미국 종목 등락'에서 크게 움직인 종목과 연결된 이슈 2~3개 (뉴스로 확인되는 이유가 있는 것만)\n"
        "각 이슈: title(뉴스에 근거한 사실, 50자 이내, 한 줄), sub(보충 사실 한 줄 40자 이내, 없으면 빈칸),\n"
        "themes(아래 '네이버 테마 목록'에 있는 이름을 글자 그대로 1~2개. 목록에 없는 이름 금지)\n"
        "규칙: 뉴스·시세에 없는 숫자 금지. '급등 예상', '매수' 같은 예측·권유 표현 금지. 한국어.\n"
        '출력은 JSON만: {"hot_issues": [{"title": "", "sub": "", "themes": [""]}], '
        '"stock_issues": [{"title": "", "sub": "", "themes": [""]}]}\n\n'
        f"[미국 종목 등락]\n{movers}\n\n[섹터 ETF]\n{etfs}\n\n[간밤 뉴스]\n{heads}\n\n"
        f"[네이버 테마 목록]\n{', '.join(themes)}"
    )
    out = gemini(prompt, cfg, want_json=True, timeout=120)
    try:
        return json.loads(re.sub(r"^```(json)?|```$", "", (out or "").strip()).strip())
    except Exception as e:  # noqa
        print("⚠️ AI 응답 해석 실패:", e, (out or "")[:200])
        return {}


# ---------------------------------------------------------------- 4) 메시지
def build(cls, etf_rows, issues, themes, uc, us_day):
    L = [f"🇺🇸 <b>미국시장 섹터별 마감</b> ({us_day:%m/%d} 마감)"]
    for label, icon in (("급등", "🚀"), ("강세", "🔺"), ("급락", "💥"), ("약세", "🔽")):
        if cls[label]:
            L.append(f"\n{icon} <b>{label}</b>\n{esc(', '.join(cls[label]))}")
    th = float(uc.get("etf_move", 1))
    up = [n for r, n in etf_rows if r >= th]
    dn = [n for r, n in reversed(etf_rows) if r <= -th]
    if up:
        L.append(f"\n📈 <b>섹터 강세</b>\n{esc(', '.join(up))}")
    if dn:
        L.append(f"\n📉 <b>섹터 약세</b>\n{esc(', '.join(dn))}")

    for key, title in (("hot_issues", "🔥 오늘의 핫 이슈"), ("stock_issues", "🏷 개별종목 이슈")):
        block, k = [], 0
        for it in (issues.get(key) or [])[:3]:
            used, stocks = related(it.get("themes") or [], themes, uc, int(uc.get("max_related", 6)))
            if not it.get("title"):
                continue
            k += 1
            s = f"{k}. <b>{esc(it['title'])}</b>"
            if it.get("sub"):
                s += f"\n   {esc(it['sub'])}"
            if stocks:
                s += f"\n- {esc(', '.join(stocks))}"
                if uc.get("show_theme", True):
                    s += f"\n  <i>(테마: {esc(' · '.join(used))})</i>"
            block.append(s)
        if block:
            L.append(f"\n{title}\n" + "\n\n".join(block))
    L.append("\n<i>※ 관련 종목은 참고용이며 투자 권유가 아닙니다.</i>")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    uc = cfg.get("us_market", {})
    if not uc.get("enabled", True) and not args.force:
        return print("⑪ 사용 안 함")
    if not args.force and not is_market_day(cfg):
        return print("국내 휴장일 → 건너뜀")
    state = load_state("us_market")

    watch = [(w["ticker"], w["name"]) for w in uc.get("watchlist", [])
             if w.get("ticker") and w.get("name")] or DEFAULT_WATCH
    etfs = [(e["name"], e["ticker"]) for e in uc.get("etfs", [])
            if e.get("ticker") and e.get("name")] or DEFAULT_ETFS
    ch = changes(sorted({t for t, _ in watch} | {t for _, t in etfs}))
    if not ch:
        return print("❌ 미국 시세를 못 받아 발송 안 함")
    us_day = max(d for _, d in ch.values())
    if state.get("last_us_day") == str(us_day) and not args.force:
        return print(f"미국 {us_day} 마감분은 이미 보냄 (미국 휴장일) → 건너뜀")

    cls = classify(watch, ch, uc)
    etf_rows = sorted(((ch[t][0], n) for n, t in etfs if t in ch), reverse=True)
    t = now()
    news = collect(uc.get("keywords") or ["뉴욕증시", "미국 증시", "나스닥", "엔비디아", "월가", "미국 국채"],
                   t - dt.timedelta(hours=14), t)
    themes = load_themes(state)
    issues = pick_issues(cls, etf_rows, news, themes.get("index") or {}, cfg) if themes.get("index") else {}
    if not issues:
        print("⚠️ 이슈 선정 실패 → 시세 부분만 발송")

    send_text(build(cls, etf_rows, issues, themes, uc, us_day), cfg, preview=False)
    state["last_us_day"] = str(us_day)
    add_history(state, f"⑪ 미국 마감 {us_day:%m/%d}: 급등 {len(cls['급등'])} · 급락 {len(cls['급락'])}")
    save_state("us_market", state)


if __name__ == "__main__":
    main()
