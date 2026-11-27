"""텔레그램 증시봇 관리자 화면 (Streamlit)
로컬 실행:  streamlit run admin/app.py
무료 배포:  Streamlit Community Cloud (share.streamlit.io)

필요한 비밀값 (admin/.streamlit/secrets.toml 또는 Streamlit Cloud > Settings > Secrets):
  ADMIN_PASSWORD = "관리자 비밀번호"
  GITHUB_TOKEN   = "github_pat_..."     # 저장소 Contents·Actions 읽기/쓰기 권한
  GITHUB_REPO    = "내아이디/telegram-stock-bot"
  TELEGRAM_BOT_TOKEN = "123:ABC..."     # (선택) 테스트/공지 발송용
"""
import base64
import json
import re

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="증시봇 관리자", page_icon="📈", layout="wide")

API = "https://api.github.com"
WORKFLOWS = {"reports.yml": "① 종목 리포트", "youtube.yml": "② 유튜브 알림", "market.yml": "③ 증시 브리핑",
             "hot_issue.yml": "④ 오늘의 핫 이슈", "stock_news.yml": "⑤ 종목 뉴스",
             "weekly_calendar.yml": "⑥ 주간 경제일정",
             "card_news.yml": "⑦ 카드뉴스",
             "breaking.yml": "⑧ 이벤트 속보",
             "stock_highs.yml": "⑨ 신고가·신저가",
             "deep_brief.yml": "⑩ 장전 키워드 심층"}


# ------------------------------------------------------------------ 로그인
if not st.session_state.get("auth"):
    st.title("📈 증시봇 관리자 로그인")
    pw = st.text_input("비밀번호", type="password")
    if st.button("로그인", type="primary"):
        if pw == st.secrets.get("ADMIN_PASSWORD"):
            st.session_state.auth = True
            st.rerun()
        else:
            st.error("비밀번호가 틀렸습니다.")
    st.stop()

REPO = st.secrets["GITHUB_REPO"]
HDR = {"Authorization": f"Bearer {st.secrets['GITHUB_TOKEN']}",
       "Accept": "application/vnd.github+json"}


def gh_get_file(path):
    r = requests.get(f"{API}/repos/{REPO}/contents/{path}", headers=HDR, timeout=20)
    r.raise_for_status()
    j = r.json()
    return base64.b64decode(j["content"]).decode("utf-8"), j["sha"]


def gh_put_file(path, text, sha, msg):
    r = requests.put(f"{API}/repos/{REPO}/contents/{path}", headers=HDR, timeout=20, json={
        "message": msg, "sha": sha,
        "content": base64.b64encode(text.encode("utf-8")).decode()})
    return r.ok, r.text


def gh_runs(wf, n=5):
    r = requests.get(f"{API}/repos/{REPO}/actions/workflows/{wf}/runs",
                     headers=HDR, params={"per_page": n}, timeout=20)
    return r.json().get("workflow_runs", []) if r.ok else []


def gh_dispatch(wf, inputs):
    r = requests.post(f"{API}/repos/{REPO}/actions/workflows/{wf}/dispatches", headers=HDR,
                      json={"ref": "main", "inputs": inputs}, timeout=20)
    return r.status_code == 204, r.text


def resolve_channel_id(text):
    """@핸들, 채널 주소, UC... 무엇을 넣어도 채널ID(UC...)로 변환"""
    text = text.strip()
    if re.fullmatch(r"UC[\w-]{22}", text):
        return text
    url = text if text.startswith("http") else f"https://www.youtube.com/{text if text.startswith('@') else '@' + text}"
    html = requests.get(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "ko"},
                        cookies={"CONSENT": "YES+"}, timeout=20).text
    m = re.search(r'"channelId":"(UC[\w-]{22})"', html) or re.search(r'channel/(UC[\w-]{22})', html)
    return m.group(1) if m else None


@st.cache_data(ttl=60)
def load_cfg():
    return gh_get_file("config.json")


# ------------------------------------------------------------------ 화면
st.title("📈 텔레그램 증시봇 관리자")
tab1, tab2, tab3, tab4 = st.tabs(["📊 대시보드", "⚙️ 설정", "▶️ 수동 실행 / 공지", "🧾 발송 이력"])

