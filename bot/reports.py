"""① 오늘 발행된 종목(기업) 리포트를 모아 요약 → 텔레그램 채널 업로드
출처: 네이버페이 증권 > 리서치 > 국내종목 (stock.naver.com/research/company)
실행: python -m bot.reports            (평소)
      python -m bot.reports --force    (주말·휴장일 무시)
      DRY_RUN=1 python -m bot.reports  (텔레그램에 보내지 않고 화면에만 출력)
"""
import argparse
import html
import json
import re
import time

import requests

from bot.common import (UA, add_history, esc, gemini, is_market_day, load_config,
                        load_state, make_card, now, save_state, send_photo, send_text)

# 네이버페이 증권 리서치 페이지가 내부적으로 쓰는 데이터 주소
API = "https://stock.naver.com/api/stockSecurity/researches/v2/company"
HEADERS = {**UA, "Accept": "application/json", "Accept-Language": "ko-KR,ko;q=0.9",
           "Referer": "https://stock.naver.com/research/company"}
# 텔레그램 '원문 보기' 링크 형식 (config.json의 reports.link_template 로 바꿀 수 있음)
DEFAULT_LINK = "https://finance.naver.com/research/company_read.naver?nid={nid}"


def clean_html(s):
    s = re.sub(r"<br\s*/?>|</p>", "\n", s or "")
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s).strip()


def fetch_page(index, size):
    r = requests.get(API, params={"index": index, "size": size}, headers=HEADERS, timeout=20)
    print(f"[접속] index={index} → HTTP {r.status_code}, {len(r.content):,} bytes")
    r.raise_for_status()
    return r.json().get("items", [])


def to_item(d, link_tpl):
    target = d.get("goalPrice")
    opinion = d.get("opinionText") or ""
    return {
        "nid": str(d.get("nid")),
        "title": clean_html(d.get("title", "")),
        "stock": d.get("itemName", ""),
        "code": d.get("itemCode", ""),
        "broker": d.get("brokerName", ""),
        "date": (d.get("writeDate") or "")[:10],
        "target": f"{int(target):,}" if str(target or "").isdigit() else "",
        "opinion": "" if opinion in ("없음", "None") else opinion,
        "body": clean_html(d.get("content", ""))[:3500],
        "url": link_tpl.format(nid=d.get("nid"), code=d.get("itemCode", "")),
    }


def fetch_today_list(cfg, size=30, max_pages=6):
    """오늘 날짜 리포트를 최신순으로 모음 (어제 리포트가 나오면 중단)"""
    today = now().strftime("%Y-%m-%d")
    link_tpl = cfg["reports"].get("link_template") or DEFAULT_LINK
    items, seen = [], set()
    index, step = 0, 1          # index가 '페이지 번호'인지 '시작 위치'인지 자동 판별
    for page in range(max_pages):
        rows = fetch_page(index, size)
        new = [d for d in rows if str(d.get("nid")) not in seen]
        if page == 1 and len(new) < len(rows) / 2:   # 대부분 중복 → index는 '시작 위치'
            step = size
            index = size
            continue
        older = False
        for d in new:
            seen.add(str(d.get("nid")))
            it = to_item(d, link_tpl)
            if it["date"] == today:
                items.append(it)
            else:
                older = True
        if older or not rows:
            break
        index += step
        time.sleep(0.5)
    if items:
        print("  예시:", json.dumps({k: v for k, v in items[0].items() if k != "body"}, ensure_ascii=False))
    print(f"[결과] 오늘({today}) 리포트 {len(items)}건")
    return items


