"""⑫ 시장을 보는 대가들의 시선 (평일 아침)
- 해외 투자 대가 21명(관리자 화면에서 수정 가능)의 '공개 발언'을 국내외 언론 보도·공식 뉴스레터에서 수집
- 국내 증권사 리서치센터장·전략가의 공개 전망도 함께 수집
- 기사 본문을 읽혀 AI가 '누가 / 무엇을 / 어디서(출처·날짜)'로 요약. 기사에 없는 발언은 만들지 않음
- 최근 5일 안에 이미 소개한 기사는 다시 쓰지 않음
실행: python -m bot.gurus [--force]
"""
import argparse
import calendar
import datetime as dt
import json
import re
import time
from urllib.parse import quote

import feedparser
import requests

from bot.common import (KST, UA, add_history, ai_json, esc, gemini, is_market_day, load_config, load_state, now,
                        save_state, send_text)
from bot.deep_brief import article
from bot.hot_issue import clean_title

# (한글 이름, 영문 이름, 소개) — 첨부 자료의 21명
DEFAULT_GURUS = [
    ("마크 미너비니", "Mark Minervini", "성장주 트레이더"),
    ("데이비드 라이언", "David Ryan", "IBD 출신 트레이더"),
    ("올리버 켈", "Oliver Kell", "추세 트레이더"),
    ("댄 쟁거", "Dan Zanger", "차트 패턴 트레이더"),
    ("크리스티안 쿨라마기", "Kristjan Kullamagi", "모멘텀 트레이더"),
    ("프라딥 본데", "Pradeep Bonde", "Stockbee"),
    ("브라이언 섀넌", "Brian Shannon", "Alphatrends"),
    ("피터 브랜트", "Peter Brandt", "차트 트레이더"),
    ("존 볼린저", "John Bollinger", "볼린저밴드 창시자"),
    ("빌 애크먼", "Bill Ackman", "퍼싱스퀘어"),
    ("레이 달리오", "Ray Dalio", "브리지워터 창업자"),
    ("캐시 우드", "Cathie Wood", "ARK인베스트"),
    ("댄 로브", "Daniel Loeb", "써드포인트"),
    ("데이비드 아인혼", "David Einhorn", "그린라이트캐피털"),
    ("칼 아이칸", "Carl Icahn", "아이칸엔터프라이즈"),
    ("제프리 건들락", "Jeffrey Gundlach", "더블라인캐피털"),
    ("빌 그로스", "Bill Gross", "PIMCO 공동창업자"),
    ("모하메드 엘에리언", "Mohamed El-Erian", "前 PIMCO CEO"),
    ("하워드 막스", "Howard Marks", "오크트리캐피털"),
    ("마이클 버리", "Michael Burry", "사이언 자산운용"),
    ("워런 버핏", "Warren Buffett", "버크셔해서웨이"),
]
# 공식 글(뉴스레터·메모) RSS
DEFAULT_FEEDS = [
    ("마이클 버리", "https://michaeljburry.substack.com/feed"),
    ("빌 그로스", "https://williamhgross.com/feed/"),
]
KOREA_QUERIES = ["리서치센터장 증시 전망", "증권사 투자전략 코스피 전망", "센터장 코스피", "증권 전략가 증시"]


def rss(url):
    try:
        return feedparser.parse(requests.get(url, headers=UA, timeout=15).content).entries
    except Exception as e:  # noqa
        print("  RSS 실패:", url[:60], str(e)[:60])
        return []


def ts_of(e):
    pub = e.get("published_parsed") or e.get("updated_parsed")
    return dt.datetime.fromtimestamp(calendar.timegm(pub), KST) if pub else None


def gnews(q, lang="ko", days=3):
    if lang == "ko":
        url = f"https://news.google.com/rss/search?q={quote(q)}+when:{days}d&hl=ko&gl=KR&ceid=KR:ko"
    else:
        url = f"https://news.google.com/rss/search?q={quote(q)}+when:{days}d&hl=en-US&gl=US&ceid=US:en"
    out = []
    for e in rss(url)[:20]:
        raw = e.get("title", "")
        src = (e.get("source") or {}).get("title") or (raw.rsplit(" - ", 1)[-1] if " - " in raw else "")
        out.append({"title": clean_title(raw), "link": e.get("link", ""), "source": src.strip(), "ts": ts_of(e)})
    return out


