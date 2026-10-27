
"""真实A股全市场 SMA20/SMA60 宽度 + TRIN，每日CSV历史回补（含沪深京、ST）。

Usage: python update_data.py
数据来自 Money Tree 第三方逐日全市场 CSV。
严格按交易所日历计算窗口。
行情缺失时输出空值而非伪造数值。
首次运行尝试获取82个历史交易日。
"""
from __future__ import annotations

import datetime as dt
import gzip
import io
import json
import math
import re
import statistics
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent
DATADIR = ROOT / "data"
MARKET = DATADIR / "market.json"
CACHE = DATADIR / "snapshots_v4.json.gz"
TZ = ZoneInfo("Asia/Shanghai")

SCHEMA = 4

DAILY_URL = (
    "https://money-tree.eyunzhu.com/daily_kline/date/{date}.csv"
)
STOCK_LIST_URL = (
    "https://money-tree.eyunzhu.com/stocks/stocks_list.csv"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; AshareBreadthDashboard/4.0)",
    "Accept": "text/csv,*/*"
}

# 市场覆盖率检查
# 不按股票名称排除 ST 和 *ST
MIN_MARKET_COUNTS = {
    "sh": 1900,
    "sz": 2400,
    "bj": 300
}

MIN_ROW_VALID = 0.94
MIN_BREADTH_COVERAGE = 0.85
MIN_CLASSIFY_COVERAGE = 0.90

SESSIONS_TO_FETCH = 82
MAX_REQUESTS_PER_RUN = 100
CACHE_SESSIONS = 90
REQUEST_DELAY = 0.8


def log(*parts):
    print(*parts, flush=True)


def read_cache():
    if not CACHE.exists():
        return {}

    try:
        with gzip.open(CACHE, "rt", encoding="utf-8") as f:
            parsed = json.load(f)

        if parsed.get("schema_version") != SCHEMA:
            log("CACHE: incompatible historical cache ignored")
            return {}

        return parsed.get("days", {})

    except (OSError, ValueError, TypeError) as exc:
        log("::warning::Could not load cache:", str(exc))
        return {}


def save_cache(snapshots):
    DATADIR.mkdir(parents=True, exist_ok=True)

    temp = CACHE.with_name(CACHE.name + ".tmp")

    with gzip.open(
        temp, "wt", encoding="utf-8", compresslevel=6
    ) as f:
        json.dump(
            {
                "schema_version": SCHEMA,
                "days": snapshots
            },
            f,
            ensure_ascii=False,
            separators=(",", ":")
        )

    temp.replace(CACHE)


def write_market(payload):
    DATADIR.mkdir(parents=True, exist_ok=True)

    tmp = MARKET.with_suffix(".json.tmp")

    tmp.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":")
        ),
        encoding="utf-8"
    )

    tmp.replace(MARKET)


def stock_code(value):
    s = str(value).strip().lower().replace(" ", "")

    hit = re.fullmatch(
        r"(?:(sh|sz|bj)\.?([0-9]{6})|([0-9]{6})\.(sh|sz|bj)|([0-9]{6}))",
        s
    )

    if not hit:
        return None

    before, a, b, after, raw = hit.groups()
    code = a or b or raw

    if re.fullmatch(r"(?:60[0135]|68[89])\d{3}", code):
        market = "sh"
    elif re.fullmatch(r"(?:00[0-3]|30[01])\d{3}", code):
        market = "sz"
    elif re.fullmatch(r"(?:[48]\d{5}|92\d{4})", code):
        market = "bj"
    else:
        return None

    if (before or after) and (before or after) != market:
        return None

    return market + code


def find_col(df, alternatives, required=True):
    cols = {
        re.sub(r"[\s_]+", "", str(c).lower()): c
        for c in df.columns
    }

    for name in alternatives:
        column = cols.get(
            re.sub(r"[\s_]+", "", name.lower())
        )

        if column is not None:
            return column

    if required:
        raise ValueError(
            "Missing CSV columns: "
            + repr(alternatives)
            + " available="
            + repr(list(df.columns))
        )

    return None


