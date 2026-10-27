
"""Fetch real CSI 300 daily closes into data/hs300.json."""
import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import akshare as ak
import pandas as pd

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "data" / "hs300.json"
CHINA_TIME = ZoneInfo("Asia/Shanghai")


def standardize(df):
    date_col = "日期" if "日期" in df.columns else "date"
    close_col = "收盘" if "收盘" in df.columns else "close"

    if date_col not in df.columns or close_col not in df.columns:
        raise ValueError(
            f"Unexpected index columns: {df.columns.tolist()}"
        )

    x = pd.DataFrame({
        "date": pd.to_datetime(
            df[date_col], errors="coerce"
        ),
        "close": pd.to_numeric(
            df[close_col], errors="coerce"
        ),
    }).dropna()

    x = x[x["close"] > 0]
    x = x.drop_duplicates("date")
    x = x.sort_values("date").tail(260)

    return [
        {
            "date": day.strftime("%Y-%m-%d"),
            "close": round(float(close), 4)
        }
        for day, close in zip(x["date"], x["close"])
    ]


def main():
    today = dt.datetime.now(CHINA_TIME).date()

    start = (
        today - dt.timedelta(days=450)
    ).strftime("%Y%m%d")

    end = today.strftime("%Y%m%d")

    try:
        frame = ak.index_zh_a_hist(
            symbol="000300",
            period="daily",
            start_date=start,
            end_date=end
        )

        source = "AKShare / Eastmoney CSI300 daily"
        values = standardize(frame)

        if len(values) < 30:
            raise ValueError("Too few CSI300 rows")

    except Exception as first_error:
        print(
            "Primary source failed:",
            repr(first_error),
            flush=True
        )

        frame = ak.stock_zh_index_daily_em(
            symbol="sh000300"
        )

        values = standardize(frame)
        source = "AKShare / alternate CSI300 daily"

    if len(values) < 30:
        raise RuntimeError(
            "Not enough verified CSI300 historical rows"
        )

    if values[-1]["date"] > today.isoformat():
        raise RuntimeError(
            "Future-dated index quote rejected"
        )

    payload = {
        "source": source,
        "symbol": "000300",
        "name": "沪深300指数",
        "updated_at": dt.datetime.now(
            dt.timezone.utc
        ).isoformat(),
        "history": values
    }

    OUTPUT.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    OUTPUT.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )

    print(
        "HS300_PUBLISHED",
        values[-1],
        "rows=",
        len(values),
        flush=True
    )


if __name__ == "__main__":
    main()
