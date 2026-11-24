"""② 기준 유튜브 채널에 새 영상/라이브가 올라오면 링크를 텔레그램에 공유
- 기본: 유튜브 RSS(무료, 키 불필요)로 새 영상 감지
- YOUTUBE_API_KEY가 있으면 라이브 상태(예정/방송중/종료)까지 구분
실행: python -m bot.youtube_watch
"""
import calendar
import json
import html
import datetime as dt
import os
import re
import time

import feedparser
import requests

from bot.common import UA, add_history, tg_call, esc, load_config, load_state, now, save_state, send_text

RSS = "https://www.youtube.com/feeds/videos.xml?channel_id={}"


def video_status(vid):
    """'upcoming' | 'live' | 'ended'(라이브 종료본) | 'video' | None(API 키 없음)"""
    key = os.getenv("YOUTUBE_API_KEY")
    if not key:
        return None
    r = requests.get("https://www.googleapis.com/youtube/v3/videos", timeout=20, params={
        "part": "snippet,liveStreamingDetails", "id": vid, "key": key})
    items = r.json().get("items", []) if r.ok else []
    if not items:
        return None
    lbc = items[0]["snippet"].get("liveBroadcastContent", "none")
    if lbc in ("upcoming", "live"):
        return lbc
    if items[0].get("liveStreamingDetails", {}).get("actualEndTime"):
        return "ended"
    return "video"


def build_msg(cfg, ch_name, title, link, kind):
    tpl = cfg["youtube"].get("templates", {}).get(kind) or "🎬 {channel} 새 영상\n<b>{title}</b>\n{link}"
    return tpl.format(channel=esc(ch_name), title=esc(title), link=link)


# ---------------------------------------------------------------- 라이브 상단 고정 메시지
def _norm(t):
    return re.sub(r"\s+", "", t or "").lower()


def streams_tab(cid, keyword=""):
    """채널 '라이브' 탭 페이지의 영상 목록: [(영상ID, 제목, 방송중 표시 여부)] — API 사용 안 함
    유튜브의 옛 형식(videoRenderer)과 새 형식(lockupViewModel)을 모두 읽고,
    그래도 못 찾으면 페이지에서 제목 글자 바로 근처의 영상 ID를 찾음"""
    try:
        r = requests.get(f"https://www.youtube.com/channel/{cid}/streams",
                         headers={**UA, "Accept-Language": "ko-KR"}, cookies={"CONSENT": "YES+"}, timeout=20)
        text = r.text
    except Exception as e:  # noqa
        print("  ⚠️ 라이브 탭 확인 실패:", e)
        return []
    m = re.search(r"var ytInitialData\s*=\s*(\{.*?\});\s*</script>", text, re.S) \
        or re.search(r'window\["ytInitialData"\]\s*=\s*(\{.*?\});', text, re.S)
    data = {}
    if m:
        try:
            data = json.loads(m.group(1))
        except Exception:  # noqa
            pass
    out, seen, stack = [], set(), [data]
    while stack:
        o = stack.pop()
        if isinstance(o, dict):
            vid, title = None, ""
            if o.get("videoId") and isinstance(o.get("title"), dict):              # 옛 형식
                t = o["title"]
                vid, title = o["videoId"], t.get("simpleText") or "".join(x.get("text", "") for x in t.get("runs", []))
            elif o.get("contentId") and "VIDEO" in str(o.get("contentType", "")):  # 새 형식
                vid = o["contentId"]
                meta = o.get("metadata", {}).get("lockupMetadataViewModel", {})
                title = (meta.get("title") or {}).get("content", "")
            if vid and vid not in seen and re.fullmatch(r"[\w-]{11}", vid):
                blob = json.dumps(o, ensure_ascii=False)
                is_live = any(x in blob for x in ("LIVE_NOW", '"style": "LIVE"', "BADGE_STYLE_LIVE", '"text": "라이브"',
                                                  '"text": "LIVE"', "OVERLAY_BADGE_STYLE_LIVE"))
                seen.add(vid)
                out.append((vid, title, is_live))
            stack.extend(o.values())
        elif isinstance(o, list):
            stack.extend(o)
    print(f"  [라이브 탭] 페이지 {len(text):,}자, 데이터 {'있음' if data else '없음'}, 영상 {len(out)}개 읽음")
    if keyword and not any(_norm(keyword) in _norm(t) for _, t, _ in out):
        # 비상 방법: 페이지에서 키워드가 나오는 곳 앞쪽 3,000자 안의 마지막 영상 ID
        for km in re.finditer(re.escape(keyword), text):
            ids = re.findall(r'"(?:videoId|contentId)":"([\w-]{11})"', text[max(0, km.start() - 3000):km.start()])
            if ids and ids[-1] not in seen:
                seen.add(ids[-1])
                out.append((ids[-1], keyword, False))
        print(f"  [라이브 탭] 비상 방법으로 '{keyword}' 근처 영상 {sum(1 for _, t, _ in out if t == keyword)}개 찾음")
    return out