def csv_to_df(content):
    text = None

    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue

    if (
        text is None
        or text.lstrip().lower().startswith(
            ("<html", "<!doctype", "{")
        )
    ):
        raise ValueError("Provider response is not CSV")

    first = text.splitlines()[0].split(",")

    headerless = (
        len(first) >= 7
        and stock_code(first[0].strip('"')) is not None
    )

    df = pd.read_csv(
        io.StringIO(text),
        header=None if headerless else "infer",
        low_memory=False,
        dtype=str
    )

    if headerless:
        names = [
            "symbol", "date", "open", "close",
            "high", "low", "volume", "amount"
        ]

        df.columns = [
            names[i] if i < len(names) else f"extra{i}"
            for i in range(len(df.columns))
        ]

    return df


def guarded_get(session, url, *, optional=False):
    last_error = None

    for attempt in range(4):
        try:
            response = session.get(
                url,
                timeout=(9, 36),
                headers=HEADERS
            )

            if (
                optional
                and response.status_code in (404, 410)
            ):
                return None

            if response.status_code in (
                429, 500, 502, 503, 504
            ):
                raise requests.HTTPError(
                    f"HTTP {response.status_code}",
                    response=response
                )

            response.raise_for_status()

            if len(response.content) < 30000:
                raise ValueError(
                    "Unexpected small CSV response"
                )

            return response.content

        except (
            requests.RequestException,
            ValueError
        ) as exc:
            last_error = exc

            if attempt < 3:
                time.sleep(
                    min(
                        12,
                        2.5 * (attempt + 1) ** 2
                    )
                )

    raise RuntimeError(
        f"CSV request failed: "
        f"{type(last_error).__name__}: {last_error}"
    )


def stock_names(session):
    try:
        blob = guarded_get(
            session, STOCK_LIST_URL
        )

        df = csv_to_df(blob)

        codecol = find_col(
            df,
            [
                "代码", "股票代码",
                "code", "symbol", "ts_code"
            ]
        )

        namecol = find_col(
            df,
            [
                "名称", "证券简称",
                "股票名称", "name", "stock_name"
            ]
        )

        names = {}

        for raw, name in zip(
            df[codecol], df[namecol]
        ):
            code = stock_code(raw)

            if code:
                names[code] = str(name).strip()

        return names

    except Exception as exc:
        log(
            "::warning::No ST-name audit list:",
            exc
        )
        return {}


def volume_scale(prices, volumes, amounts):
    """检查沪深京成交量单位是否一致。"""

    med = {}

    for market in ("sh", "sz", "bj"):
        ratios = [
            amounts[c] / (p * volumes[c])
            for c, p in prices.items()
            if (
                c.startswith(market)
                and p > 0
                and volumes[c] > 0
                and amounts[c] > 0
            )
        ]

        if len(ratios) < 100:
            raise ValueError(
                f"Not enough {market} samples "
                "to audit units"
            )

        med[market] = float(
            statistics.median(ratios)
        )

    baseline = min(med.values())
    factors = {}

    for market, ratio in med.items():
        relative = ratio / baseline

        if 0.7 < relative < 1.43:
            factors[market] = 1

        elif 70 < relative < 143:
            factors[market] = 100

        else:
            raise ValueError(
                "Unverifiable cross-exchange "
                "unit ratios: " + repr(med)
            )

    return factors, med