# ---------------- 대시보드
with tab1:
    if st.button("🔄 새로고침"):
        st.cache_data.clear()
    cols = st.columns(len(WORKFLOWS))
    for col, (wf, label) in zip(cols, WORKFLOWS.items()):
        with col:
            st.subheader(label)
            runs = gh_runs(wf)
            if not runs:
                st.caption("실행 기록 없음 (Actions 탭에서 워크플로 활성화 확인)")
            for r in runs:
                icon = {"success": "✅", "failure": "❌", "cancelled": "⚪"}.get(r["conclusion"], "⏳")
                t = r["created_at"].replace("T", " ")[:16]
                st.markdown(f"{icon} [{t} UTC]({r['html_url']}) · {r['event']}")
    st.info("시간은 UTC 기준입니다 (한국시간 = UTC + 9시간). ❌ 를 누르면 오류 로그를 볼 수 있어요.")

# ---------------- 설정
with tab2:
    raw, sha = load_cfg()
    cfg = json.loads(raw)

    st.subheader("텔레그램")
    cfg["telegram"]["chat_id"] = st.text_input("채널 ID (@채널아이디 또는 -100으로 시작하는 숫자)",
                                               cfg["telegram"]["chat_id"])
    cfg["footer"] = st.text_area("하단 고지문구", cfg.get("footer", ""), height=70)
    cfg["holidays"] = [d.strip() for d in st.text_area(
        "휴장일 (한 줄에 하나, YYYY-MM-DD)", "\n".join(cfg.get("holidays", []))).splitlines() if d.strip()]

    st.divider()
    st.subheader("① 종목 리포트")
    rc = cfg["reports"]
    c1, c2, c3 = st.columns(3)
    rc["enabled"] = c1.toggle("사용", rc.get("enabled", True), key="r_en")
    rc["format"] = c2.radio("업로드 형식", ["text", "image"], horizontal=True,
                            index=0 if rc.get("format") != "image" else 1,
                            format_func=lambda x: "텍스트 묶음" if x == "text" else "이미지 카드")
    rc["max_reports"] = c3.number_input("최대 건수", 1, 60, rc.get("max_reports", 20))
    rc["per_message"] = c3.number_input("메시지 1개당 종목 수", 1, 20, rc.get("per_message", 5))
    rc["header"] = st.text_input("제목 문구 ({date} = 날짜)", rc.get("header", ""))
    rc["stocks_watch"] = [s.strip() for s in st.text_input(
        "관심종목만 (쉼표로 구분, 비우면 전체)", ", ".join(rc.get("stocks_watch", []))).split(",") if s.strip()]
    rc["brokers_exclude"] = [s.strip() for s in st.text_input(
        "제외할 증권사 (쉼표로 구분)", ", ".join(rc.get("brokers_exclude", []))).split(",") if s.strip()]

    st.divider()
    st.subheader("② 유튜브 알림")
    yc = cfg["youtube"]
    c1, c2 = st.columns(2)
    yc["enabled"] = c1.toggle("사용", yc.get("enabled", True), key="y_en")
    opts = ["start", "end", "both"]
    yc["notify_live"] = c2.radio("라이브 알림 시점", opts, horizontal=True,
                                 index=opts.index(yc.get("notify_live", "start")),
                                 format_func={"start": "방송 시작", "end": "방송 종료(다시보기)",
                                              "both": "둘 다"}.get)
    ch_df = pd.DataFrame(yc.get("channels", []), columns=["name", "channel_id"])
    edited = st.data_editor(ch_df, num_rows="dynamic", width="stretch",
                            column_config={"name": "채널 이름", "channel_id": "채널 ID (UC...)"})
    yc["channels"] = [r for r in edited.fillna("").to_dict("records") if r["channel_id"]]
    lp = yc.setdefault("live_pin", {})
    lp["enabled"] = st.checkbox("📌 라이브 상단 고정 메시지 자동 갱신 (첫 번째 채널 기준, 봇에 '메시지 고정' 권한 필요)",
                                lp.get("enabled", False))
    lp["check_hours"] = st.number_input("고정 라이브 확인 간격(시간) — 24시간 송출이면 6시간(하루 4번) 권장",
                                        1, 24, int(lp.get("check_hours", 6)))
    lp["title_keyword"] = st.text_input("고정할 라이브 제목 키워드 (이 단어가 들어간 라이브만 고정)",
                                        lp.get("title_keyword", "부자되는 좋은습관"))
    st.markdown("**프로그램별 공유 시각** — 제목에 키워드가 들어간 영상을 지정 시각에 공유 (시각을 비우면 올라오는 즉시)")
    yc["only_programs"] = st.checkbox("아래 지정 프로그램만 공유 (목록에 없는 영상은 공유 안 함)",
                                      yc.get("only_programs", False))
    prog_df = pd.DataFrame([{"name": p.get("name", ""), "keywords": ", ".join(p.get("keywords", [])),
                             "times": ", ".join(p.get("times", []))} for p in yc.get("programs", [])],
                           columns=["name", "keywords", "times"])
    prog_ed = st.data_editor(prog_df, num_rows="dynamic", width="stretch", key="prog_ed",
                             column_config={"name": "프로그램명", "keywords": "제목 키워드 (쉼표로 여러 개)",
                                            "times": "공유 시각 (예: 08:00, 18:30)"})
    yc["programs"] = [{"name": r["name"].strip(),
                       "keywords": [k.strip() for k in r["keywords"].split(",") if k.strip()],
                       "times": [t.strip() for t in r["times"].split(",") if t.strip()]}
                      for r in prog_ed.fillna("").to_dict("records") if r["keywords"].strip()]
    with st.expander("🔎 채널 ID 찾기 (@핸들이나 채널 주소 입력)"):
        h = st.text_input("예: @mtnschool 또는 https://www.youtube.com/@...")
        if st.button("찾기") and h:
            cid = resolve_channel_id(h)
            if cid:
                st.success(f"채널 ID: {cid}")
            else:
                st.error("찾지 못했습니다. 주소를 확인해 주세요.")
    with st.expander("메시지 문구 ({channel}, {title}, {link} 사용 가능)"):
        for k, label in [("video", "새 영상"), ("live", "라이브 시작"), ("ended", "라이브 다시보기")]:
            yc["templates"][k] = st.text_area(label, yc["templates"].get(k, ""), height=90)

    st.divider()
    st.subheader("③ 증시 브리핑")
    mc = cfg["market"]
    c1, c2, c3, c4 = st.columns(4)
    mc["enabled"] = c1.toggle("사용", mc.get("enabled", True), key="m_en")
    mc["slots"]["pre"] = c2.checkbox("장전 08시", mc["slots"].get("pre", True))
    mc["slots"]["mid"] = c3.checkbox("장중 11시", mc["slots"].get("mid", True))
    mc["slots"]["close"] = c4.checkbox("장마감 (16시·지연일 17시)", mc["slots"].get("close", True))
    late_txt = st.text_area(
        "장마감 지연일 (한 줄에 하나, '날짜 마감시각' 형식 · 예: 2026-11-19 16:30)",
        "\n".join(f"{d} {t}" for d, t in sorted(mc.get("late_close", {}).items())), height=80)
    mc["late_close"] = {ln.split()[0]: ln.split()[1] for ln in late_txt.splitlines() if len(ln.split()) == 2}
    mc["keywords"] = [s.strip() for s in st.text_input(
        "뉴스 검색 키워드 (쉼표로 구분)", ", ".join(mc.get("keywords", []))).split(",") if s.strip()]

    st.divider()
    st.subheader("④ 오늘의 핫 이슈 (이미지)")
    hc = cfg.setdefault("hot_issue", {})
    c1, c2, c3 = st.columns(3)
    hc["enabled"] = c1.toggle("사용", hc.get("enabled", True), key="h_en")
    hc["count"] = c2.slider("이슈 개수", 3, 5, int(hc.get("count", 5)))
    hc["weekdays_only"] = c3.checkbox("평일(장 열리는 날)만 발송", hc.get("weekdays_only", False))
    c1, c2 = st.columns(2)
    hc["title"] = c1.text_input("이미지 제목", hc.get("title", "오늘의 핫 이슈"))
    hc["brand"] = c2.text_input("우측 상단 채널명 (비우면 표시 안 함)", hc.get("brand", ""))
    hc["keywords"] = [s.strip() for s in st.text_input(
        "이슈 뉴스 검색 키워드 (쉼표로 구분)",
        ", ".join(hc.get("keywords") or ["뉴욕증시", "미국 증시 마감", "코스피 전망", "환율",
                                          "국제유가", "반도체 주가", "트럼프 관세"])).split(",") if s.strip()]

    st.divider()
    st.subheader("⑤ 종목 뉴스 (매시간)")
    sc = cfg.setdefault("stock_news", {})
    c1, c2, c3, c4 = st.columns(4)
    sc["enabled"] = c1.toggle("사용", sc.get("enabled", True), key="s_en")
    sc["max_per_hour"] = c2.slider("1시간 최대 건수", 1, 5, int(sc.get("max_per_hour", 3)))
    sc["min_score"] = c3.slider("최소 중요도 점수", 2, 10, int(sc.get("min_score", 4)),
                                help="높일수록 수주·단독·대형 계약 같은 굵직한 뉴스만 올라갑니다")
    sc["show_link"] = c4.checkbox("기사 링크 표시", sc.get("show_link", True))
    sc["show_chart"] = st.checkbox("📊 종목 일봉 차트 함께 보내기 (종목별 메시지, 글 아래 차트)", sc.get("show_chart", True), key="sn_chart")
    sc["perspectives"] = st.checkbox("🧠 투자 대가 관점 붙이기 (가치·성장·퀀트, 같은 종목은 하루 1회)",
                                     sc.get("perspectives", True))
    styles = ["plain", "expand", "spoiler"]
    sc["perspective_style"] = st.radio(
        "관점 표시 방식", styles, horizontal=True, index=styles.index(sc.get("perspective_style", "plain")),
        format_func={"plain": "항상 보이기", "expand": "접어두기 (앞부분만 보이고 눌러서 펼침)",
                     "spoiler": "가림 처리 (다시 들어오면 또 가려짐)"}.get)
    c1, c2, c3 = st.columns(3)
    sc["start_hour"] = c1.number_input("시작 시각(시)", 0, 23, int(sc.get("start_hour", 7)))
    sc["end_hour"] = c2.number_input("종료 시각(시)", 0, 23, int(sc.get("end_hour", 20)))
    sc["weekdays_only"] = c3.checkbox("평일(장 열리는 날)만", sc.get("weekdays_only", False), key="s_wd")
    sc["keywords"] = [x.strip() for x in st.text_input(
        "뉴스 검색 키워드 (쉼표로 구분)",
        ", ".join(sc.get("keywords") or ["단독", "수주", "공급계약", "계약 체결", "영업이익", "실적", "흑자전환",
                                          "최대 실적", "기술수출", "FDA", "인수", "합병", "자사주", "유상증자", "임상"])
    ).split(",") if x.strip()]
    st.markdown("**뉴스 매체 지정** — 비워 두면 모든 매체. 매체명은 구글 뉴스에 표시되는 이름, 주소는 도메인만 (예: 머니투데이 / mt.co.kr)")
    med_df = pd.DataFrame(sc.get("media", []), columns=["name", "domain"])
    med_ed = st.data_editor(med_df, num_rows="dynamic", width="stretch", key="media_ed",
                            column_config={"name": "매체명", "domain": "도메인 (예: mt.co.kr)"})
    sc["media"] = [{"name": r["name"].strip(), "domain": r["domain"].strip()}
                   for r in med_ed.fillna("").to_dict("records") if r["name"].strip() or r["domain"].strip()]
    sc["exclude_names"] = [x.strip() for x in st.text_input(
        "잘못 잡히는 회사명 제외 (쉼표로 구분, 예: 흔한 단어와 같은 회사명)",
        ", ".join(sc.get("exclude_names", []))).split(",") if x.strip()]

    st.divider()
    st.subheader("⑥ 주간 경제일정 (매주 월요일 이미지)")
    wc = cfg.setdefault("weekly_calendar", {})
    c1, c2 = st.columns([1, 3])
    wc["enabled"] = c1.toggle("사용", wc.get("enabled", True), key="w_en")
    wc["source_text"] = c2.text_input("출처 표기 (비우면 표시 안 함)", wc.get("source_text", ""))

    st.divider()
    st.subheader("⑦ 카드뉴스 (하루 3번, 시간대별 최다 보도 이슈)")
    cc = cfg.setdefault("card_news", {})
    c1, c2, c3 = st.columns(3)
    cc["enabled"] = c1.toggle("사용", cc.get("enabled", True), key="c_en")
    cc["weekdays_only"] = c2.checkbox("평일(장 열리는 날)만", cc.get("weekdays_only", False), key="c_wd")
    cc["brand"] = c3.text_input("카드 좌측 하단 채널명", cc.get("brand", ""))
    cc["keywords"] = [x.strip() for x in st.text_input(
        "이슈 뉴스 검색 키워드 (쉼표로 구분)",
        ", ".join(cc.get("keywords") or ["증시", "코스피", "코스닥", "뉴욕증시", "경제", "금리", "환율", "반도체", "실적", "관세"])
    ).split(",") if x.strip()]

    st.divider()
    st.subheader("⑧ 이벤트 속보 (금통위·FOMC 등, 지정 시간대에만 1분마다 감시)")
    bc = cfg.setdefault("breaking", {})
    c1, c2 = st.columns(2)
    bc["enabled"] = c1.toggle("사용", bc.get("enabled", True), key="b_en")
    bc["require_flash"] = c2.checkbox("[속보]·1보 표시가 있는 기사만", bc.get("require_flash", True))
    st.markdown("**이벤트 일정** — 날짜 YYYY-MM-DD, 시각 HH:MM (한국 시각), 키워드는 쉼표로")
    ev_df = pd.DataFrame([{**e, "keywords": ", ".join(e.get("keywords", []))} for e in bc.get("events", [])],
                         columns=["date", "start", "end", "name", "keywords"])
    ev_ed = st.data_editor(ev_df, num_rows="dynamic", width="stretch", key="ev_ed", column_config={
        "date": "날짜", "start": "감시 시작", "end": "감시 종료", "name": "이벤트명", "keywords": "속보 키워드"})
    bc["events"] = [{"date": r["date"].strip(), "start": r["start"].strip() or "00:00",
                     "end": r["end"].strip() or "23:59", "name": r["name"].strip(),
                     "keywords": [k.strip() for k in r["keywords"].split(",") if k.strip()]}
                    for r in ev_ed.fillna("").astype(str).to_dict("records") if r["date"].strip()]
    st.markdown("**속보 감시 매체** — 도메인만 넣어도 됨. RSS 주소를 알면 같이 넣으면 더 빠름")
    src_df = pd.DataFrame(bc.get("sources") or [{"name": "연합뉴스", "domain": "yna.co.kr", "rss": ""},
                                                 {"name": "뉴시스", "domain": "newsis.com", "rss": ""},
                                                 {"name": "뉴스1", "domain": "news1.kr", "rss": ""}],
                          columns=["name", "domain", "rss"])
    src_ed = st.data_editor(src_df, num_rows="dynamic", width="stretch", key="src_ed", column_config={
        "name": "매체명", "domain": "도메인", "rss": "RSS 주소 (선택)"})
    bc["sources"] = [{k: str(r[k]).strip() for k in ("name", "domain", "rss")}
                     for r in src_ed.fillna("").to_dict("records") if str(r["domain"]).strip() or str(r["rss"]).strip()]

    st.divider()
    st.subheader("⑨ 52주 신고가·신저가 TOP (평일 15:40, 한 메시지)")
    shc = cfg.setdefault("stock_highs", {})
    c1, c2, c3 = st.columns(3)
    shc["enabled"] = c1.toggle("사용", shc.get("enabled", True), key="sh_en")
    shc["include_low"] = c2.checkbox("신저가도 함께", shc.get("include_low", True), key="sh_low")
    shc["check_alltime"] = c3.checkbox("(역사적)/(52주) 구분", shc.get("check_alltime", True), key="sh_all")
    shc["top_n"] = int(st.number_input("신고가 상승률 / 신저가 하락률 상위 몇 종목", 1, 30, int(shc.get("top_n", 5)), key="sh_top"))
    st.caption("네이버 실시간랭킹 '52주 최고·최저' 종목 중 상위 종목만 한 메시지로 보냅니다.")
    shc["exclude_names"] = [x.strip() for x in st.text_input(
        "제외할 종목명 (쉼표로 구분)", ", ".join(shc.get("exclude_names", [])), key="sh_ex").split(",") if x.strip()]

    st.divider()
    st.subheader("⑩ 장전 키워드 심층 브리핑 (평일 07:50, 기사 본문 기반)")
    dbc = cfg.setdefault("deep_brief", {})
    c1, c2 = st.columns(2)
    dbc["enabled"] = c1.toggle("사용", dbc.get("enabled", True), key="db_en")
    dbc["issues"] = int(c2.slider("이슈 개수", 3, 8, int(dbc.get("issues", 6)), key="db_n"))
    dbc["keywords"] = [x.strip() for x in st.text_input(
        "이슈 기사 검색 키워드 (쉼표로 구분)",
        ", ".join(dbc.get("keywords") or ["증시", "코스피", "코스닥", "뉴욕증시", "반도체", "금리", "환율", "실적",
                                          "수주", "금융당국", "유가", "AI", "바이오", "2차전지"]),
        key="db_kw").split(",") if x.strip()]
    st.caption("전일 15:30 이후 여러 언론사가 다룬 이슈를 골라, 기사 본문을 읽고 사실 중심으로 정리합니다. 매매 권유 문구는 넣지 않습니다.")

    st.divider()
    st.subheader("🧠 AI 작성 관점 (모든 AI 글에 공통 적용)")
    st.caption("브리핑·리포트 요약·핫이슈·종목뉴스·카드뉴스·주간일정 정리에 모두 적용됩니다. "
               "비워 두면 기본값(30년 경력 증권·경제 전문가 관점)을 씁니다.")
    cfg["gemini"]["persona"] = st.text_area("AI 작성 관점", cfg["gemini"].get("persona", ""), height=220,
                                            key="g_persona", placeholder="비워 두면 기본 전문가 관점 사용")
    c1, c2 = st.columns(2)
    cfg["gemini"]["model"] = c1.text_input(
        "기본 AI 모델 (브리핑·리포트·핫이슈·카드뉴스·주간일정)",
        cfg["gemini"].get("model", "gemini-flash-latest"), key="g_model")
    cfg["gemini"]["light_model"] = c2.text_input(
        "가벼운 AI 모델 (매시간 종목뉴스 — 하루 호출이 많아서)",
        cfg["gemini"].get("light_model", "gemini-flash-lite-latest"), key="g_light")
    st.caption("무료 등급은 모델마다 하루 한도(RPD)가 있습니다. 한도가 끝나면 자동으로 다음 모델로 넘어갑니다.")
    if st.button("💾 설정 저장", type="primary"):
        ok, msg = gh_put_file("config.json", json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
                              sha, "관리자 화면에서 설정 변경")
        st.cache_data.clear()
        if ok:
            st.success("저장 완료! 다음 실행부터 반영됩니다.")
        else:
            st.error(msg)

