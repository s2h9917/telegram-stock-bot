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
WORKFLOWS = {"reports.yml": "① 종목 리포트", "youtube.yml": "② 유튜브 알림", "market.yml": "③ 증시 브리핑"}


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
    cols = st.columns(3)
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
    mc["slots"]["close"] = c4.checkbox("장마감 16시", mc["slots"].get("close", True))
    mc["keywords"] = [s.strip() for s in st.text_input(
        "뉴스 검색 키워드 (쉼표로 구분)", ", ".join(mc.get("keywords", []))).split(",") if s.strip()]

    st.divider()
    cfg["gemini"]["model"] = st.text_input("Gemini 모델 이름 (오류 시 AI Studio에서 무료 모델명 확인)",
                                           cfg["gemini"].get("model", "gemini-flash-latest"))
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
    for name in ["reports", "youtube", "market"]:
        try:
            state = json.loads(gh_get_file(f"state/{name}.json")[0])
            rows += state.get("history", [])
        except Exception:  # noqa
            pass
    rows.sort(key=lambda x: x["time"], reverse=True)
    st.dataframe(rows, width="stretch",
                 column_config={"time": "발송 시각(KST)", "title": "내용",
                                "link": st.column_config.LinkColumn("링크")})