def parse_one_day(df, date, names):
    sym = find_col(
        df,
        [
            "symbol", "ts_code",
            "code", "股票代码", "代码"
        ]
    )

    price = find_col(
        df,
        [
            "close", "close_price",
            "收盘", "收盘价"
        ]
    )

    volume = find_col(
        df,
        ["volume", "vol", "成交量"]
    )

    amount = find_col(
        df,
        [
            "amount", "turnover",
            "成交额", "成交金额"
        ]
    )

    datecol = find_col(
        df,
        [
            "date", "trade_date",
            "交易日期", "日期"
        ],
        False
    )

    namecol = find_col(
        df,
        [
            "name", "股票简称",
            "证券简称", "名称"
        ],
        False
    )

    pctcol = find_col(
        df,
        [
            "pct_change", "pct_chg",
            "change_percent", "涨跌幅"
        ],
        False
    )

    prevcol = find_col(
        df,
        [
            "pre_close", "prev_close",
            "昨收", "昨收价"
        ],
        False
    )

    if datecol is not None:
        parsed = pd.to_datetime(
            df[datecol].astype(str),
            errors="coerce"
        ).dt.date

        df = df.loc[
            parsed == date
        ].copy()

    keys = df[sym].map(stock_code)

    keep = (
        keys.notna()
        & ~keys.duplicated()
    )

    df = df.loc[keep].copy()
    keys = keys.loc[keep]

    p = pd.to_numeric(
        df[price],
        errors="coerce"
    )

    v = pd.to_numeric(
        df[volume],
        errors="coerce"
    )

    a = pd.to_numeric(
        df[amount],
        errors="coerce"
    )

    valid = (
        p.gt(0)
        & v.gt(0)
        & a.gt(0)
    )

    total = len(df)

    exch = {
        m: int(keys.str.startswith(m).sum())
        for m in ("sh", "sz", "bj")
    }

    if any(
        exch[m] < MIN_MARKET_COUNTS[m]
        for m in exch
    ):
        raise ValueError(
            f"Incomplete SH/SZ/BJ universe {exch}"
        )

    if (
        total == 0
        or valid.sum() / total < MIN_ROW_VALID
    ):
        raise ValueError(
            f"Insufficient active quotes "
            f"{valid.sum()}/{total}"
        )

    symbols = keys.loc[valid]

    prices = dict(
        zip(
            symbols,
            p.loc[valid].astype(float)
        )
    )

    volumes = dict(
        zip(
            symbols,
            v.loc[valid].astype(float)
        )
    )

    amounts = dict(
        zip(
            symbols,
            a.loc[valid].astype(float)
        )
    )

    factors, medians = volume_scale(
        prices, volumes, amounts
    )

    quotes = {
        c: [
            round(prices[c], 4),
            round(
                volumes[c] * factors[c[:2]],
                3
            ),
            round(amounts[c], 2)
        ]
        for c in prices
    }

    pct = {}

    if pctcol is not None:
        changes = pd.to_numeric(
            df.loc[valid, pctcol]
              .astype(str)
              .str.replace(
                  "%", "", regex=False
              ),
            errors="coerce"
        )

        pct = {
            c: float(x)
            for c, x in zip(
                symbols, changes
            )
            if pd.notna(x)
        }

    elif prevcol is not None:
        prev = pd.to_numeric(
            df.loc[valid, prevcol],
            errors="coerce"
        )

        pct = {
            c: (
                prices[c] / float(x) - 1
            ) * 100
            for c, x in zip(
                symbols, prev
            )
            if (
                pd.notna(x)
                and x > 0
            )
        }

    named = {}

    if namecol is not None:
        named = {
            c: str(n)
            for c, n in zip(
                symbols,
                df.loc[valid, namecol]
            )
        }

    matches = sum(
        c in named or c in names
        for c in quotes
    )

    has_names = (
        matches >= 0.95 * len(quotes)
    )

    st_codes = {
        c
        for c in quotes
        if re.search(
            r"(?:\*\s*ST|ST)",
            named.get(
                c, names.get(c, "")
            ),
            re.I
        )
    }

    stats = {
        m: {
            "listings": exch[m],
            "active": sum(
                c.startswith(m)
                for c in quotes
            ),
            "st": (
                sum(
                    c.startswith(m)
                    for c in st_codes
                )
                if has_names else None
            ),
            "volume_factor": factors[m],
            "amount_volume_ratio": round(
                medians[m], 3
            )
        }
        for m in exch
    }

    return {
        "schema_version": SCHEMA,
        "quotes": quotes,
        "pct": pct,
        "total": total,
        "active": len(quotes),
        "exchange_stats": stats,
        "st_count": (
            len(st_codes)
            if has_names else None
        ),
        "st_audit_available": has_names,
        "provider": "Money Tree CSV"
    }


def stock_sessions(
    today,
    n=SESSIONS_TO_FETCH
):
    cal = xcals.get_calendar("XSHG")

    day = min(
        pd.Timestamp(today),
        cal.last_session
    )

    beg = day - pd.Timedelta(days=210)

    sessions = cal.sessions_in_range(
        beg, day
    )

    return [
        x.date().isoformat()
        for x in sessions
    ][-n:]