def api_videos(ids, key):
    r = requests.get("https://www.googleapis.com/youtube/v3/videos", timeout=20, params={
        "part": "snippet,liveStreamingDetails", "id": ",".join(ids), "key": key})
    if not r.ok:
        print("  ⚠️ 유튜브 API 오류:", r.status_code, r.text[:150])
    return r.json().get("items", []) if r.ok else []


def current_live(cid, pin, keyword):
    """제목에 keyword가 들어간 라이브만 찾음 (예: '부자되는 좋은습관')
    1) 직전에 고정한 라이브가 아직 방송 중이면 그대로 (API 사용량 1)
    2) 아니면 채널 '라이브' 탭에서 제목이 맞는 영상을 찾아 방송 중인지 확인 (API 사용량 1)"""
    key = (os.getenv("YOUTUBE_API_KEY") or "").strip()
    kw = _norm(keyword)

    def live_items(ids):
        items = api_videos(ids, key) if key and ids else []
        return [it for it in items if it["snippet"].get("liveBroadcastContent") == "live"
                and kw in _norm(it["snippet"].get("title"))]

    if key and pin.get("vid"):
        still = live_items([pin["vid"]])
        if still:
            print("  [라이브 확인] 고정 중인 라이브가 계속 방송 중")
            return {"vid": still[0]["id"], "title": still[0]["snippet"]["title"]}

    tab = [(v, t, lv) for v, t, lv in streams_tab(cid, keyword) if kw in _norm(t)]
    print(f"  [라이브 탭] '{keyword}' 제목 영상 {len(tab)}개" + (f", 방송중 표시 {sum(1 for x in tab if x[2])}개" if tab else ""))
    if not tab:
        return None
    if not key:                                     # API 키가 없으면 페이지의 '방송 중' 표시로 판단
        live = next(((v, t) for v, t, lv in tab if lv), None)
        return {"vid": live[0], "title": live[1]} if live else None
    ordered = [v for v, t, lv in tab if lv] + [v for v, t, lv in tab if not lv]
    lives = live_items(ordered[:10])
    if not lives:
        return None
    best = max(lives, key=lambda it: int(it.get("liveStreamingDetails", {}).get("concurrentViewers", 0) or 0))
    return {"vid": best["id"], "title": best["snippet"]["title"]}


