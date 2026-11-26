"""⑨ 52주 신고가 / 신저가 종목 현황을 한 메시지로 발송 (각각 상승률·하락률 TOP 5).

데이터: 네이버증권 신고가·신저가 목록 (stock.naver.com/market/stock/kr/stocklist/high52week)
역사적 신고가: 네이버 월봉 차트의 상장 이후 최고가와 오늘 고가를 비교해서 구분.
"""
import re
import sys
import time

import requests

from bot.common import (UA, add_history, esc, is_market_day, load_config,
                        load_state, now, save_state, send_text)

LIST_API = "https://stock.naver.com/api/domestic/market/stock/default"
CHART_API = "https://fchart.stock.naver.com/sise.nhn"
HEAD = {**UA, "Referer": "https://stock.naver.com/market/stock/kr/stocklist/high52week"}


# ---------------------------------------------------------------- 데이터 수집
def fetch_list(order_type, limit=300):
    """order_type: high52week(신고가) / low52week(신저가). 100개씩 넘겨 가며 수집"""
    items, start = [], 0
    while start < limit:
        try:
            r = requests.get(LIST_API, headers=HEAD, timeout=20, params={
                "tradeType": "KRX", "marketType": "ALL", "orderType": order_type,
                "startIdx": start, "pageSize": 100})
            r.raise_for_status()
            data = r.json()
        except Exception as e:  # noqa
            print(f"⚠️ {order_type} 목록 수집 실패 (startIdx={start}): {str(e)[:150]}")
            break
        page = data if isinstance(data, list) else (data.get("items") or data.get("stocks") or [])
        items += page
        if len(page) < 100:
            break
        start += 100
        time.sleep(0.5)
    print(f"[{order_type}] {len(items)}건 수집")
    return items


def num(v, default=0.0):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return default


def to_row(x):
    return {"code": x.get("itemcode", ""), "name": x.get("itemname", ""),
            "market": "코스피" if str(x.get("sosok")) == "0" else "코스닥",
            "rate": num(x.get("prevChangeRate")),
            "high": num(x.get("highPrice")), "low": num(x.get("lowPrice")),
            "amount": num(x.get("tradeAmount")), "type": x.get("type", "")}


def filter_rows(items, sc, kind):
    """네이버 '52주 최고/최저' 목록에 선별된 종목을 그대로 사용 (중복·제외 종목명만 걸러냄)"""
    rows, seen = [], set()
    exclude = set(sc.get("exclude_names") or [])
    for x in items:
        r = to_row(x)
        if not r["code"] or r["code"] in seen or r["name"] in exclude:
            continue
        seen.add(r["code"])
        rows.append(r)
    return rows


def chart_extreme(code, kind):
    """월봉 전체(상장 이후)에서 최고가(신고가) 또는 최저가(신저가). 실패 시 None"""
    try:
        r = requests.get(CHART_API, headers=UA, timeout=15, params={
            "symbol": code, "timeframe": "month", "count": 600, "requestType": 0})
        vals = re.findall(r'data="([^"]+)"', r.text)
        hl = [(num(v.split("|")[2]), num(v.split("|")[3])) for v in vals if v.count("|") >= 4]
        if len(hl) < 2:
            return None
        return max(h for h, _ in hl) if kind == "high" else min(l for _, l in hl if l > 0)
    except Exception as e:  # noqa
        print(f"⚠️ 월봉 조회 실패 {code}: {str(e)[:100]}")
        return None


def mark_alltime(rows, kind, sc):
    """(역사적) / (52주) 구분. 상장 1년 미만 종목은 52주 = 상장 이후라서 (역사적)으로 보지 않음"""
    if not sc.get("check_alltime", True):
        for r in rows:
            r["tag"] = "52주"
        return
    for r in rows:
        ext = chart_extreme(r["code"], kind)
        today = r["high"] if kind == "high" else r["low"]
        if ext and today:
            r["tag"] = "역사적" if (today >= ext if kind == "high" else today <= ext) else "52주"
        else:
            r["tag"] = "52주"
        time.sleep(0.15)


# ---------------------------------------------------------------- 메시지
def top(rows, kind, n):
    """신고가는 상승률 높은 순, 신저가는 하락률 큰 순으로 상위 n개"""
    return sorted(rows, key=lambda r: r["rate"], reverse=(kind == "high"))[:n]


def section(kind, rows, total):
    title, mark, label = (("52주 신고가", "✅", "상승률") if kind == "high"
                          else ("52주 신저가", "🔻", "하락률"))
    head = f"{mark} <b>{title} {label} TOP {len(rows)}</b> (총 {total}종목)"
    if not rows:
        return f"{mark} <b>{title}</b>\n해당 종목 없음"
    lines = [head, f"[{label}] 종목명"]
    for r in rows:
        lines.append(f"[{r['rate']:+.1f}%] {esc(r['name'])}({r['tag']})")
    return "\n".join(lines)


def send_chunks(text, cfg, limit=3900):
    """4,000자를 넘으면 줄 단위로 나눠 이어서 발송 (종목이 잘리지 않게)"""
    chunks, buf = [], ""
    for line in text.split("\n"):
        if buf and len(buf) + len(line) + 1 > limit:
            chunks.append(buf)
            buf = ""
        buf = f"{buf}\n{line}" if buf else line
    if buf:
        chunks.append(buf)
    ok = True
    for c in chunks:
        ok &= send_text(c, cfg, preview=False)
    return ok


def collect(kind, cfg):
    """(상위 n개, 전체 종목 수). (역사적) 확인은 상위 n개만 해서 빠름"""
    sc = cfg.get("stock_highs", {})
    items = fetch_list("high52week" if kind == "high" else "low52week", int(sc.get("max_fetch", 500)))
    rows = filter_rows(items, sc, kind)
    best = top(rows, kind, int(sc.get("top_n", 5)))
    if best:
        mark_alltime(best, kind, sc)
    return best, len(rows)


def main():
    force = "--force" in sys.argv
    cfg = load_config()
    sc = cfg.get("stock_highs", {})
    if not sc.get("enabled", True) and not force:
        print("⑨ 사용 안 함")
        return
    if not is_market_day(cfg) and not force:
        print("휴장일 → 건너뜀")
        return
    state = load_state("stock_highs")
    day = now().strftime("%Y-%m-%d")
    done = state.setdefault("done", {})
    if done.get(day) and not force:
        print("오늘 이미 발송함")
        return
    highs, n_hi = collect("high", cfg)
    lows, n_lo = collect("low", cfg) if sc.get("include_low", True) else (None, 0)
    if not highs and not lows:
        print("신고가·신저가 모두 0건 → 발송 안 함 (수집 실패 여부는 위 로그 확인)")
        save_state("stock_highs", state)
        return
    parts = [f"📊 <b>신고가·신저가 종목 현황({day}기준)</b>",
             section("high", highs, n_hi)]
    if lows is not None:
        parts.append(section("low", lows, n_lo))
    if send_chunks("\n\n".join(parts), cfg):
        done[day] = now().strftime("%H:%M")
        add_history(state, f"⑨ 신고가 {n_hi} · 신저가 {n_lo}종목 (TOP {sc.get('top_n', 5)})")
    for k in sorted(done)[:-20]:
        done.pop(k)
    save_state("stock_highs", state)


if __name__ == "__main__":
    main()