# ---------------------------------------------------------------- 1) 수집
def collect_gurus(gurus, days, used):
    found = []
    for ko, en, desc in gurus:
        last_ko = ko.split()[-1] if " " in ko else ko
        last_en = en.split()[-1]
        items = gnews(f'"{ko}"', "ko", days) + gnews(f'"{en}"', "en", days)
        keep = []
        for it in items:
            t = it["title"]
            if it["title"] in used or not it["ts"]:
                continue
            if last_ko not in t and ko not in t and last_en.lower() not in t.lower():
                continue                       # 제목에 이름이 있는 기사만 (언급 수준 기사 제외)
            keep.append({**it, "who": ko, "role": desc})
        keep.sort(key=lambda x: x["ts"], reverse=True)
        if keep:
            print(f"[대가] {ko}: 기사 {len(keep)}건")
        found += keep[:3]
        time.sleep(0.3)
    return found


def collect_feeds(feeds, used, days=7):
    out, t = [], now()
    for who, url in feeds:
        for e in rss(url)[:5]:
            ts = ts_of(e)
            title = e.get("title", "")
            if not ts or (t - ts).days > days or title in used:
                continue
            body = re.sub(r"<[^>]+>", " ", (e.get("summary") or ""))[:1500]
            out.append({"title": title, "link": e.get("link", ""), "source": f"{who} 공식 글", "ts": ts,
                        "who": who, "role": "공식 뉴스레터·메모", "body": body})
    print(f"[공식 글] {len(out)}건")
    return out


def collect_korea(used):
    out, seen = [], set()
    for q in KOREA_QUERIES:
        for it in gnews(q, "ko", 1):
            t = it["title"]
            if t in seen or t in used or not it["ts"]:
                continue
            if not re.search(r"센터장|전략가|연구원|팀장|본부장", t):
                continue
            seen.add(t)
            out.append({**it, "who": "", "role": "국내 증권사"})
    out.sort(key=lambda x: x["ts"], reverse=True)
    print(f"[국내 전문가] 기사 {len(out)}건")
    return out[:8]


def add_bodies(items, limit):
    """기사 본문을 붙임 (발언 내용 확인용). 본문을 못 읽으면 제목만으로는 쓰지 않도록 제외"""
    out = []
    for it in items:
        if len(out) >= limit:
            break
        if not it.get("body"):
            url, body = article(it["link"])
            it["link"], it["body"] = url, body[:1500]
        if len(it.get("body") or "") >= 150:
            out.append(it)
    return out


# ---------------------------------------------------------------- 2) AI 정리
def build_prompt(glob, korea, ng, nk):
    def fmt(i, x):
        return (f"<자료 {i}> 대상: {x['who'] or '(기사 속 국내 전문가)'} / {x['role']} / 매체: {x['source']} / "
                f"날짜: {x['ts']:%m/%d}\n제목: {x['title']}\n본문: {x['body']}")
    g = "\n\n".join(fmt(i, x) for i, x in enumerate(glob, 1))
    k = "\n\n".join(fmt(i, x) for i, x in enumerate(korea, len(glob) + 1))
    return (
        "오늘 아침 텔레그램에 '시장을 보는 대가들의 시선'을 올린다. 아래 자료는 실제 보도·공식 글이다.\n"
        "[가장 중요한 규칙]\n"
        "- 자료 본문에 그 사람이 '직접 말하거나 쓴 내용'으로 나온 것만 쓴다. 기자의 해석·단순 언급·과거 일화는 제외.\n"
        "- 자료에 없는 발언·숫자·날짜를 절대 만들지 않는다. 확실하지 않으면 그 사람은 뺀다.\n"
        "- quote: 발언의 요지 (45자 이내, 한국어). 원문 표현을 옮긴 것이면 is_direct=true, 요약이면 false.\n"
        "- meaning: 지금 시장과 연결한 의미 한 줄 (60자 이내). 매수·매도 권유 금지.\n"
        "- stance: 시장(또는 해당 자산)에 대해 bull(낙관) / bear(신중·비관) / neutral\n"
        "- source: 매체명 그대로, date: 자료의 날짜(MM/DD), ref: 자료 번호\n"
        f"- global: 해외 대가 중 최대 {ng}명(한 사람당 1개, 최신·시장 영향 큰 순)\n"
        f"- korea: 국내 증권사 리서치센터장·전략가 최대 {nk}명. who는 '이름 직함', role은 '증권사명'\n"
        "- headline: 오늘 대가들 시각을 한 줄로 (40자 이내)\n"
        '출력은 JSON만: {"headline": "", "global": [{"who": "", "role": "", "quote": "", "is_direct": false, '
        '"meaning": "", "stance": "neutral", "source": "", "date": "", "ref": 1}], "korea": [같은 형식]}\n\n'
        f"[해외 대가 자료]\n{g or '(없음)'}\n\n[국내 전문가 자료]\n{k or '(없음)'}"
    )


