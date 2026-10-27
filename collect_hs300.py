
"""沪深300真实历史行情自动采集。

数据源优先级：
1. 腾讯证券
2. 新浪财经
3. Yahoo Finance

不生成模拟数据。
所有数据源失败时保留旧文件。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import akshare as ak
import pandas as pd
import requests


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "data" / "hs300.json"

SHANGHAI = ZoneInfo("Asia/Shanghai")

MIN_ROWS = 60
MAX_AGE_DAYS = 15


def normalize(frame):

    names = {
        str(c).strip().lower(): c
        for c in frame.columns
    }

    date_key = names.get(
        "date", names.get("日期")
    )

    close_key = names.get(
        "close", names.get("收盘")
    )

    if date_key is None or close_key is None:
        raise ValueError(
            f"Unexpected columns: {frame.columns.tolist()}"
        )

    x = pd.DataFrame({
        "date": pd.to_datetime(
            frame[date_key],
            errors="coerce"
        ),
        "close": pd.to_numeric(
            frame[close_key],
            errors="coerce"
        ),
    }).dropna()

    x = x[
        (x["close"] > 500)
        & (x["close"] < 20000)
    ]

    x = x.drop_duplicates(
        subset=["date"],
        keep="last"
    )

    x = x.sort_values("date").tail(260)

    values = [
        {
            "date": day.strftime("%Y-%m-%d"),
            "close": round(float(close), 4)
        }
        for day, close in zip(
            x["date"], x["close"]
        )
    ]

    if len(values) < MIN_ROWS:
        raise ValueError(
            f"Only {len(values)} valid CSI300 trading days"
        )

    now = dt.datetime.now(
        SHANGHAI
    ).date()

    asof = dt.date.fromisoformat(
        values[-1]["date"]
    )

    if (
        asof > now
        or (now - asof).days > MAX_AGE_DAYS
    ):
        raise ValueError(
            f"Index history is stale or future dated: {asof}"
        )

    return values


def from_tencent():

    end = dt.datetime.now(
        SHANGHAI
    ).date()

    start = end - dt.timedelta(
        days=470
    )

    return ak.stock_zh_index_daily_tx(
        symbol="sh000300",
        start_date=start.strftime("%Y%m%d"),
        end_date=end.strftime("%Y%m%d")
    )


def from_sina():

    return ak.stock_zh_index_daily(
        symbol="sh000300"
    )


def from_yahoo():

    url = (
        "https://query1.finance.yahoo.com"
        "/v8/finance/chart/000300.SS"
    )

    response = requests.get(
        url,
        params={
            "range": "2y",
            "interval": "1d"
        },
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json"
        },
        timeout=25
    )

    response.raise_for_status()

    payload = response.json()["chart"]

    if payload.get("error"):
        raise ValueError(
            str(payload["error"])
        )

    block = payload["result"][0]

    timestamps = block["timestamp"]

    closes = block[
        "indicators"
    ]["quote"][0]["close"]

    observations = []

    for stamp, close in zip(
        timestamps, closes
    ):

        if close is None:
            continue

        local_day = dt.datetime.fromtimestamp(
            stamp,
            tz=dt.timezone.utc
        ).astimezone(
            SHANGHAI
        ).date()

        observations.append({
            "date": local_day.isoformat(),
            "close": close
        })

    return pd.DataFrame(
        observations
    )


def main():

    attempts = [
        (
            "Tencent via AKShare",
            from_tencent
        ),
        (
            "Sina via AKShare",
            from_sina
        ),
        (
            "Yahoo Finance 000300.SS",
            from_yahoo
        )
    ]

    errors = []

    for name, loader in attempts:

        print(
            "Trying CSI300 provider:",
            name,
            flush=True
        )

        try:

            prices = normalize(
                loader()
            )

            result = {
                "symbol": "000300",
                "name": "沪深300指数",
                "source": name,
                "updated_at": dt.datetime.now(
                    dt.timezone.utc
                ).isoformat(),
                "asof": prices[-1]["date"],
                "history": prices
            }

            OUTPUT.parent.mkdir(
                parents=True,
                exist_ok=True
            )

            staging = OUTPUT.with_suffix(
                ".json.tmp"
            )

            staging.write_text(
                json.dumps(
                    result,
                    ensure_ascii=False,
                    indent=2
                ),
                encoding="utf-8"
            )

            staging.replace(
                OUTPUT
            )

            print(
                "HS300_PUBLISHED",
                "provider=", name,
                "date=", prices[-1]["date"],
                "close=", prices[-1]["close"],
                "rows=", len(prices),
                flush=True
            )

            return

        except Exception as exc:

            detail = (
                f"{name}: "
                f"{type(exc).__name__}: "
                f"{exc}"
            )

            errors.append(
                detail
            )

            print(
                "Provider failed:",
                detail[:320],
                flush=True
            )

    raise RuntimeError(
        "CSI300: all real providers failed. "
        + " | ".join(errors)
    )


if __name__ == "__main__":
    main()
