# -*- coding: utf-8 -*-
"""market-oracle 取数入口：一次调用并发拉取多源交易数据，输出结构化 JSON。

为什么要有这个脚本：SKILL 的纪律是「只用交易数据、至少 3 个独立维度」。手写
Python 片段能取数，但每次都要重写解析、字段名靠记忆、失败处理各写各的。这里
把「信号名 → 数据源」的映射固化成一份 spec，取数、容错、时间标注一次做完，
报告阶段只需读 JSON。

用法：
    python scripts/oracle_signals.py --preset macro-risk
    python scripts/oracle_signals.py --preset a-share --symbol 600519
    python scripts/oracle_signals.py --preset options --symbol SPY
    python scripts/oracle_signals.py --signals '{"gold":{"type":"price_history","symbol":"GC=F"}}'
    python scripts/oracle_signals.py --preset crypto --proxy http://127.0.0.1:7897

输出：stdout 一份 JSON（含 data / errors / meta），也可 --out 落盘。
每个信号自带 as_of（数据日期）与 horizon（定价时间尺度），报告里必须照抄这两个
字段——时间尺度混用的信号不能放进同一票。
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import inspect
import json
import os
import sys
import time
from urllib.parse import quote
from typing import Any, Callable

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SKILL_ROOT not in sys.path:
    sys.path.insert(0, SKILL_ROOT)

from digital_oracle import (  # noqa: E402
    BisCreditGapQuery,
    BisProvider,
    BisRateQuery,
    CftcCotProvider,
    CftcCotQuery,
    CoinGeckoPriceQuery,
    CoinGeckoProvider,
    DeribitFuturesCurveQuery,
    DeribitOptionChainQuery,
    DeribitProvider,
    EastmoneyFundFlowQuery,
    EastmoneyKlineQuery,
    EastmoneyProvider,
    EastmoneyQuoteQuery,
    EastmoneySectorFlowQuery,
    EdgarInsiderQuery,
    EdgarProvider,
    FearGreedProvider,
    KalshiMarketQuery,
    KalshiProvider,
    OptionsChainQuery,
    PolymarketEventQuery,
    PolymarketProvider,
    PriceHistoryQuery,
    USTreasuryProvider,
    WebSearchProvider,
    WorldBankProvider,
    WorldBankQuery,
    YFinanceProvider,
    YahooPriceProvider,
    YieldCurveQuery,
    gather,
)
from digital_oracle.http import UrllibJsonClient  # noqa: E402
from digital_oracle.proxy_pool import ProxyPool  # noqa: E402

#: 每类信号固化「定价时间尺度」——报告的时间分层一节直接引用，避免事后凭感觉贴标签。
HORIZONS = {
    "polymarket": "到期日决定（多为 3-12 个月）",
    "kalshi": "到期日决定（多为 3-12 个月）",
    "price_history": "取决于 interval（日线=数周，月线=1-3 年）",
    "options_chain": "到期日决定（数周至 1 年）",
    "yield_curve": "短端 3 个月 / 长端 10-30 年",
    "inflation_breakeven": "10 年期通胀预期",
    "cftc": "周度持仓，指向 1-6 个月",
    "edgar_insider": "内部人行为指向 3-12 个月",
    "coingecko": "现货快照，指向数周",
    "deribit_futures": "期限结构覆盖 1-12 个月",
    "deribit_options": "到期日决定（数周至 1 年）",
    "fear_greed": "情绪快照，指向 0-3 个月",
    "eastmoney_quote": "盘中快照",
    "eastmoney_kline": "取决于 period",
    "eastmoney_fund_flow": "日度资金流，指向数日至数周",
    "eastmoney_sector": "日度轮动，指向数日至数周",
    "bis_rates": "政策利率，指向 1-3 年",
    "bis_credit_gap": "季度信贷缺口，指向 1-5 年（周期晚期信号）",
    "worldbank": "年度数据，滞后 1-2 年（只用于长期参照）",
    "web_search": "二手引用，即时快照（须标注来源与日期）",
    "akshare_foreign": "日线商品/外盘指数期货，指向数周至数月",
    "akshare_bond": "国债收益率曲线：短端3个月 / 长端10-30年",
    "yahoo_chart": "取决于 interval（日线=数周，月线=1-3 年）",
}


# ---------------------------------------------------------------- 序列化工具


def _round(value: Any, digits: int = 4) -> Any:
    if isinstance(value, float):
        return round(value, digits)
    return value


def _plain(value: Any, depth: int = 0) -> Any:
    """把 dataclass / tuple / 嵌套结构压成 JSON 友好的普通对象。"""
    if depth > 6:
        return str(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        fields = {f.name: _plain(getattr(value, f.name), depth + 1) for f in dataclasses.fields(value)}
        return {k: _round(v) for k, v in fields.items() if k != "raw"}
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        head = [_plain(v, depth + 1) for v in items[:60]]
        if len(items) > 60:
            head.append(f"... ({len(items) - 60} more)")
        return head
    if isinstance(value, dict):
        return {str(k): _plain(v, depth + 1) for k, v in value.items()}
    return _round(value)


def _today() -> str:
    return dt.date.today().isoformat()


# ---------------------------------------------------------------- provider 构造
#
# 实测结论（2026-10-26）：境内源与境外源需要**不同的出口**。
#   · 境外源（Polymarket/CFTC/美债/Yahoo/CNN/SEC）直连多被重置，经代理可达；
#   · 境内源（东财/Tencent/新浪）走代理反而被对端拒绝，必须直连。
# 所以这里按信号类型决定「这条请求走不走代理」，而不是整批一个开关。
# 另外 CNN 缺 UA 会返回 418，SEC 缺联系邮箱会返回 403——都不是网络问题，
# 是请求头问题，所以按源分别设置请求头。

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {"Accept": "application/json,text/plain,*/*", "User-Agent": BROWSER_UA}

#: 这些信号的宿主在境内，必须直连；走代理会被对端拒绝或触发风控。
DIRECT_TYPES = frozenset(
    {
        "eastmoney_quote",
        "eastmoney_kline",
        "eastmoney_fund_flow",
        "eastmoney_sector",
        "akshare_foreign",
        "akshare_bond",
    }
)

#: 按源覆盖请求头：CNN 需要浏览器指纹，SEC 需要可联系的身份（否则 403）。
HEADER_PROFILES = {
    "fear_greed": {**DEFAULT_HEADERS, "Referer": "https://edition.cnn.com/"},
    "edgar_insider": {
        "Accept": "application/json",
        "User-Agent": os.environ.get("EDGAR_USER_EMAIL", "market-oracle research contact@know-your-money.local"),
    },
}


def _system_proxy() -> str | None:
    """读 Windows 系统代理（Clash/V2Ray 等客户端会把端口写在这里）。

    urllib 默认不读系统代理，「本地开了 VPN」和「我们的请求走了 VPN」是两件事——
    这个函数就是补上这一步，免得每次都要手动传 --proxy。
    """
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        ) as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
    except Exception:  # noqa: BLE001 — 读不到就当没有代理
        return None
    if not enabled or not server:
        return None
    if "=" in server:  # 形如 http=127.0.0.1:7897;https=127.0.0.1:7897
        parts = dict(item.split("=", 1) for item in server.split(";") if "=" in item)
        server = parts.get("https") or parts.get("http") or ""
    if not server:
        return None
    return server if server.startswith("http") else f"http://{server}"


def _resolve_proxy(explicit: str | None) -> str | None:
    """把 --proxy 的三种取值解析成真实代理地址：auto / none / 显式 URL。"""
    if explicit == "none":
        return None
    if explicit and explicit != "auto":
        return explicit
    return (
        os.environ.get("MARKET_ORACLE_PROXY")
        or os.environ.get("DIGITAL_ORACLE_PROXIES")
        or _system_proxy()
    )


def _build_client(proxy: str | None, headers: dict | None = None, timeout: float = 25.0):
    """构造 HTTP 客户端；proxy 为空即直连。"""
    pool = ProxyPool(proxies=(proxy,)) if proxy else None
    return UrllibJsonClient(timeout_seconds=timeout, proxy_pool=pool, headers=dict(headers or DEFAULT_HEADERS))


def _make(cls, client):
    """有 http_client 参数就注入，没有就按默认构造（兼容不同数据源的签名差异）。"""
    if client is None:
        return cls()
    try:
        params = inspect.signature(cls).parameters
    except (TypeError, ValueError):
        return cls()
    if "http_client" in params:
        return cls(http_client=client)
    return cls()


# ---------------------------------------------------------------- 各信号构造器


def _polymarket(client, spec):
    events = _make(PolymarketProvider, client).list_events(
        PolymarketEventQuery(
            slug_contains=spec.get("query"),
            slug=spec.get("slug"),
            limit=int(spec.get("limit", 10)),
        )
    )
    rows = []
    for event in events:
        markets = [
            {
                "question": m.question,
                "yes_probability": _round(m.yes_probability),
                "volume_usd": _round(m.volume),
                "liquidity_usd": _round(m.liquidity),
                "best_bid": _round(m.best_bid),
                "best_ask": _round(m.best_ask),
                "end_date": m.end_date,
            }
            for m in event.markets[:8]
        ]
        rows.append(
            {
                "title": event.title,
                "slug": event.slug,
                "volume_usd": _round(event.volume),
                "volume_24h_usd": _round(event.volume_24hr),
                "liquidity_usd": _round(event.liquidity),
                "markets": markets,
            }
        )
    return {"events": rows, "as_of": _today()}


def _kalshi(client, spec):
    markets = _make(KalshiProvider, client).list_markets(
        KalshiMarketQuery(
            series_ticker=spec.get("series_ticker"),
            event_ticker=spec.get("event_ticker"),
            limit=int(spec.get("limit", 10)),
        )
    )
    rows = [
        {
            "ticker": m.ticker,
            "title": m.title,
            "subtitle": m.subtitle,
            "yes_probability": _round(m.yes_probability),
            "yes_bid": _round(m.yes_bid),
            "yes_ask": _round(m.yes_ask),
            "volume": _round(m.volume),
            "open_interest": _round(m.open_interest),
            "close_time": m.close_time,
        }
        for m in markets[:20]
    ]
    return {"markets": rows, "as_of": _today()}


def _price_history(client, spec):
    history = YahooPriceProvider().get_history(
        PriceHistoryQuery(
            symbol=spec["symbol"],
            interval=spec.get("interval", "d"),
            limit=int(spec.get("limit", 60)),
        )
    )
    bars = history.bars
    closes = [b.close for b in bars if b.close is not None]
    change_pct = None
    if len(closes) >= 2 and closes[0]:
        change_pct = (closes[-1] / closes[0] - 1) * 100
    return {
        "symbol": history.symbol,
        "interval": history.interval,
        "latest_date": bars[-1].date if bars else None,
        "latest_close": _round(closes[-1] if closes else None),
        "period_change_pct": _round(change_pct, 2),
        "period_high": _round(max(closes) if closes else None),
        "period_low": _round(min(closes) if closes else None),
        "bars": [{"date": b.date, "close": _round(b.close)} for b in bars[-30:]],
        "as_of": bars[-1].date if bars else _today(),
    }


def _options_chain(client, spec):
    chain = YFinanceProvider().get_chain(
        OptionsChainQuery(ticker=spec["ticker"], expiration=spec.get("expiration"))
    )
    return {
        "ticker": chain.ticker,
        "expiration": chain.expiration,
        "underlying_price": _round(chain.underlying_price),
        "atm_iv": _round(chain.atm_iv),
        "implied_move": _round(chain.implied_move()),
        "put_call_oi_ratio": _round(chain.put_call_oi_ratio),
        "put_call_volume_ratio": _round(chain.put_call_volume_ratio),
        "max_pain": _round(chain.max_pain()),
        "atm_strike": _round(chain.atm_strike),
        "total_open_interest": chain.total_open_interest,
        "as_of": _today(),
    }


def _yield_curve(client, spec):
    provider = _make(USTreasuryProvider, client)
    snapshot = provider.latest_yield_curve(
        YieldCurveQuery(year=int(spec.get("year", dt.date.today().year)), curve_kind=spec.get("kind", "nominal"))
    )
    if snapshot is None:
        raise RuntimeError("yield curve returned no snapshot")
    points = {p.tenor: p.value for p in snapshot.points}
    return {
        "curve_kind": snapshot.curve_kind,
        "date": snapshot.date,
        "tenors": points,
        "spread_10y_2y": _round(snapshot.spread("10Y", "2Y")),
        "spread_10y_3m": _round(snapshot.spread("10Y", "3M")),
        "as_of": snapshot.date,
    }


def _inflation_breakeven(client, spec):
    provider = _make(USTreasuryProvider, client)
    year = int(spec.get("year", dt.date.today().year))
    nominal = provider.latest_yield_curve(YieldCurveQuery(year=year, curve_kind="nominal"))
    real = provider.latest_yield_curve(YieldCurveQuery(year=year, curve_kind="real"))
    if nominal is None or real is None:
        raise RuntimeError("nominal or real curve unavailable")
    tenors = ("5Y", "10Y", "30Y")
    breakeven = {
        t: _round((nominal.yield_for(t) or 0) - (real.yield_for(t) or 0))
        for t in tenors
        if nominal.yield_for(t) is not None and real.yield_for(t) is not None
    }
    return {"date": nominal.date, "breakeven_inflation_pct": breakeven, "as_of": nominal.date}


def _cftc(client, spec):
    reports = _make(CftcCotProvider, client).list_reports(
        CftcCotQuery(commodity_name=spec.get("commodity"), limit=int(spec.get("limit", 4)))
    )
    rows = [
        {
            "market": r.market_name,
            "report_date": r.report_date,
            "managed_money_net": r.mm_net,
            "producer_net": r.prod_net,
            "open_interest": r.open_interest,
        }
        for r in reports[:8]
    ]
    return {"reports": rows, "as_of": rows[0]["report_date"] if rows else _today()}


def _edgar_insider(client, spec):
    summary = _make(EdgarProvider, client).get_insider_transactions(
        EdgarInsiderQuery(ticker=spec["ticker"], limit=int(spec.get("limit", 20)))
    )
    return {
        "ticker": summary.ticker,
        "company_name": summary.company_name,
        "total_form4_count": summary.total_form4_count,
        "recent_form4s": [
            {"filing_date": f.filing_date, "report_date": f.report_date, "form_type": f.form_type}
            for f in summary.recent_form4s[:15]
        ],
        "as_of": _today(),
    }


def _coingecko(client, spec):
    provider = _make(CoinGeckoProvider, client)
    payload: dict[str, Any] = {}
    ids = spec.get("coin_ids") or ("bitcoin", "ethereum")
    if isinstance(ids, str):
        ids = tuple(x.strip() for x in ids.split(",") if x.strip())
    payload["prices"] = _plain(provider.get_prices(CoinGeckoPriceQuery(coin_ids=tuple(ids))))
    # 全球概览与价格是两个端点，CoinGecko 免费额度对前者更吝啬；一个挂掉不该吞掉另一个。
    if spec.get("global", True):
        try:
            payload["global"] = _plain(provider.get_global())
        except Exception as exc:  # noqa: BLE001 — 局部降级，保留价格信号
            payload["global_error"] = f"{type(exc).__name__}: {exc}"
    payload["as_of"] = _today()
    return payload


def _deribit_futures(client, spec):
    curve = _make(DeribitProvider, client).get_futures_term_structure(
        DeribitFuturesCurveQuery(currency=spec.get("currency", "BTC"))
    )
    points = [
        {
            "instrument": p.instrument_name,
            "expiry": p.expiration_label,
            "mark_price": _round(p.mark_price),
            "annualized_basis_pct": _round(p.annualized_basis_vs_perpetual, 2),
        }
        for p in curve.points[:10]
    ]
    return {"currency": curve.currency, "points": points, "as_of": _today()}


def _deribit_options(client, spec):
    chain = _make(DeribitProvider, client).get_option_chain(
        DeribitOptionChainQuery(currency=spec.get("currency", "BTC"), expiration_label=spec.get("expiration"))
    )
    if chain is None:
        raise RuntimeError("option chain unavailable")
    atm = chain.atm_strike()
    call_iv = _round(getattr(getattr(atm, "call", None), "mark_iv", None))
    put_iv = _round(getattr(getattr(atm, "put", None), "mark_iv", None))
    # ATM 看跌与看涨 IV 的差就是 skew 的雏形：put 更贵说明市场在为下跌买保险。
    skew = _round(put_iv - call_iv, 4) if (call_iv is not None and put_iv is not None) else None
    return {
        "currency": chain.currency,
        "expiration": chain.expiration_label,
        "underlying_price": _round(chain.underlying_price),
        "atm_strike": _round(getattr(atm, "strike", None)),
        "atm_call_iv": call_iv,
        "atm_put_iv": put_iv,
        "put_minus_call_iv": skew,
        "as_of": _today(),
    }


def _fear_greed(client, spec):
    snap = _make(FearGreedProvider, client).get_index()
    return {
        "score": _round(snap.score, 2),
        "rating": snap.rating,
        "previous_close": _round(snap.previous_close, 2),
        "one_week_ago": _round(snap.one_week_ago, 2),
        "one_month_ago": _round(snap.one_month_ago, 2),
        "one_year_ago": _round(snap.one_year_ago, 2),
        "as_of": (snap.timestamp or "")[:10] or _today(),
    }


def _eastmoney_quote(client, spec):
    quote = _make(EastmoneyProvider, client).get_quote(EastmoneyQuoteQuery(symbol=spec["symbol"]))
    return {"quote": _plain(quote), "as_of": _today()}


def _eastmoney_kline(client, spec):
    kline = _make(EastmoneyProvider, client).get_history(
        EastmoneyKlineQuery(
            symbol=spec["symbol"],
            period=spec.get("period", "daily"),
            adjust=spec.get("adjust", "forward"),
            limit=int(spec.get("limit", 120)),
        )
    )
    bars = kline.bars
    return {
        "symbol": spec["symbol"],
        "bars": [{"date": b.date, "close": _round(b.close)} for b in bars[-30:]],
        "latest_close": _round(bars[-1].close) if bars else None,
        "as_of": bars[-1].date if bars else _today(),
    }


def _eastmoney_fund_flow(client, spec):
    flow = _make(EastmoneyProvider, client).get_fund_flow(
        EastmoneyFundFlowQuery(symbol=spec["symbol"], limit=int(spec.get("limit", 10)))
    )
    days = [
        {
            "date": d.date,
            "main_net_cny": _round(d.main_net),
            "main_net_pct": _round(d.main_net_pct, 2),
            "retail_net_cny": _round((d.medium_net or 0) + (d.small_net or 0)),
        }
        for d in flow.days[:10]
    ]
    return {"symbol": spec["symbol"], "days": days, "as_of": days[0]["date"] if days else _today()}


def _eastmoney_sector(client, spec):
    sectors = _make(EastmoneyProvider, client).list_sector_fund_flow(
        EastmoneySectorFlowQuery(kind=spec.get("kind", "industry"), limit=int(spec.get("limit", 15)))
    )
    return {"sectors": _plain(sectors[:20]), "as_of": _today()}


def _bis_rates(client, spec):
    rows = _make(BisProvider, client).get_policy_rates(
        BisRateQuery(
            countries=tuple(spec.get("countries") or ("US", "CN")),
            start_year=int(spec.get("start_year", dt.date.today().year - 1)),
        )
    )
    latest: dict[str, Any] = {}
    for row in rows:
        latest[row.country] = {"period": row.period, "rate": _round(row.rate, 3)}
    return {"latest_by_country": latest, "as_of": _today()}


def _bis_credit_gap(client, spec):
    rows = _make(BisProvider, client).get_credit_to_gdp(
        BisCreditGapQuery(
            countries=tuple(spec.get("countries") or ("US", "CN")),
            start_year=int(spec.get("start_year", dt.date.today().year - 10)),
        )
    )
    latest: dict[str, Any] = {}
    for row in rows:
        latest[row.country] = {"period": row.period, "gap_pct": _round(row.gap_pct, 2)}
    return {"credit_gap_by_country": latest, "as_of": _today()}


def _worldbank(client, spec):
    result = _make(WorldBankProvider, client).get_indicator(
        WorldBankQuery(
            indicator=spec.get("indicator", "NY.GDP.MKTP.CD"),
            countries=tuple(spec.get("countries") or ("US", "CN")),
            date_range=spec.get("date_range", "2015:2025"),
        )
    )
    points = [p for p in result.points if p.value is not None]
    return {
        "indicator": result.indicator_id,
        "indicator_name": result.indicator_name,
        "latest": _plain(points[:12]),
        "as_of": points[0].date if points else _today(),
    }


def _web_search(client, spec):
    result = _make(WebSearchProvider, client).search(spec["query"])
    return {"query": result.query, "summary": result.text()[:2000], "as_of": _today()}


def _akshare_foreign(client, spec):
    """境内替代：用 AKShare（新浪外盘）取商品与外盘指数期货的日线。

    本仓库新增的桥接，不属于上游引擎。存在理由：Yahoo/yfinance 在境内常被限流，
    而「黄金 / 原油 / 铜 / 标普期货」是判断风险偏好与通胀预期最直接的几个价格，
    这条通路让报告在境内也能凑齐维度。
    """
    try:
        import akshare as ak  # 延迟导入：只有用到这个信号才需要装 akshare
    except ImportError as exc:  # pragma: no cover - 环境问题
        raise RuntimeError("需要 akshare：pip install akshare") from exc

    symbol = spec["symbol"]
    frame = ak.futures_foreign_hist(symbol=symbol)
    if frame is None or frame.empty:
        raise RuntimeError(f"akshare 未返回 {symbol} 的行情")
    frame = frame.tail(int(spec.get("limit", 30)))
    closes = [float(x) for x in frame["close"].tolist()]
    dates = [str(x)[:10] for x in frame["date"].tolist()]
    change_pct = round((closes[-1] / closes[0] - 1) * 100, 2) if len(closes) > 1 and closes[0] else None
    return {
        "symbol": symbol,
        "source": "akshare.futures_foreign_hist（新浪外盘）",
        "latest_date": dates[-1],
        "latest_close": round(closes[-1], 4),
        "period_change_pct": change_pct,
        "period_high": round(max(closes), 4),
        "period_low": round(min(closes), 4),
        "bars": [{"date": d, "close": round(c, 4)} for d, c in zip(dates[-20:], closes[-20:])],
        "as_of": dates[-1],
    }


def _akshare_bond(client, spec):
    """中国 + 美国国债收益率曲线（AKShare `bond_zh_us_rate`，新浪/中债口径）。

    本仓库新增桥接。存在理由：判断衰退最直接的信号是 10Y-2Y 利差，而美债官网
    （home.treasury.gov）在境内经常不可达；这条通路同时给中国与美国的期限结构，
    比原来只有一个美国曲线还多一个视角。
    """
    try:
        import akshare as ak
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("需要 akshare：pip install akshare") from exc

    frame = ak.bond_zh_us_rate(start_date=spec.get("start", "20240101"))
    if frame is None or frame.empty:
        raise RuntimeError("akshare 未返回国债收益率数据")
    frame = frame.dropna(subset=["美国国债收益率10年"], how="all")
    latest = frame.iloc[-1]

    def change(column, lookback):
        if len(frame) <= lookback:
            return None
        return round(float(latest[column]) - float(frame[column].iloc[-1 - lookback]), 4)

    def block(prefix):
        spread = float(latest[f"{prefix}国债收益率10年-2年"])
        return {
            "2y": round(float(latest[f"{prefix}国债收益率2年"]), 4),
            "10y": round(float(latest[f"{prefix}国债收益率10年"]), 4),
            "30y": round(float(latest[f"{prefix}国债收益率30年"]), 4),
            "spread_10y_2y": round(spread, 4),
            "inverted": spread < 0,
            "chg_10y_1m": change(f"{prefix}国债收益率10年", 20),
            "chg_spread_1m": change(f"{prefix}国债收益率10年-2年", 20),
        }

    return {
        "date": str(latest["日期"])[:10],
        "china": block("中国"),
        "us": block("美国"),
        "note": "spread 为 10Y-2Y（百分点）；inverted=True 即倒挂；chg_*_1m 为约 20 个交易日的变化。",
        "as_of": str(latest["日期"])[:10],
    }


def _yahoo_chart(client, spec):
    """Yahoo chart 接口直取日线，绕开 yfinance 的 429。

    为什么不用 yfinance：它自带 UA 与 cookie 轮换，反而更容易被 Yahoo 判定为脚本，
    经代理后仍常见 429。直接用 chart 接口 + 浏览器 UA，实测稳定，且能取到商品期货
    （GC=F）、外汇（USDCNY=X）、美股与 ETF——把 price_history 的能力补回来。
    """
    symbol = spec["symbol"]
    interval = spec.get("interval", "d")
    span = {"d": "1y", "w": "5y", "m": "10y"}.get(interval, "1y")
    payload = client.get_json(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol, safe='')}",
        params={"range": spec.get("range", span), "interval": f"1{interval}"},
    )
    results = ((payload or {}).get("chart") or {}).get("result") or []
    if not results:
        raise RuntimeError(f"Yahoo 未返回 {symbol} 的数据")
    node = results[0]
    stamps = node.get("timestamp") or []
    closes = ((node.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    pairs = [
        (dt.datetime.fromtimestamp(ts).date().isoformat(), float(close))
        for ts, close in zip(stamps, closes)
        if close is not None
    ]
    if not pairs:
        raise RuntimeError(f"Yahoo 返回的 {symbol} 价格序列为空")
    pairs = pairs[-int(spec.get("limit", len(pairs))):]
    values = [item[1] for item in pairs]
    change = (values[-1] / values[0] - 1) * 100 if len(values) > 1 and values[0] else None
    return {
        "symbol": symbol,
        "interval": interval,
        "latest_date": pairs[-1][0],
        "latest_close": round(values[-1], 4),
        "period_change_pct": round(change, 2) if change is not None else None,
        "period_high": round(max(values), 4),
        "period_low": round(min(values), 4),
        "bars": [{"date": d, "close": round(c, 4)} for d, c in pairs[-20:]],
        "as_of": pairs[-1][0],
    }


BUILDERS: dict[str, Callable[[Any, dict], dict]] = {
    "polymarket": _polymarket,
    "kalshi": _kalshi,
    "price_history": _price_history,
    "options_chain": _options_chain,
    "yield_curve": _yield_curve,
    "inflation_breakeven": _inflation_breakeven,
    "cftc": _cftc,
    "edgar_insider": _edgar_insider,
    "coingecko": _coingecko,
    "deribit_futures": _deribit_futures,
    "deribit_options": _deribit_options,
    "fear_greed": _fear_greed,
    "eastmoney_quote": _eastmoney_quote,
    "eastmoney_kline": _eastmoney_kline,
    "eastmoney_fund_flow": _eastmoney_fund_flow,
    "eastmoney_sector": _eastmoney_sector,
    "bis_rates": _bis_rates,
    "bis_credit_gap": _bis_credit_gap,
    "worldbank": _worldbank,
    "web_search": _web_search,
    "akshare_foreign": _akshare_foreign,
    "akshare_bond": _akshare_bond,
    "yahoo_chart": _yahoo_chart,
}

REQUIRED = {
    "price_history": ("symbol",),
    "options_chain": ("ticker",),
    "edgar_insider": ("ticker",),
    "eastmoney_quote": ("symbol",),
    "eastmoney_kline": ("symbol",),
    "eastmoney_fund_flow": ("symbol",),
    "web_search": ("query",),
    "akshare_foreign": ("symbol",),
    "yahoo_chart": ("symbol",),
}


# ---------------------------------------------------------------- 预设信号组合

PRESETS: dict[str, list[dict]] = {
    # 「现在风险偏好是扩张还是收缩」——最常用的宏观温度计。
    # 商品与外盘指数用 akshare_foreign（境内可达），收益率曲线留作境外/代理可用时的主力。
    "macro-risk": [
        {"label": "fear_greed", "type": "fear_greed"},
        {"label": "crypto_risk_appetite", "type": "coingecko"},
        {"label": "btc_futures_basis", "type": "deribit_futures", "currency": "BTC"},
        {"label": "gold", "type": "akshare_foreign", "symbol": "GC", "limit": 60},
        {"label": "copper", "type": "akshare_foreign", "symbol": "HG", "limit": 60},
        {"label": "cn_us_curve", "type": "akshare_bond"},
        {"label": "us_curve", "type": "yield_curve"},
        {"label": "cn_10y_via_web", "type": "web_search", "query": "中国 10 年期国债收益率 最新"},
    ],
    # 衰退/周期位置
    "recession": [
        {"label": "cn_us_curve", "type": "akshare_bond"},
        {"label": "us_curve", "type": "yield_curve"},
        {"label": "breakeven_10y", "type": "inflation_breakeven"},
        {"label": "gold", "type": "akshare_foreign", "symbol": "GC", "limit": 24},
        {"label": "copper", "type": "akshare_foreign", "symbol": "HG", "limit": 24},
        {"label": "crude", "type": "akshare_foreign", "symbol": "CL", "limit": 24},
        {"label": "sp500_futures", "type": "akshare_foreign", "symbol": "ES", "limit": 24},
        {"label": "credit_gap", "type": "bis_credit_gap", "countries": ["US", "CN"]},
        {"label": "policy_rates", "type": "bis_rates", "countries": ["US", "CN", "JP", "EU"]},
    ],
    # A 股：个股或板块的资金面（需要 --symbol）
    "a-share": [
        {"label": "quote", "type": "eastmoney_quote", "symbol": "SYMBOL"},
        {"label": "kline", "type": "eastmoney_kline", "symbol": "SYMBOL", "limit": 120},
        {"label": "fund_flow", "type": "eastmoney_fund_flow", "symbol": "SYMBOL", "limit": 10},
        {"label": "sector_rotation", "type": "eastmoney_sector", "kind": "industry", "limit": 15},
        {"label": "fear_greed", "type": "fear_greed"},
    ],
    # 大类资产「现在贵不贵」
    "asset": [
        {"label": "fear_greed", "type": "fear_greed"},
        {"label": "gold", "type": "akshare_foreign", "symbol": "GC", "limit": 24},
        {"label": "sp500_futures", "type": "akshare_foreign", "symbol": "ES", "limit": 24},
        {"label": "crypto_risk_appetite", "type": "coingecko"},
        {"label": "us_curve", "type": "yield_curve"},
    ],
    # 美股期权/波动率（需要 --symbol，默认 SPY）
    "options": [
        {"label": "chain", "type": "options_chain", "ticker": "TICKER"},
        {"label": "underlying", "type": "yahoo_chart", "symbol": "TICKER_PROXY", "interval": "d", "limit": 90},
        {"label": "fear_greed", "type": "fear_greed"},
    ],
    # 加密
    "crypto": [
        {"label": "spot", "type": "coingecko"},
        {"label": "futures_basis", "type": "deribit_futures", "currency": "BTC"},
        {"label": "options_iv", "type": "deribit_options", "currency": "BTC"},
        {"label": "fear_greed", "type": "fear_greed"},
    ],
}


def expand_preset(name: str, symbol: str | None) -> dict[str, dict]:
    if name not in PRESETS:
        raise SystemExit(f"未知预设：{name}。可用：{', '.join(PRESETS)}")
    specs: dict[str, dict] = {}
    for item in PRESETS[name]:
        spec = {k: v for k, v in item.items() if k != "label"}
        for key, value in list(spec.items()):
            if value == "SYMBOL":
                if not symbol:
                    raise SystemExit(f"预设 {name} 需要 --symbol 指定标的代码")
                spec[key] = symbol
            elif value == "TICKER":
                spec[key] = symbol or "SPY"
            elif value == "TICKER_PROXY":
                spec[key] = symbol or "SPY"
        specs[item["label"]] = spec
    return specs


# ---------------------------------------------------------------- 主流程


def load_specs(args) -> dict[str, dict]:
    if args.signals:
        raw = args.signals
        if raw.startswith("@"):
            with open(raw[1:], "r", encoding="utf-8") as handle:
                raw = handle.read()
        parsed = json.loads(raw)
        return parsed.get("signals", parsed) if isinstance(parsed, dict) else parsed
    return expand_preset(args.preset, args.symbol)


def validate(specs: dict[str, dict]) -> None:
    for label, spec in specs.items():
        kind = spec.get("type")
        if kind not in BUILDERS:
            raise SystemExit(f"信号 {label} 的类型 {kind!r} 不支持。可用类型：{', '.join(BUILDERS)}")
        for field in REQUIRED.get(kind, ()):
            if not spec.get(field):
                raise SystemExit(f"信号 {label}（{kind}）缺少必填参数 {field}")


TRANSIENT_MARKERS = ("429", "418", "timed out", "timeout", "connection reset", "temporarily")


def _is_transient(error: BaseException) -> bool:
    """区分「这次挂了下次可能好」与「结构上不可达」。

    限流与超时值得重试；DNS 不通、域名被墙重试一百次也一样，重试反而拖慢整批。
    异常链要一起看：引擎把底层原因裹成 `HttpClientError("request failed: <url>")`，
    「超时」或「429」只出现在 __cause__ 里。
    """
    seen: set[int] = set()
    node: BaseException | None = error
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        text = f"{type(node).__name__}: {node}".lower()
        if any(marker in text for marker in TRANSIENT_MARKERS):
            return True
        node = node.__cause__ or node.__context__
    return False


def _with_retry(fn: Callable[[], Any], attempts: int, backoff: float = 2.0) -> Any:
    last: BaseException | None = None
    for index in range(max(1, attempts)):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — 交给上层决定是否记录失败
            last = exc
            if not _is_transient(exc) or index >= attempts - 1:
                raise
            time.sleep(backoff * (index + 1))
    raise last  # pragma: no cover - 循环正常退出时不会到这里


def run(specs: dict[str, dict], proxy: str | None, timeout: float | None, retries: int = 1) -> dict[str, Any]:
    """按信号类型分配出口：境内源直连、境外源走代理，并套用各自的请求头。"""
    clients: dict[str, Any] = {}

    def client_for(kind: str):
        if kind not in clients:
            destination = None if kind in DIRECT_TYPES else proxy
            clients[kind] = _build_client(destination, HEADER_PROFILES.get(kind))
        return clients[kind]

    tasks = {}
    for label, spec in specs.items():
        kind = spec["type"]
        tasks[label] = (
            lambda s=spec, k=kind: _with_retry(
                lambda: BUILDERS[k](client_for(k), s), attempts=retries + 1
            )
        )

    result = gather(tasks, timeout_seconds=timeout)
    data: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for label, spec in specs.items():
        if label in result.errors:
            errors[label] = f"{type(result.errors[label]).__name__}: {result.errors[label]}"
            continue
        payload = {"type": spec["type"], "horizon": HORIZONS.get(spec["type"], "未标注")}
        payload.update(result.results[label])
        data[label] = payload
    return {
        "data": data,
        "errors": errors,
        "meta": {
            "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "signal_count": len(data),
            "failed_count": len(errors),
            "proxy": proxy or "直连",
            "routing": "境内源直连 / 境外源走代理",
            "note": "单源失败不代表其他信号无效；报告必须标注失败的信号为「不可用」，禁止编造数值。",
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="并发拉取市场信号，输出结构化 JSON")
    parser.add_argument("--preset", default="macro-risk", help=f"信号组合：{', '.join(PRESETS)}")
    parser.add_argument("--signals", help="自定义 spec JSON（字符串或 @文件路径）")
    parser.add_argument("--symbol", help="标的代码（A 股 6 位 / 美股 ticker）")
    parser.add_argument(
        "--proxy",
        default="auto",
        help="HTTP 代理：auto=读系统代理（开 VPN 时默认即可），none=全部直连，或显式 URL",
    )
    parser.add_argument("--timeout", type=float, default=90.0, help="整批超时秒数")
    parser.add_argument("--retries", type=int, default=1, help="瞬时错误（限流/超时）重试次数，默认 1")
    parser.add_argument("--out", help="结果写入文件")
    parser.add_argument("--quiet", action="store_true", help="不打印到 stdout")
    args = parser.parse_args(argv)

    specs = load_specs(args)
    validate(specs)
    proxy = _resolve_proxy(args.proxy)
    payload = run(specs, proxy, args.timeout, retries=max(0, args.retries))

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text)
    if not args.quiet:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        print(text)
    return 0 if payload["data"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