STANCE = {"bull": "📈", "bear": "📉", "neutral": "⚖️"}


def block(x):
    q = esc(x.get("quote", ""))
    q = f"“{q}”" if x.get("is_direct") else f"{q} (취지)"
    s = f"{STANCE.get(x.get('stance'), '⚖️')} <b>{esc(x.get('who', ''))}</b>"
    if x.get("role"):
        s += f" ({esc(x['role'])})"
    s += f"\n{q}"
    if x.get("meaning"):
        s += f"\n→ {esc(x['meaning'])}"
    s += f"\n<i>출처: {esc(x.get('source', ''))} ({esc(x.get('date', ''))})</i>"
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    gc = cfg.get("gurus", {})
    if not gc.get("enabled", True) and not args.force:
        return print("⑫ 사용 안 함")
    if not args.force and not is_market_day(cfg):
        return print("휴장일 → 건너뜀")
    state = load_state("gurus")
    t = now()
    if state.get("last_date") == f"{t:%Y-%m-%d}" and not args.force:
        return print("오늘 이미 발송함")
    used = {u["title"] for u in state.get("used", [])
            if (t - dt.datetime.fromisoformat(u["at"])).days < 5}

    gurus = [(g["name"], g["en"], g.get("desc", "")) for g in gc.get("list", []) if g.get("name") and g.get("en")] \
        or DEFAULT_GURUS
    ng, nk = int(gc.get("max_global", 6)), int(gc.get("max_korea", 3))
    glob = collect_feeds(DEFAULT_FEEDS, used) + collect_gurus(gurus, int(gc.get("days", 3)), used)
    glob.sort(key=lambda x: x["ts"], reverse=True)
    glob = add_bodies(glob, 18)
    korea = add_bodies(collect_korea(used), 6) if gc.get("korea", True) else []
    print(f"[자료] 해외 {len(glob)}건 · 국내 {len(korea)}건 (본문 확인)")
    if len(glob) + len(korea) < 2:
        return print("❌ 발언 자료가 부족해 오늘은 건너뜀")

    out = gemini(build_prompt(glob, korea, ng, nk), cfg, want_json=True, timeout=150)
    data = ai_json(out, "global")
    if not data:
        return print("❌ AI 정리 실패 → 발송 안 함:", (out or "")[:200])

    def ok(lst):
        return [x for x in (lst if isinstance(lst, list) else [])
                if isinstance(x, dict) and x.get("who") and x.get("quote")]
    gl = ok(data.get("global"))[:ng]
    ko = ok(data.get("korea"))[:nk]
    if not ko and gl:   # 목록 하나로만 온 경우: '센터장·증권' 등은 국내로 분리
        ko = [x for x in gl if re.search(r"센터장|전략가|증권|연구원", f"{x.get('who')}{x.get('role')}")][:nk]
        gl = [x for x in gl if x not in ko][:ng]
    if len(gl) + len(ko) < 2:
        return print("❌ 확인된 발언이 2건 미만 → 건너뜀")

    wd = "월화수목금토일"[t.weekday()]
    parts = [f"🎙 <b>시장을 보는 대가들의 시선</b> | {t:%m/%d}({wd})"]
    if isinstance(data.get("headline"), str) and data["headline"]:
        parts[0] += f"\n<i>{esc(data['headline'])}</i>"
    if gl:
        parts.append("🌎 <b>해외 투자 대가</b>\n\n" + "\n\n".join(block(x) for x in gl))
    if ko:
        parts.append("🇰🇷 <b>국내 증시 전문가</b>\n\n" + "\n\n".join(block(x) for x in ko))
    parts.append("<i>※ 공개 발언·보도를 AI가 요약한 것으로 원문 표현과 다를 수 있으며, 투자 권유가 아닙니다.</i>")
    send_text("\n\n".join(parts), cfg, preview=False)

    allsrc = glob + korea
    for x in gl + ko:
        try:
            used_item = allsrc[int(x.get("ref", 0)) - 1]
            state.setdefault("used", []).append({"title": used_item["title"], "at": t.isoformat()})
        except Exception:  # noqa
            pass
    state["used"] = state.get("used", [])[-80:]
    state["last_date"] = f"{t:%Y-%m-%d}"
    add_history(state, f"⑫ 대가들의 시선: 해외 {len(gl)} · 국내 {len(ko)}")
    save_state("gurus", state)


if __name__ == "__main__":
    main()
