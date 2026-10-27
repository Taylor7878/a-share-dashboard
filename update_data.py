"""Collect close-of-day full A-share TRIN and SMA20 breadth for GitHub Pages.

Primary: Eastmoney via AKShare. Fallback: Sina via AKShare.
Do not publish partial-market samples or pre-close quotes as finalized EOD data.
SMA20 needs 20 successfully collected trading-session snapshots.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import akshare as ak
import pandas as pd

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
OUTPUT = DATA_DIR / "market.json"
CACHE = DATA_DIR / "price_cache.json"
CHINA_TZ = ZoneInfo("Asia/Shanghai")
MIN_UNIVERSE = 3500
MIN_ACTIVE_COVERAGE = 0.70
MIN_SMA20_COVERAGE = 0.85
MIN_BJ_LISTINGS = 100  # Reject source that silently omits Beijing Stock Exchange.
KEEP_SNAPSHOTS = 45
KEEP_HISTORY = 250


def retry(call, name, attempts=2):
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except Exception as exc:
            last_error = exc
            print(f"{name} attempt {attempt}/{attempts}: {type(exc).__name__}: {exc}", flush=True)
            if attempt != attempts:
                time.sleep(attempt * 4)
    raise last_error


def read_json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)


def is_trading_day(day):
    days = retry(ak.tool_trade_date_hist_sina, "Trading calendar", attempts=2)
    if "trade_date" not in days.columns:
        raise RuntimeError("Trading calendar schema changed")
    return day in set(pd.to_datetime(days["trade_date"]).dt.date)


def get_codes(raw_codes):
    """Accept Eastmoney '600000' and Sina 'sh600000' formats."""
    values = raw_codes.astype(str).str.strip().str.lower()
    # Only six-digit mainland security identifiers, optionally prefixed sh/sz/bj.
    return values.str.extract(r"^(?:(?:sh|sz|bj))?(\d{6})$", expand=False)


def is_stock_code(code):
    """Exclude funds/indices when providers return other exchange instruments."""
    return bool(re.fullmatch(r"(?:600|601|603|605|688|689|000|001|002|003|300|301|4\d{2}|8\d{2}|92\d)\d{3}", code))


def normalize_market(df, name):
    required = {"代码", "最新价", "涨跌幅", "成交量"}
    if df is None or df.empty or not required.issubset(df.columns):
        raise RuntimeError(f"{name}: expected quote columns missing")
    df = df.copy()
    code = get_codes(df["代码"])
    valid = code.notna() & code.map(lambda x: is_stock_code(x) if isinstance(x, str) else False) & ~code.duplicated()
    df = df.loc[valid].copy()
    df["code"] = code.loc[valid]
    total = len(df)
    bj = int(df["code"].str.match(r"(?:[48]\d{5}|92\d{4})").sum())
    # BJ prefixes such as 4xxxxx/8xxxxx/92xxxxxx are all 6 digits; regex above checks them.
    print(f"{name}: stock listings={total}, Beijing candidates={bj}", flush=True)
    if total < MIN_UNIVERSE or bj < MIN_BJ_LISTINGS:
        raise RuntimeError(f"{name}: incomplete full A-share sample ({total} listings, {bj} BJ listings)")

    close = pd.to_numeric(df["最新价"], errors="coerce")
    pct = pd.to_numeric(df["涨跌幅"], errors="coerce")
    vol = pd.to_numeric(df["成交量"], errors="coerce")
    # Eastmoney supplies hands (lots of 100 shares); Sina supplies shares.
    if name == "Sina":
        vol = vol / 100.0
    ok = close.notna() & close.gt(0) & pct.notna() & vol.notna() & vol.gt(0)
    active = int(ok.sum())
    if active < total * MIN_ACTIVE_COVERAGE:
        raise RuntimeError(f"{name}: too few active quotes ({active}/{total})")
    up = ok & pct.gt(0)
    down = ok & pct.lt(0)
    up_count, down_count = int(up.sum()), int(down.sum())
    up_vol = float(vol[up].sum())
    down_vol = float(vol[down].sum())
    if min(up_count, down_count, up_vol, down_vol) <= 0:
        raise RuntimeError(f"{name}: invalid TRIN inputs")
    trin = round((up_count / down_count) / (up_vol / down_vol), 4)
    if not (0 < trin < 100):
        raise RuntimeError(f"{name}: TRIN out of expected bounds")
    prices = {
        symbol: round(float(value), 4)
        for symbol, value in zip(df.loc[ok, "code"], close.loc[ok])
    }
    return prices, {
        "trin": trin,
        "advancers": up_count,
        "decliners": down_count,
        "up_volume": round(up_vol, 2),
        "down_volume": round(down_vol, 2),
        "active_count": active,
        "total_count": total,
        "data_provider": name,
    }


def fetch_market():
    errors = []
    for source, getter in (("Eastmoney", ak.stock_zh_a_spot_em), ("Sina", ak.stock_zh_a_spot)):
        try:
            df = retry(getter, source, attempts=2)
            return normalize_market(df, source)
        except Exception as exc:
            errors.append(f"{source}: {type(exc).__name__}: {exc}")
            print(f"{source} not usable; trying alternate source if available", flush=True)
    raise RuntimeError("Both quote sources failed: " + " | ".join(errors))


def calculate_breadth(snapshots, current_day, active_count):
    sessions = sorted(snapshots)
    if len(sessions) < 20:
        return None, 0, len(sessions)
    current_prices = snapshots[current_day]
    recent_days = sessions[-KEEP_SNAPSHOTS:]
    above = valid = 0
    for code, close in current_prices.items():
        values = [snapshots[date][code] for date in recent_days if code in snapshots[date]]
        if len(values) < 20:
            continue
        valid += 1
        above += close > sum(values[-20:]) / 20
    if not active_count or valid / active_count < MIN_SMA20_COVERAGE:
        return None, valid, len(sessions)
    return round(100 * above / valid, 2), valid, len(sessions)


def main():
    now = dt.datetime.now(CHINA_TZ)
    today = now.date()
    # Any manual US-evening trigger can be the NEXT MORNING in China.
    # Avoid stamping yesterday's closing snapshot as today's quote.
    if now.hour < 16:
        print(f"China time {now:%Y-%m-%d %H:%M}; before 16:00, skip snapshot. Website deployment can still proceed.")
        return
    if today.weekday() >= 5 or not is_trading_day(today):
        print(f"{today}: non-trading day; keeping previously published data")
        return

    prices, record = fetch_market()
    record["date"] = today.isoformat()
    cache = read_json(CACHE, {"snapshots": {}})
    snapshots = cache.get("snapshots", {})
    if not isinstance(snapshots, dict):
        raise RuntimeError("Invalid cache structure")
    snapshots[today.isoformat()] = prices
    snapshots = {key: snapshots[key] for key in sorted(snapshots)[-KEEP_SNAPSHOTS:]}
    breadth, valid_count, sessions = calculate_breadth(snapshots, today.isoformat(), record["active_count"])
    record.update({
        "breadth": breadth,
        "valid_count": valid_count,
        "warmup_sessions": min(sessions, 20),
        "coverage_pct": round(100 * valid_count / record["active_count"], 1),
    })
    previous = read_json(OUTPUT, {"history": []})
    history_map = {entry["date"]: entry for entry in previous.get("history", []) if "date" in entry}
    history_map[today.isoformat()] = record
    history = [history_map[date] for date in sorted(history_map)[-KEEP_HISTORY:]]
    output = {
        "source": f"AKShare / {record['data_provider']} · 沪深京全A股盘后快照",
        "universe": "沪市 + 深市 + 北交所A股；当天有成交、行情有效的股票",
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "breadth_method": "全A股活跃样本中收盘价高于最近20次有效交易日收盘价均值的比例；覆盖率不低于85%",
        "history": history,
    }
    save_json(CACHE, {"snapshots": snapshots})
    save_json(OUTPUT, output)
    label = f"{breadth}%" if breadth is not None else f"warming up ({sessions}/20 sessions)"
    print(f"{today}: source={record['data_provider']} active={record['active_count']}; TRIN={record['trin']}; breadth={label}")


if __name__ == "__main__":
    main()
