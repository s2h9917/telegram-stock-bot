"""⑤ 종목 뉴스 — 07~20시 매시간, 코스피·코스닥 상장사의 주가 영향 뉴스를 골라 업로드
- 뉴스: 구글 뉴스 RSS (단독·수주·공급계약·실적 등 키워드, 최근 몇 시간)
- 상장사 확인: 한국거래소(KIND) 상장법인 목록 (일주일마다 갱신해 state/listed.json에 보관)
- 선별: 중요도 점수(키워드) → Gemini가 최종 선택·제목 정리 (AI 실패 시 점수순)
- 해당 뉴스가 없으면 아무것도 보내지 않음
실행: python -m bot.stock_news          (평소)
      python -m bot.stock_news --force  (시간대·휴장일 무시)
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

from bot.common import (UA, add_history, esc, gemini, is_market_day, load_config, load_state,
                        now, save_state, send_text)
from bot.hot_issue import clean_title, similar

# ---------------------------------------------------------------- 중요도 기준
DEFAULT_KEYWORDS = ["단독", "수주", "공급계약", "계약 체결", "영업이익", "실적", "흑자전환",
                    "최대 실적", "기술수출", "FDA", "인수", "합병", "자사주", "유상증자", "임상"]

# (단어, 점수, 분류) — 점수가 높을수록 주가 영향이 큰 뉴스
SCORES = [
    ("단독", 5, "단독"), ("수주", 5, "수주"), ("공급계약", 5, "계약"), ("공급 계약", 5, "계약"),
    ("계약 체결", 4, "계약"), ("계약체결", 4, "계약"), ("기술수출", 6, "계약"), ("기술이전", 5, "계약"),
    ("사상 최대", 5, "실적"), ("최대 실적", 5, "실적"), ("어닝 서프라이즈", 5, "실적"),
    ("어닝서프라이즈", 5, "실적"), ("흑자전환", 5, "실적"), ("흑자 전환", 5, "실적"),
    ("어닝 쇼크", 5, "실적"), ("적자전환", 5, "실적"), ("영업이익", 3, "실적"), ("실적", 2, "실적"),
    ("잠정실적", 3, "실적"), ("FDA", 5, "승인"), ("품목허가", 5, "승인"), ("승인", 3, "승인"),
    ("임상", 3, "임상"), ("인수", 4, "M&A"), ("합병", 4, "M&A"), ("M&A", 4, "M&A"), ("매각", 3, "M&A"),
    ("자사주", 3, "주주환원"), ("소각", 3, "주주환원"), ("배당", 2, "주주환원"),
    ("유상증자", 4, "자본"), ("무상증자", 4, "자본"), ("전환사채", 3, "자본"),
    ("상장폐지", 6, "리스크"), ("거래정지", 6, "리스크"), ("횡령", 5, "리스크"), ("배임", 5, "리스크"),
    ("압수수색", 4, "리스크"), ("파업", 3, "리스크"), ("리콜", 4, "리스크"), ("특허", 3, "특허"), ("최초", 2, ""),
    ("상한가", 3, "급등락"), ("하한가", 3, "급등락"), ("신고가", 2, "급등락"), ("투자 유치", 2, "투자"),
    ("공장", 2, "투자"), ("증설", 3, "투자"),
]
# 이런 기사는 제외 (리딩방성 추천 기사, 칼럼, 인사·부고 등)
EXCLUDE = re.compile(r"(추천주|급등주|관심주|주목할 종목|특징주 모음|오늘의 운세|부고|인사\]|\[인사|칼럼|사설|"
                     r"\[포토\]|\[사진\]|기자수첩|광고|이벤트|채용|주간 증시|증시 일정|ETF 수익률 순위)")
# 회사 이름이 흔한 단어와 겹쳐 오인하기 쉬운 상장사 (필요하면 관리자 화면에서 추가)
AMBIGUOUS = {"대상", "미래", "한국", "서울", "우리", "동방", "신화", "대원", "국보", "진도", "세원", "삼일",
             "성안", "대성", "한일", "동양", "일신", "태양", "디오", "에이스", "백산", "유니온", "기산", "선진",
             "이원", "제일", "화신", "삼양", "대한", "평화", "신성", "동원", "보성", "상신", "한창", "부국"}
PARTICLE = set("가는이의와과도에은을를로,·…'\" )]")


# ---------------------------------------------------------------- 상장사 목록
def fetch_listed():
    """KIND(한국거래소 기업공시채널) 상장법인 목록: {회사명: (종목코드, 시장)}"""
    out = {}
    for mkt, label in [("stockMkt", "코스피"), ("kosdaqMkt", "코스닥")]:
        url = ("https://kind.krx.co.kr/corpgeneral/corpList.do?method=download"
               f"&searchType=13&marketType={mkt}")
        r = requests.get(url, headers=UA, timeout=30)
        html = r.content.decode("euc-kr", errors="replace")
        rows = re.findall(r"<tr>\s*<td>(.*?)</td>.*?<td[^>]*>\s*([0-9A-Z]{6})\s*</td>", html, re.S)
        for name, code in rows:
            name = re.sub(r"<[^>]+>", "", name).strip()
            if name:
                out[name] = [code, label]
        print(f"[상장사] {label} {len(rows)}개")
    return out


def load_listed():
    st = load_state("listed")
    fresh = st.get("updated", "") >= (now().date() - dt.timedelta(days=7)).isoformat()
    if st.get("companies") and fresh:
        return st["companies"]
    try:
        comp = fetch_listed()
        if len(comp) > 1500:
            save_state("listed", {"updated": now().date().isoformat(), "companies": comp})
            return comp
        print(f"⚠️ 상장사 목록이 비정상적으로 적음({len(comp)}개) → 이전 목록 우선 사용")
        if not st.get("companies"):
            return comp
    except Exception as e:  # noqa
        print("⚠️ 상장사 목록 갱신 실패 → 이전 목록 사용:", e)
    return st.get("companies", {})


def build_matcher(companies, extra_exclude=()):
    """긴 이름부터 찾도록 정렬 (예: 'SK하이닉스'가 'SK'보다 먼저)"""
    skip = AMBIGUOUS | set(extra_exclude)
    names = sorted((n for n in companies if len(n) >= 2 and n not in skip), key=len, reverse=True)
    return names


def find_company(title, names):
    for n in names:
        start = 0
        while True:
            i = title.find(n, start)
            if i < 0:
                break
            start = i + 1
            before = title[i - 1] if i > 0 else " "
            if re.match(r"[가-힣A-Za-z0-9]", before):   # '미래에셋' 안의 '미래'처럼 단어 중간이면 무시
                continue
            after = title[i + len(n)] if i + len(n) < len(title) else " "
            # 'SK그룹', 'LG전자'처럼 짧은 이름 뒤에 글자가 붙으면 다른 회사일 수 있음 → 조사·문장부호만 허용
            if len(n) <= 3 and re.match(r"[가-힣A-Za-z0-9]", after) and after not in PARTICLE:
                continue
            return n
    return None


def same_event(comp, a, b):
    """같은 회사의 두 기사가 같은 사건인지 (회사명은 빼고 비교해야 다른 사건을 구분할 수 있음)"""
    return similar(a.replace(comp, " "), b.replace(comp, " "))


# ---------------------------------------------------------------- 뉴스 수집·점수
def score_of(title):
    s, tags = 0, []
    for word, pt, tag in SCORES:
        if word in title:
            s += pt
            if tag and tag not in tags:
                tags.append(tag)
    if "[단독]" in title or title.startswith("단독"):
        s += 2
    return s, (tags[0] if tags else "")


def media_ok(src_name, src_url, media):
    """지정 매체 목록이 비어 있으면 모두 허용, 있으면 이름이나 주소가 맞는 기사만"""
    if not media:
        return True
    for m in media:
        name, dom = (m.get("name") or "").strip(), (m.get("domain") or "").strip().lower()
        if (name and name in src_name) or (dom and dom in src_url.lower()):
            return True
    return False


def collect(keywords, hours, media=()):
    queries = list(keywords)
    # 지정 매체가 있으면 '매체 주소 + 핵심 키워드' 검색도 추가 → 해당 매체 기사를 충분히 확보
    ors = " OR ".join(f'"{k}"' if " " in k else k for k in keywords[:10])
    queries += [f"site:{m['domain'].strip()} ({ors})" for m in media if (m.get("domain") or "").strip()]
    items, seen = [], set()
    for q in queries:
        url = f"https://news.google.com/rss/search?q={quote(q)}+when:{hours}h&hl=ko&gl=KR&ceid=KR:ko"
        try:
            feed = feedparser.parse(requests.get(url, headers=UA, timeout=15).content)
        except Exception as e:  # noqa
            print("뉴스 실패:", q, e)
            continue
        for e in feed.entries[:40]:
            raw = e.get("title", "")
            src = e.get("source") or {}
            src_name = (src.get("title") or "").strip() or (raw.rsplit(" - ", 1)[-1] if " - " in raw else "")
            src_url = src.get("href") or ""
            if raw in seen or not media_ok(src_name, src_url, media):
                continue
            seen.add(raw)
            pub = e.get("published_parsed")
            items.append({"raw": raw, "title": clean_title(raw), "link": e.get("link", ""),
                          "source": src_name, "ts": calendar.timegm(pub) if pub else 0})
    print(f"[뉴스] 검색 {len(queries)}회, " + ("지정 매체 " if media else "") + f"기사 {len(items)}건")
    return items


# ---------------------------------------------------------------- AI 최종 선택
def ai_pick(cands, n, cfg):
    lines = "\n".join(f"{i}. [{c['company']}] {c['title']}" for i, c in enumerate(cands))
    prompt = (
        "너는 증권 방송 뉴스 데스크야. 아래는 최근 1~2시간 상장사 뉴스 후보다.\n"
        f"주가에 영향을 줄 만한 중요한 뉴스를 중요도 순으로 최대 {n}개 골라.\n"
        "- 같은 회사의 같은 사건은 1개만, 단순 시황·홍보성·추천주 기사는 제외\n"
        "- 중요한 뉴스가 없으면 빈 배열\n"
        "- headline: 기사 제목을 사실 그대로 간결하게 다듬기 (40자 이내, 언론사·말머리 제거, 없는 사실 추가 금지)\n"
        "- tone: 해당 기업 주가에 호재면 up, 악재면 down, 중립이면 flat\n"
        '출력은 JSON만: {"items": [{"idx": 0, "headline": "...", "tone": "up|down|flat"}]}\n\n' + lines
    )
    out = gemini(prompt, cfg, want_json=True)
    if not out:
        return None
    try:
        data = json.loads(re.sub(r"^```(json)?|```$", "", out.strip()).strip())
        picked = []
        for it in data.get("items", []):
            i = int(it.get("idx", -1))
            if 0 <= i < len(cands):
                c = dict(cands[i])
                hl = clean_title(str(it.get("headline") or ""))
                # AI가 다른 기사 제목을 붙이는 실수 방지: 회사명이 없거나 원제목과 너무 다르면 원제목 사용
                ok = hl and c["company"] in hl and (similar(hl, c["title"], 0.2) or len(set(hl) & set(c["title"])) > len(hl) * 0.6)
                c["headline"] = hl if ok else c["title"]
                c["tone"] = it.get("tone") if it.get("tone") in ("up", "down", "flat") else "flat"
                picked.append(c)
        return picked
    except Exception as e:  # noqa
        print("⚠️ AI 응답 해석 실패:", e)
        return None


def resolve_links(items):
    """구글 뉴스 중간 주소를 실제 언론사 기사 주소로 바꿈 (짧고, 누르면 바로 기사로 이동)"""
    g = [c for c in items if "news.google.com" in c.get("link", "")]
    if not g:
        return
    try:
        from googlenewsdecoder import gnewsdecoder
        res = gnewsdecoder([c["link"] for c in g], interval=1)
        for c, r in zip(g, res if isinstance(res, list) else [res]):
            if r.get("success") or r.get("status"):
                c["orig_link"] = c["link"]
                c["link"] = r["decoded_url"]
            else:
                print("⚠️ 주소 변환 실패:", c["company"], r.get("message"))
        print(f"[링크] {sum(1 for c in g if 'orig_link' in c)}/{len(g)}건 실제 기사 주소로 변환")
    except Exception as e:  # noqa
        print("⚠️ 주소 변환 오류 → 구글 뉴스 주소 사용:", e)


# ---------------------------------------------------------------- 투자 대가 관점 (AI)
PERSPECTIVES = [("value", "🧓", "가치투자(버핏 스타일)"),
                ("growth", "🚀", "성장투자(캐시 우드 스타일)"),
                ("quant", "📊", "퀀트·수급 관점")]


def stock_metrics(code):
    """네이버 증권: 시총·PER·PBR·배당·52주 범위·컨센서스·외국인/기관 수급 (있는 것만)"""
    out = {}
    if not code:
        return out
    try:
        j = requests.get(f"https://m.stock.naver.com/api/stock/{code}/integration", headers=UA, timeout=15).json()
        for it in j.get("totalInfos", []) or []:
            k, v = it.get("key"), it.get("value")
            if k and v and v not in ("-", "N/A"):
                out[k] = v
        cons = j.get("consensusInfo") or {}
        if cons.get("priceTargetMean"):
            out["목표주가 평균"] = cons["priceTargetMean"]
        deal = (j.get("dealTrendInfos") or [{}])[0]
        for k, label in [("foreignerPureBuyQuant", "외국인 순매수(주)"), ("organPureBuyQuant", "기관 순매수(주)")]:
            if deal.get(k):
                out[label] = deal[k]
    except Exception as e:  # noqa
        print("지표 수집 실패:", code, e)
    try:
        b = requests.get(f"https://m.stock.naver.com/api/stock/{code}/basic", headers=UA, timeout=15).json()
        if b.get("closePrice"):
            out["현재가"] = b["closePrice"]
            out["등락률(%)"] = b.get("fluctuationsRatio", "")
    except Exception:  # noqa
        pass
    return out


def make_perspectives(items, companies, cfg):
    """새로 분석할 종목들을 한 번의 AI 호출로 처리 → {회사명: {value, growth, quant}}"""
    rows = []
    for c in items:
        code = companies.get(c["company"], [""])[0]
        m = stock_metrics(code)
        rows.append({"company": c["company"], "news": c["headline"],
                     "metrics": {k: str(v) for k, v in list(m.items())[:14]}})
    print("[관점] 분석 대상:", [r["company"] for r in rows])
    prompt = (
        "너는 투자 교육 콘텐츠 작가야. 각 종목 뉴스를 세 가지 투자 철학으로 해석한 '관전 포인트'를 써.\n"
        "1) value: 가치투자(버핏 스타일) — 경제적 해자, 이익의 지속성, 밸류에이션(PER·PBR), 재무 안정성\n"
        "2) growth: 성장투자(캐시 우드 스타일) — 혁신성, 시장 확장성, 장기 성장 스토리\n"
        "3) quant: 퀀트·수급 관점 — 지표 수준, 주가 위치(52주 범위), 외국인·기관 수급\n"
        "규칙:\n"
        "- 각 항목 한 문장, 45자 이내, 한국어\n"
        "- 주어진 뉴스와 지표(metrics)에 있는 사실·숫자만 사용, 없는 숫자 만들기 금지 (지표가 없으면 일반 원칙으로)\n"
        "- 매수·매도·목표가 제시 등 투자 권유 금지, '~를 확인할 필요', '~가 관건'처럼 체크포인트로 표현\n"
        "- 실제 인물이 한 말처럼 쓰지 말 것 (따옴표·'버핏은 말했다' 금지)\n"
        '출력 JSON만: {"items": [{"company": "...", "value": "...", "growth": "...", "quant": "..."}]}\n\n'
        + json.dumps(rows, ensure_ascii=False)
    )
    out = gemini(prompt, cfg, want_json=True)
    if not out:
        return {}
    try:
        data = json.loads(re.sub(r"^```(json)?|```$", "", out.strip()).strip())
        res = {}
        for it in data.get("items", []):
            name = str(it.get("company", "")).strip()
            if name and all(str(it.get(k, "")).strip() for k, _, _ in PERSPECTIVES):
                res[name] = {k: clean_title(str(it[k]))[:80] for k, _, _ in PERSPECTIVES}
        return res
    except Exception as e:  # noqa
        print("⚠️ 관점 응답 해석 실패:", e)
        return {}


# ---------------------------------------------------------------- 실행
TONE = {"up": "🔸", "down": "🔹", "flat": "▪️"}
NEG = ("파업", "유상증자", "상장폐지", "거래정지", "횡령", "배임", "압수수색", "리콜", "적자전환", "어닝 쇼크", "하한가")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    sc = cfg.get("stock_news", {})
    if not sc.get("enabled", True):
        return print("종목 뉴스 기능 꺼짐")
    h = now().hour
    if not args.force:
        if not (sc.get("start_hour", 7) <= h <= sc.get("end_hour", 20)):
            return print(f"발송 시간대 아님 ({h}시)")
        if sc.get("weekdays_only", False) and not is_market_day(cfg):
            return print("휴장일 → 건너뜀")

    state = load_state("stock_news")
    sent_links = set(state.get("links", []))
    recent = state.get("recent", [])            # 최근 보낸 [회사, 제목]
    names = build_matcher(load_listed(), sc.get("exclude_names", []))
    if not names:
        return print("❌ 상장사 목록이 없어 실행 중단")

    min_score = int(sc.get("min_score", 4))
    cands, used = [], set()
    media = [m for m in sc.get("media", []) if (m.get("name") or m.get("domain"))]
    if media:
        print("[매체] " + ", ".join(m.get("name") or m.get("domain") for m in media))
    for it in sorted(collect(sc.get("keywords") or DEFAULT_KEYWORDS, int(sc.get("lookback_hours", 2)), media),
                     key=lambda x: -x["ts"]):
        t = it["title"]
        if it["link"] in sent_links or EXCLUDE.search(it["raw"]) or len(t) < 10:
            continue
        comp = find_company(t, names)
        if not comp:
            continue
        s, tag = score_of(it["raw"])
        if s < min_score:
            continue
        if any(c == comp and same_event(comp, t, rt) for c, rt in recent) or \
                any(c["company"] == comp and same_event(comp, t, c["title"]) for c in cands):
            continue                              # 이미 보냈거나 같은 사건의 다른 기사
        it.update(company=comp, score=s, tag=tag)
        cands.append(it)
    cands.sort(key=lambda x: (-x["score"], -x["ts"]))
    cands = cands[:20]
    print(f"[후보] {len(cands)}건:", [f"{c['company']}({c['score']})" for c in cands[:10]])
    if not cands:
        state["last_run"] = now().strftime("%Y-%m-%d %H:%M")
        save_state("stock_news", state)
        return print("이번 시간 보낼 중요 뉴스 없음")

    n = max(1, min(5, int(sc.get("max_per_hour", 3))))
    picked = ai_pick(cands, n, cfg)
    if picked is None:                            # AI 실패 → 점수순, 회사당 1건
        picked, seen_c = [], set()
        for c in cands:
            if c["company"] in seen_c:
                continue
            seen_c.add(c["company"])
            c["headline"] = c["title"]
            c["tone"] = "down" if any(w in c["raw"] for w in NEG) else "up" if c["score"] >= 5 else "flat"
            picked.append(c)
            if len(picked) >= n:
                break
    if not picked:
        state["last_run"] = now().strftime("%Y-%m-%d %H:%M")
        save_state("stock_news", state)
        return print("AI 판단: 이번 시간 중요 뉴스 없음")

    if sc.get("show_link", True):
        resolve_links(picked)

    companies = load_listed()
    today = now().strftime("%Y-%m-%d")
    analyzed = state.get("analyzed", {})
    analyzed = {today: analyzed.get(today, [])}          # 오늘 기록만 유지
    views = {}
    if sc.get("perspectives", True):
        targets, seen_c = [], set(analyzed[today])
        for c in picked:                                    # 같은 종목은 당일 최초 1회만
            if c["company"] not in seen_c:
                seen_c.add(c["company"])
                targets.append(c)
        if targets:
            views = make_perspectives(targets, companies, cfg)
        else:
            print("[관점] 오늘 이미 분석한 종목뿐 → 생략")

    blocks = [f"📢 <b>종목 뉴스</b> | {now():%m/%d %H}:00"]
    shown = set()
    for c in picked:
        code, mkt = companies.get(c["company"], ["", ""])
        tag = f" · {c['tag']}" if c.get("tag") else ""
        head = esc(c["headline"])
        if c.get("source"):
            head += f" <i>{esc(c['source'])}</i>"
        line = (f"{TONE[c['tone']]} <b>{esc(c['company'])}</b> <code>{code}</code> {esc(mkt)}{esc(tag)}\n"
                f"{head}")
        if sc.get("show_link", True) and c.get("link"):
            # 주소를 그대로 보여줘야 텔레그램이 '이 링크를 열까요?'를 묻지 않고 바로 열어 줌
            line += f"\n🔗 {esc(c['link'])}"
        v = views.get(c["company"])
        if v and c["company"] not in shown:
            shown.add(c["company"])
            body = "\n".join(f"{ic} <b>{esc(label)}</b>: {esc(v[k])}" for k, ic, label in PERSPECTIVES)
            style = sc.get("perspective_style", "plain")
            if style == "spoiler":     # 가림 처리 (텔레그램 특성상 다시 들어오면 또 가려짐)
                line += f"\n\n🧠 <b>투자 대가 관점으로 보면?</b> (눌러서 보기)\n<tg-spoiler>{body}</tg-spoiler>"
            elif style == "expand":    # 접힌 인용: 가림 없이 앞부분이 보이고, 누르면 전체 펼침
                line += f"\n\n🧠 <b>투자 대가 관점으로 보면?</b>\n<blockquote expandable>{body}</blockquote>"
            else:                      # 항상 보이기 (기본)
                line += f"\n\n🧠 <b>투자 대가 관점으로 보면?</b>\n<blockquote>{body}</blockquote>"
        blocks.append(line)
    if shown:
        blocks.append("<i>🧠 관점은 AI가 투자 철학을 바탕으로 만든 참고용 해석이며, 실제 인물의 의견이 아닙니다.</i>")
    if cfg.get("footer"):
        blocks.append(f"<i>{esc(cfg['footer'])}</i>")
    send_text("\n\n".join(blocks), cfg)

    for c in picked:
        sent_links.add(c.get("orig_link", c["link"]))
        recent.append([c["company"], c["title"]])
        add_history(state, f"[종목뉴스] {c['company']} - {c['headline']}", c["link"])
    analyzed[today] = sorted(set(analyzed[today]) | shown)
    state["analyzed"] = analyzed
    state["links"] = list(sent_links)[-400:]
    state["recent"] = recent[-200:]
    state["last_run"] = now().strftime("%Y-%m-%d %H:%M")
    save_state("stock_news", state)


if __name__ == "__main__":
    main()
