"""⑧ 이벤트 속보 — 금통위·FOMC처럼 발표 시각이 정해진 이벤트 시간대에만 1분마다 감시,
'[속보]·1보' 기사가 뜨면 즉시 텍스트로 발송 (이벤트당 1회)
- 감시 매체: 속보용으로 따로 지정 (RSS 주소가 있으면 RSS, 없으면 구글 뉴스 'site:도메인' 검색)
- 이벤트 시간대가 아니면 아무것도 하지 않고 바로 종료
실행: python -m bot.breaking            (평소)
      python -m bot.breaking --check    (지금 감시할 이벤트가 있는지만 출력: yes/no)
      python -m bot.breaking --force    (시간대 무시, 오늘 첫 번째 이벤트 기준 테스트)
"""
import argparse
import calendar
import datetime as dt
import math
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import feedparser
import requests

from bot.common import KST, UA, add_history, esc, load_config, load_state, now, save_state, send_text
from bot.hot_issue import clean_title

FLASH = re.compile(r"\[\s*(속보|1보|긴급)\s*\]|^\s*(속보|1보)\b|\(\s*(속보|1보)\s*\)")
DEFAULT_SOURCES = [{"name": "연합뉴스", "domain": "yna.co.kr", "rss": ""},
                   {"name": "뉴시스", "domain": "newsis.com", "rss": ""},
                   {"name": "뉴스1", "domain": "news1.kr", "rss": ""}]


def _hm(s):
    h, m = map(int, str(s).strip().split(":"))
    return h, m


def active_events(bc, t, force=False):
    """오늘 날짜이면서 지금이 감시 시간대인 이벤트 (자정 넘는 시간대도 지원)"""
    out = []
    for ev in bc.get("events", []):
        try:
            day = dt.date.fromisoformat(str(ev.get("date", "")).strip())
            sh, sm = _hm(ev.get("start", "00:00"))
            eh, em = _hm(ev.get("end", "23:59"))
        except Exception:  # noqa
            continue
        start = dt.datetime(day.year, day.month, day.day, sh, sm, tzinfo=KST)
        end = dt.datetime(day.year, day.month, day.day, eh, em, 59, tzinfo=KST)
        if end < start:
            end += dt.timedelta(days=1)
        if (force and day == t.date()) or start <= t <= end:
            out.append({**ev, "_start": start, "_end": end})
    return out[:1] if force else out


def fetch(src, keywords, hours=1):
    """매체 하나에서 최근 기사 가져오기 (hours: 검색할 최근 시간 범위)"""
    urls = []
    if (src.get("rss") or "").strip():
        urls.append(src["rss"].strip())
    if (src.get("domain") or "").strip():
        q = f"site:{src['domain'].strip()} (" + " OR ".join(keywords) + ")"
        urls.append(f"https://news.google.com/rss/search?q={quote(q)}+when:{hours}h&hl=ko&gl=KR&ceid=KR:ko")
    items = []
    for u in urls:
        try:
            feed = feedparser.parse(requests.get(u, headers=UA, timeout=8).content)
        except Exception as e:  # noqa
            print(f"  ⚠️ {src.get('name')} 실패: {str(e)[:60]}")
            continue
        for e in feed.entries[:40]:
            pub = e.get("published_parsed") or e.get("updated_parsed")
            ts = dt.datetime.fromtimestamp(calendar.timegm(pub), KST) if pub else None
            items.append({"raw": e.get("title", ""), "link": e.get("link", ""), "ts": ts,
                          "source": src.get("name", "")})
    return items


def find_flash(ev, sources, require_flash):
    kws = [k.strip() for k in ev.get("keywords", []) if k.strip()]
    norm = lambda s: re.sub(r"\s+", "", s or "").lower()
    since = ev["_start"] - dt.timedelta(minutes=5)
    hours = max(1, math.ceil((now() - since).total_seconds() / 3600))   # 이벤트 시작부터 지금까지
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda s: fetch(s, kws, hours), sources))
    hits = []
    for items in results:
        for it in items:
            raw = re.sub(r"\s+-\s+[^-]{1,25}$", "", it["raw"])
            if not any(norm(k) in norm(raw) for k in kws):
                continue
            if require_flash and not FLASH.search(raw):
                continue
            if it["ts"] and it["ts"] < since:     # 이벤트 전 기사(전망 기사 등) 제외
                continue
            hits.append({**it, "title": raw})
    hits.sort(key=lambda x: x["ts"] or now())
    print(f"  [{ev.get('name')}] 매체 {len(sources)}곳 확인 → 해당 속보 {len(hits)}건")
    return hits[0] if hits else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    bc = cfg.get("breaking", {})
    t = now()
    evs = active_events(bc, t, args.force) if bc.get("enabled", True) else []
    state = load_state("breaking")
    done = set(state.get("done", []))
    evs = [e for e in evs if args.force or f"{e['date']}|{e.get('name')}" not in done]
    if args.check:
        print("yes" if evs else "no")
        return
    if not evs:
        return print("감시 중인 이벤트 없음")

    sources = [s for s in (bc.get("sources") or DEFAULT_SOURCES) if s.get("domain") or s.get("rss")]
    for ev in evs:
        hit = find_flash(ev, sources, bc.get("require_flash", True))
        if not hit:
            continue
        title = clean_title(hit["title"])
        when = (hit["ts"] or t).strftime("%m/%d %H:%M")
        msg = f"🚨 <b>[속보] {esc(title)}</b>\n{when} · {esc(ev.get('name', ''))}"
        send_text(msg, cfg, preview=False)
        print("✅ 속보 발송:", title)
        done.add(f"{ev['date']}|{ev.get('name')}")
        add_history(state, f"[속보] {title}", hit["link"])
        state["done"] = sorted(done)[-100:]
        state["last_run"] = t.strftime("%Y-%m-%d %H:%M")
        save_state("breaking", state)


if __name__ == "__main__":
    main()