def backfill(
    target,
    existing,
    session,
    names
):
    results = {
        day: row
        for day, row in existing.items()
        if (
            row.get("schema_version") == SCHEMA
            and day in target
        )
    }

    attempted = 0
    newly = 0

    # 最新交易日优先回补
    for day in reversed(target):
        if day in results:
            continue

        if attempted >= MAX_REQUESTS_PER_RUN:
            break

        attempted += 1

        try:
            blob = guarded_get(
                session,
                DAILY_URL.format(
                    date=day.replace("-", "")
                ),
                optional=True
            )

            if blob is None:
                log(
                    "::warning::Missing "
                    "archived trading session",
                    day
                )
                continue

            row = parse_one_day(
                csv_to_df(blob),
                dt.date.fromisoformat(day),
                names
            )

            results[day] = row
            newly += 1

            log(
                "ARCHIVE_REAL",
                day,
                row["active"],
                "SH/SZ/BJ",
                {
                    k: v["active"]
                    for k, v in
                    row["exchange_stats"].items()
                }
            )

            if newly % 8 == 0:
                save_cache(results)

            time.sleep(REQUEST_DELAY)

        except Exception as exc:
            log(
                "::warning::BACKFILL_FAILED",
                day,
                str(exc)[:250]
            )

    log(
        "BACKFILL_SUMMARY",
        f"verified={len(results)}/{len(target)}",
        f"new={newly}",
        f"attempts={attempted}"
    )

    save_cache(results)

    return results


def breadth_for_window(
    day,
    snapshots,
    sessions,
    window
):
    loc = sessions.index(day)

    expected = sessions[
        max(0, loc - window + 1):
        loc + 1
    ]

    present = [
        x for x in expected
        if x in snapshots
    ]

    count = len(present)

    info = {
        "sessions": count,
        "required": window,
        "coverage_pct": 0.0,
        "eligible": 0,
        "above": None,
        "missing_dates": [
            x for x in expected
            if x not in snapshots
        ]
    }

    if (
        len(expected) != window
        or count != window
    ):
        return None, info

    quotes = [
        snapshots[x]["quotes"]
        for x in expected
    ]

    latest = quotes[-1]

    eligible = 0
    above = 0

    for code, (close, _, _) in latest.items():
        if all(
            code in historical
            for historical in quotes
        ):
            eligible += 1

            sma = (
                sum(
                    historical[code][0]
                    for historical in quotes
                ) / window
            )

            if close > sma:
                above += 1

    coverage = (
        eligible / len(latest)
        if latest else 0
    )

    info.update({
        "coverage_pct": round(
            coverage * 100, 2
        ),
        "eligible": eligible,
        "above": above
    })

    if coverage < MIN_BREADTH_COVERAGE:
        return None, info

    return round(
        100 * above / eligible, 2
    ), info


def trin_from_day(
    day,
    snapshots,
    sessions
):
    at = sessions.index(day)
    today = snapshots[day]

    previous = (
        snapshots.get(
            sessions[at - 1]
        )
        if at else None
    )

    change = today.get("pct") or {}

    method = (
        "provider_pct"
        if len(change) >= (
            MIN_CLASSIFY_COVERAGE
            * today["active"]
        )
        else None
    )

    if method is None:
        if previous is None:
            return None

        old = previous["quotes"]

        change = {
            c: 100 * (
                v[0] / old[c][0] - 1
            )
            for c, v in
            today["quotes"].items()
            if (
                c in old
                and old[c][0] > 0
            )
        }

        method = (
            "previous_official_session_close"
        )

    parts = {
        m: {
            "up": 0,
            "down": 0,
            "flat": 0,
            "up_vol": 0.0,
            "down_vol": 0.0,
            "up_amt": 0.0,
            "down_amt": 0.0
        }
        for m in ("sh", "sz", "bj")
    }

    count = 0

    for code, (_, vol, amt) in (
        today["quotes"].items()
    ):
        delta = change.get(code)

        if (
            delta is None
            or not math.isfinite(delta)
        ):
            continue

        count += 1
        x = parts[code[:2]]

        if delta > 1e-8:
            x["up"] += 1
            x["up_vol"] += vol
            x["up_amt"] += amt

        elif delta < -1e-8:
            x["down"] += 1
            x["down_vol"] += vol
            x["down_amt"] += amt

        else:
            x["flat"] += 1

    if count < (
        MIN_CLASSIFY_COVERAGE
        * today["active"]
    ):
        return None

    up = sum(
        x["up"] for x in parts.values()
    )

    down = sum(
        x["down"] for x in parts.values()
    )

    up_vol = sum(
        x["up_vol"] for x in parts.values()
    )

    down_vol = sum(
        x["down_vol"] for x in parts.values()
    )

    up_amt = sum(
        x["up_amt"] for x in parts.values()
    )

    down_amt = sum(
        x["down_amt"] for x in parts.values()
    )

    if min(
        up, down,
        up_vol, down_vol,
        up_amt, down_amt
    ) <= 0:
        return None

    return {
        "trin": round(
            up * down_vol / (
                down * up_vol
            ), 4
        ),
        "trin_amount": round(
            up * down_amt / (
                down * up_amt
            ), 4
        ),
        "advancers": up,
        "decliners": down,
        "unchanged": sum(
            x["flat"]
            for x in parts.values()
        ),
        "up_volume": round(
            up_vol, 3
        ),
        "down_volume": round(
            down_vol, 3
        ),
        "up_amount": round(
            up_amt, 2
        ),
        "down_amount": round(
            down_amt, 2
        ),
        "pct_method": method,
        "daily_classification": parts
    }


