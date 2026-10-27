
"""沪深ETF期权成交量PCR真实历史采集（约250交易日，逐次补齐）。

仅上交所/深交所ETF期权，不包括中金所股指或商品期权。
合并PCR = sum(认沽成交量) / sum(认购成交量)，不是成交额加权。
失败日保留为空，不用前日数据代替；旧的有效历史不会因缺档被截断。
"""
import datetime as dt
import json
import math
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
CACHE = DATA / 'options_etf_cache.json'
OUTPUT = DATA / 'options_pcr.json'
TZ = ZoneInfo('Asia/Shanghai')
SSE_URL = 'https://query.sse.com.cn/commonQuery.do'
SZSE_URL = 'https://investor.szse.cn/api/report/ShowReport/data'

MAX_NEW_PER_RUN = 35
A

SZSE_HEADERS = {
    'User-Agent': 'Mozilla/5.0',
    'Referer': 'https://investor.szse.cn/',
    'Accept': 'application/json,*/*'
}


def load(path, default):
    try:
        return json.loads(
            path.read_text(encoding='utf-8')
        )
    except (FileNotFoundError, ValueError, OSError):
        return default


def save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(
        json.dumps(
            obj, ensure_ascii=False,
            separators=(',', ':')
        ),
        encoding='utf-8'
    )
    tmp.replace(path)


def number(raw):
    try:
        n = float(str(raw).replace(',', '').strip())
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def parse(rows, exchange, date):
    parsed = []
    for r in rows:
        if not isinstance(r, dict):
            continue

        if exchange == 'SSE':
            code = str(
                r.get('SECURITY_CODE', '')
            ).strip()
            name = str(r.get('SECURITY_ABBR', ''))
            call, put, total = (
                number(r.get(k))
                for k in (
                    'CALL_VOLUME',
                    'PUT_VOLUME',
                    'TOTAL_VOLUME'
                )
            )
            actual = str(
                r.get('TRADE_DATE') or ''
            ).replace('-', '').replace('/', '')[:8]

            if actual and actual != date.replace('-', ''):
                continue
        else:
            code = str(r.get('bddm', '')).strip()
            name = str(r.get('bdmc', ''))
            call, put, total = (
                number(r.get(k))
                for k in ('rccjl', 'rpcjl', 'cjl')
            )

        if not (code.isdigit() and len(code) == 6):
            continue

        if (
            any(v is None for v in (call, put, total))
            or call <= 0 or put < 0 or total <= 0
        ):
            continue

        if abs(call + put - total) > max(3, total * .02):
            continue

        parsed.append({
            'exchange': exchange,
            'code': code,
            'name': name,
            'call_volume': int(call),
            'put_volume': int(put),
            'total_volume': int(total)
        })

    return parsed


def fetch(session, url, params, headers, day, exchange):
    error = None

    for attempt in range(2):
        try:
            response = session.get(
                url,
                params=params,
                headers=headers,
                timeout=(8, 18)
            )
            response.raise_for_status()
            body = response.json()

            rows = (
                body.get('result', [])
                if exchange == 'SSE'
                else body[0].get('data', [])
            )

            products = parse(rows, exchange, day)

            if len(products) < (
                4 if exchange == 'SSE' else 3
            ):
                raise ValueError(
                    f'Incomplete {exchange} records: '
                    f'{len(products)}'
                )
            return products

        except (
            requests.RequestException,
            ValueError, KeyError,
            TypeError, IndexError
        ) as exc:
            error = exc
            if attempt == 0:
                time.sleep(1.5)

    raise RuntimeError(
        f'{exchange} {day} unavailable: '
        f'{type(error).__name__}: '
        f'{str(error)[:100]}'
    )


