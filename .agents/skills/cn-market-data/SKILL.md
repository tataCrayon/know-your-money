---
name: cn-market-data
description: A股/基金/指数免费数据查询技能（基于 AKShare，无需 API Key）。当用户查询 A股行情、指数点位与估值分位、基金/ETF 历史净值、板块资金流向、财务指标，或 market-analysis/company-valuation 技能需要中国市场数据时使用。替代需要 EASTMONEY_APIKEY 的 eastmoney_financial_data。
---

# 中国市场免费数据技能（AKShare）

数据源为东方财富/新浪/乐咕乐股等公开接口（经 AKShare 聚合），**免费、无需任何 Key**。已在 Python 3.11 + akshare 1.18.94 实测验证。

## 使用规则

- 首次调用较慢（akshare 导入约 5~10 秒），属正常。
- **每次输出必须注明**：数据日期、接口名（便于复核）。AKShare 返回的是接口抓取时点的数据，非实时推送。
- 估值分位必须写明**计算窗口**（如「近10年」）和计算方法。
- 接口偶尔因上游改版失效：报错时先检查 akshare 版本并 `pip install -U akshare`，再查官方文档 https://akshare.akfamily.xyz/ 对应章节。

## 已验证的常用接口（2026-09-07 实测）

```python
import akshare as ak

# 1) 指数实时行情（沪深300/中证500/创业板指等）
df = ak.stock_zh_index_spot_em(symbol='沪深重要指数')
# 注意：symbol 参数早期版本为 '沪深主要指数'，1.18.x 用 '沪深重要指数'

# 2) ETF/场内基金历史行情（如 510300 沪深300ETF）
f = ak.fund_etf_hist_em(symbol='510300', period='daily',
                        start_date='20260901', end_date='20260907', adjust='qfq')

# 3) 指数估值（乐咕乐股）：PE/PB 历史序列 → 自行计算分位
pe = ak.stock_index_pe_lg(symbol='沪深300')
# 常用列：'滚动市盈率'。分位示例（近10年窗口）：
# pct = (pe.tail(2500)['滚动市盈率'] < 最新值).mean()
```

## 待按需查阅的接口族（用前先看官方文档确认签名）

- 个股行情/财务：`ak.stock_zh_a_spot_em()`、`ak.stock_financial_abstract(symbol)`
- 场外基金净值：`ak.fund_open_fund_info_em(symbol='110022')`
- 板块资金流：`ak.stock_fund_flow_industry(symbol='即时')`
- 指数历史：`ak.index_zh_a_hist(symbol='000300', period='daily')`

## 与其他技能的配合

- `market-analysis` 分析 A股标的时，由本技能提供数据，分析技能负责框架与成文。
- `company-valuation` 默认 yfinance（美股）；分析 A 股时数据改由本技能供给，估值方法仍按其三法流程执行。
- 免责与溯源纪律见仓库 `AGENTS.md`。
