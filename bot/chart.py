"""종목 일봉 차트 이미지 (캔들 + 5일선·20일선 + 거래량). 데이터: 네이버 일봉 (fchart)
다른 기능(종목뉴스·신고가·특징주 등)에서 make_chart(code, name)로 불러 씀
"""
import datetime as dt
import io
import os
import re

import requests

from bot.common import UA

CHART_API = "https://fchart.stock.naver.com/sise.nhn"
UP, DOWN = "#e5383b", "#1c6dd0"          # 한국식: 상승 빨강, 하락 파랑


def fetch_daily(code, count=120):
    """[(date, open, high, low, close, volume), ...] 오래된 순"""
    r = requests.get(CHART_API, headers=UA, timeout=15, params={
        "symbol": code, "timeframe": "day", "count": count, "requestType": 0})
    rows = []
    for v in re.findall(r'data="([^"]+)"', r.text):
        p = v.split("|")
        if len(p) >= 6:
            rows.append((dt.datetime.strptime(p[0], "%Y%m%d").date(),
                         *[float(x) for x in p[1:5]], float(p[5])))
    return rows


def realtime(code):
    """네이버 실시간 시세 (네이버 화면과 같은 등락률). 실패하면 None
    반환: {"rate": 등락률(부호 포함), "open","high","low","close","volume", "date"}"""
    try:
        r = requests.get(f"https://polling.finance.naver.com/api/realtime/domestic/stock/{code}",
                         headers=UA, timeout=10)
        d = r.json()["datas"][0]
        num = lambda k: float(str(d.get(k) or "0").replace(",", ""))  # noqa: E731
        sign = str((d.get("compareToPreviousPrice") or {}).get("code", ""))
        rate = abs(num("fluctuationsRatio"))
        rate = -rate if sign in ("4", "5") else 0.0 if sign == "3" else rate   # 4 하한·5 하락, 3 보합
        when = str(d.get("localTradedAt") or "")[:10]
        return {"rate": rate, "open": num("openPrice"), "high": num("highPrice"), "low": num("lowPrice"),
                "close": num("closePrice"), "volume": num("accumulatedTradingVolume"),
                "date": dt.date.fromisoformat(when) if when else None}
    except Exception as e:  # noqa
        print(f"⚠️ 실시간 시세 실패 {code}: {str(e)[:100]}")
        return None


def fix_halts(rows):
    """거래정지일(거래량 0 또는 시가·고가·저가 0)은 전일 종가로 시가=고가=저가=종가 처리.
    네이버 일봉에서 이런 날은 0원~가격까지 긴 봉처럼 보이는 문제를 막음"""
    out = []
    for i, (d, o, h, l, c, v) in enumerate(rows):
        prev = out[-1][4] if out else (c or o or h)
        if v == 0 or 0 in (o, h, l) or c == 0:
            p = c if c and v else prev
            out.append((d, p, p, p, p, 0))
        else:
            out.append((d, o, h, l, c, v))
    return out


