"""② 기준 유튜브 채널에 새 영상/라이브가 올라오면 링크를 텔레그램에 공유
- 기본: 유튜브 RSS(무료, 키 불필요)로 새 영상 감지
- YOUTUBE_API_KEY가 있으면 라이브 상태(예정/방송중/종료)까지 구분
실행: python -m bot.youtube_watch
"""
import calendar
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


def main():
    cfg = load_config()
    yc = cfg["youtube"]
    if not yc.get("enabled", True):
        return print("유튜브 기능 꺼짐")
    notify_live = yc.get("notify_live", "start")  # start | end | both
    state = load_state("youtube")
    seen = state.setdefault("seen", {})          # vid -> 마지막으로 처리한 상태
    inited = set(state.setdefault("initialized", []))

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
                send_text(build_msg(cfg, name, title, link, kind), cfg, preview=True)
                add_history(state, f"[유튜브] {title}", link)
                time.sleep(1)
            seen[vid] = st
        if first_time:
            inited.add(cid)
            print(f"  [{name}] 채널 첫 등록 완료 (3시간 이내 영상만 공유, 이후 새 영상 자동 알림)")

    state["initialized"] = sorted(inited)
    state["seen"] = dict(list(seen.items())[-500:])
    state["last_run"] = now().strftime("%Y-%m-%d %H:%M")
    save_state("youtube", state)


if __name__ == "__main__":
    main()
