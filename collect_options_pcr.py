
"""真实沪深ETF期权PCR采集程序。

数据来源：
上交所ETF期权每日统计
深交所ETF期权每日统计

公式：
加权PCR = 全部认沽成交量 / 全部认购成交量

等价于按认购成交量加权各ETF的PCR。

注意：尚未纳入中金所股指期权和商品期权，
因此不能称为全市场期权PCR。
"""

from __future__ import annotations

import datetime as dt
import json
import math
import time
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

MARKET = DATA / "market.json"
OUTPUT = DATA / "options_pcr.json"
CACHE = DATA / "options_etf_cache.json"

SSE_URLS = [
    "https://query.sse.com.cn/commonQuery.do",
    "http://query.sse.com.cn/commonQuery.do",
]

SZSE_URL = (
    "https://investor.szse.cn"
    "/api/report/ShowReport/data"
)


def read_json(path, default):
    if not path.exists():
        return default

    try:
        return json.loads(
            path.read_text(encoding="utf-8")
        )
    except (ValueError, OSError):
        return default


def save_json(path, obj):
    path.parent.mkdir(
        parents=True, exist_ok=True
    )

    temp = path.with_name(
        path.name + ".tmp"
    )

    temp.write_text(
        json.dumps(
            obj,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )

    temp.replace(path)


def to_number(value):
    try:
        result = float(
            str(value).replace(",", "").strip()
        )

        if math.isfinite(result):
            return result

    except (ValueError, TypeError):
        pass

    return None


def parse_rows(rows, exchange):
    products = []

    for r in rows:
        if exchange == "SSE":
            code = str(
                r.get("SECURITY_CODE", "")
            ).strip()

            name = r.get(
                "SECURITY_ABBR", ""
            )

            call = to_number(
                r.get("CALL_VOLUME")
            )

            put = to_number(
                r.get("PUT_VOLUME")
            )

            total = to_number(
                r.get("TOTAL_VOLUME")
            )

        else:
            code = str(
                r.get("bddm", "")
            ).strip()

            name = r.get(
                "bdmc", ""
            )

            call = to_number(
                r.get("rccjl")
            )

            put = to_number(
                r.get("rpcjl")
            )

            total = to_number(
                r.get("cjl")
            )

        if not (
            code.isdigit()
            and len(code) == 6
        ):
            continue

        if (
            call is None
            or put is None
            or total is None
        ):
            continue

        if (
            call <= 0
            or put < 0
            or total <= 0
        ):
            continue

        if abs(call + put - total) > max(
            3, total * 0.02
        ):
            continue

        products.append({
            "exchange": exchange,
            "code": code,
            "name": str(name),
            "call_volume": int(call),
            "put_volume": int(put),
            "total_volume": int(total)
        })

    return products


def fetch_sse(session, day):
    params = {
        "isPagination": "false",
        "sqlId": (
            "COMMON_SSE_ZQPZ_YSP_QQ_"
            "SJTJ_MRTJ_CX"
        ),
        "tradeDate": day.replace("-", "")
    }

    errors = []

    for url in SSE_URLS:
        try:
            response = session.get(
                url,
                params=params,
                timeout=12,
                headers={
                    "Referer": (
                        "https://www.sse.com.cn/"
                    ),
                    "User-Agent": "Mozilla/5.0"
                }
            )

            response.raise_for_status()

            body = response.json()

            products = parse_rows(
                body.get("result", []),
                "SSE"
            )

            if len(products) < 4:
                raise ValueError(
                    "Too few SSE ETF products"
                )

            return products

        except (
            requests.RequestException,
            ValueError,
            TypeError
        ) as exc:
            errors.append(str(exc)[:120])

    raise RuntimeError(
        "SSE API failed: "
        + " | ".join(errors)
    )


def fetch_szse(session, day):
    response = session.get(
        SZSE_URL,
        params={
            "SHOWTYPE": "JSON",
            "CATALOGID": "ysprdzb",
            "TABKEY": "tab1",
            "txtQueryDate": day
        },
        timeout=12,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": (
                "https://investor.szse.cn/"
            )
        }
    )

    response.raise_for_status()

    body = response.json()

    if (
        not isinstance(body, list)
        or not body
    ):
        raise ValueError(
            "Invalid SZSE response"
        )

    products = parse_rows(
        body[0].get("data", []),
        "SZSE"
    )

    if len(products) < 3:
        raise ValueError(
            "Too few SZSE ETF products"
        )

    return products


