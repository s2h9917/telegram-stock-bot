"""종목 일봉 차트 이미지 (캔들 + 20일 이동평균 + 거래량). 데이터: 네이버 일봉 (fchart)
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

    n = len(rows)
    x = list(range(n))
    o, h, l, c, v = ([r[i] for r in rows] for i in range(1, 6))
    ma = [sum(c[max(0, i - 19):i + 1]) / len(c[max(0, i - 19):i + 1]) for i in range(n)]
    color = [UP if c[i] >= o[i] else DOWN for i in range(n)]

    fig = plt.figure(figsize=(8, 6.2), dpi=140, facecolor="white")
    ax = fig.add_axes([0.08, 0.32, 0.9, 0.56])
    av = fig.add_axes([0.08, 0.08, 0.9, 0.18], sharex=ax)

    ax.vlines(x, l, h, color=color, linewidth=0.8)
    ax.bar(x, [abs(c[i] - o[i]) or (h[i] - l[i]) * 0.02 + 1e-9 for i in range(n)],
           bottom=[min(c[i], o[i]) for i in range(n)], color=color, width=0.7)
    ax.plot(x, ma, color="#f0a830", linewidth=1.3)
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
    """종목코드로 바로 차트 PNG. 실패하면 None (차트 없이 글만 보내도록)"""
    try:
        rows = fetch_daily(code, days)
        if len(rows) < 5:
            return None
        return render(name, rows, rate, stamp)
    except Exception as e:  # noqa
        print(f"⚠️ 차트 실패 {name}: {str(e)[:100]}")
        return None