def build_history(
    snapshots,
    sessions
):
    records = []

    for day in sessions:
        if day not in snapshots:
            continue

        row = snapshots[day]

        b20, i20 = breadth_for_window(
            day,
            snapshots,
            sessions,
            20
        )

        b60, i60 = breadth_for_window(
            day,
            snapshots,
            sessions,
            60
        )

        trin = trin_from_day(
            day,
            snapshots,
            sessions
        )

        record = {
            "date": day,
            "breadth": b20,
            "breadth20": b20,
            "breadth60": b60,
            "sma20": i20,
            "sma60": i60,
            "warmup_sessions": i20["sessions"],
            "active_count": row["active"],
            "total_count": row["total"],
            "exchange_stats": row["exchange_stats"],
            "st_count": row["st_count"],
            "st_audit_available": row[
                "st_audit_available"
            ],
            "data_provider": row["provider"],
            "price_basis": "unadjusted close",
            "volume_basis": (
                "cross-exchange audited volume"
            ),
            **(trin or {})
        }

        records.append(record)

    return records


def main():
    now = dt.datetime.now(TZ)

    cutoff = (
        now.date()
        if now.hour >= 17
        else now.date() - dt.timedelta(days=1)
    )

    sessions = stock_sessions(cutoff)

    if not sessions:
        raise RuntimeError(
            "Cannot determine exchange trading calendar"
        )

    snapshots = read_cache()

    snapshots = {
        d: row
        for d, row in snapshots.items()
        if d in sessions
    }

    log(
        "START",
        now.isoformat(),
        "exchange sessions",
        len(sessions),
        "latest",
        sessions[-1]
    )

    with requests.Session() as session:
        names = stock_names(session)

        snapshots = backfill(
            sessions,
            snapshots,
            session,
            names
        )

    history = build_history(
        snapshots,
        sessions
    )

    latest = (
        history[-1]
        if history else None
    )

    true_asof = (
        latest["date"]
        if latest else None
    )

    latest_asof = sessions[-1]

    fresh = (
        true_asof == latest_asof
    )

    status = (
        "real"
        if fresh
        else (
            "stale"
            if history
            else "unavailable"
        )
    )

    payload = {
        "schema_version": SCHEMA,
        "status": status,
        "source": (
            "Money Tree full-market daily CSV "
            "(third-party)"
        ),
        "updated_at": dt.datetime.now(
            dt.timezone.utc
        ).isoformat(),
        "asof": true_asof,
        "expected_session": latest_asof,
        "universe": (
            "Shanghai, Shenzhen, Beijing "
            "A shares including ST and *ST"
        ),
        "disclaimer": (
            "Unadjusted closing prices; "
            "corporate actions can affect "
            "moving-average breadth."
        ),
        "session_count": len(snapshots),
        "target_sessions": len(sessions),
        "history": history
    }

    write_market(payload)

    log(
        "MARKET_DATA_STATUS",
        status,
        "asof", true_asof,
        "expected", latest_asof
    )

    if latest:
        log(
            "LATEST_REAL",
            latest["date"],
            "SMA20=", latest["breadth20"],
            "SMA60=", latest["breadth60"],
            "TRIN=", latest.get("trin"),
            "20 sessions=",
            latest["sma20"]["sessions"],
            "60 sessions=",
            latest["sma60"]["sessions"]
        )

    if (
        latest
        and fresh
        and latest["breadth20"] is not None
        and latest["breadth60"] is not None
    ):
        log(
            "PUBLISHED_REAL_SMA20_AND_SMA60"
        )
    else:
        log(
            "::warning::PENDING_BACKFILL: "
            "unavailable breadth stays blank "
            "until verified history is complete"
        )


if __name__ == "__main__":
    main()
