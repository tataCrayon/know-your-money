# -*- coding: utf-8 -*-
"""AKShare 侧数据体检：确认「行情/板块/估值/利率/基金/商品」各条替代通路此刻通不通。

为什么单独有这个脚本：market-oracle 引擎在国内有大量源不可达，实际分析靠
AKShare 与腾讯/新浪/天天基金兜底。这些替代通路的可用性同样会漂移——
2026-10-26 当天，东方财富 push2 段从「限流」恶化到「整段连接重置」，导致
行情、个股资金流、板块成分、ETF 历史同时失效，而腾讯/新浪/中债口径仍正常。
没有这张表，就只能靠一条条试错。

用法：
    python scripts/probe_akshare.py
    python scripts/probe_akshare.py --json out.json
    python scripts/probe_akshare.py --only 估值,利率

判定：返回非空 DataFrame = 通；抛错 = 不通。只回答连通性，不评价数据质量。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time

#: 让 requests（AKShare 底层）绕过 Windows 系统代理，避免 Clash 把境内域名也代理走。
os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")

import akshare as ak  # noqa: E402


def _today(days_back: int = 0) -> str:
    return (dt.date.today() - dt.timedelta(days=days_back)).strftime("%Y%m%d")


#: (分组, 名称, 调用, 宿主说明)
CHECKS: list[tuple[str, str, object, str]] = [
    # ---- 行情 ----
    ("行情", "stock_zh_index_daily 沪深300", lambda: ak.stock_zh_index_daily(symbol="sh000300"), "新浪"),
    ("行情", "stock_zh_a_daily 个股日线", lambda: ak.stock_zh_a_daily(symbol="sh600519", adjust="qfq"), "新浪"),
    ("行情", "stock_zh_a_hist_tx 个股日线", lambda: ak.stock_zh_a_hist_tx(symbol="sz000798", start_date=_today(90), end_date=_today()), "腾讯"),
    ("行情", "stock_zh_index_spot_em 指数实时", lambda: ak.stock_zh_index_spot_em(symbol="沪深重要指数"), "东财 push2"),
    ("行情", "stock_zh_a_hist 个股日线", lambda: ak.stock_zh_a_hist(symbol="600519", period="daily", start_date=_today(90), end_date=_today(), adjust="qfq"), "东财 push2his"),
    # ---- 板块 / 资金流 ----
    ("板块资金", "stock_sector_spot 行业板块", lambda: ak.stock_sector_spot(indicator="新浪行业"), "新浪（延时快照）"),
    ("板块资金", "stock_fund_flow_industry 行业资金流", lambda: ak.stock_fund_flow_industry(symbol="即时"), "同花顺"),
    ("板块资金", "stock_fund_flow_individual 个股资金流", lambda: ak.stock_fund_flow_individual(symbol="即时"), "同花顺"),
    ("板块资金", "stock_individual_fund_flow 个股拆单", lambda: ak.stock_individual_fund_flow(stock="600519", market="sh"), "东财 push2his"),
    ("板块资金", "stock_board_industry_name_em 行业列表", lambda: ak.stock_board_industry_name_em(), "东财 push2"),
    # ---- 期权 ----
    ("期权", "option_finance_board 50ETF期权", lambda: ak.option_finance_board(symbol="华夏上证50ETF期权", end_month="2612"), "上交所"),
    ("期权", "option_sse_greeks_sina 希腊值", lambda: ak.option_sse_greeks_sina(symbol="10009158"), "新浪"),
    # ---- 估值 / 利率 ----
    ("估值", "stock_index_pe_lg 指数 PE 序列", lambda: ak.stock_index_pe_lg(symbol="沪深300"), "乐咕乐股（慢）"),
    ("估值", "stock_zh_index_value_csindex 指数估值", lambda: ak.stock_zh_index_value_csindex(symbol="000300"), "中证官网（慢）"),
    ("利率", "bond_zh_us_rate 中美国债", lambda: ak.bond_zh_us_rate(start_date=_today(400)), "中债/新浪"),
    ("利率", "macro_china_lpr LPR", lambda: ak.macro_china_lpr(), "东财宏观"),
    # ---- 基金 ----
    ("基金", "fund_name_em 全量基金名录", lambda: ak.fund_name_em(), "天天基金"),
    ("基金", "fund_etf_category_sina ETF 行情", lambda: ak.fund_etf_category_sina(symbol="ETF基金"), "新浪"),
    ("基金", "fund_open_fund_info_em 场外净值", lambda: ak.fund_open_fund_info_em(symbol="003634", indicator="单位净值走势"), "天天基金"),
    ("基金", "fund_etf_fund_info_em ETF 净值", lambda: ak.fund_etf_fund_info_em(fund="159865", start_date=_today(400), end_date=_today()), "天天基金"),
    ("基金", "fund_etf_hist_em ETF 历史行情", lambda: ak.fund_etf_hist_em(symbol="159865", period="daily", start_date=_today(90), end_date=_today(), adjust="qfq"), "东财 push2his"),
    # ---- 商品 / 宏观 ----
    ("商品宏观", "futures_foreign_hist 外盘商品", lambda: ak.futures_foreign_hist(symbol="GC"), "新浪外盘"),
    ("商品宏观", "futures_main_sina 国内期货主力", lambda: ak.futures_main_sina(symbol="RB0"), "新浪"),
]


def run(selected: set[str] | None) -> list[dict]:
    report = []
    for group, name, call, host in CHECKS:
        if selected and group not in selected:
            continue
        started = time.time()
        try:
            value = call()
            rows = getattr(value, "shape", (0,))[0] if hasattr(value, "shape") else 0
            ok = bool(rows)
            report.append({"分组": group, "名称": name, "宿主": host, "ok": ok,
                           "rows": int(rows), "耗时s": round(time.time() - started, 1), "错误": None})
        except Exception as exc:  # noqa: BLE001 — 体检要吞掉所有异常继续跑
            report.append({"分组": group, "名称": name, "宿主": host, "ok": False,
                           "rows": 0, "耗时s": round(time.time() - started, 1),
                           "错误": f"{type(exc).__name__}: {exc}"[:100]})
        item = report[-1]
        mark = "OK  " if item["ok"] else "FAIL"
        detail = f"{item['rows']} 行" if item["ok"] else item["错误"]
        print(f"  [{mark}] {item['分组']:6s} {item['名称']:34s} {item['耗时s']:>5.1f}s  {detail}", flush=True)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AKShare 侧数据源体检")
    parser.add_argument("--only", help="只测这些分组，逗号分隔：行情,板块资金,期权,估值,利率,基金,商品宏观")
    parser.add_argument("--json", help="结果写入 JSON 文件")
    args = parser.parse_args(argv)

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    selected = {s.strip() for s in args.only.split(",")} if args.only else None
    print(f"AKShare {ak.__version__} ｜ {dt.datetime.now().astimezone().isoformat(timespec='seconds')}")
    report = run(selected)

    ok = [r for r in report if r["ok"]]
    print(f"\n通 {len(ok)}/{len(report)}")
    broken = [r for r in report if not r["ok"]]
    if broken:
        hosts = sorted({r["宿主"] for r in broken})
        print("不通的宿主：" + "、".join(hosts))
        print("→ 报告里必须标注这些通路不可用，改用同组的其它宿主或降级（见 references/signal-routing.md 第六节）。")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump({"version": ak.__version__, "report": report}, handle, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
