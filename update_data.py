
"""Full SH + SZ + BJ A-share market TRIN / SMA20 breadth (ST and *ST included).

Source: third-party Money Tree daily CSV. No demo values or index constituents.
TRIN: ((advancing issues / declining issues) /
       (advancing share volume / declining share volume)).

Revision 3: audits per-exchange volume units, handles gaps and corporate actions
conservatively, records source/coverage, and publishes both volume and amount
versions without mixing them. Requires live GitHub Actions run to verify provider.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import math
import re
import statistics
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
MARKET = DATA / 'market.json'
CACHE = DATA / 'price_cache.json'
TZ = ZoneInfo('Asia/Shanghai')
URL = 'https://money-tree.eyunzhu.com/daily_kline/date/{date}.csv'
MASTER_URL = 'https://money-tree.eyunzhu.com/stocks/stocks_list.csv'
SCHEMA = 3

MIN_EXCHANGE = {'sh': 1900, 'sz': 2400, 'bj': 300}
MIN_VALID_FRACTION = 0.97
MIN_SMA_COVERAGE = 0.85
MIN_UNIT_AUDIT_ROWS = 100

HEADERS = {
    'User-Agent': 'AshareResearchDashboard/3.0 (public daily files)',
    'Accept': 'text/csv,*/*'
}


def log(*message):
    print(*message, flush=True)


def read_json(path, fallback):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else fallback


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(
        json.dumps(value, ensure_ascii=False, separators=(',', ':')),
        encoding='utf-8'
    )
    temp.replace(path)


def stock_code(raw):
    s = str(raw).strip().lower().replace(' ', '')
    m = re.fullmatch(
        r'(?:(sh|sz|bj)\.?([0-9]{6})|([0-9]{6})\.(sh|sz|bj)|([0-9]{6}))',
        s
    )
    if not m:
        return None

    prefix, first, second, suffix, bare = m.groups()
    code = first or second or bare

    # No name/ST filter. ST and *ST remain included.
    if re.fullmatch(r'(?:60[0135]|68[89])\d{3}', code):
        market = 'sh'
    elif re.fullmatch(r'(?:00[0-3]|30[01])\d{3}', code):
        market = 'sz'
    elif re.fullmatch(r'(?:[48]\d{5}|92\d{4})', code):
        market = 'bj'
    else:
        return None

    if (prefix or suffix) and (prefix or suffix) != market:
        return None

    return market + code


def col(df, aliases, required=True):
    normalized = {
        re.sub(r'[\s_]+', '', str(c).lower()): c
        for c in df.columns
    }

    for alias in aliases:
        hit = normalized.get(
            re.sub(r'[\s_]+', '', alias.lower())
        )
        if hit is not None:
            return hit

    if required:
        raise ValueError(
            f'Missing column {aliases}; source columns={list(df.columns)}'
        )
    return None


def parse_csv(content):
    decoded = None

    for encoding in ('utf-8-sig', 'gb18030'):
        try:
            decoded = content.decode(encoding)
            break
        except UnicodeDecodeError:
            pass

    if decoded is None or decoded.lstrip().lower().startswith(
        ('<html', '<!doctype', '{')
    ):
        raise ValueError('Response is not a CSV')

    first = decoded.splitlines()[0].split(',')
    headerless = (
        len(first) >= 7
        and stock_code(first[0].strip('"')) is not None
    )

    df = pd.read_csv(
        io.StringIO(decoded),
        header=None if headerless else 'infer',
        low_memory=False,
        dtype={0: str} if headerless else None
    )

    if headerless:
        columns = [
            'symbol', 'date', 'open', 'close',
            'high', 'low', 'volume', 'amount'
        ]
        df.columns = [
            columns[i] if i < len(columns) else f'extra{i}'
            for i in range(len(df.columns))
        ]

    return df


def names_from_master(session):
    """Optional ST auditing; missing names never exclude ST stocks."""
    try:
        response = session.get(
            MASTER_URL,
            headers=HEADERS,
            timeout=(8, 22)
        )
        response.raise_for_status()

        df = parse_csv(response.content)
        cc = col(
            df,
            ['代码', '股票代码', '证券代码',
             'code', 'symbol', 'ts_code']
        )
        nc = col(
            df,
            ['名称', '证券简称', '股票简称',
             '股票名称', 'name', 'stock_name']
        )

        result = {
            code: str(name).strip()
            for raw, name in zip(df[cc], df[nc])
            if (code := stock_code(raw)) is not None
        }

        log(
            'ST stock-name audit reference:',
            len(result),
            'A-share symbols'
        )
        return result

    except Exception as exc:
        log(
            '::warning::Stock-name list unavailable; '
            'ST count will be unknown:',
            exc
        )
        return {}


def volume_unit_audit(symbols, close, volume, amount):
    """Audit volume units using amount / (price * volume)."""
    ratios = {}

    for market in ('sh', 'sz', 'bj'):
        vals = []

        for code in symbols:
            if code[:2] != market:
                continue

            p = close[code]
            v = volume[code]
            a = amount.get(code)

            if p > 0 and v > 0 and a is not None and a > 0:
                r = a / (p * v)

                if math.isfinite(r) and r > 0:
                    vals.append(r)

        if len(vals) < MIN_UNIT_AUDIT_ROWS:
            raise ValueError(
                f'Cannot verify {market.upper()} volume units: '
                f'{len(vals)} turnover records'
            )

        ratios[market] = statistics.median(vals)

    ref = min(ratios.values())
    factors = {}

    for market, median in ratios.items():
        factor = median / ref

        if 0.70 <= factor <= 1.43:
            factors[market] = 1
        elif 70 <= factor <= 143:
            factors[market] = 100
        else:
            raise ValueError(
                'Unreconciled cross-exchange volume units: '
                f'{ratios}'
            )

    log(
        'TURNOVER/VOLUME UNIT AUDIT:',
        ratios,
        'multipliers:',
        factors
    )
    return factors, ratios


def parse_bar(df, date, master):
    code_column = col(
        df, ['symbol', 'ts_code', 'code', '股票代码', '代码']
    )
    close_column = col(
        df, ['close', '收盘价', '收盘', 'close_price']
    )
    volume_column = col(
        df, ['volume', 'vol', '成交量', 'volume_shares']
    )
    amount_column = col(
        df, ['amount', 'turnover', '成交额', '成交金额']
    )
    name_column = col(
        df,
        ['name', '股票名称', '股票简称', '证券简称', '名称'],
        False
    )
    date_column = col(
        df,
        ['date', 'trade_date', 'tradedate', '日期', '交易日期'],
        False
    )
    pct_column = col(
        df,
        ['pct_chg', 'pct_change', 'change_percent', '涨跌幅'],
        False
    )
    prev_column = col(
        df,
        ['pre_close', 'prev_close', '昨收', '昨收价'],
        False
    )

    if date_column is not None:
        dates = pd.to_datetime(
            df[date_column].astype(str),
            errors='coerce'
        ).dt.date
        df = df.loc[dates == date].copy()

    symbols = df[code_column].map(stock_code)
    included = symbols.notna() & ~symbols.duplicated()
    df = df.loc[included].copy()
    symbols = symbols.loc[included]

    prices = pd.to_numeric(
        df[close_column], errors='coerce'
    )
    volumes = pd.to_numeric(
        df[volume_column], errors='coerce'
    )
    amounts = pd.to_numeric(
        df[amount_column], errors='coerce'
    )

    valid = (
        prices.gt(0)
        & volumes.gt(0)
        & amounts.gt(0)
    )

    total = len(df)
    active = int(valid.sum())

    market_counts = {
        m: int(symbols.str.startswith(m).sum())
        for m in ('sh', 'sz', 'bj')
    }

    for market, min_size in MIN_EXCHANGE.items():
        if market_counts[market] < min_size:
            raise ValueError(
                f'{market.upper()} incomplete sample '
                f'{market_counts[market]} < {min_size}'
            )

    if active < total * MIN_VALID_FRACTION:
        raise ValueError(
            f'Too few valid quotes: {active}/{total}'
        )

    symbols = symbols.loc[valid]
    prices = prices.loc[valid]
    volumes = volumes.loc[valid]
    amounts = amounts.loc[valid]

    p = dict(zip(symbols, prices.astype(float)))
    v = dict(zip(symbols, volumes.astype(float)))
    a = dict(zip(symbols, amounts.astype(float)))

    factors, ratios = volume_unit_audit(
        list(p), p, v, a
    )

    quotes = {
        code: [
            round(price, 4),
            round(v[code] * factors[code[:2]], 3),
            round(a[code], 2)
        ]
        for code, price in p.items()
    }

    daily_pct = {}

    if pct_column:
        pct = pd.to_numeric(
            df.loc[valid, pct_column]
              .astype(str)
              .str.replace('%', '', regex=False),
            errors='coerce'
        )
        daily_pct = {
            code: float(pc)
            for code, pc in zip(symbols, pct)
            if pd.notna(pc)
        }

    elif prev_column:
        prev = pd.to_numeric(
            df.loc[valid, prev_column],
            errors='coerce'
        )
        daily_pct = {
            code: 100 * (p[code] / float(price) - 1)
            for code, price in zip(symbols, prev)
            if pd.notna(price) and price > 0
        }

    st = set()
    have_name = name_column is not None or bool(master)

    for ix, code in zip(df.loc[valid].index, symbols):
        name = (
            str(df.at[ix, name_column])
            if name_column
            else master.get(code, '')
        )

        if re.search(
            r'(?:\*\s*ST|ST)',
            name,
            re.IGNORECASE
        ):
            st.add(code)

    stats = {}

    for market in ('sh', 'sz', 'bj'):
        stats[market] = {
            'listings': market_counts[market],
            'active': sum(
                code.startswith(market)
                for code in quotes
            ),
            'st': (
                sum(
                    code.startswith(market)
                    for code in st
                )
                if have_name else None
            ),
            'turnover_volume_median': round(
                ratios[market], 3
            ),
            'volume_unit_factor': factors[market],
        }

    log(
        'FULL SH/SZ/BJ UNIVERSE '
        '(ST included; name filtering NOT used):',
        stats
    )

    return {
        'schema_version': SCHEMA,
        'quotes': quotes,
        'pct': daily_pct,
        'total': total,
        'active': active,
        'bj': market_counts['bj'],
        'exchange_stats': stats,
        'st_count': len(st) if have_name else None,
        'st_audit_available': have_name,
        'provider': (
            'Money Tree daily CSV '
            '(cross-exchange units audited)'
        )
    }


def request_day(day, session, master):
    url = URL.format(
        date=day.strftime('%Y%m%d')
    )

    response = session.get(
        url,
        headers=HEADERS,
        timeout=(8, 30)
    )

    if response.status_code in (404, 410):
        return None

    response.raise_for_status()

    if len(response.content) < 65000:
        raise ValueError(
            f'Unexpected small file '
            f'{len(response.content)} bytes'
        )

    return parse_bar(
        parse_csv(response.content),
        day,
        master
    )


def calendar_dates():
    try:
        import akshare as ak

        cal = ak.tool_trade_date_hist_sina()
        days = sorted(
            set(
                pd.to_datetime(
                    cal['trade_date']
                ).dt.date
            )
        )

        log(
            'Trading calendar loaded:',
            len(days),
            'dates'
        )
        return days

    except Exception as exc:
        log(
            '::warning::Could not retrieve trading calendar:',
            exc
        )
        return None


def previous_session(day, calendar):
    if calendar:
        earlier = [
            date for date in calendar
            if date < day
        ]
        return earlier[-1] if earlier else None

    d = day - dt.timedelta(days=1)

    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)

    return d


def collect(now, snapshots, master):
    cutoff = (
        now.date()
        if now.hour >= 17
        else now.date() - dt.timedelta(days=1)
    )

    days = [
        cutoff - dt.timedelta(days=i)
        for i in range(95)
    ]
    days = [
        d for d in days
        if d.weekday() < 5
    ]

    successes = 0
    attempts = 0

    with requests.Session() as session:
        for day in days:
            key = day.isoformat()

            if (
                key in snapshots
                and snapshots[key].get('schema_version') == SCHEMA
            ):
                continue

            if attempts >= 30:
                break

            attempts += 1

            try:
                entry = request_day(
                    day, session, master
                )

                if entry:
                    snapshots[key] = entry
                    successes += 1

                    log(
                        '[REAL]',
                        key,
                        'SH/SZ/BJ=',
                        {
                            k: v['active']
                            for k, v in
                            entry['exchange_stats'].items()
                        },
                        'ST=',
                        entry['st_count']
                    )

                    time.sleep(0.6)

            except Exception as exc:
                log(
                    '::warning::Daily archive unavailable:',
                    key,
                    type(exc).__name__,
                    str(exc)[:220]
                )

    log(
        'Collection attempted',
        attempts,
        'days; successful new verified files',
        successes
    )

    return snapshots


def calc_session(
    day,
    row,
    previous,
    recent,
    previous_date,
    calendar
):
    current = row['quotes']

    change = row.get('pct') or {}

    method = (
        'source_day_pct'
        if len(change) >= 0.95 * len(current)
        else None
    )

    if method is None:
        expected = previous_session(
            dt.date.fromisoformat(day),
            calendar
        )

        if (
            expected is None
            or previous is None
            or previous_date != expected.isoformat()
        ):
            return None

        old = previous['quotes']
        change = {
            code: (
                values[0] / old[code][0] - 1
            ) * 100
            for code, values in current.items()
            if code in old and old[code][0] > 0
        }

        method = 'previous_verified_trading_close'

    classified = {
        m: {
            'up': 0,
            'down': 0,
            'flat': 0,
            'up_vol': 0.0,
            'down_vol': 0.0,
            'up_amt': 0.0,
            'down_amt': 0.0
        }
        for m in ('sh', 'sz', 'bj')
    }

    for code, (_, vol, amt) in current.items():
        pct = change.get(code)

        if (
            pct is None
            or not math.isfinite(float(pct))
        ):
            continue

        r = classified[code[:2]]

        if pct > 1e-8:
            r['up'] += 1
            r['up_vol'] += vol
            r['up_amt'] += amt

        elif pct < -1e-8:
            r['down'] += 1
            r['down_vol'] += vol
            r['down_amt'] += amt

        else:
            r['flat'] += 1

    plus = sum(
        x['up'] for x in classified.values()
    )
    minus = sum(
        x['down'] for x in classified.values()
    )
    plusv = sum(
        x['up_vol'] for x in classified.values()
    )
    minusv = sum(
        x['down_vol'] for x in classified.values()
    )
    plusa = sum(
        x['up_amt'] for x in classified.values()
    )
    minusa = sum(
        x['down_amt'] for x in classified.values()
    )

    if min(
        plus, minus,
        plusv, minusv,
        plusa, minusa
    ) <= 0:
        return None

    classified_count = sum(
        x['up'] + x['down'] + x['flat']
        for x in classified.values()
    )

    if classified_count < 0.95 * row['active']:
        raise ValueError(
            'Insufficient same-day price-change coverage: '
            f'{classified_count}/{row["active"]}'
        )

    # Standard share-volume TRIN
    trin = (
        (plus * minusv)
        / (minus * plusv)
    )

    # Turnover amount variant
    trin_amount = (
        (plus * minusa)
        / (minus * plusa)
    )

    above = 0
    eligible = 0
    width_days = 0

    if calendar is not None:
        last20 = [
            x.isoformat()
            for x in calendar
            if x <= dt.date.fromisoformat(day)
        ][-20:]

        in_cache = [
            x for x in recent
            if x[0] in last20
        ]

        if (
            len(last20) == 20
            and len(in_cache) == 20
        ):
            width_days = 20

            for code, (price, _, _) in current.items():
                history = [
                    snap['quotes'][code][0]
                    for _, snap in in_cache
                    if code in snap['quotes']
                ]

                if len(history) == 20:
                    eligible += 1
                    above += (
                        price > sum(history) / 20
                    )
        else:
            width_days = len(in_cache)

    coverage = (
        eligible / row['active']
        if row['active']
        else 0
    )

    breadth = (
        round(100 * above / eligible, 2)
        if (
            eligible
            and coverage >= MIN_SMA_COVERAGE
        )
        else None
    )

    return {
        'date': day,
        'trin': round(trin, 4),
        'trin_amount': round(trin_amount, 4),
        'breadth': breadth,
        'advancers': plus,
        'decliners': minus,
        'unchanged': sum(
            x['flat']
            for x in classified.values()
        ),
        'up_volume': round(plusv, 3),
        'down_volume': round(minusv, 3),
        'up_amount': round(plusa, 2),
        'down_amount': round(minusa, 2),
        'active_count': row['active'],
        'total_count': row['total'],
        'beijing_count': row['bj'],
        'valid_count': eligible,
        'coverage_pct': round(
            coverage * 100, 2
        ),
        'warmup_sessions': width_days,
        'exchange_stats': row['exchange_stats'],
        'daily_classification': classified,
        'st_count': row['st_count'],
        'st_audit_available': row[
            'st_audit_available'
        ],
        'data_provider': row['provider'],
        'pct_method': method,
        'volume_basis': (
            'unit-audited cross-exchange volume'
        ),
        'amount_basis': 'source turnover column',
        'price_basis': 'unadjusted close'
    }


def build_history(snapshots, calendar):
    dates = sorted(snapshots)
    records = []

    for i, day in enumerate(dates):
        recent = [
            (d, snapshots[d])
            for d in dates[
                max(0, i - 45):i + 1
            ]
        ]

        previous = (
            snapshots[dates[i - 1]]
            if i else None
        )

        row = calc_session(
            day,
            snapshots[day],
            previous,
            recent,
            dates[i - 1] if i else None,
            calendar
        )

        if row:
            records.append(row)

    return records[-250:]


def main():
    now = dt.datetime.now(TZ)

    log(
        'China local:',
        now.isoformat(),
        '| SH+SZ+BJ including ST and *ST'
    )

    saved = read_json(
        CACHE,
        {'snapshots': {}}
    ).get('snapshots', {})

    snapshots = {
        day: row
        for day, row in saved.items()
        if (
            isinstance(row, dict)
            and row.get('schema_version') == SCHEMA
        )
    }

    with requests.Session() as session:
        master = names_from_master(session)

    calendar = calendar_dates()

    snapshots = collect(
        now,
        snapshots,
        master
    )

    if not snapshots:
        raise RuntimeError(
            'No validated 3-exchange data; '
            'previous market.json left unchanged'
        )

    snapshots = {
        d: snapshots[d]
        for d in sorted(snapshots)[-85:]
    }

    history = build_history(
        snapshots,
        calendar
    )

    if not history:
        raise RuntimeError(
            'No full-market session with verified change '
            'and volume; no fake figures published'
        )

    target = (
        now.date()
        if now.hour >= 17
        else now.date() - dt.timedelta(days=1)
    )

    age = (
        target
        - dt.date.fromisoformat(
            history[-1]['date']
        )
    ).days

    if age > 5:
        raise RuntimeError(
            'Historical quotes too old; '
            'refusing publication'
        )

    last = history[-1]

    payload = {
        'schema_version': SCHEMA,
        'status': 'real',
        'source': (
            'REAL - Money Tree verified SH/SZ/BJ CSV'
        ),
        'updated_at': dt.datetime.now(
            dt.timezone.utc
        ).isoformat(),
        'asof': last['date'],
        'universe': (
            'SH + SZ + BJ mainland A-shares '
            'including ST and *ST; '
            'only active quotes classified'
        ),
        'method': (
            'Traditional share-volume TRIN is primary; '
            'amount-based TRIN shown separately'
        ),
        'history': history
    }

    save_json(
        MARKET,
        payload
    )
    save_json(
        CACHE,
        {
            'schema_version': SCHEMA,
            'snapshots': snapshots
        }
    )

    log(
        'PUBLISHED REAL VERIFIED:',
        last['date'],
        'TRIN volume=', last['trin'],
        'TRIN amount=', last['trin_amount'],
        'breadth=', last['breadth'],
        'SH/SZ/BJ=',
        {
            k: v['active']
            for k, v in
            last['exchange_stats'].items()
        },
        'ST=', last['st_count'],
        'classification=', last['pct_method']
    )


if __name__ == '__main__':
    main()
