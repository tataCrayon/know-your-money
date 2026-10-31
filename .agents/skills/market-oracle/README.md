# market-oracle（市场先知）

用交易数据读市场共识：一件事市场认为概率多大、分歧在哪、谁在什么时间尺度上下注。

- 主指令：`SKILL.md`
- 信号路由：`references/signal-routing.md`
- 角色与精简报告骨架：`references/agent-persona.md`
- 上游 API 速查：`references/providers.md`、`references/symbols.md`
- 取数：`scripts/oracle_signals.py`；体检：`scripts/probe_sources.py`

## 迁移出处

| 项 | 来源 | 处理方式 |
|---|---|---|
| 数据引擎 `digital_oracle/` + `tests/` + `references/providers.md` + `references/symbols.md` + `LICENSE` | [komako-workshop/digital-oracle](https://github.com/komako-workshop/digital-oracle) v1.0.3（MIT） | **原样搬入**，未改一行，便于日后与上游 diff 升级 |
| 方法论（五条铁律、六步工作流、四维分析、权重原则） | 同上 `SKILL.md` | 重写为学习向中文版：老师身份、免责与溯源、概念对照、境内可用性 |
| 角色口吻、信号菜单、输出骨架、硬约束 | 原 `digital-oracle-agent`（Agent 定义） | **降级为 `references/agent-persona.md`**：只留角色与模板 |
| 平台权限矩阵、SQL 安全、CMDB 发布、审计日志 | 同上 | **不迁移**——属 portal-bot 平台基础设施，与本仓库无关 |
| `scripts/oracle_signals.py`、`scripts/probe_sources.py`、`scripts/probe_akshare.py` | 本仓库新增 | 替代原平台的 `gather_signals` 工具；两个体检脚本分别覆盖引擎侧与 AKShare 侧 |
| `akshare_foreign` 桥接（商品/外盘指数期货） | 本仓库新增 | yfinance 被限流时的境内替代 |
| `akshare_bond` 桥接（中美国债收益率） | 本仓库新增 | 美债官网不可达时的境内替代，含 10Y-2Y 倒挂判断 |

## 为什么加了 `akshare_foreign`

实测（2026-10-26）：Yahoo/yfinance 在境内多为 429，预测市场、CFTC、SEC、美债、世界银行均不可达。若不加这条通路，「至少 3 个独立维度」在境内几乎凑不齐。`akshare.futures_foreign_hist`（新浪外盘）可稳定取到黄金 GC、白银 SI、原油 CL、铜 HG、标普 ES、纳指 NQ 的当日日线，是目前性价比最高的一条补齐。

## 出口与代理（重要）

`urllib` 不读系统代理，所以「本机开了 VPN」不等于「脚本走了 VPN」。脚本内置了分层路由：

| 用法 | 行为 |
|---|---|
| `--proxy auto`（默认） | 读 Windows 系统代理（Clash/V2Ray 写在注册表里），开了 VPN 自动生效 |
| `--proxy none` | 全部直连，用于排查「是不是代理导致的问题」 |
| `--proxy http://host:port` | 显式指定 |

境内源（东财/腾讯/新浪/AKShare）**强制直连**，境外源走代理；CNN 需要浏览器 UA、SEC 需要带邮箱的 UA，均已按源内置。开了代理后的实测见 `SKILL.md` 的可用性一节。

## 验收命令

```bash
cd .agents/skills/market-oracle

# 1) 引擎完整性与可跑性（离线快照测试，应全绿）
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests -q -p no:cacheprovider

# 2) 数据源体检（两条链路都要测：引擎侧 + AKShare 侧）
python scripts/probe_sources.py
python scripts/probe_akshare.py

# 3) 端到端取数（6 个信号里有失败属正常，看 errors 字段）
python scripts/oracle_signals.py --preset macro-risk
python scripts/oracle_signals.py --preset recession

# 4) A 股资金面
python scripts/oracle_signals.py --preset a-share --symbol 600519
```

## 三个测试问题（验证触发与输出）

1. 「现在市场是避险还是去杠杆？」→ 应走 `macro-risk`，并给出「黄金与风险资产同跌 = 去杠杆」这类判别式结论
2. 「A 股半导体现在资金面怎么样？」→ 应走 `--preset a-share`，区分主力与散户，并声明无预测市场定价
3. 「衰退概率多大？」→ 应先声明美债曲线不可用、降置信度，**不许**给出无依据的干净概率

## 回滚

本目录为独立新增，回滚即删除：

```powershell
Remove-Item -Recurse -Force .agents\skills\market-oracle
```

同时把 `AGENTS.md` 技能表中的 `market-oracle` 一行删掉即可，无其他耦合。

## 升级到上游新版本

```powershell
# 取上游新版本后，只替换引擎与两份上游参考，保留本仓库的 SKILL.md 与 scripts/
robocopy <upstream>\digital_oracle .\digital_oracle /E /XD __pycache__
robocopy <upstream>\tests          .\tests          /E /XD __pycache__
Copy-Item <upstream>\references\*.md .\references\ -Force
```

升级后重跑「验收命令」的 1、2 两项：测试全绿说明引擎替换成功，体检表用来更新 `SKILL.md` 的可用性一节。
