"""⑥ 주간 경제일정 — 매주 월요일 아침, SBS Biz '[주간경제일정]' 기사를 찾아 요일별로 정리한 이미지로 업로드
- 기사 찾기: 구글 뉴스 RSS ('주간경제일정' + SBS Biz, 최근 1일) → 실패 시 설정의 기사 주소
- 본문 추출: 페이지 HTML (본문 영역 → 구조화 데이터 → 메타 설명) 순서로 시도
- 정리: Gemini가 방송 원고체 본문을 '요일별 일정 목록'으로 변환 (기사에 있는 내용만)
- 같은 기사는 한 번만 발송 (월요일 두 번 실행해도 중복 없음)
실행: python -m bot.weekly_calendar                (평소: 월요일만)
      python -m bot.weekly_calendar --force        (요일·중복 무시)
      python -m bot.weekly_calendar --url 기사주소  (특정 기사로 테스트)
      DRY_RUN=1 python -m bot.weekly_calendar --force --save week.png
"""
import argparse
import datetime as dt
import html as htmlmod
import io
import json
import re
from urllib.parse import quote

import feedparser
import requests
from bs4 import BeautifulSoup

from bot.common import UA, add_history, esc, gemini, load_config, load_state, now, save_state, send_photo
from bot.hot_issue import background, font, gold_text

W = 1080


# ---------------------------------------------------------------- 1) 기사 찾기
def find_article(wc):
    q = wc.get("search", "주간경제일정 SBS Biz")
    url = f"https://news.google.com/rss/search?q={quote(q)}+when:2d&hl=ko&gl=KR&ceid=KR:ko"
    try:
        feed = feedparser.parse(requests.get(url, headers=UA, timeout=15).content)
        for e in feed.entries:
            title = e.get("title", "")
            src = (e.get("source") or {}).get("href", "")
            if "주간경제일정" in title.replace(" ", "") and ("sbs" in src.lower() or "SBS" in title):
                print("[기사] 구글 뉴스에서 찾음:", title)
                return e.get("link"), title
    except Exception as e:  # noqa
        print("구글 뉴스 검색 실패:", e)
    print("⚠️ 이번 주 기사를 찾지 못함")
    return None, None


def resolve_url(url):
    """구글 뉴스 중간 페이지 주소 → 실제 기사 주소 (SBS Biz)"""
    if "news.google.com" not in url:
        return url
    try:  # 1) 중간 페이지 안에 원래 주소가 들어 있는 경우
        t = requests.get(url, headers=UA, timeout=20).text
        m = re.search(r"https?://biz\.sbs\.co\.kr/article/\d+", t) or re.search(r'data-n-au="([^"]+)"', t)
        if m:
            real = m.group(1) if m.re.groups else m.group(0)
            print("[주소 변환] 중간 페이지에서 찾음:", real)
            return real
    except Exception as e:  # noqa
        print("중간 페이지 읽기 실패:", e)
    try:  # 2) 구글 뉴스 주소 해독 라이브러리
        from googlenewsdecoder import gnewsdecoder
        res = gnewsdecoder(url, interval=1)
        if res.get("success") or res.get("status"):
            print("[주소 변환] 해독 성공:", res["decoded_url"])
            return res["decoded_url"]
        print("⚠️ 주소 해독 실패:", res.get("message"))
    except Exception as e:  # noqa
        print("⚠️ 주소 해독 라이브러리 오류:", e)
    return url


def feed_api_body(article_url):
    """SBS 기사 데이터 주소에서 본문 받기 (페이지에 본문이 바로 없을 때)"""
    m = re.search(r"/article/(\d+)", article_url)
    if not m:
        return ""
    try:
        r = requests.get(f"https://feedapi.cloud.sbs.co.kr/feed-api/article_hub/{m.group(1)}",
                         headers={**UA, "Referer": article_url}, timeout=20)
        print(f"[데이터 주소] HTTP {r.status_code}, {len(r.content):,} bytes")
        texts = []

        def walk(o):
            if isinstance(o, dict):
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
            elif isinstance(o, str) and len(o) > 200:
                texts.append(o)
        walk(r.json())
        best = max(texts, key=lambda t: ("◇" in t, len(t)), default="")
        return BeautifulSoup(best.replace("<br", "\n<br"), "html.parser").get_text("\n", strip=True)
    except Exception as e:  # noqa
        print("⚠️ 데이터 주소 실패:", e)
        return ""