# ---------------- 수동 실행 / 공지
with tab3:
    st.subheader("지금 바로 실행")
    c1, c2, c3 = st.columns(3)
    if c1.button("① 리포트 지금 보내기"):
        ok, m = gh_dispatch("reports.yml", {"force": True})
        if ok:
            st.success("요청 완료 (1~2분 후 발송)")
        else:
            st.error(m)
    if c2.button("② 유튜브 지금 확인"):
        ok, m = gh_dispatch("youtube.yml", {"force": True})
        if ok:
            st.success("요청 완료")
        else:
            st.error(m)
    slot = c3.selectbox("브리핑 종류", ["pre", "mid", "close"],
                        format_func={"pre": "장전", "mid": "장중", "close": "장마감"}.get)
    if c3.button("③ 브리핑 지금 보내기"):
        ok, m = gh_dispatch("market.yml", {"force": True, "slot": slot})
        if ok:
            st.success("요청 완료 (1~2분 후 발송)")
        else:
            st.error(m)

    if st.button("④ 오늘의 핫 이슈 지금 보내기"):
        ok, m = gh_dispatch("hot_issue.yml", {"force": True})
        if ok:
            st.success("요청 완료 (1~2분 후 발송)")
        else:
            st.error(m)

    if st.button("⑤ 종목 뉴스 지금 확인"):
        ok, m = gh_dispatch("stock_news.yml", {"force": True})
        if ok:
            st.success("요청 완료 (1~2분 후, 중요 뉴스가 있으면 발송)")
        else:
            st.error(m)

    c1, c2 = st.columns([3, 1])
    wurl = c1.text_input("⑥ 주간 경제일정 기사 주소 (비우면 이번 주 기사를 자동으로 찾음)")
    if c2.button("⑥ 주간 경제일정 지금 보내기"):
        ok, m = gh_dispatch("weekly_calendar.yml", {"force": True, "url": wurl.strip()})
        if ok:
            st.success("요청 완료 (1~2분 후 발송)")
        else:
            st.error(m)

    c1, c2 = st.columns([1, 3])
    cslot = c1.selectbox("카드뉴스 시간대", ["night", "day", "evening"],
                         format_func={"night": "00:00~07:00", "day": "07:01~16:00", "evening": "16:01~19:30"}.get)
    if c2.button("⑦ 카드뉴스 지금 만들기"):
        ok, m = gh_dispatch("card_news.yml", {"force": True, "slot": cslot})
        if ok:
            st.success("요청 완료 (2~3분 후 발송)")
        else:
            st.error(m)

    if st.button("⑧ 이벤트 속보 지금 확인 (감시 시간대 안일 때만 발송)"):
        ok, m = gh_dispatch("breaking.yml", {"force": False})
        if ok:
            st.success("요청 완료 (약 1분 후, 해당 속보가 있으면 발송)")
        else:
            st.error(m)

    if st.button("⑨ 신고가·신저가 지금 보내기"):
        ok, m = gh_dispatch("stock_highs.yml", {"force": True})
        if ok:
            st.success("요청 완료 (2~3분 후 발송)")
        else:
            st.error(m)

    if st.button("⑩ 장전 키워드 심층 브리핑 지금 보내기"):
        ok, m = gh_dispatch("deep_brief.yml", {"force": True})
        if ok:
            st.success("요청 완료 (3~5분 후 발송)")
        else:
            st.error(m)

    st.divider()
    st.subheader("📢 채널에 직접 공지 보내기")
    tg = st.secrets.get("TELEGRAM_BOT_TOKEN")
    if not tg:
        st.caption("secrets에 TELEGRAM_BOT_TOKEN을 넣으면 사용할 수 있습니다.")
    else:
        msg = st.text_area("내용 (HTML 태그 <b>, <i>, <a href> 사용 가능)", height=150)
        if st.button("보내기", disabled=not msg):
            chat = json.loads(load_cfg()[0])["telegram"]["chat_id"]
            r = requests.post(f"https://api.telegram.org/bot{tg}/sendMessage",
                              data={"chat_id": chat, "text": msg, "parse_mode": "HTML"}, timeout=20)
            if r.ok:
                st.success("발송 완료")
            else:
                st.error(r.text)

# ---------------- 발송 이력
with tab4:
    rows = []
    for name in ["reports", "youtube", "market", "hot_issue", "stock_news", "weekly_calendar", "card_news", "breaking", "stock_highs", "deep_brief"]:
        try:
            state = json.loads(gh_get_file(f"state/{name}.json")[0])
            rows += state.get("history", [])
        except Exception:  # noqa
            pass
    rows.sort(key=lambda x: x["time"], reverse=True)
    st.dataframe(rows, width="stretch",
                 column_config={"time": "발송 시각(KST)", "title": "내용",
                                "link": st.column_config.LinkColumn("링크")})