def update_live_pin(cfg, state):
    yc = cfg["youtube"]
    lp = yc.get("live_pin", {})
    if not lp.get("enabled", False):
        return
    chans = [c for c in yc.get("channels", []) if re.fullmatch(r"UC[\w-]{22}", c.get("channel_id", "").strip())]
    if not chans:
        return
    ch = next((c for c in chans if c["channel_id"] == lp.get("channel_id")), chans[0])
    pin = state.setdefault("live_pin", {})
    # 24시간 송출 라이브라 자주 확인할 필요 없음: 방송 중이면 N시간마다,
    # 대기 중·미확인 상태는 잘못 판단했을 가능성이 높으므로 매번 다시 확인
    hours = float(lp.get("check_hours", 6))
    unsure = not pin.get("message_id") or pin.get("key", "").startswith("offline") or pin.get("miss")
    last_check = pin.get("last_check")
    if not unsure and last_check and now() - dt.datetime.fromisoformat(last_check) < dt.timedelta(hours=hours):
        gap = dt.timedelta(hours=hours)
        print(f"[라이브 고정] 다음 확인: {(dt.datetime.fromisoformat(last_check) + gap):%H:%M} 이후")
        return
    pin["last_check"] = now().isoformat()
    live = current_live(ch["channel_id"].strip(), pin, lp.get("title_keyword", "부자되는 좋은습관"))
    cur = now()
    if live:
        pin["vid"], pin["miss"] = live["vid"], 0
    else:                                          # 한 번 놓친 것으로 바로 '대기 중'이 되지 않게 2회 연속 확인
        pin["miss"] = pin.get("miss", 0) + 1
        if pin.get("key", "").split("|")[0] != "offline" and pin["miss"] < 2:
            print("[라이브 고정] 라이브가 안 보임 (1회) → 다음 확인 때 다시 판단")
            return
    if live:
        url = f"https://www.youtube.com/watch?v={live['vid']}"
        caption = (f"🔴 <b>지금 LIVE</b> | {esc(ch.get('name', ''))}\n{esc(live['title'])}\n\n"
                   f"▶️ 바로 시청: {url}\n<i>{cur:%m/%d %H:%M} 기준</i>")
        photo = f"https://i.ytimg.com/vi/{live['vid']}/hqdefault_live.jpg?t={int(cur.timestamp())}"
    else:
        url = f"https://www.youtube.com/channel/{ch['channel_id'].strip()}/live"
        caption = (f"⚪ <b>방송 대기 중</b> | {esc(ch.get('name', ''))}\n"
                   f"라이브가 시작되면 이 메시지가 자동으로 바뀝니다.\n\n▶️ 채널 라이브: {url}\n"
                   f"<i>{cur:%m/%d %H:%M} 기준</i>")
        photo = None
    # 링크 버튼: 고정 바 오른쪽에도 표시되어, 누르면 바로 라이브로 이동
    btn_text = lp.get("button_text", "▶️ LIVE 보기") if live else "▶️ 채널 라이브"
    markup = json.dumps({"inline_keyboard": [[{"text": btn_text, "url": url}]]}, ensure_ascii=False)
    key = (live["vid"] + live["title"] if live else "offline") + "|btn2"
    last = pin.get("updated")
    stale = not last or cur - dt.datetime.fromisoformat(last) > dt.timedelta(minutes=int(lp.get("refresh_min", 60)))
    if pin.get("message_id") and pin.get("key") == key and not stale:
        print("[라이브 고정] 변화 없음")
        return
    if not pin.get("message_id") and not live:
        print("[라이브 고정] 아직 고정 메시지 없음 → 첫 라이브 때 만듦")
        return
    print("[라이브 고정]", "LIVE " + live["title"][:30] if live else "방송 대기 중")
    ok = False
    if pin.get("message_id"):                          # 기존 고정 메시지 수정
        if photo:
            media = {"type": "photo", "media": photo, "caption": caption, "parse_mode": "HTML"}
            res = tg_call("editMessageMedia", cfg, {"message_id": pin["message_id"], "reply_markup": markup,
                                                     "media": json.dumps(media, ensure_ascii=False)})
        else:
            res = tg_call("editMessageCaption", cfg, {"message_id": pin["message_id"], "caption": caption,
                                                       "parse_mode": "HTML", "reply_markup": markup})
        ok = res.get("ok") or "not modified" in str(res.get("description", ""))
    if not ok and photo:                                # 처음이거나 메시지가 지워졌으면 새로 올리고 고정
        res = tg_call("sendPhoto", cfg, {"photo": photo, "caption": caption, "parse_mode": "HTML",
                                          "disable_notification": "true", "reply_markup": markup})
        if res.get("ok"):
            pin["message_id"] = res["result"]["message_id"]
            tg_call("pinChatMessage", cfg, {"message_id": pin["message_id"], "disable_notification": "true"})
            ok = True
    if ok:
        pin.update(key=key, updated=cur.isoformat())


# ---------------------------------------------------------------- 프로그램별 발송 시각
def match_program(title, programs):
    """제목에 프로그램 키워드가 들어 있으면 그 프로그램 설정 반환"""
    t = title.replace(" ", "").lower()
    for p in programs:
        for kw in p.get("keywords", []):
            if kw and kw.replace(" ", "").lower() in t:
                return p
    return None


def next_slot(times, base):
    """base 이후 가장 가까운 발송 시각 (오늘 남은 시각 → 없으면 다음 날 첫 시각)"""
    slots = sorted(t for t in times if re.fullmatch(r"\d{1,2}:\d{2}", t))
    if not slots:
        return base
    for day in range(0, 8):
        d = base.date() + dt.timedelta(days=day)
        for t in slots:
            hh, mm = map(int, t.split(":"))
            cand = base.replace(year=d.year, month=d.month, day=d.day, hour=hh, minute=mm, second=0, microsecond=0)
            if cand >= base:
                return cand
    return base


def flush_queue(state, cfg):
    """발송 시각이 된 대기 영상 공유"""
    q, keep, cur = state.get("queue", []), [], now()
    for it in q:
        ready = dt.datetime.fromisoformat(it["ready_at"])
        if ready <= cur:
            if cur - ready > dt.timedelta(hours=20):
                print(f"  ⏭️ 발송 시각이 너무 지나 생략: {it['title'][:30]}")
                continue
            send_text(it["msg"], cfg, preview=True)
            add_history(state, f"[유튜브] {it['title']}", it["link"])
            print(f"  ⏰ 예약 공유: {it['title'][:40]} ({it['program']}, {ready:%H:%M})")
            time.sleep(1)
        else:
            keep.append(it)
    state["queue"] = keep
    if keep:
        print("[대기] " + ", ".join(f"{i['title'][:15]}→{i['ready_at'][11:16]}" for i in keep))