def collect(session, day):
    sse = fetch(
        session, SSE_URL, {
            'isPagination': 'false',
            'sqlId': 'COMMON_SSE_ZQPZ_YSP_QQ_SJTJ_MRTJ_CX',
            'tradeDate': day.replace('-', '')
        },
        SSE_HEADERS, day, 'SSE'
    )

    szse = fetch(
        session, SZSE_URL, {
            'SHOWTYPE': 'JSON',
            'CATALOGID': 'ysprdzb',
            'TABKEY': 'tab1',
            'txtQueryDate': day
        },
        SZSE_HEADERS, day, 'SZSE'
    )

    rows = sse + szse

    calls = sum(p['call_volume'] for p in rows)
    puts = sum(p['put_volume'] for p in rows)

    if calls <= 0 or not 0.02 <= puts / calls <= 20:
        raise ValueError(
            f'Invalid market-wide ETF PCR {day}'
        )

    pair = [
        p for p in rows
        if (p['exchange'], p['code']) in {
            ('SSE', '510300'),
            ('SZSE', '159919')
        }
    ]

    pcr300 = (
        sum(x['put_volume'] for x in pair) /
        sum(x['call_volume'] for x in pair)
    ) if len(pair) == 2 else None

    return {
        'date': day,
        'weighted_pcr': round(puts / calls, 7),
        'pcr_300etf': (
            round(pcr300, 7)
            if pcr300 is not None
            else None
        ),
        'call_volume': calls,
        'put_volume': puts,
        'sse_products': len(sse),
        'szse_products': len(szse),
        'components': rows,
        'source': (
            'SSE+SZSE ETF daily option statistics'
        )
    }


def main():
    now = dt.datetime.now(TZ)

    target = (
        now.date()
        if now.hour >= 18
        else now.date() - dt.timedelta(days=1)
    )

    calendar = xcals.get_calendar('XSHG')

    trading = [
        d.date().isoformat()
        for d in calendar.sessions_in_range(
            pd.Timestamp(
                target - dt.timedelta(days=430)
            ),
            pd.Timestamp(target)
        )
    ][-252:]

    expected = trading[-1]
    allowed = set(trading)

    old_cache = load(CACHE, {}).get('days', {})
    old_history = load(OUTPUT, {}).get('history', [])

    merged = {
        **{
            r['date']: r
            for r in old_history
            if (
                isinstance(r, dict)
                and r.get('date') in allowed
            )
        },
        **{
            d: r
            for d, r in old_cache.items()
            if (
                d in allowed
                and isinstance(r, dict)
            )
        }
    }

    missing = [
        d for d in reversed(trading)
        if d not in merged
    ]

    attempted = 0
    successes = 0
    failed_in_row = 0

    with requests.Session() as session:
        for day in missing[:MAX_NEW_PER_RUN]:
            attempted += 1

            try:
                record = collect(session, day)
                merged[day] = record
                successes += 1
                failed_in_row = 0

                print(
                    'PCR_OK',
                    day,
                    'ETF=', record['weighted_pcr'],
                    '300ETF=', record['pcr_300etf'],
                    flush=True
                )

            except Exception as exc:
                failed_in_row += 1

                print(
                    '::warning::PCR_MISSING',
                    day,
                    str(exc)[:260],
                    flush=True
                )

                if failed_in_row >= 5:
                    print(
                        '::warning::Many provider failures; '
                        'stop this run to avoid rate limiting',
                        flush=True
                    )
                    break

            time.sleep(.65)

    save(
        CACHE,
        {
            'days': {
                d: merged[d]
                for d in sorted(merged)
            }
        }
    )

    history = [
        merged[d]
        for d in trading
        if d in merged
    ]

    if not history:
        raise RuntimeError(
            'No verified options statistics: '
            'leaving existing data unchanged'
        )

    asof = history[-1]['date']

    save(
        OUTPUT,
        {
            'source': (
                'SSE+SZSE ETF options '
                '(not CFFEX or commodity options)'
            ),
            'scope': (
                '沪深ETF期权（不含股指、商品期权）'
            ),
            'weight_method': (
                'sum(put volume) / sum(call volume)'
            ),
            'asof': asof,
            'expected_session': expected,
            'updated_at': dt.datetime.now(
                dt.timezone.utc
            ).isoformat(),
            'history': history,
            'missing_sessions': [
                d for d in trading
                if d not in merged
            ],
            'available_sessions': len(history),
            'target_sessions': len(trading)
        }
    )

    print(
        'PCR_PUBLISHED',
        'asof=', asof,
        'expected=', expected,
        'available=', len(history),
        '/', len(trading),
        'new=', successes,
        'attempted=', attempted,
        flush=True
    )

    if asof != expected:
        print(
            '::warning::PCR_STALE: '
            'target trading date not yet available:',
            expected,
            flush=True
        )


if __name__ == '__main__':
    main()