def summarize(items, cfg):
    """8건씩 묶어서 Gemini 1회 호출 (무료 한도 절약)"""
    for i in range(0, len(items), 8):
        batch = items[i:i + 8]
        payload = [{"nid": x["nid"], "종목": x["stock"], "제목": x["title"], "본문": x["body"]}
                   for x in batch]
        prompt = (
            "너는 증권 리포트 요약 담당자야. 아래 각 리포트를 핵심만 3줄로 요약해.\n"
            "규칙: 본문에 있는 사실·수치만 사용, 추측·매수권유 금지, 각 줄 45자 이내, 한국어.\n"
            '출력은 JSON 배열만: [{"nid": "...", "lines": ["...", "...", "..."]}]\n\n'
            + json.dumps(payload, ensure_ascii=False)
        )
        out = gemini(prompt, cfg, want_json=True)
        result = {}
        try:
            result = {str(r["nid"]): r["lines"] for r in json.loads(out)}
        except Exception:  # noqa  AI 실패 시 본문 앞부분으로 대체
            pass
        for x in batch:
            lines = result.get(x["nid"])
            if not lines:
                sents = re.split(r"(?<=[다요\.])\s+", x["body"].replace("\n", " "))
                lines = [s[:60] for s in sents if len(s) > 10][:3]
            x["lines"] = lines
        time.sleep(5)
    return items


def format_item(x):
    meta = " / ".join(filter(None, [
        f"투자의견 {x['opinion']}" if x.get("opinion") else "",
        f"목표가 {x['target']}원" if x.get("target") else ""]))
    lines = "\n".join(f"• {esc(l)}" for l in x["lines"])
    return (f"📈 <b>{esc(x['stock'])}</b> | {esc(x['broker'])}\n"
            f"<b>{esc(x['title'])}</b>\n"
            + (f"{esc(meta)}\n" if meta else "")
            + f"{lines}\n<a href=\"{x['url']}\">원문 보기</a>")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    rc = cfg["reports"]
    if not rc.get("enabled", True):
        return print("리포트 기능 꺼짐")
    if not args.force and not is_market_day(cfg):
        return print("휴장일 → 건너뜀")

    state = load_state("reports")
    sent = set(state.get("sent", []))

    items = [x for x in fetch_today_list(cfg) if x["nid"] not in sent]
    if rc.get("brokers_exclude"):
        items = [x for x in items if x["broker"] not in rc["brokers_exclude"]]
    if rc.get("stocks_watch"):  # 관심종목만 보고 싶을 때
        items = [x for x in items if x["stock"] in rc["stocks_watch"]]
    items = items[: rc.get("max_reports", 20)]
    print(f"새 리포트 {len(items)}건")
    if not items:
        if args.force:  # 수동 실행일 때는 관리자 화면 '발송 이력'에 결과를 남김
            add_history(state, "[리포트] 보낼 새 리포트 없음 (Actions 로그 확인)")
        state["last_run"] = now().strftime("%Y-%m-%d %H:%M")
        return save_state("reports", state)

    summarize(items, cfg)

    footer = cfg.get("footer", "")
    if rc.get("format") == "image":
        for x in items:
            png = make_card(f"{x['broker']} · {now():%m/%d} 리포트",
                            f"{x['stock']} | {x['title']}", x["lines"],
                            " · ".join(filter(None, [
                                f"투자의견 {x['opinion']}" if x.get("opinion") else "",
                                f"목표가 {x['target']}원" if x.get("target") else "",
                                "출처: 네이버 금융 리서치"])))
            cap = f"📈 <b>{esc(x['stock'])}</b> | {esc(x['title'])}\n<a href=\"{x['url']}\">원문 보기</a>"
            send_photo(png, cap, cfg)
            time.sleep(1.5)
    else:
        header = rc.get("header", "☀️ {date} 오늘의 종목 리포트").format(date=now().strftime("%m월 %d일"))
        blocks = [f"<b>{esc(header)}</b> ({len(items)}건)"] + [format_item(x) for x in items]
        if footer:
            blocks.append(f"<i>{esc(footer)}</i>")
        send_text("\n\n".join(blocks), cfg)

    for x in items:
        sent.add(x["nid"])
        add_history(state, f"[리포트] {x['stock']} - {x['title']}", x["url"])
    state["sent"] = sorted(sent)[-1000:]
    state["last_run"] = now().strftime("%Y-%m-%d %H:%M")
    save_state("reports", state)


if __name__ == "__main__":
    main()
