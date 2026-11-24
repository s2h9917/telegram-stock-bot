"""② 기준 유튜브 채널에 새 영상/라이브가 올라오면 링크를 텔레그램에 공유
- 기본: 유튜브 RSS(무료, 키 불필요)로 새 영상 감지
- YOUTUBE_API_KEY가 있으면 라이브 상태(예정/방송중/종료)까지 구분
실행: python -m bot.youtube_watch
"""
import calendar
import datetime as dt
import os
import re
import time

import feedparser
import requests

from bot.common import UA, add_history, esc, load_config, load_state, now, save_state, send_text

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
    state["initialized"] = sorted(inited)
    state["seen"] = dict(list(seen.items())[-500:])
    state["last_run"] = now().strftime("%Y-%m-%d %H:%M")
    save_state("youtube", state)


if __name__ == "__main__":
    main()