def collect_day(session, day):
    sse = fetch_sse(session, day)
    szse = fetch_szse(session, day)

    components = sse + szse

    total_calls = sum(
        r["call_volume"]
        for r in components
    )

    total_puts = sum(
        r["put_volume"]
        for r in components
    )

    if min(total_calls, total_puts) <= 0:
        raise ValueError(
            "Missing call/put volume"
        )

    weighted_pcr = (
        total_puts / total_calls
    )

    if not 0.02 <= weighted_pcr <= 20:
        raise ValueError(
            "Implausible PCR value"
        )

    return {
        "date": day,
        "weighted_pcr": round(
            weighted_pcr, 6
        ),
        "call_volume": total_calls,
        "put_volume": total_puts,
        "sse_products": len(sse),
        "szse_products": len(szse),
        "components": components,
        "scope_verified": False,
        "source": (
            "SSE+SZSE ETF options "
            "daily statistics"
        )
    }


def main():
    market = read_json(
        MARKET, {}
    )

    trading_dates = sorted({
        r["date"]
        for r in market.get("history", [])
        if (
            isinstance(r, dict)
            and isinstance(r.get("date"), str)
        )
    })

    if not trading_dates:
        raise RuntimeError(
            "No verified market trading dates"
        )

    # 回补最近35个已知交易日
    requested = trading_dates[-35:]

    old = read_json(
        CACHE, {}
    ).get("days", {})

    collected = {
        day: old[day]
        for day in requested
        if day in old
    }

    failures = 0

    with requests.Session() as session:
        for day in reversed(requested):
            if day in collected:
                failures = 0
                continue

            try:
                record = collect_day(
                    session, day
                )

                collected[day] = record
                failures = 0

                print(
                    "ETF_PCR_OK",
                    day,
                    "PCR=",
                    record["weighted_pcr"],
                    "SSE=",
                    record["sse_products"],
                    "SZSE=",
                    record["szse_products"],
                    flush=True
                )

            except Exception as exc:
                failures += 1

                print(
                    "ETF_PCR_UNAVAILABLE",
                    day,
                    type(exc).__name__,
                    str(exc)[:200],
                    flush=True
                )

                if failures >= 3:
                    print(
                        "Stopping after repeated "
                        "provider failures",
                        flush=True
                    )
                    break

            time.sleep(0.35)

    save_json(
        CACHE,
        {"days": collected}
    )

    if not collected:
        raise RuntimeError(
            "No real ETF PCR collected. "
            "Existing published data unchanged."
        )

    latest_date = max(collected)

    # 保证用于均值计算的是连续交易日
    position = requested.index(
        latest_date
    )

    history = []

    for day in reversed(
        requested[:position + 1]
    ):
        if day not in collected:
            break

        history.append(
            collected[day]
        )

    history.reverse()

    payload = {
        "source": (
            "SSE+SZSE ETF options only; "
            "CFFEX and commodity options "
            "not included"
        ),
        "scope": (
            "沪深ETF期权，"
            "不是全市场期权"
        ),
        "scope_verified": False,
        "asof": latest_date,
        "updated_at": (
            dt.datetime.now(
                dt.timezone.utc
            ).isoformat()
        ),
        "weight_method": (
            "Sum put volume / "
            "sum call volume"
        ),
        "history": history
    }

    save_json(
        OUTPUT, payload
    )

    print(
        "ETF_PCR_PUBLISHED",
        "date=", latest_date,
        "consecutive_sessions=",
        len(history),
        "PCR=",
        history[-1]["weighted_pcr"],
        flush=True
    )

    if len(history) < 20:
        print(
            "WARNING: fewer than 20 "
            "consecutive sessions; "
            "standard deviation unavailable",
            flush=True
        )


if __name__ == "__main__":
    main()
