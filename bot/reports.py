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

BASE = "https://finance.naver.com/research/"   # 예전 주소 (새 주소로 자동 이동됨)
HEADERS = {**UA, "Accept-Language": "ko-KR,ko;q=0.9", "Referer": "https://stock.naver.com/"}


# ---------------------------------------------------------------- 페이지 읽기
def get_page(url, **params):
    """페이지를 받아 (soup, html) 반환. 옛 페이지(EUC-KR)·새 페이지(UTF-8) 모두 자동 판별"""
    r = requests.get(url, params=params, headers=HEADERS, timeout=20)
    print(f"[접속] {r.url} → HTTP {r.status_code}, {len(r.content):,} bytes")
    raw = r.content
    head = raw[:3000].lower()
    if b"euc-kr" in head or "euc-kr" in r.headers.get("content-type", "").lower():
        html = raw.decode("euc-kr", errors="replace")
    else:
        try:
            html = raw.decode("utf-8")
        except UnicodeDecodeError:
            html = raw.decode("euc-kr", errors="replace")
    return BeautifulSoup(html, "html.parser"), html


def embedded_json(soup, html):
    """페이지 안에 숨어 있는 데이터(JSON)를 모두 꺼냄 (Next.js 등 최신 웹페이지 대응)"""
    found, texts = [], []
    for sc in soup.find_all("script"):
        t = sc.string or sc.get_text() or ""
        if sc.get("id") == "__NEXT_DATA__" or sc.get("type") in ("application/json", "application/ld+json"):
            try:
                found.append(json.loads(t))
            except Exception:  # noqa
                pass
        for m in re.finditer(r"self\.__next_f\.push\((\[.*?\])\)\s*;?\s*$", t, re.S):
            try:
                texts += [x for x in json.loads(m.group(1)) if isinstance(x, str)]
            except Exception:  # noqa
                pass
    blob = "".join(texts)
    dec = json.JSONDecoder()
    tries = 0
    for m in re.finditer(r'[\[{]"', blob):
        tries += 1
        if tries > 30000:
            break
        try:
            obj, _ = dec.raw_decode(blob, m.start())
            if isinstance(obj, (dict, list)):
                found.append(obj)
        except Exception:  # noqa
            pass
    return found


def walk_dicts(obj):
    stack = [obj]
    while stack:
        o = stack.pop()
        if isinstance(o, dict):
            yield o
            stack.extend(o.values())
        elif isinstance(o, list):
            stack.extend(o)


DATE_PATTERNS = [
    (re.compile(r"(20\d{2})[-./](\d{1,2})[-./](\d{1,2})"), lambda m: (m[1], m[2], m[3])),
    (re.compile(r"\b(\d{2})\.(\d{2})\.(\d{2})\b"), lambda m: ("20" + m[1], m[2], m[3])),
    (re.compile(r"^(20\d{2})(\d{2})(\d{2})"), lambda m: (m[1], m[2], m[3])),
]


def norm_date(text):
    for rx, f in DATE_PATTERNS:
        m = rx.search(str(text))
        if m:
            y, mo, d = f(m)
            return f"{y}-{int(mo):02d}-{int(d):02d}"
    return ""


def pick(d, keys, exclude=()):
    for k, v in d.items():
        kl = k.lower()
        if isinstance(v, (str, int)) and str(v).strip() and any(s in kl for s in keys) \
                and not any(e in kl for e in exclude):
            return str(v).strip()
    return ""


def report_from_dict(d):
    """JSON 묶음 하나가 '리포트'처럼 생겼으면 표준 형태로 변환"""
    title = pick(d, ["title"], exclude=["sub"])
    if not title:
        return None
    date = ""
    for k, v in d.items():
        if isinstance(v, (str, int)) and any(s in k.lower() for s in ["date", "time", "dt", "at"]):
            date = norm_date(v)
            if date:
                break
    if not date:
        return None
    rid = pick(d, ["nid", "researchid", "reportid"]) or pick(d, ["id"], exclude=["code", "stock", "item"])
    pdf = next((str(v) for v in d.values() if isinstance(v, str) and ".pdf" in v.lower()), "")
    return {
        "nid": rid or title,
        "title": re.sub(r"<[^>]+>", "", title),
        "stock": pick(d, ["stockname", "itemname", "corpname", "companyname", "stknm", "name"],
                      exclude=["broker", "firm", "secur", "writer", "author", "analyst", "title"]),
        "broker": pick(d, ["broker", "firm", "secur", "provider", "source", "org"]),
        "date": date,
        "pdf": pdf,
        "body": re.sub(r"<[^>]+>", " ", pick(d, ["summary", "content", "body", "desc"]))[:3500],
        "url": (f"{BASE}company_read.naver?nid={rid}" if rid.isdigit() else
                (pdf or "https://stock.naver.com/research/company")),
    }