# ---------------------------------------------------------------- 2) 본문 추출
def fetch_body(url):
    url = resolve_url(url)
    m = re.search(r"biz\.sbs\.co\.kr/(?:amp/)?article/(\d+)", url)
    if m:   # SBS Biz 일반 페이지는 본문을 나중에 불러오므로, 본문이 들어 있는 AMP 페이지를 먼저 읽음
        url = f"https://biz.sbs.co.kr/amp/article/{m.group(1)}"
    r = requests.get(url, headers={**UA, "Accept-Language": "ko-KR"}, timeout=20, allow_redirects=True)
    print(f"[접속] {r.url} → HTTP {r.status_code}, {len(r.content):,} bytes")
    r.encoding = r.apparent_encoding or "utf-8"
    soup = BeautifulSoup(r.text, "html.parser")
    title = (soup.find("meta", property="og:title") or {}).get("content") or (soup.title.string if soup.title else "")

    # (1) 본문 영역: '◇ ○월 ○일' 이 들어 있는 가장 짧은 큰 덩어리
    best = ""
    for el in soup.find_all(["div", "article", "section"]):
        t = el.get_text("\n", strip=True)
        if "◇" in t and re.search(r"\d+월\s*\d+일", t) and len(t) > 200:
            if not best or len(t) < len(best):
                best = t
    # (2) 구조화 데이터(JSON-LD)의 articleBody
    if not best:
        for sc in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(sc.string or "")
                for d in data if isinstance(data, list) else [data]:
                    if isinstance(d, dict) and len(d.get("articleBody", "")) > 200:
                        best = d["articleBody"]
            except Exception:  # noqa
                pass
    # (3) 스크립트 안에 들어 있는 본문 문자열
    if not best:
        for m in re.finditer(r'"(?:content|body|articleBody|text)"\s*:\s*"((?:[^"\\]|\\.){300,})"', r.text):
            s = json.loads(f'"{m.group(1)}"')
            if "◇" in s:
                best = BeautifulSoup(s, "html.parser").get_text("\n", strip=True)
                break
    # (4) SBS 기사 데이터 주소
    if not best or "◇" not in best:
        alt = feed_api_body(r.url.replace("/amp/", "/"))
        if len(alt) > len(best):
            best = alt
    if not best or "◇" not in best:   # 원인 파악용: 본문을 불러오는 주소 후보
        aid = (re.search(r"/article/(\d+)", r.url) or [None, ""])[1]
        cands = sorted(set(re.findall(r"""["'(]((?:https?:)?//[^"'()\s]*(?:api|feed|article|content)[^"'()\s]*)""", r.text)))
        print("  🔎 본문 후보 주소:", [c for c in cands if aid in c or "api" in c][:12])
        print("  🔎 script 파일:", [sc.get("src") for sc in soup.find_all("script", src=True)][:12])
        print("  🔎 '◇' 포함 여부:", "◇" in r.text, "/ 첫 날짜 위치 문맥:",
              (lambda i: r.text[max(0, i - 80):i + 120].replace("\n", " ") if i >= 0 else "없음")(r.text.find("◇")))
    # (5) 최후: 메타 설명 (앞부분만 있음)
    if not best:
        best = (soup.find("meta", property="og:description") or {}).get("content", "")
        print("⚠️ 본문을 찾지 못해 메타 설명만 사용 (일부 요일만 나올 수 있음)")
    best = htmlmod.unescape(best)
    print(f"[본문] {len(best)}자")
    return (title or "").replace(" - SBS Biz", "").strip(), best


# ---------------------------------------------------------------- 2-1) 보충 자료 (우리 방식으로 추가 수집)
EXTRA_QUERIES = ["이번주 증시 일정", "주간 증시 캘린더", "이번 주 주요 경제지표 발표", "이번주 주요 일정 증시"]


