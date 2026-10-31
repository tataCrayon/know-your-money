# -*- coding: utf-8 -*-
"""数据源体检：确认「此刻哪些信号真的取得到」。

为什么需要它：网络环境会变（限流、被墙、上游改版），而 SKILL.md 里写死的可用性
表会过期。动手选信号前先跑一遍，比在报告里写「该信号不可用」再解释为什么强得多。

用法：
    python scripts/probe_sources.py                # 全量探测（短线超时，约 20-40 秒）
    python scripts/probe_sources.py --proxy http://127.0.0.1:7897
    python scripts/probe_sources.py --only eastmoney_quote,fear_greed
    python scripts/probe_sources.py --json out.json

判定标准：拿到非空数据 = 可用；抛错或返回空 = 不可用。这里只回答「通不通」，
不评价数据质量。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from digital_oracle import gather  # noqa: E402
from oracle_signals import (  # noqa: E402
    BUILDERS,
    DIRECT_TYPES,
    HEADER_PROFILES,
    _build_client,
    _resolve_proxy,
)

#: 每个数据源的最小探测参数——够触发一次真实请求即可。
PROBES: list[tuple[str, str, dict]] = [
    ("eastmoney_quote", "eastmoney_quote", {"symbol": "600519"}),
    ("eastmoney_kline", "eastmoney_kline", {"symbol": "600519", "limit": 5}),
    ("eastmoney_fund_flow", "eastmoney_fund_flow", {"symbol": "600519", "limit": 5}),
    ("eastmoney_sector", "eastmoney_sector", {"kind": "industry", "limit": 5}),
    ("fear_greed", "fear_greed", {}),
    ("deribit_futures", "deribit_futures", {"currency": "BTC"}),
    ("deribit_options", "deribit_options", {"currency": "BTC"}),
    ("coingecko", "coingecko", {"coin_ids": ("bitcoin",), "global": False}),
    ("bis_rates", "bis_rates", {"countries": ("US",), "start_year": dt.date.today().year - 1}),
    ("bis_credit_gap", "bis_credit_gap", {"countries": ("US",)}),
    ("web_search", "web_search", {"query": "VIX index level"}),
    ("polymarket", "polymarket", {"limit": 2}),
    ("kalshi", "kalshi", {"series_ticker": "KXFED", "limit": 2}),
    ("yield_curve", "yield_curve", {}),
    ("inflation_breakeven", "inflation_breakeven", {}),
    ("cftc", "cftc", {"commodity": "GOLD", "limit": 2}),
    ("edgar_insider", "edgar_insider", {"ticker": "AAPL", "limit": 3}),
    ("worldbank", "worldbank", {"countries": ("US",)}),
    ("price_history", "price_history", {"symbol": "SPY", "limit": 5}),
    ("yahoo_chart", "yahoo_chart", {"symbol": "SPY", "limit": 5}),
    ("options_chain", "options_chain", {"ticker": "SPY"}),
    ("akshare_foreign", "akshare_foreign", {"symbol": "GC", "limit": 5}),
    ("akshare_bond", "akshare_bond", {}),
]


def run_probes(selected: set[str] | None, proxy: str | None, timeout: float) -> dict[str, dict]:
    chosen = [p for p in PROBES if not selected or p[0] in selected]
    clients: dict[str, object] = {}

    def client_for(kind: str):
        """与 oracle_signals 同一套出口策略：境内直连、境外走代理、按源设请求头。"""
        if kind not in clients:
            clients[kind] = _build_client(
                None if kind in DIRECT_TYPES else proxy,
                HEADER_PROFILES.get(kind),
                timeout=22.0,
            )
        return clients[kind]

    tasks = {label: (lambda s=spec, k=kind: BUILDERS[k](client_for(k), s)) for label, kind, spec in chosen}
    result = gather(tasks, timeout_seconds=timeout)

    report: dict[str, dict] = {}
    for label, kind, _spec in chosen:
        if label in result.errors:
            error = result.errors[label]
            report[label] = {"ok": False, "type": kind, "error": f"{type(error).__name__}: {error}"[:160]}
        else:
            payload = result.results[label]
            report[label] = {"ok": bool(payload), "type": kind, "as_of": payload.get("as_of") if isinstance(payload, dict) else None}
    return report


def render(report: dict[str, dict]) -> None:
    ok = [k for k, v in report.items() if v["ok"]]
    bad = [k for k, v in report.items() if not v["ok"]]
    print(f"可用 {len(ok)}/{len(report)}")
    for label, item in report.items():
        mark = "OK  " if item["ok"] else "FAIL"
        detail = item.get("as_of") or item.get("error", "")
        print(f"  [{mark}] {label:20s} {detail}")
    if bad:
        print("\n不可用信号：报告里必须标注「不可用」，不得编造数值。其次考虑降级到替代信号（见 references/signal-routing.md 第六节）。")
    print(f"\n探测时间：{dt.datetime.now().astimezone().isoformat(timespec='seconds')}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="数据源健康检查")
    parser.add_argument(
        "--proxy",
        default="auto",
        help="HTTP 代理：auto=读系统代理（开 VPN 时默认即可），none=全部直连，或显式 URL",
    )
    parser.add_argument("--only", help="只探这些源（逗号分隔）")
    parser.add_argument("--timeout", type=float, default=60.0, help="整批超时秒数")
    parser.add_argument("--json", help="把结果写入 JSON 文件")
    args = parser.parse_args(argv)

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    proxy = _resolve_proxy(args.proxy)
    print(f"出口策略：境内源直连 / 境外源{'走 ' + proxy if proxy else '直连（未检测到代理）'}")

    selected = {s.strip() for s in args.only.split(",")} if args.only else None
    report = run_probes(selected, proxy, args.timeout)
    render(report)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