def _font():
    from matplotlib import font_manager
    for path in ["/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
                 "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                 "C:/Windows/Fonts/malgun.ttf"]:
        if os.path.exists(path):
            font_manager.fontManager.addfont(path)
            return font_manager.FontProperties(fname=path).get_name()
    return None


def render(name, rows, rate=None, stamp=""):
    """rows로 PNG bytes 생성"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    fam = _font()
    if fam:
        plt.rcParams["font.family"] = fam
    plt.rcParams["axes.unicode_minus"] = False

    rows = fix_halts(rows)
    n = len(rows)
    x = list(range(n))
    o, h, l, c, v = ([r[i] for r in rows] for i in range(1, 6))
    halt = [rows[i][5] == 0 for i in range(n)]       # 거래정지(거래량 0) 날
    def avg(k):   # k일 이동평균 (데이터가 k일이 안 되는 앞부분은 비움)
        return [sum(c[i - k + 1:i + 1]) / k if i >= k - 1 else float("nan") for i in range(n)]
    ma5, ma20 = avg(5), avg(20)
    color = [UP if c[i] >= o[i] else DOWN for i in range(n)]
    for i in range(n):
        if halt[i]:
            color[i] = "#555555"

    fig = plt.figure(figsize=(8, 6.2), dpi=140, facecolor="white")
    ax = fig.add_axes([0.08, 0.32, 0.9, 0.56])
    av = fig.add_axes([0.08, 0.08, 0.9, 0.18], sharex=ax)

    live = [i for i in range(n) if not halt[i]]
    ax.vlines([x[i] for i in live], [l[i] for i in live], [h[i] for i in live],
              color=[color[i] for i in live], linewidth=0.8)
    ax.bar([x[i] for i in live], [abs(c[i] - o[i]) or (h[i] - l[i]) * 0.02 + 1e-9 for i in live],
           bottom=[min(c[i], o[i]) for i in live], color=[color[i] for i in live], width=0.7)
    hx = [i for i in range(n) if halt[i]]           # 거래정지: 전일 종가에 가로줄(–)
    if hx:
        ax.hlines([c[i] for i in hx], [i - 0.4 for i in hx], [i + 0.4 for i in hx],
                  color="#555555", linewidth=1.6)
    ax.plot(x, ma5, color="#1faa55", linewidth=1.2, label="5일선")
    ax.plot(x, ma20, color="#d81b1b", linewidth=1.4, label="20일선")
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), fontsize=8, frameon=False, ncol=2,
              handlelength=1.6, borderaxespad=0.2)
    av.bar(x, v, color=color, width=0.7)

    for a in (ax, av):
        for s in ("top", "right", "left"):
            a.spines[s].set_visible(False)
        a.grid(axis="y", color="#eeeeee")
        a.tick_params(colors="#666666", labelsize=8, length=0)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda y, _: f"{y:,.0f}"))
    av.yaxis.set_major_formatter(FuncFormatter(
        lambda y, _: f"{y/1e6:.1f}M" if y >= 1e6 else f"{y/1e3:.0f}K" if y else "0"))
    plt.setp(ax.get_xticklabels(), visible=False)
    ticks = [i for i in range(n) if i == 0 or rows[i][0].month != rows[i - 1][0].month][1:]
    av.set_xticks(ticks)
    av.set_xticklabels([rows[i][0].strftime("%b %Y") for i in ticks])
    av.set_xlim(-1, n)

    if rate is None and n > 1:
        rate = (c[-1] / c[-2] - 1) * 100
    rc = UP if (rate or 0) > 0 else DOWN if (rate or 0) < 0 else "#333333"
    fig.text(0.5, 0.955, name, ha="center", fontsize=15, fontweight="bold", color="#222222")
    if rate is not None:
        fig.text(0.5 + 0.012 * len(name) + 0.03, 0.955, f"{rate:+.2f}%", fontsize=14,
                 color=rc, fontweight="bold")
    if stamp:
        fig.text(0.5, 0.915, stamp, ha="center", fontsize=9, color="#999999")

    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def make_chart(code, name, rate=None, stamp="", days=120):
    """종목코드로 바로 차트 PNG. 실패하면 None (차트 없이 글만 보내도록)
    등락률은 네이버 실시간 값(전일 종가 대비)을 쓰고, 오늘 봉도 실시간 값으로 맞춤"""
    try:
        rows = fetch_daily(code, days)
        if len(rows) < 5:
            return None
        rt = realtime(code)
        if rt and rt["close"] and rt["date"]:
            today = (rt["date"], rt["open"] or rt["close"], rt["high"] or rt["close"],
                     rt["low"] or rt["close"], rt["close"], rt["volume"])
            if rows[-1][0] == rt["date"]:
                rows[-1] = today            # 차트 데이터의 오늘 봉을 실시간 값으로 갱신
            elif rows[-1][0] < rt["date"]:
                rows.append(today)          # 차트 데이터에 오늘 봉이 아직 없으면 추가
        if rate is None:
            rate = rt["rate"] if rt else None
        print(f"[차트] {name} 등락률 {rate if rate is not None else '계산값'}"
              f" (실시간 {'O' if rt else 'X'}, 마지막 봉 {rows[-1][0]})")
        return render(name, rows, rate, stamp)
    except Exception as e:  # noqa
        print(f"⚠️ 차트 실패 {name}: {str(e)[:100]}")
        return None