def week_range(t):
    mon = t.date() - dt.timedelta(days=t.weekday())
    return mon, mon + dt.timedelta(days=6)


def extra_material(wc, cfg):
    """SBS Biz 외 다른 언론사의 '이번 주 일정' 기사 2~3건 본문 + 관리자 화면에 등록한 이벤트(금통위·FOMC 등)"""
    from bot.deep_brief import article
    t = now()
    mon, sun = week_range(t)
    texts, seen = [], set()
    for q in wc.get("extra_queries") or EXTRA_QUERIES:
        url = f"https://news.google.com/rss/search?q={quote(q)}+when:3d&hl=ko&gl=KR&ceid=KR:ko"
        try:
            feed = feedparser.parse(requests.get(url, headers=UA, timeout=15).content)
        except Exception as e:  # noqa
            print("보충 자료 검색 실패:", q, e)
            continue
        for e in feed.entries[:8]:
            title = e.get("title", "")
            src = (e.get("source") or {}).get("title", "")
            if "sbs" in src.lower() or "SBS" in title or src in seen:
                continue
            if not re.search(r"일정|캘린더|이번\s?주|주간", title):
                continue
            link, body = article(e.get("link", ""))
            if len(body) < 300:
                continue
            seen.add(src)
            texts.append(f"<보충 {len(texts) + 1}> {title}\n{body[:3500]}")
            print("[보충 자료]", title[:50])
            break
        if len(texts) >= int(wc.get("extra_max", 3)):
            break
    evs = []
    for ev in (cfg.get("breaking", {}).get("events") or []):
        try:
            d = dt.date.fromisoformat(str(ev.get("date")))
        except Exception:  # noqa
            continue
        if mon <= d <= sun:
            evs.append(f"{d.month}월 {d.day}일 {ev.get('start', '')} {ev.get('name', '')}")
    if evs:
        texts.append("<등록 이벤트>\n" + "\n".join(evs))
    print(f"[보충 자료] 기사 {len([x for x in texts if x.startswith('<보충')])}건, 등록 이벤트 {len(evs)}건")
    return "\n\n".join(texts)


# ---------------------------------------------------------------- 3) 요일별 정리 (AI)
def structure(body, cfg, extra=""):
    mon, sun = week_range(now())
    prompt = (
        "아래 '기본 원고'(경제방송 주간경제일정)와 '보충 자료'(다른 언론 일정 기사·등록 이벤트)를 합쳐 "
        "이번 주 요일별 경제일정표를 만든다.\n"
        "규칙:\n"
        "- 기본 원고의 일정은 모두 포함. 보충 자료에서는 기본 원고에 없는 일정만 추가 (같은 일정은 하나로)\n"
        f"- 이번 주({mon.month}월 {mon.day}일~{sun.month}월 {sun.day}일) 일정만. 자료에 없는 일정·수치 추가 금지\n"
        "- 하루 최대 9개, 시장 영향이 큰 순서로\n"
        "- 각 항목은 '국가) 일정명' 형식의 짧은 명사구, 25자 이내 (예: '美) 11월 제조업 PMI 예비치', '韓) 11월 1~20일 수출입')\n"
        "- 국가 표기: 한국 韓, 미국 美, 중국 中, 일본 日, 유로존 유로존, 독일 獨, 영국 英. 기업 일정은 국가 생략 가능\n"
        "- 원고에 나온 전망치·이전치 같은 숫자는 note에 짧게 (없으면 빈 문자열)\n"
        "- week_title: 이번 주 핵심 키워드를 '·'로 이은 한 줄 (25자 이내)\n"
        '출력 JSON만: {"week_title": "...", "days": [{"date": "11월 23일", "weekday": "월", '
        '"items": [{"text": "...", "note": "..."}]}]}\n\n'
        "[기본 원고]\n" + body[:12000] + ("\n\n[보충 자료]\n" + extra[:10000] if extra else "")
    )
    out = gemini(prompt, cfg, want_json=True, timeout=120)
    if out:
        try:
            data = json.loads(re.sub(r"^```(json)?|```$", "", out.strip()).strip())
            if data.get("days"):
                return data
        except Exception as e:  # noqa
            print("⚠️ AI 응답 해석 실패:", e)
    # AI 실패 → '◇ 날짜' 기준으로 문장 단위 정리
    print("AI 정리 실패 → 원문 문장으로 정리")
    days = []
    for block in re.split(r"◇", body)[1:]:
        m = re.match(r"\s*(\d+월\s*\d+일)\s*([월화수목금토일])?요?일?", block)
        if not m:
            continue
        sents = [s.strip() for s in re.split(r"(?<=[다요])\.\s*", block[m.end():]) if len(s.strip()) > 8]
        days.append({"date": m.group(1), "weekday": m.group(2) or "",
                     "items": [{"text": s[:45], "note": ""} for s in sents[:8]]})
    return {"week_title": "", "days": days}


