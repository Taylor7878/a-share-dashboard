"""All mainland A-share daily breadth and TRIN (SH/SZ/BJ), no CSI300 restriction.

Collect one end-of-day, market-wide snapshot per trading session. The SMA20
breadth requires 20 successfully captured trading sessions, so the indicator
stays null during warm-up. No unverified historical values are invented.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import time
from zoneinfo import ZoneInfo

import akshare as ak
import pandas as pd

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
OUTPUT = DATA_DIR / "market.json"
CACHE = DATA_DIR / "price_cache.json"
CHINA_TZ = ZoneInfo("Asia/Shanghai")
TODAY = dt.datetime.now(CHINA_TZ).date()
MIN_UNIVERSE = 3500            # Detect incomplete API responses instead of publishing fake full-market figures.
MIN_ACTIVE_COVERAGE = 0.70    # Fraction of listed tickers with valid traded quotes.
MIN_SMA20_COVERAGE = 0.85     # Never label a small subset as the complete A-share market.
KEEP_SNAPSHOTS = 45
KEEP_HISTORY = 250


def retry(call, attempts=3):
    for attempt in range(attempts):
        try:
            return call()
        except Exception:
            if attempt + 1 == attempts:
                raise
            time.sleep(3 * (attempt + 1))


def read_json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + ".tmp")
    pending.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    pending.replace(path)


def is_trading_day(day: dt.date) -> bool:
    """Use trading calendar, not just weekday; avoids labeling holiday leftovers as fresh."""
    days = retry(ak.tool_trade_date_hist_sina)
    if "trade_date" not in days.columns:
        raise RuntimeError("Trading calendar schema changed")
    return day in set(pd.to_datetime(days["trade_date"]).dt.date)


def fetch_market():
    df = retry(ak.stock_zh_a_spot_em)
    required = {"代码", "最新价", "涨跌幅", "成交量"}
    if df is None or df.empty or not required.issubset(df.columns):
        raise RuntimeError("Full A-share spot market schema missing expected fields")

    codes = df["代码"].astype(str).str.strip().str.zfill(6)
    # AKShare's endpoint covers Shanghai, Shenzhen and Beijing A shares only.
    # Avoid adding indices, B shares or duplicate tickers.
    eligible = codes.str.fullmatch(r"\d{6}") & ~codes.duplicated(keep="first")
    df = df.loc[eligible].copy()
    df["code"] = codes.loc[eligible]
    total = len(df)
    if total < MIN_UNIVERSE:
        raise RuntimeError(f"Only {total} A-share listings received; incomplete feed")

    close = pd.to_numeric(df["最新价"], errors="coerce")
    pct = pd.to_numeric(df["涨跌幅"], errors="coerce")
    volume = pd.to_numeric(df["成交量"], errors="coerce")
    ok = close.notna() & (close > 0) & pct.notna() & volume.notna() & (volume > 0)
    active = int(ok.sum())
    if active < total * MIN_ACTIVE_COVERAGE:
        raise RuntimeError(f"Incomplete or stale market feed: {active}/{total} active quotes")
    up = ok & (pct > 0)
    down = ok & (pct < 0)
    up_count, down_count = int(up.sum()), int(down.sum())
    up_volume, down_volume = float(volume[up].sum()), float(volume[down].sum())
    if min(up_count, down_count, up_volume, down_volume) <= 0:
        raise RuntimeError("Cannot compute valid TRIN from this snapshot")
    trin = round((up_count / down_count) / (up_volume / down_volume), 4)
    if not 0 < trin < 100:
        raise RuntimeError("Implausible TRIN; refusing to publish")
    closes = {code: round(float(price), 4) for code, price in zip(df.loc[ok, "code"], close[ok])}
    record = {
        "date": TODAY.isoformat(),
        "trin": trin,
        "advancers": up_count,
        "decliners": down_count,
        "up_volume": round(up_volume, 2),
        "down_volume": round(down_volume, 2),
        "active_count": active,
        "total_count": total,
    }
    return closes, record


def calculate_breadth(snapshots, current_day, active_count):
    # A 20-day SMA uses 20 *observed* session closes, with the current day included.
    # Suspended / zero-volume securities cannot qualify on the calculation day.
    sessions = sorted(snapshots)
    if len(sessions) < 20:
        return None, 0, len(sessions)
    current_closes = snapshots[current_day]
    recent_days = sessions[-KEEP_SNAPSHOTS:]
    above = valid = 0
    for code, last_price in current_closes.items():
        values = [snapshots[day][code] for day in recent_days if code in snapshots[day]]
        if len(values) < 20:
            continue
        sma20 = sum(values[-20:]) / 20
        valid += 1
        above += last_price > sma20
    if active_count == 0 or valid / active_count < MIN_SMA20_COVERAGE:
        return None, valid, len(sessions)
    return round(100 * above / valid, 2), valid, len(sessions)


def main():
    if TODAY.weekday() >= 5 or not is_trading_day(TODAY):
        print(f"{TODAY}: non-trading day; skipped")
        return
    closes, today_record = fetch_market()

    # Cache contains only directly observed daily closes. Do not silently backfill
    # with unadjusted price histories or the user's earlier CSI300 observations.
    cache = read_json(CACHE, {"snapshots": {}})
    snapshots = cache.get("snapshots", {})
    if not isinstance(snapshots, dict):
        raise RuntimeError("Price cache corrupt: snapshots is not an object")
    snapshots[TODAY.isoformat()] = closes
    snapshots = {key: snapshots[key] for key in sorted(snapshots)[-KEEP_SNAPSHOTS:]}
    breadth, valid, sessions = calculate_breadth(snapshots, TODAY.isoformat(), today_record["active_count"])
    today_record.update({
        "breadth": breadth,
        "valid_count": valid,
        "warmup_sessions": min(sessions, 20),
        "coverage_pct": round(100 * valid / today_record["active_count"], 1),
    })

    previous = read_json(OUTPUT, {"history": []})
    histories = {item["date"]: item for item in previous.get("history", []) if "date" in item}
    histories[TODAY.isoformat()] = today_record
    history = [histories[key] for key in sorted(histories)[-KEEP_HISTORY:]]
    output = {
        "source": "AKShare / 东方财富 · 沪深京全A股盘后快照",
        "universe": "沪市 + 深市 + 北交所A股；当天有成交、行情有效的股票",
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "breadth_method": "全A股当日有效交易股票中：最新收盘价严格大于该股最近20次有效日收盘价均值的比例；覆盖率低于85%时不发布宽度",
        "history": history,
    }
    save_json(CACHE, {"snapshots": snapshots})
    save_json(OUTPUT, output)
    status = f"{breadth}%" if breadth is not None else f"warming up ({sessions}/20 sessions, coverage {today_record['coverage_pct']}%)"
    print(f"{TODAY}: all A shares {today_record['total_count']}, active {today_record['active_count']}; TRIN {today_record['trin']}; SMA20 {status}")


if __name__ == "__main__":
    main()