def parse_old_table(soup):
    """예전 네이버 금융 표 형식"""
    out = []
    for tr in soup.find_all("tr"):
        link = tr.find("a", href=re.compile(r"company_read\.naver\?nid="))
        if not link:
            continue
        nid = re.search(r"nid=(\d+)", link["href"]).group(1)
        tds = tr.find_all("td")
        texts = [td.get_text(strip=True) for td in tds]
        pdf = tr.find("a", href=re.compile(r"\.pdf", re.I))
        out.append({
            "nid": nid, "title": link.get_text(strip=True),
            "stock": texts[0] if texts else "",
            "broker": texts[2] if len(texts) > 2 else "",
            "date": norm_date(tr.get_text(" ", strip=True)),
            "pdf": pdf["href"] if pdf else "", "body": "",
            "url": f"{BASE}company_read.naver?nid={nid}",
        })
    return out


def fetch_today_list():
    today = now().strftime("%Y-%m-%d")
    soup, html = get_page(BASE + "company_list.naver")
    title = soup.title.get_text(strip=True) if soup.title else "(제목 없음)"
    print("  페이지 제목:", title)

    reports = parse_old_table(soup)
    how = "예전 표 형식"
    if not reports:
        how = "새 페이지 데이터(JSON)"
        seen = set()
        for obj in embedded_json(soup, html):
            for d in walk_dicts(obj):
                rep = report_from_dict(d)
                if rep and rep["nid"] not in seen:
                    seen.add(rep["nid"])
                    reports.append(rep)
    print(f"[분석] {how}에서 리포트 {len(reports)}건 발견")

    if reports:
        print("  예시:", json.dumps(reports[0], ensure_ascii=False)[:400])
    else:  # 원인 파악용 정보
        links = sorted({a["href"] for a in soup.find_all("a", href=True) if "research" in a["href"]})[:15]
        print("  research 링크 예시:", links)
        print("  script 개수:", len(soup.find_all("script")),
              "/ __NEXT_DATA__:", bool(soup.find("script", id="__NEXT_DATA__")),
              "/ __next_f:", "__next_f" in html)
        apis = sorted(set(re.findall(r"https?://[\w./-]*api[\w./?=&-]*", html)))[:15]
        print("  API 주소 후보:", apis)
        print("  본문 앞부분:", soup.get_text(" ", strip=True)[:300])

    items = [x for x in reports if x["date"] == today]
    print(f"[결과] 오늘({today}) 리포트 {len(items)}건")
    return items


def fetch_detail(item):
    """본문 확보 순서: 목록 데이터 → 상세 페이지 → PDF 앞 2쪽"""
    text = item.get("body", "")
    page_text = ""
    if len(text) < 200 and item["url"].startswith(BASE):
        try:
            soup, html = get_page(item["url"])
            box = soup.select_one("td.view_cnt")
            text = box.get_text("\n", strip=True) if box else ""
            page_text = soup.get_text(" ", strip=True)
            if len(text) < 200:  # 새 페이지: 숨은 데이터 중 가장 긴 글을 본문으로 사용
                longest = ""
                for obj in embedded_json(soup, html):
                    for d in walk_dicts(obj):
                        for v in d.values():
                            if isinstance(v, str) and len(v) > len(longest) and "http" not in v[:10]:
                                longest = v
                text = re.sub(r"<[^>]+>", " ", longest) if len(longest) > len(text) else text
        except Exception as e:  # noqa
            print("상세 페이지 실패:", e)
    tp = re.search(r"목표가\D{0,5}([\d,]{4,})", page_text + " " + text)
    op = re.search(r"투자의견\s*:?\s*([A-Za-z가-힣]+)", page_text + " " + text)
    item["target"] = tp.group(1) if tp else ""
    item["opinion"] = op.group(1) if op else ""
    if len(text) < 200 and item.get("pdf"):
        try:
            from pypdf import PdfReader
            pdf = requests.get(item["pdf"], headers=HEADERS, timeout=30).content
            reader = PdfReader(io.BytesIO(pdf))
            text += "\n" + "\n".join((p.extract_text() or "") for p in reader.pages[:2])
        except Exception as e:  # noqa
            print("PDF 읽기 실패:", e)
    item["body"] = re.sub(r"\s+\n", "\n", text)[:3500]
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