def main():
    cfg = load_config()
    yc = cfg["youtube"]
    if not yc.get("enabled", True):
        return print("유튜브 기능 꺼짐")
    notify_live = yc.get("notify_live", "start")  # start | end | both
    state = load_state("youtube")
    seen = state.setdefault("seen", {})          # vid -> 마지막으로 처리한 상태
    inited = set(state.setdefault("initialized", []))
    programs = [p for p in yc.get("programs", []) if p.get("keywords")]
    only_programs = yc.get("only_programs", False) and bool(programs)
    flush_queue(state, cfg)                       # 먼저, 발송 시각이 된 예약 영상부터

    for ch in yc.get("channels", []):
        cid = ch.get("channel_id", "").strip()
        name = ch.get("name", "")
        if not re.fullmatch(r"UC[\w-]{22}", cid):
            print(f"⚠️ [{name}] 채널 ID 형식이 아님 → 건너뜀: {cid!r}")
            continue
        try:
            r = requests.get(RSS.format(cid), headers=UA, timeout=20)
        except Exception as e:  # noqa
            print(f"⚠️ [{name}] RSS 접속 실패: {e}")
            continue
        feed = feedparser.parse(r.content)
        print(f"[{name}] RSS HTTP {r.status_code}, 영상 {len(feed.entries)}개")
        if r.status_code != 200 or not feed.entries:
            print("  ⚠️ 영상 목록을 받지 못함 (채널 ID 확인 필요) → 이번 회차는 건너뜀")
            continue
        first_time = cid not in inited
        for e in feed.entries[:15]:
            vid, title, link = e.get("yt_videoid"), e.get("title", ""), e.get("link", "")
            if not vid:
                continue
            published = e.get("published_parsed")
            age_h = (time.time() - calendar.timegm(published)) / 3600 if published else 999
            if first_time and age_h > 3:   # 처음 등록한 채널: 3시간 넘은 기존 영상은 '본 것'으로만 표시
                seen[vid] = "video"
                continue
            prev = seen.get(vid)
            if prev in ("video", "ended", "posted"):
                continue
            st = video_status(vid)
            kind = None
            if st is None:                         # API 키 없음 → 새 항목이면 바로 공유
                kind = "video" if prev is None else None
                st = "posted"
            elif st == "upcoming":
                kind = None                        # 예약 상태는 대기
            elif st == "live" and prev != "live" and notify_live in ("start", "both"):
                kind = "live"
            elif st == "ended" and notify_live in ("end", "both"):
                kind = "ended"
            elif st == "ended" and prev is None and notify_live == "start":
                kind = "ended"                     # 시작 알림을 놓친 경우라도 한 번은 공유
            elif st == "video":
                kind = "video"
            print(f"  · {title[:40]} | {age_h:.1f}시간 전 | 상태 {st} → {'공유' if kind else '대기/생략'}")
            if kind:
                prog = match_program(title, programs)
                if only_programs and not prog:
                    print("    → 지정 프로그램이 아니라 공유 안 함")
                elif prog and prog.get("times") and kind != "live":   # 라이브 시작 알림은 즉시
                    ready = next_slot(prog["times"], now())
                    state.setdefault("queue", []).append({
                        "vid": vid, "title": title, "link": link, "program": prog.get("name", ""),
                        "ready_at": ready.isoformat(), "msg": build_msg(cfg, name, title, link, kind)})
                    print(f"    → [{prog.get('name')}] {ready:%m/%d %H:%M} 공유 예약")
                else:
                    send_text(build_msg(cfg, name, title, link, kind), cfg, preview=True)
                    add_history(state, f"[유튜브] {title}", link)
                    time.sleep(1)
            seen[vid] = st
        if first_time:
            inited.add(cid)
            print(f"  [{name}] 채널 첫 등록 완료 (3시간 이내 영상만 공유, 이후 새 영상 자동 알림)")

    flush_queue(state, cfg)                       # 방금 예약된 것 중 바로 보낼 시각인 것
    try:
        update_live_pin(cfg, state)
    except Exception as e:  # noqa  고정 메시지 오류가 영상 알림을 막지 않도록
        print("⚠️ 라이브 고정 메시지 오류:", e)
    state["initialized"] = sorted(inited)
    state["seen"] = dict(list(seen.items())[-500:])
    state["last_run"] = now().strftime("%Y-%m-%d %H:%M")
    save_state("youtube", state)


if __name__ == "__main__":
    main()
