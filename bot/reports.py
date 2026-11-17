"""① 오늘 발행된 종목(기업) 리포트를 모아 요약 → 텔레그램 채널 업로드
출처: 네이버 금융 > 리서치 > 종목분석 리포트
실행: python -m bot.reports            (평소)
      python -m bot.reports --force    (주말·휴장일 무시)
      DRY_RUN=1 python -m bot.reports  (텔레그램에 보내지 않고 화면에만 출력)
"""
import argparse
import io
import json
import re
import time

import requests
from bs4 import BeautifulSoup

from bot.common import (UA, add_history, esc, gemini, is_market_day, load_config,
                        load_state, make_card, now, save_state, send_photo, send_text)

BASE = "https://finance.naver.com/research/"


def get_html(url, **params):
    r = requests.get(url, params=params, headers={
        **UA, "Referer": "https://finance.naver.com/research/",
        "Accept-Language": "ko-KR,ko;q=0.9"}, timeout=20)
    print(f"[접속] {r.url} → HTTP {r.status_code}, {len(r.content):,} bytes")
    r.encoding = "euc-kr"
    return BeautifulSoup(r.text, "html.parser")


DATE_RE = re.compile(r"\b(\d{2}\.\d{2}\.\d{2})\b")


def fetch_today_list(max_pages=5):
    """오늘 날짜(YY.MM.DD) 리포트 목록.
    표의 class 이름이 바뀌어도 동작하도록 'company_read.naver?nid=' 링크가 있는 줄을 모두 찾음"""
    today = now().strftime("%y.%m.%d")
    items, seen = [], set()
    for page in range(1, max_pages + 1):
        soup = get_html(BASE + "company_list.naver", page=page)
        rows = [tr for tr in soup.find_all("tr") if tr.find("a", href=re.compile(r"company_read\.naver\?nid="))]
        print(f"[페이지 {page}] 리포트 줄 {len(rows)}개 발견")
        if not rows:
            title = soup.title.get_text(strip=True) if soup.title else "(제목 없음)"
            print("  ⚠️ 리포트 표를 찾지 못함. 페이지 제목:", title)
            print("  페이지 앞부분:", soup.get_text(" ", strip=True)[:300])
            break
        older_found = False
        for tr in rows:
            link = tr.find("a", href=re.compile(r"company_read\.naver\?nid="))
            nid = re.search(r"nid=(\d+)", link["href"]).group(1)
            if nid in seen:
                continue
            seen.add(nid)
            tds = tr.find_all("td")
            m = DATE_RE.search(tr.get_text(" ", strip=True))
            date = m.group(1) if m else ""
            if page == 1 and len(items) == 0 and len(seen) <= 3:
                print(f"  예시: {[td.get_text(strip=True)[:20] for td in tds]}")
            if date != today:
                older_found = True
                continue
            stock_a = tr.find("a", href=re.compile(r"code=\d+")) or (tds[0] if tds else None)
            pdf = tr.find("a", href=re.compile(r"\.pdf", re.I))
            texts = [td.get_text(strip=True) for td in tds]
            broker = next((t for t in texts if t.endswith(("증권", "투자증권", "증권(주)", "리서치", "자산운용"))
                           or "증권" in t), texts[2] if len(texts) > 2 else "")
            items.append({
                "nid": nid,
                "stock": stock_a.get_text(strip=True) if stock_a else "",
                "title": link.get_text(strip=True),
                "broker": broker,
                "pdf": pdf["href"] if pdf else "",
                "url": f"{BASE}company_read.naver?nid={nid}",
            })
        if older_found:  # 어제 리포트가 나오기 시작하면 더 볼 필요 없음
            break
        time.sleep(0.5)
    print(f"[결과] 오늘({today}) 리포트 {len(items)}건")
    return items


def fetch_detail(item):
    """상세 페이지 본문 + 투자의견/목표가. 본문이 짧으면 PDF 앞부분을 읽음"""
    soup = get_html(BASE + "company_read.naver", nid=item["nid"], page=1)
    body = soup.select_one("td.view_cnt")
    text = body.get_text("\n", strip=True) if body else ""
    page_text = soup.get_text(" ", strip=True)
    tp = re.search(r"목표가\s*([\d,]+)", page_text)
    op = re.search(r"투자의견\s*([A-Za-z가-힣]+)", page_text)
    item["target"] = tp.group(1) if tp else ""
    item["opinion"] = op.group(1) if op else ""
    if len(text) < 200 and item.get("pdf"):
        try:
            from pypdf import PdfReader
            pdf = requests.get(item["pdf"], headers=UA, timeout=30).content
            reader = PdfReader(io.BytesIO(pdf))
            text += "\n" + "\n".join((p.extract_text() or "") for p in reader.pages[:2])
        except Exception as e:  # noqa
            print("PDF 읽기 실패:", e)
    item["body"] = text[:3500]
    return item


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

    items = [x for x in fetch_today_list() if x["nid"] not in sent]
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

    for x in items:
        fetch_detail(x)
        time.sleep(0.5)
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