# ---------------------------------------------------------------- 4) 이미지
NAVY, INK, SUB, LINE = (14, 32, 78), (24, 30, 44), (110, 120, 140), (226, 231, 240)
DAY_COLOR = {"월": (37, 99, 235), "화": (14, 165, 233), "수": (16, 150, 110), "목": (217, 119, 6),
             "금": (220, 38, 38), "토": (100, 116, 139), "일": (100, 116, 139)}
FLAG = re.compile(r"^(韓|美|中|日|獨|英|佛|유로존|EU|한국|미국|중국|일본|독일|영국)\)\s*")


def wrap(d, text, fnt, max_w):
    lines, cur = [], ""
    for ch in text:
        if d.textlength(cur + ch, font=fnt) > max_w and cur:
            lines.append(cur)
            cur = ch.lstrip()
        else:
            cur += ch
    return lines + ([cur] if cur else [])


def render(data, title_txt, source_txt):
    from PIL import Image, ImageDraw
    f_day, f_item, f_note, f_tag = font(50, "heavy"), font(44, "bold"), font(34, "regular"), font(33, "bold")
    tmp = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    pad, text_x, max_w = 50, 50 + 38, W - 50 - 38 - 44

    # 높이 미리 계산
    blocks, h = [], 0
    for day in data["days"]:
        rows = []
        for it in day.get("items", []):
            t = str(it.get("text", "")).strip()
            if not t:
                continue
            m = FLAG.match(t)
            tag, body = (m.group(1), t[m.end():]) if m else ("", t)
            indent = (tmp.textlength(tag, font=f_tag) + 40) if tag else 0
            lines = wrap(tmp, body, f_item, max_w - indent)
            note = wrap(tmp, str(it.get("note") or ""), f_note, max_w - indent) if it.get("note") else []
            rh = len(lines) * 60 + len(note) * 46 + 26
            rows.append((tag, indent, lines, note, rh))
        bh = 104 + sum(r[4] for r in rows) + 24
        blocks.append((day, rows, bh))
        h += bh + 26
    header_h, footer_h = 400, (90 if source_txt else 30)   # 출처 문구가 없으면 하단 여백 줄임
    H = header_h + h + footer_h

    img = Image.new("RGB", (W, H), (244, 246, 251))
    head = background(now().strftime("%Y%W")).crop((0, 0, W, header_h))
    img.paste(head.convert("RGB"), (0, 0))
    canvas = Image.new("RGBA", (W, 1350), (0, 0, 0, 0))
    canvas.paste(head, (0, 0))
    gold_text(canvas, (W // 2, 165), "주간 경제일정", font(118, "heavy"))
    img.paste(canvas.crop((0, 0, W, header_h)).convert("RGB"), (0, 0))
    d = ImageDraw.Draw(img)
    days = data["days"]
    rng = f"{days[0].get('date', '')}({days[0].get('weekday', '')}) ~ {days[-1].get('date', '')}({days[-1].get('weekday', '')})"
    d.text((W // 2, 272), rng, font=font(40, "bold"), fill=(205, 222, 255), anchor="mm")
    if title_txt:
        d.text((W // 2, 340), title_txt[:28], font=font(36, "bold"), fill=(255, 226, 150), anchor="mm")

    y = header_h + 26
    for day, rows, bh in blocks:
        wd = day.get("weekday", "")
        color = DAY_COLOR.get(wd, NAVY)
        d.rounded_rectangle([pad - 20, y, W - pad + 20, y + bh], 22, fill="white", outline=LINE, width=2)
        d.rounded_rectangle([pad - 20, y, W - pad + 20, y + 84], 22, fill=color)
        d.rectangle([pad - 20, y + 60, W - pad + 20, y + 84], fill=color)
        d.text((pad + 8, y + 42), f"{day.get('date', '')} ({wd})", font=f_day, fill="white", anchor="lm")
        yy = y + 110
        for tag, indent, lines, note, rh in rows:
            d.ellipse([pad + 2, yy + 18, pad + 18, yy + 34], fill=color)
            x = text_x
            if tag:
                tw = d.textlength(tag, font=f_tag)
                d.rounded_rectangle([x, yy + 3, x + tw + 24, yy + 51], 10, fill=(236, 241, 252))
                d.text((x + 12, yy + 27), tag, font=f_tag, fill=NAVY, anchor="lm")
                x += indent
            for ln in lines:
                d.text((x, yy), ln, font=f_item, fill=INK)
                yy += 60
            for ln in note:
                d.text((x, yy), ln, font=f_note, fill=SUB)
                yy += 46
            yy += 26
        y += bh + 26
    if source_txt:
        d.text((W // 2, H - 48), source_txt, font=font(24, "regular"), fill=SUB, anchor="mm")
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue(), H


# ---------------------------------------------------------------- 실행
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--url")
    ap.add_argument("--save")
    args = ap.parse_args()

    cfg = load_config()
    wc = cfg.get("weekly_calendar", {})
    if not wc.get("enabled", True):
        return print("주간 경제일정 기능 꺼짐")
    if not args.force and now().weekday() != 0:
        return print("월요일이 아님 → 건너뜀")

    url, title = (args.url, None) if args.url else find_article(wc)
    if not url:
        return print("기사를 찾지 못해 발송하지 않음 (다음 실행 때 다시 시도)")
    state = load_state("weekly_calendar")
    page_title, body = fetch_body(url)
    title = page_title or title or ""
    key = re.sub(r"\s+", "", title)[:60]
    if not args.force and key and key in state.get("done", []):
        return print("이미 발송한 기사:", title)
    if len(body) < 100:
        return print("❌ 본문이 너무 짧아 발송하지 않음")

    extra = extra_material(wc, cfg) if wc.get("add_extra", True) else ""
    data = structure(body, cfg, extra)
    data["days"] = [dd for dd in data.get("days", []) if dd.get("items")]
    if not data["days"]:
        return print("❌ 일정을 정리하지 못해 발송하지 않음")
    print("[정리]", [(dd["date"], len(dd["items"])) for dd in data["days"]])
    if len(data["days"]) < int(wc.get("min_days", 3)):
        return print(f"❌ 요일이 {len(data['days'])}개뿐 → 본문 일부만 읽은 것으로 보고 발송하지 않음")

    week_title = data.get("week_title") or re.sub(r"^\[[^\]]+\]\s*", "", title)
    source = wc.get("source_text", "")       # 비우면 이미지·설명글에 출처 표기 없음
    png, h = render(data, week_title, source)
    if args.save:
        open(args.save, "wb").write(png)
    first, last = data["days"][0], data["days"][-1]
    cap = (f"📅 <b>주간 경제일정</b> | {esc(first.get('date'))}~{esc(last.get('date'))}\n"
           f"{esc(week_title)}")
    if source:
        cap += f"\n<i>{esc(source)}</i>"
    send_photo(png, cap, cfg)

    state.setdefault("done", []).append(key)
    state["done"] = state["done"][-20:]
    add_history(state, f"[주간일정] {title}", url)
    state["last_run"] = now().strftime("%Y-%m-%d %H:%M")
    save_state("weekly_calendar", state)


if __name__ == "__main__":
    main()
