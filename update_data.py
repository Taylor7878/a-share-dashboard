
"""全A股真实行情采集：SMA20市场宽度 + 阿姆斯指数TRIN。

优先尝试公开的每日全市场K线CSV。
备用数据源为AKShare的腾讯、东方财富行情接口。
采集失败时不生成模拟行情，也不覆盖已有的有效数据。
"""

from __future__ import annotations

import datetime as dt
import io
import json
import math
import re
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
CACHE_FILE = DATA_DIR / "price_cache.json"
OUTPUT_FILE = DATA_DIR / "market.json"

CHINA_TZ = ZoneInfo("Asia/Shanghai")
BASE_URL = "https://money-tree.eyunzhu.com/daily_kline/date/"

MIN_LISTINGS = 3500
MIN_BEIJING = 100
MIN_VALID_RATIO = 0.75
MIN_BREADTH_COVERAGE = 0.85
CACHE_LIMIT = 55

HEADERS = {
    "User-Agent": "AshareBreadthResearch/1.0",
    "Accept": "text/csv,text/plain,*/*",
}


def log(*args):
    print(*args, flush=True)


def read_json(path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    temp.replace(path)


def normalize_code(raw):
    s = str(raw).strip().lower().replace(" ", "")
    m = re.fullmatch(
        r"(?:(sh|sz|bj)\.?([0-9]{6})|([0-9]{6})\.(sh|sz|bj)|([0-9]{6}))",
        s,
    )
    if not m:
        return None

    prefix, a, b, suffix, plain = m.groups()
    code = a or b or plain

    sh = ("600", "601", "603", "605", "688", "689")
    sz = ("000", "001", "002", "003", "300", "301")

    if code.startswith(sh):
        market = "sh"
    elif code.startswith(sz):
        market = "sz"
    elif re.fullmatch(r"(?:[48]\d{5}|92\d{4})", code):
        market = "bj"
    else:
        return None

    if (prefix or suffix) and (prefix or suffix) != market:
        return None

    return market + code


def find_column(df, names, required=True):
    mapping = {
        str(c).strip().lower().replace(" ", "").replace("_", ""): c
        for c in df.columns
    }

    for name in names:
        key = str(name).strip().lower().replace(" ", "").replace("_", "")
        if key in mapping:
            return mapping[key]

    if required:
        raise ValueError(
            f"Missing required columns {names}; received {list(df.columns)}"
        )

    return None


def read_csv_bytes(content):
    decoded = None

    for encoding in ("utf-8-sig", "gb18030"):
        try:
            decoded = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue

    if decoded is None:
        raise ValueError("Cannot decode CSV")

    if decoded.lstrip().startswith(("<html", "<!DOCTYPE", "{", "[")):
        raise ValueError("Response is not a daily price CSV")

    if decoded.count("\n") < MIN_LISTINGS:
        raise ValueError("CSV has too few rows")

    first = decoded.splitlines()[0].split(",")
    headerless = (
        len(first) >= 7
        and normalize_code(first[0].strip('"')) is not None
    )

    if headerless:
        df = pd.read_csv(
            io.StringIO(decoded),
            header=None,
            low_memory=False,
        )
        names = [
            "symbol", "date", "open", "close",
            "high", "low", "volume", "amount",
        ]
        df.columns = [
            names[i] if i < len(names) else f"extra{i}"
            for i in range(len(df.columns))
        ]
    else:
        df = pd.read_csv(io.StringIO(decoded), low_memory=False)

    return df


def parse_daily_csv(df, target_date):
    symbol_col = find_column(
        df, ["symbol", "ts_code", "code", "股票代码", "代码"]
    )
    close_col = find_column(
        df, ["close", "收盘", "收盘价", "closeprice"]
    )
    volume_col = find_column(
        df, ["volume", "vol", "成交量", "volume_shares"]
    )
    date_col = find_column(
        df, ["date", "trade_date", "交易日期", "日期"],
        required=False,
    )

    if date_col is not None:
        dates = pd.to_datetime(
            df[date_col].astype(str), errors="coerce"
        ).dt.date
        df = df.loc[dates == target_date].copy()

    codes = df[symbol_col].map(normalize_code)
    valid_codes = codes.notna() & ~codes.duplicated()

    clean = df.loc[valid_codes].copy()
    codes = codes.loc[valid_codes]

    close = pd.to_numeric(clean[close_col], errors="coerce")
    volume = pd.to_numeric(clean[volume_col], errors="coerce")

    valid = close.gt(0) & volume.gt(0)

    total = len(clean)
    active = int(valid.sum())
    bj = int(codes.str.startswith("bj").sum())

    if (
        total < MIN_LISTINGS
        or bj < MIN_BEIJING
        or active < MIN_VALID_RATIO * total
    ):
        raise ValueError(
            f"Incomplete A-share data: total={total}, "
            f"active={active}, Beijing={bj}"
        )

    quotes = {
        symbol: [float(price), float(vol)]
        for symbol, price, vol in zip(
            codes.loc[valid],
            close.loc[valid],
            volume.loc[valid],
        )
    }

    return {
        "quotes": quotes,
        "total": total,
        "active": active,
        "bj": bj,
        "provider": "Money Tree daily CSV",
    }


def fetch_csv(day, session):
    url = BASE_URL + day.strftime("%Y%m%d") + ".csv"

    response = session.get(
        url,
        headers=HEADERS,
        timeout=(10, 30),
    )

    if response.status_code in (404, 410):
        return None

    response.raise_for_status()

    if len(response.content) < 60000:
        raise ValueError("Daily CSV is unexpectedly small")

    df = read_csv_bytes(response.content)
    return parse_daily_csv(df, day)


def collect_csv_history(now, snapshots):
    last_day = (
        now.date()
        if now.hour >= 17
        else now.date() - dt.timedelta(days=1)
    )

    days = [
        last_day - dt.timedelta(days=i)
        for i in range(57)
    ]

    days = [d for d in days if d.weekday() < 5]
    days.sort(reverse=True)

    good = 0
    failures = 0

    with requests.Session() as session:
        for day in days:
            key = day.isoformat()

            if key in snapshots:
                good += 1
                if good >= 23:
                    break
                continue

            try:
                result = fetch_csv(day, session)

                if result is None:
                    continue

                snapshots[key] = result
                good += 1

                log(
                    "[CSV]",
                    key,
                    result["active"],
                    "active stocks",
                    "BJ:",
                    result["bj"],
                )

                if good >= 23:
                    break

                time.sleep(1)

            except Exception as error:
                log("[CSV] Failed:", key, str(error))
                failures += 1

                if failures >= 6 and good == 0:
                    break

    return snapshots


def is_trading_day(day):
    import akshare as ak

    calendar = ak.tool_trade_date_hist_sina()
    dates = set(
        pd.to_datetime(calendar["trade_date"]).dt.date
    )
    return day in dates


def parse_spot(df, provider):
    code_col = find_column(
        df, ["代码", "code", "symbol", "stock_code"]
    )
    price_col = find_column(
        df, ["最新价", "price", "trade", "current_price", "now"]
    )
    volume_col = find_column(
        df, ["成交量", "volume", "vol", "total_volume"]
    )

    pct_col = find_column(
        df,
        ["涨跌幅", "change_percent", "pct_chg", "percent"],
        required=False,
    )

    prev_col = find_column(
        df,
        ["昨收", "pre_close", "prev_close"],
        required=False,
    )

    if pct_col is None and prev_col is None:
        raise ValueError("No price-change field available")

    codes = df[code_col].map(normalize_code)
    mask = codes.notna() & ~codes.duplicated()

    work = df.loc[mask].copy()
    codes = codes.loc[mask]

    price = pd.to_numeric(work[price_col], errors="coerce")
    volume = pd.to_numeric(work[volume_col], errors="coerce")

    if pct_col is not None:
        pct = pd.to_numeric(
            work[pct_col].astype(str).str.replace(
                "%", "", regex=False
            ),
            errors="coerce",
        )
    else:
        previous = pd.to_numeric(
            work[prev_col], errors="coerce"
        )
        pct = (price / previous - 1) * 100

    valid = price.gt(0) & volume.gt(0) & pct.notna()

    total = len(work)
    active = int(valid.sum())
    bj = int(codes.str.startswith("bj").sum())

    if (
        total < MIN_LISTINGS
        or bj < MIN_BEIJING
        or active < MIN_VALID_RATIO * total
    ):
        raise ValueError(
            f"{provider}: incomplete market coverage "
            f"total={total}, active={active}, BJ={bj}"
        )

    quotes = {
        symbol: [float(p), float(v)]
        for symbol, p, v in zip(
            codes.loc[valid],
            price.loc[valid],
            volume.loc[valid],
        )
    }

    changes = {
        symbol: float(p)
        for symbol, p in zip(
            codes.loc[valid],
            pct.loc[valid],
        )
    }

    return {
        "quotes": quotes,
        "pct": changes,
        "total": total,
        "active": active,
        "bj": bj,
        "provider": provider,
    }


def collect_spot(now, snapshots):
    if now.hour < 17 or now.weekday() >= 5:
        return None

    try:
        if not is_trading_day(now.date()):
            return None
    except Exception as error:
        log("Cannot confirm trading day:", error)
        return None

    import akshare as ak

    providers = [
        ("Tencent", ak.stock_zh_a_spot_tx),
        ("Eastmoney", ak.stock_zh_a_spot_em),
    ]

    for name, fetch in providers:
        try:
            result = parse_spot(fetch(), name)

            previous_days = [
                day for day in snapshots
                if day < now.date().isoformat()
            ]

            if previous_days:
                previous = snapshots[max(previous_days)]["quotes"]
                overlap = [
                    (v[0], previous[k][0])
                    for k, v in result["quotes"].items()
                    if k in previous
                ]

                if len(overlap) >= 3000:
                    changed = sum(
                        abs(a - b) > 0.0001
                        for a, b in overlap
                    )

                    if changed < 500:
                        raise ValueError(
                            "Possible stale market snapshot"
                        )

            log(
                "[SPOT]", name,
                result["active"], "stocks"
            )
            return result

        except Exception as error:
            log("[SPOT]", name, "failed:", error)

    return None


def calculate_day(date, entry, previous, recent):
    current = entry["quotes"]
    pct = entry.get("pct")

    if pct is None:
        if previous is None:
            return None

        old = previous["quotes"]
        pct = {
            k: 100 * (v[0] / old[k][0] - 1)
            for k, v in current.items()
            if k in old and old[k][0] > 0
        }

    up_count = 0
    down_count = 0
    up_volume = 0.0
    down_volume = 0.0

    for symbol, values in current.items():
        change = pct.get(symbol)

        if change is None or not math.isfinite(change):
            continue

        volume = values[1]

        if change > 0:
            up_count += 1
            up_volume += volume
        elif change < 0:
            down_count += 1
            down_volume += volume

    if min(
        up_count,
        down_count,
        up_volume,
        down_volume,
    ) <= 0:
        return None

    trin = (
        (up_count / down_count)
        / (up_volume / down_volume)
    )

    if not 0.01 < trin < 40:
        return None

    above = 0
    eligible = 0

    if len(recent) >= 20:
        for symbol, values in current.items():
            prices = [
                day["quotes"][symbol][0]
                for day in recent
                if symbol in day["quotes"]
            ]

            if len(prices) < 20:
                continue

            sma20 = sum(prices[-20:]) / 20
            eligible += 1

            if values[0] > sma20:
                above += 1

    coverage = (
        eligible / entry["active"]
        if entry["active"] else 0
    )

    breadth = None

    if (
        eligible > 0
        and coverage >= MIN_BREADTH_COVERAGE
    ):
        breadth = round(
            100 * above / eligible, 2
        )

    return {
        "date": date,
        "trin": round(trin, 4),
        "breadth": breadth,
        "advancers": up_count,
        "decliners": down_count,
        "up_volume": round(up_volume, 3),
        "down_volume": round(down_volume, 3),
        "active_count": entry["active"],
        "total_count": entry["total"],
        "beijing_count": entry["bj"],
        "valid_count": eligible,
        "coverage_pct": round(coverage * 100, 1),
        "warmup_sessions": min(len(recent), 20),
        "data_provider": entry["provider"],
        "price_basis": "unadjusted close",
        "volume_basis": "provider-native",
    }


def build_history(snapshots):
    dates = sorted(snapshots)
    history = []

    for i, day in enumerate(dates):
        previous = (
            snapshots[dates[i - 1]]
            if i > 0 else None
        )

        recent = [
            snapshots[k]
            for k in dates[max(0, i - 39):i + 1]
        ]

        result = calculate_day(
            day,
            snapshots[day],
            previous,
            recent,
        )

        if result is not None:
            history.append(result)

    return history[-250:]


def main():
    now = dt.datetime.now(CHINA_TZ)

    log(
        "China time:",
        now.strftime("%Y-%m-%d %H:%M:%S")
    )

    saved = read_json(
        CACHE_FILE, {"snapshots": {}}
    )

    snapshots = saved.get("snapshots", {})

    snapshots = collect_csv_history(
        now, snapshots
    )

    completed = (
        now.date()
        if now.hour >= 17
        else now.date() - dt.timedelta(days=1)
    )

    latest = max(snapshots) if snapshots else None

    if (
        completed == now.date()
        and latest != now.date().isoformat()
    ):
        spot = collect_spot(now, snapshots)

        if spot is not None:
            snapshots[now.date().isoformat()] = spot

    if not snapshots:
        raise RuntimeError(
            "No verified real A-share market data available"
        )

    dates = sorted(snapshots)[-CACHE_LIMIT:]
    snapshots = {
        key: snapshots[key]
        for key in dates
    }

    history = build_history(snapshots)

    if not history:
        raise RuntimeError(
            "Insufficient real historical quotes for TRIN"
        )

    last_date = dt.date.fromisoformat(
        history[-1]["date"]
    )

    age = (completed - last_date).days

    if age > 7:
        raise RuntimeError(
            f"Market data too old: {last_date}"
        )

    latest_record = history[-1]

    payload = {
        "source": (
            "REAL - "
            + latest_record["data_provider"]
        ),
        "status": "real",
        "updated_at": dt.datetime.now(
            dt.timezone.utc
        ).isoformat(),
        "asof": last_date.isoformat(),
        "universe": (
            "Shanghai, Shenzhen and Beijing A-shares"
        ),
        "history": history,
    }

    write_json(
        CACHE_FILE,
        {"snapshots": snapshots},
    )

    write_json(
        OUTPUT_FILE,
        payload,
    )

    log(
        "PUBLISHED REAL:",
        last_date,
        "stocks=",
        latest_record["active_count"],
        "TRIN=",
        latest_record["trin"],
        "SMA20 breadth=",
        latest_record["breadth"],
    )


if __name__ == "__main__":
    main()
