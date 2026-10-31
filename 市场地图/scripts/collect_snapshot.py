# -*- coding: utf-8 -*-
"""多市场数据快照采集器 v2。

用途：为《市场地图》知识库提供一次性、可复现的行情读数。
运行：python 市场地图/scripts/collect_snapshot.py
输出：市场地图/data/snapshot-YYYY-MM-DD.json（同日覆盖）+ data/collect.log

设计原则
- 每个数据源独立 try/except：取不到就写 null 并记录原因，**绝不补数、绝不猜数**。
- 所有数值带 source 字段，写报告时可直接引用。
- 只做只读抓取，不下单、不写远端。

已知不可用（2026-10 实测，勿再试）
- push2his kline / clist/get / stock_*_spot_em / fund_etf_spot_em：RemoteDisconnected
- Yahoo(yfinance) 403；stooq 返回 HTML；CoinGecko/Binance/OKX 不通；CNN F&G 418
"""
import io
import json
import logging
import os
import socket
import time
import warnings

socket.setdefaulttimeout(25)
warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)

import numpy as np
import pandas as pd
import requests

import akshare as ak

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
os.makedirs(DATA_DIR, exist_ok=True)
LOG = io.open(os.path.join(DATA_DIR, "collect.log"), "w", encoding="utf-8")
SNAP = {}


def log(msg):
    LOG.write(str(msg) + "\n")
    LOG.flush()


def safe(name, fn, default=None, retries=1):
    t = time.time()
    err = ""
    for i in range(retries):
        try:
            val = fn()
            log("OK   %-40s %5.1fs" % (name, time.time() - t) + (" (retry %d)" % i if i else ""))
            return val
        except Exception as e:
            err = "%s: %s" % (type(e).__name__, str(e)[:160])
            if i + 1 < retries:
                time.sleep(1.2)
    log("ERR  %-40s %5.1fs %s" % (name, time.time() - t, err))
    return default


def num(x, nd=2):
    try:
        v = float(x)
        return None if np.isnan(v) else round(v, nd)
    except Exception:
        return None


def nump(x, nd=2):
    """带百分号/---- 的字符串转数字。"""
    if x is None:
        return None
    if isinstance(x, str):
        x = x.replace("%", "").replace(",", "").strip()
        if x in ("", "---", "--", "-", "nan"):
            return None
    return num(x, nd)


def lg_percentiles(df, value_col, prefix):
    """乐咕估值序列的分位。乐咕是月频，尾部 2500 行会覆盖全部历史，

    所以这里显式给出窗口天数与样本数，避免把"全历史分位"说成"10 年分位"。
    """
    d = pd.DataFrame({"date": pd.to_datetime(df["日期"], errors="coerce"),
                      "value": pd.to_numeric(df[value_col], errors="coerce")}
                     ).dropna().sort_values("date")
    if len(d) < 13:
        return {}
    last = d["value"].iloc[-1]
    out = {prefix + "_lg": num(last), prefix + "_lg_n": int(len(d)),
           prefix + "_lg_start": d["date"].iloc[0].strftime("%Y-%m-%d")}
    if prefix == "pe":
        out["pe_ttm_lg"] = num(last)

    def _pct(sub):
        return num((sub < last).mean() * 100, 1) if len(sub) >= 30 else None

    for label, years in (("all", None), ("10y", 10), ("5y", 5)):
        sub = d if years is None else d[d["date"] >= d["date"].iloc[-1] - pd.DateOffset(years=years)]
        out["%s_pct_%s" % (prefix, label)] = _pct(sub["value"])
        out["%s_%s_n" % (prefix, label)] = int(len(sub))
    return out


def series_stats(df, col="close", date_col=None):
    if df is None or len(df) == 0:
        return {}
    d = df.copy()
    dc = date_col or ("date" if "date" in d.columns else "日期")
    if dc not in d.columns:
        return {}
    d[dc] = pd.to_datetime(d[dc], errors="coerce")
    d = d.dropna(subset=[dc]).sort_values(dc)
    d = d[~d[dc].duplicated(keep="last")]
    s = pd.to_numeric(d[col], errors="coerce").dropna()
    if s.empty:
        return {}
    dts = d.loc[s.index, dc]
    last = float(s.iloc[-1])
    out = {"date": str(dts.iloc[-1].date()), "last": num(last)}
    for n, label in [(1, "chg1d"), (5, "ret5d"), (20, "ret20d"), (60, "ret60d"), (120, "ret120d"), (250, "ret1y")]:
        if len(s) > n:
            prev = float(s.iloc[-1 - n])
            out[label] = num((last / prev - 1) * 100) if prev else None
    mask = dts.dt.year == dts.iloc[-1].year
    if mask.any():
        base = float(s[mask].iloc[0])
        out["ytd"] = num((last / base - 1) * 100) if base else None
    tail = s.tail(250)
    out["high250"] = num(tail.max())
    out["low250"] = num(tail.min())
    if tail.max():
        out["dd_from_high250"] = num((last / tail.max() - 1) * 100)
    rets = s.pct_change().dropna()
    if len(rets) >= 20:
        out["vol20d_ann"] = num(float(rets.tail(20).std() * np.sqrt(252) * 100))
    if len(rets) >= 60:
        out["vol60d_ann"] = num(float(rets.tail(60).std() * np.sqrt(252) * 100))
    return out


EM_H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
        "Referer": "https://quote.eastmoney.com/"}


def em_ulist(secids, fields="f1,f2,f3,f4,f12,f13,f14,f20,f6", retries=9):
    """东财批量行情。实测极不稳定：单次成功率约 1/8，必须多重试。"""
    for i in range(retries):
        try:
            r = requests.get("https://push2.eastmoney.com/api/qt/ulist.np/get",
                             params={"fltt": 2, "secids": secids, "fields": fields},
                             headers=EM_H, timeout=15)
            data = r.json().get("data") or {}
            got = {it["f12"]: it for it in (data.get("diff") or [])}
            if got:
                return got
        except Exception:
            pass
        time.sleep(2.5 + i * 0.5)
    return {}


# ---------------------------------------------------------------- 1. A 股指数
CN_INDEX = {
    "000001": ("上证指数", "sh000001"),
    "399001": ("深证成指", "sz399001"),
    "000300": ("沪深300", "sh000300"),
    "000905": ("中证500", "sh000905"),
    "000852": ("中证1000", "sh000852"),
    "000016": ("上证50", "sh000016"),
    "399006": ("创业板指", "sz399006"),
    "000688": ("科创50", "sh000688"),
    "899050": ("北证50", "bj899050"),
    "000012": ("国债指数", "sh000012"),
    "399975": ("证券公司", "sz399975"),
    "000827": ("中证环保", "sh000827"),
}


def collect_cn_index():
    out = {}
    secids = ",".join(["1." + c if c[0] in "09" else "0." + c for c in CN_INDEX])
    live = safe("em_ulist_cn_index", lambda: em_ulist(secids), default={}, retries=3)
    for code, (name, sym) in CN_INDEX.items():
        item = {"name": name, "symbol": sym}
        it = live.get(code)
        if it:
            item.update({"last": num(it.get("f2")), "chg1d": num(it.get("f3")),
                         "amount": num(it.get("f6"), 0), "mktcap": num(it.get("f20"), 0)})
        hist = safe("stock_zh_index_daily_" + sym,
                    lambda s=sym: ak.stock_zh_index_daily(symbol=s), retries=2)
        if hist is not None and len(hist):
            st = series_stats(hist)
            for k, v in st.items():
                item.setdefault(k, v)
            if "last" not in item or item.get("last") is None:
                item["last"] = st.get("last")
                item["date"] = st.get("date")
        out[code] = item

    # 乐咕 估值分位（PE/PB）。注意：乐咕这份序列是**月频**（沪深300 约 259 个月，起点 2005 前后），
    # 所以"分位"必须同时报出窗口与样本数，不能笼统写"10 年分位"。
    lg_manual = {}
    lg_path = os.path.join(DATA_DIR, "manual-lg-%s.json" % SNAP["meta"]["trade_date"])
    if os.path.exists(lg_path):
        lg_manual = json.load(io.open(lg_path, encoding="utf-8"))
    for code, lg in [("000300", "沪深300"), ("000905", "中证500"),
                     ("000852", "中证1000"), ("000016", "上证50")]:
        got_live = False
        pe = safe("stock_index_pe_lg_" + lg, lambda s=lg: ak.stock_index_pe_lg(symbol=s), retries=4)
        if pe is not None and len(pe):
            for k, v in lg_percentiles(pe, "滚动市盈率", "pe").items():
                out.setdefault(code, {"name": lg})
                out[code][k] = v
            out[code]["pe_lg_date"] = str(pe["日期"].iloc[-1])
            got_live = True
        pb = safe("stock_index_pb_lg_" + lg, lambda s=lg: ak.stock_index_pb_lg(symbol=s), retries=4)
        if pb is not None and len(pb):
            for k, v in lg_percentiles(pb, "市净率", "pb").items():
                out.setdefault(code, {"name": lg})
                out[code][k] = v
            got_live = True
        if not got_live and code in lg_manual:
            # 抄录兜底：只补该 code 确实抄到的字段，并打标记，绝不混充实时值
            m = lg_manual[code]
            out.setdefault(code, {"name": lg})
            for k, v in m.items():
                if k != "name":
                    out[code][k] = v
            out[code]["_fallback"] = {"file": os.path.basename(lg_path),
                                      "captured_at": lg_manual["_meta"]["captured_at"],
                                      "note": lg_manual["_meta"]["percentile_note"]}
            log("WARN 乐咕 %s 接口失败，已合并抄录值 %s" % (lg, lg_path))

    # 中证官网（含科创50/主题），延迟约 20 个交易日
    for code in ["000300", "000905", "000852", "000016", "000688", "000001",
                 "930713", "H30184", "930599", "931865"]:
        df = safe("csindex_" + code,
                  lambda s=code: ak.stock_zh_index_value_csindex(symbol=s), retries=1)
        if df is not None and len(df):
            r = df.iloc[-1]
            out.setdefault(code, {"name": str(r.get("指数中文简称"))})
            out[code].update({"pe_csi": num(r.get("市盈率1")), "pe_csi2": num(r.get("市盈率2")),
                              "div_yield_csi": num(r.get("股息率1")),
                              "csi_date": str(r.get("日期"))})
    return out


# ---------------------------------------------------------------- 2. 港股
def collect_hk():
    out = {}
    for sym, name in [("HSI", "恒生指数"), ("HSCEI", "国企指数"), ("HSTECH", "恒生科技")]:
        df = safe("hk_index_daily_" + sym,
                  lambda s=sym: ak.stock_hk_index_daily_sina(symbol=s), retries=2)
        st = series_stats(df) if df is not None else {}
        st["name"] = name
        out[sym] = st
    spot = safe("hk_index_spot_sina", ak.stock_hk_index_spot_sina)
    if spot is not None and len(spot):
        for code, name in [("VHSI", "恒指波幅指数"), ("HSTECH", None)]:
            row = spot[spot["代码"] == code]
            if len(row) and name:
                out.setdefault(code, {}).update({"name": name, "last": num(row["最新价"].iloc[0]),
                                                 "chg1d": num(row["涨跌幅"].iloc[0])})
    return out


SINA_H = {"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"}


def sina_batch(codes):
    """新浪批量行情：返回 {code: [字段...]}。实测 A股ETF/美股/外汇/REIT 均可用。"""
    out = {}
    for i in range(0, len(codes), 40):
        chunk = codes[i:i + 40]
        try:
            r = requests.get("https://hq.sinajs.cn/list=" + ",".join(chunk),
                             headers=SINA_H, timeout=15)
            r.encoding = "gbk"
        except Exception as e:
            log("ERR  sina_batch_%d %s" % (i, str(e)[:80]))
            continue
        for line in r.text.split("\n"):
            if "=" not in line or '=""' in line:
                continue
            key = line.split("=")[0].split("hq_str_")[-1].strip()
            body = line.split('"')[1].split(",")
            if body and body[0]:
                out[key] = body
    return out


# ---------------------------------------------------------------- 3. 美股
US_STOCKS = {"nvda": "英伟达", "aapl": "苹果", "msft": "微软", "googl": "谷歌A",
             "amzn": "亚马逊", "meta": "Meta", "tsla": "特斯拉", "baba": "阿里巴巴", "pdd": "拼多多"}


def collect_us():
    out = {}
    for sym, name in [(".INX", "标普500"), (".NDX", "纳斯达克100"),
                      (".IXIC", "纳斯达克综合"), (".DJI", "道琼斯")]:
        df = safe("us_index_" + sym,
                  lambda s=sym: ak.index_us_stock_sina(symbol=s), retries=2)
        st = series_stats(df) if df is not None else {}
        st["name"] = name
        out[sym.strip(".")] = st

    def vix():
        r = requests.get("https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv",
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        df = pd.read_csv(io.StringIO(r.text))
        df.columns = [c.lower() for c in df.columns]
        return df

    df = safe("cboe_vix", vix)
    if df is not None and len(df):
        st = series_stats(df, col="close", date_col="date")
        st["name"] = "VIX"
        out["VIX"] = st

    # 个股：新浪 gb_ 接口（东财批量接口不稳）
    # 字段位：1 现价 2 涨跌% 12 总市值 13 EPS 14 市盈率
    q = sina_batch(["gb_" + k for k in US_STOCKS])
    stocks = {}
    for k, name in US_STOCKS.items():
        b = q.get("gb_" + k)
        if not b or len(b) < 15:
            continue
        last, chg = nump(b[1], 3), nump(b[2])
        stocks[k.upper()] = {"name": name, "last": last, "chg1d": chg,
                             "prev": num(last / (1 + chg / 100), 3) if last and chg is not None else None,
                             "mktcap": nump(b[12], 0), "eps": nump(b[13]), "pe": nump(b[14])}
    if stocks:
        out["stocks"] = stocks
    return out


# ---------------------------------------------------------------- 4. 日本/欧洲/新兴
GLOBAL_EQ = {
    "jp": {"N225": "日经225"},
    "eu": {"FTSE": "英国富时100", "GDAXI": "德国DAX", "FCHI": "法国CAC40", "SX5E": "欧洲斯托克50"},
    "em": {"KS11": "韩国KOSPI", "SENSEX": "印度SENSEX", "STI": "新加坡海峡时报",
           "AS51": "澳洲标普200", "BDI": "波罗的海BDI", "TWII": "台湾加权"},
}


def collect_global_equity():
    """全球指数：一次批量请求全部 secid（实测分批/单只反而易被拒），失败多重试。
    实时接口长期不可用时，合并 data/manual-YYYY-MM-DD.json 中已人工抄录的读数。"""
    out = {}
    flat = [(g, code, name) for g, d in GLOBAL_EQ.items() for code, name in d.items()]
    secs = ",".join("100." + code for _, code, _ in flat)
    live = safe("em_ulist_global_all", lambda: em_ulist(secs, retries=20), default={}, retries=1)
    for g, code, name in flat:
        it = live.get(code) or {}
        out.setdefault(g, {})[code] = {"name": name, "last": num(it.get("f2")),
                                       "chg1d": num(it.get("f3"))}
    out["_live_ok"] = len(live)
    if not live:
        manual_path = os.path.join(DATA_DIR, "manual-%s.json" % SNAP["meta"]["trade_date"])
        if os.path.exists(manual_path):
            m = json.load(io.open(manual_path, encoding="utf-8"))
            for g in ("jp", "eu", "em"):
                for code, v in (m.get(g) or {}).items():
                    out.setdefault(g, {})[code] = dict(v)
            out["_fallback"] = {"file": os.path.basename(manual_path),
                                "captured_at": m.get("captured_at"), "source": m.get("source")}
            log("WARN global_equity 实时接口失败，已合并手工快照 " + manual_path)
    return out


# ---------------------------------------------------------------- 5. 商品
COMM = {"GC": "COMEX黄金", "SI": "COMEX白银", "HG": "COMEX铜", "CL": "WTI原油",
        "NG": "NYMEX天然气", "CT": "ICE棉花", "KC": "ICE咖啡", "CC": "ICE可可"}
COMM_CN = {"RB0": "螺纹钢", "I0": "铁矿石", "CU0": "沪铜", "AU0": "沪金",
           "AG0": "沪银", "SC0": "原油(SC)", "M0": "豆粕", "CF0": "郑棉"}


def collect_commodity():
    out = {}
    for s, name in COMM.items():
        df = safe("futures_foreign_" + s, lambda x=s: ak.futures_foreign_hist(symbol=x), retries=2)
        st = series_stats(df) if df is not None else {}
        st["name"] = name
        out[s] = st
    out["cn"] = {}
    for s, name in COMM_CN.items():
        df = safe("futures_main_sina_" + s, lambda x=s: ak.futures_main_sina(symbol=x), retries=2)
        if df is not None and len(df):
            df = df.rename(columns={"日期": "date", "收盘价": "close"})
            st = series_stats(df)
            st["name"] = name
            out["cn"][s] = st
    return out


# ---------------------------------------------------------------- 6. 利率与债券
def collect_rates():
    out = {}
    df = safe("bond_zh_us_rate", lambda: ak.bond_zh_us_rate(start_date="20250101"), retries=2)
    if df is not None and len(df):
        c = df.dropna(subset=["中国国债收益率10年"]).iloc[-1]
        out["cn"] = {"date": str(c["日期"]), "cn2y": num(c.get("中国国债收益率2年")),
                     "cn5y": num(c.get("中国国债收益率5年")), "cn10y": num(c.get("中国国债收益率10年")),
                     "cn30y": num(c.get("中国国债收益率30年")),
                     "cn10_2": num(c.get("中国国债收益率10年-2年"))}
        u = df.dropna(subset=["美国国债收益率10年"]).iloc[-1]
        out["us"] = {"date": str(u["日期"]), "us2y": num(u.get("美国国债收益率2年")),
                     "us5y": num(u.get("美国国债收益率5年")), "us10y": num(u.get("美国国债收益率10年")),
                     "us30y": num(u.get("美国国债收益率30年")),
                     "us10_2": num(u.get("美国国债收益率10年") - u.get("美国国债收益率2年"))}
        out["chg1y"] = {}
        for k, side in [("cn10y", "中国国债收益率10年"), ("us10y", "美国国债收益率10年")]:
            s = pd.to_numeric(df[side], errors="coerce").dropna()
            if len(s) > 250:
                out["chg1y"][k] = num(s.iloc[-1] - s.iloc[-251])
    y = safe("bond_china_yield", lambda: ak.bond_china_yield(start_date="20261001", end_date="20261031"))
    if y is not None and len(y):
        gov = y[y["曲线名称"].astype(str).str.contains("国债", na=False)]
        if len(gov):
            r = gov.iloc[-1]
            out["cn_curve"] = {"date": str(r["日期"])}
            for k, col in [("3m", "3月"), ("6m", "6月"), ("1y", "1年"), ("3y", "3年"),
                           ("5y", "5年"), ("7y", "7年"), ("10y", "10年"), ("30y", "30年")]:
                out["cn_curve"][k] = num(r.get(col))
    lpr = safe("macro_china_lpr", ak.macro_china_lpr)
    if lpr is not None and len(lpr):
        r = lpr.iloc[-1]
        out["lpr"] = {"date": str(r.get("TRADE_DATE")), "lpr1y": num(r.get("LPR1Y")),
                      "lpr5y": num(r.get("LPR5Y"))}
    # 中债指数（净值型，可看债券「价格」方向）
    for fn, key in [("bond_new_composite_index_cbond", "cbond_new_composite"),
                    ("bond_treasury_index_cbond", "cbond_treasury")]:
        d = safe(fn, getattr(ak, fn))
        if d is not None and len(d):
            st = series_stats(d.rename(columns={"value": "close"}), col="close", date_col="date")
            out[key] = {k: st.get(k) for k in ("date", "last", "ret20d", "ret60d", "ret1y")}
    # 可转债市场温度（集思录口径）
    cb = safe("bond_cb_index_jsl", ak.bond_cb_index_jsl)
    if cb is not None and len(cb):
        r = cb.iloc[-1]
        out["cb_market"] = {"date": str(r.get("price_dt")), "index": num(r.get("price")),
                            "chg1d": num(r.get("increase_rt")),
                            "avg_price": num(r.get("avg_price")), "mid_price": num(r.get("mid_price")),
                            "avg_premium": num(r.get("avg_premium_rt")),
                            "avg_ytm": num(r.get("avg_ytm_rt")),
                            "temperature": num(r.get("temperature")),
                            "turnover": num(r.get("turnover_rt")),
                            "count": int(r.get("count") or 0)}
    return out


# ---------------------------------------------------------------- 7. 汇率
def collect_fx():
    out = {}
    # 新浪外汇即期（实测可用）：字段 0 时间 1 买价 2 卖价 3 昨收 ... 9 名称 10 涨跌幅%
    q = sina_batch(["fx_susdcnh", "fx_susdjpy", "fx_seurusd", "fx_sgbpusd",
                    "fx_saudusd", "fx_susdcny", "DINIW"])
    name_map = {"fx_susdcnh": "USDCNH 离岸人民币", "fx_susdjpy": "USDJPY 美元兑日元",
                "fx_seurusd": "EURUSD 欧元兑美元", "fx_sgbpusd": "GBPUSD 英镑兑美元",
                "fx_saudusd": "AUDUSD 澳元兑美元", "fx_susdcny": "USDCNY 在岸人民币",
                "DINIW": "美元指数 DXY"}
    for k, name in name_map.items():
        b = q.get(k)
        if k == "DINIW" and b and len(b) > 8:
            prev = nump(b[3], 2)
            last = nump(b[1], 2)
            out["DXY"] = {"name": name, "last": last, "prev": prev, "date": b[10] if len(b) > 10 else None,
                          "day_high": nump(b[6], 2), "day_low": nump(b[7], 2)}
            if last and prev:
                out["DXY"]["chg1d"] = num((last / prev - 1) * 100)
            continue
        if b and len(b) > 12:
            out[k.replace("fx_s", "")] = {"name": name, "last": nump(b[1], 4),
                                          "chg1d": nump(b[10]), "date": b[-1],
                                          "day_open": nump(b[5], 4), "day_high": nump(b[6], 4),
                                          "day_low": nump(b[7], 4),
                                          "high52w": nump(b[14], 4) if len(b) > 14 else None,
                                          "low52w": nump(b[15], 4) if len(b) > 15 else None}
    mid = safe("currency_boc_sina",
               lambda: ak.currency_boc_sina(symbol="美元", start_date="20250101", end_date="20261031"))
    if mid is not None and len(mid):
        # 中间价只在交易日发布，节假日为空；必须把日期一起 dropna，否则日期与数值错位一天
        m = pd.DataFrame({"date": pd.to_datetime(mid["日期"], errors="coerce"),
                          "mid": pd.to_numeric(mid["央行中间价"], errors="coerce"),
                          "bank": pd.to_numeric(mid["中行汇买价"], errors="coerce")}
                         ).dropna(subset=["date", "mid"]).sort_values("date").reset_index(drop=True)
        if len(m):
            out["USDCNY_mid"] = {"date": m["date"].iloc[-1].strftime("%Y-%m-%d"),
                                 "last": num(m["mid"].iloc[-1], 4),
                                 "unit": "100 美元折人民币"}
            if len(m) > 20:
                out["USDCNY_mid"]["ret20d"] = num((m["mid"].iloc[-1] / m["mid"].iloc[-21] - 1) * 100)
            if len(m) > 250:
                out["USDCNY_mid"]["ret1y"] = num((m["mid"].iloc[-1] / m["mid"].iloc[-251] - 1) * 100)
            # 中间价 vs 银行现汇买入价的偏离：>1% 说明在岸价被系统性顶强
            if pd.notna(m["bank"].iloc[-1]) and m["bank"].iloc[-1]:
                out["USDCNY_mid"]["gap_vs_bank_pct"] = num(
                    (m["mid"].iloc[-1] - m["bank"].iloc[-1]) / m["mid"].iloc[-1] * 100)
                out["USDCNY_mid"]["gap_note"] = "中间价高于银行现汇买入价的幅度；正值=官方定价强于市场成交价"
    return out


# ---------------------------------------------------------------- 8. 公募基金
def collect_funds():
    out = {}
    names = safe("fund_name_em", ak.fund_name_em)
    if names is not None and len(names):
        out["type_counts"] = {str(k): int(v) for k, v in names["基金类型"].value_counts().items()}
        out["total_funds"] = int(len(names))
    rank = safe("fund_open_fund_rank_em", lambda: ak.fund_open_fund_rank_em(symbol="全部"))
    if rank is not None and len(rank) and names is not None:
        df = rank.merge(names[["基金代码", "基金类型"]], on="基金代码", how="left")
        for c in ["近1月", "近3月", "近6月", "近1年"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        g = df.groupby("基金类型").agg(n=("基金代码", "size"), m1=("近1月", "median"),
                                        m3=("近3月", "median"), m12=("近1年", "median")).reset_index()
        out["type_perf"] = [{"type": str(r["基金类型"]), "n": int(r["n"]), "m1": num(r["m1"]),
                             "m3": num(r["m3"]), "m12": num(r["m12"])}
                            for _, r in g.iterrows() if pd.notna(r["基金类型"])]
        out["rank_date"] = str(df["日期"].iloc[0]) if "日期" in df.columns else None
    mm = safe("money_fund_daily", ak.fund_money_fund_daily_em)
    if mm is not None and len(mm):
        col = [c for c in mm.columns if "7日年化" in str(c)][0]
        s = mm[col].map(lambda v: nump(v, 4)).dropna()
        out["money_fund"] = {"col": col, "n": int(len(s)), "median_7d": num(s.median()),
                             "max_7d": num(s.max()), "min_7d": num(s.min())}
    newf = safe("fund_new_found_em", ak.fund_new_found_em)
    if newf is not None and len(newf):
        newf = newf.copy()
        newf["成立日期"] = pd.to_datetime(newf["成立日期"], errors="coerce")
        recent = newf[newf["成立日期"] >= pd.Timestamp("2026-01-01")]
        out["new_funds_2026"] = int(len(recent))
        out["new_funds_30d"] = int((newf["成立日期"] >= pd.Timestamp("2026-10-01")).sum())
        out["new_funds_by_type"] = {str(k): int(v) for k, v in
                                    recent["基金类型"].value_counts().head(10).items()}
    aum = safe("fund_aum_em", ak.fund_aum_em)
    if aum is not None and len(aum):
        aum = aum.copy()
        aum["全部管理规模"] = pd.to_numeric(aum["全部管理规模"], errors="coerce")
        top = aum.nlargest(10, "全部管理规模")[["基金公司", "全部管理规模", "全部基金数"]]
        out["aum_top10"] = [{"company": str(r["基金公司"]), "aum_yi": num(r["全部管理规模"]),
                             "funds": int(r["全部基金数"])} for _, r in top.iterrows()]
        out["aum_total_yi"] = num(aum["全部管理规模"].sum())
    return out


# ---------------------------------------------------------------- 9. ETF
ETF_POOL = {"510300": "沪深300ETF", "510500": "中证500ETF", "510050": "上证50ETF",
            "588000": "科创50ETF", "512480": "半导体ETF", "518880": "黄金ETF",
            "513100": "纳指ETF", "511260": "十年国债ETF", "159915": "创业板ETF",
            "159919": "沪深300ETF(深)", "510880": "红利ETF", "513500": "标普500ETF",
            "512100": "中证1000ETF", "513050": "中概互联ETF", "159934": "黄金ETF(深)",
            "511010": "国债ETF", "159649": "0-4地债ETF", "512890": "红利低波ETF"}


def collect_etf():
    out = {}
    daily = safe("fund_etf_fund_daily_em", ak.fund_etf_fund_daily_em)
    if daily is not None and len(daily):
        out["n_funds"] = int(len(daily))
        prem_col = [c for c in daily.columns if "折价率" in str(c)]
        if prem_col:
            s = daily[prem_col[0]].map(lambda v: nump(v, 3)).dropna()
            out["premium"] = {"n": int(len(s)), "median": num(s.median()),
                              "p95": num(s.quantile(0.95)), "p05": num(s.quantile(0.05)),
                              "max": num(s.max()), "min": num(s.min())}
        nav_col = [c for c in daily.columns if "单位净值" in str(c)]
        if nav_col:
            out["nav_col"] = nav_col[0]
    spot = safe("fund_etf_spot_ths", ak.fund_etf_spot_ths)
    if spot is not None and len(spot):
        out["n_spot"] = int(len(spot))
    # 场内报价：新浪批量（东财批量不稳）
    codes = ["sh" + c if c.startswith("5") else "sz" + c for c in ETF_POOL]
    q = sina_batch(codes)
    samples = {}
    for c, name in ETF_POOL.items():
        key = ("sh" if c.startswith("5") else "sz") + c
        b = q.get(key)
        if not b or len(b) < 10:
            continue
        last, prev = nump(b[3], 3), nump(b[2], 3)
        samples[c] = {"name": name, "last": last, "prev": prev,
                      "chg1d": num((last / prev - 1) * 100) if last and prev else None,
                      "amount": nump(b[9], 0)}
    out["samples"] = samples
    chg = pd.Series([v["chg1d"] for v in samples.values() if v["chg1d"] is not None])
    if len(chg):
        out["sample_chg_median"] = num(chg.median())
    return out


# ---------------------------------------------------------------- 10. REITs（新浪批量行情）
def collect_reits():
    names = safe("fund_name_em", ak.fund_name_em, retries=2)
    if names is None or not len(names):
        return {}
    codes = list(names[names["基金类型"].astype(str).str.contains("Reit", case=False, na=False)]
                 ["基金代码"].astype(str))
    H = {"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"}
    rows, missing = [], []
    for i in range(0, len(codes), 40):
        chunk = codes[i:i + 40]
        q = ",".join(["sh" + c if c.startswith("5") else "sz" + c for c in chunk])
        try:
            r = requests.get("https://hq.sinajs.cn/list=" + q, headers=H, timeout=15)
            r.encoding = "gbk"
        except Exception as e:
            log("ERR  reits_chunk_%d %s" % (i, str(e)[:80]))
            missing += chunk
            continue
        got = {}
        for line in r.text.split("\n"):
            if "=" not in line or '=""' in line:
                continue
            key = line.split("=")[0].split("_")[-1]
            body = line.split('"')[1].split(",")
            got[key[2:]] = body
        for c in chunk:
            b = got.get(c)
            if not b or len(b) < 10:
                missing.append(c)
                continue
            try:
                rows.append({"code": c, "name": b[0], "open": num(b[1], 3), "prev": num(b[2], 3),
                             "last": num(b[3], 3), "amount": num(b[9], 0)})
            except Exception:
                missing.append(c)
    for r in rows:
        if r["prev"]:
            r["chg1d"] = num((r["last"] / r["prev"] - 1) * 100)
    chg = pd.Series([r["chg1d"] for r in rows if r["chg1d"] is not None])
    out = {"n_listed": len(codes), "n_quoted": len(rows), "n_missing": len(missing)}
    if len(chg):
        out.update({"chg1d_median": num(chg.median()), "chg1d_up": int((chg > 0).sum()),
                    "chg1d_down": int((chg < 0).sum()), "chg1d_max": num(chg.max()),
                    "chg1d_min": num(chg.min())})
    out["quotes"] = sorted(rows, key=lambda x: -(x["amount"] or 0))[:15]
    amt = pd.Series([r["amount"] for r in rows if r["amount"]])
    if len(amt):
        out["amount_total_yi"] = num(amt.sum() / 1e8)
    return out


# ---------------------------------------------------------------- 11. 一级市场
def collect_primary():
    out = {}
    dec = safe("stock_ipo_declare_em", ak.stock_ipo_declare_em, retries=2)
    if dec is not None and len(dec):
        out["pipeline_total"] = int(len(dec))
        out["by_status"] = {str(k): int(v) for k, v in dec["最新状态"].value_counts().head(10).items()}
        out["by_market"] = {str(k): int(v) for k, v in dec["拟上市地点"].value_counts().head(8).items()}
        dec = dec.copy()
        dec["更新日期"] = pd.to_datetime(dec["更新日期"], errors="coerce")
        out["updated_30d"] = int((dec["更新日期"] >= pd.Timestamp("2026-10-01")).sum())
    newipo = safe("stock_new_ipo_cninfo", ak.stock_new_ipo_cninfo)
    if newipo is not None and len(newipo):
        n = newipo.copy()
        n["申购日期"] = pd.to_datetime(n["申购日期"], errors="coerce")
        y26 = n[n["申购日期"] >= pd.Timestamp("2026-01-01")]
        out["ipo_2026_count"] = int(len(y26))
        out["ipo_30d_count"] = int((n["申购日期"] >= pd.Timestamp("2026-10-01")).sum())
        out["ipo_date_range"] = [str(y26["申购日期"].min().date()), str(y26["申购日期"].max().date())] \
            if len(y26) else None
        pe = pd.to_numeric(y26["发行市盈率"], errors="coerce").dropna()
        if len(pe):
            out["ipo_pe_median"] = num(pe.median())
            out["ipo_pe_p25"] = num(pe.quantile(0.25))
            out["ipo_pe_p75"] = num(pe.quantile(0.75))
        rate = pd.to_numeric(y26["上网发行中签率"], errors="coerce").dropna()
        if len(rate):
            out["ipo_winrate_median"] = num(rate.median(), 4)
        out["ipo_boards"] = {str(k): int(v) for k, v in
                             y26["证劵代码"].astype(str).str[:3].value_counts().head(8).items()}
        price = pd.to_numeric(y26["发行价"], errors="coerce")
        qty = pd.to_numeric(y26["总发行数量"], errors="coerce")
        raise_yi = (price * qty).dropna().sum() / 1e4  # 万股*元/1e4 = 亿元
        if raise_yi:
            out["ipo_raise_yi"] = num(raise_yi, 0)
    # 转债发行（一级/一级半）
    cb = safe("bond_zh_cov", ak.bond_zh_cov)
    if cb is not None and len(cb):
        c = cb.copy()
        c["申购日期"] = pd.to_datetime(c["申购日期"], errors="coerce")
        out["cb_new_total"] = int(len(c))
        out["cb_new_30d"] = int((c["申购日期"] >= pd.Timestamp("2026-10-01")).sum())
        pre = pd.to_numeric(c["转股溢价率"], errors="coerce").dropna()
        if len(pre):
            out["cb_issue_premium_median"] = num(pre.median())
    mgr = safe("amac_manager_info", ak.amac_manager_info)
    if mgr is not None and len(mgr):
        out["private_managers"] = int(len(mgr))
        out["private_by_type"] = {str(k): int(v) for k, v in mgr["机构类型"].value_counts().head(8).items()}
    abs_ = safe("amac_fund_abs", ak.amac_fund_abs, retries=4)
    if abs_ is not None and len(abs_):
        out["abs_plans"] = int(len(abs_))
        # 备案通过时间的最新值，用于说明"这个 12140 是截至哪天的存量"
        if "备案通过时间" in abs_.columns:
            t = pd.to_datetime(abs_["备案通过时间"], errors="coerce").dropna()
            if len(t):
                out["abs_latest_filing"] = t.max().strftime("%Y-%m-%d")
    return out


# ---------------------------------------------------------------- 12. 加密
def collect_crypto():
    out = {}

    def bc():
        r = requests.get("https://blockchain.info/ticker", headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        return r.json()

    t = safe("blockchain_ticker", bc)
    if t:
        for ccy in ["USD", "CNY"]:
            if ccy in t:
                out["BTC_" + ccy] = {"last": num(t[ccy].get("last")), "symbol": t[ccy].get("symbol")}
    cme = safe("crypto_bitcoin_cme", ak.crypto_bitcoin_cme)
    if cme is not None and len(cme):
        out["cme"] = cme.fillna("").to_dict(orient="records")
    return out


# ---------------------------------------------------------------- 13. A 股结构
def collect_cn_structure():
    out = {}
    ind = safe("board_industry_summary_ths", ak.stock_board_industry_summary_ths)
    if ind is not None and len(ind):
        ind = ind.copy()
        ind["涨跌幅"] = pd.to_numeric(ind["涨跌幅"], errors="coerce")
        ind["净流入"] = pd.to_numeric(ind["净流入"], errors="coerce")
        cols = ["板块", "涨跌幅", "净流入", "上涨家数", "下跌家数"]
        out["n_industries"] = int(len(ind))
        out["top5"] = ind.nlargest(5, "涨跌幅")[cols].to_dict(orient="records")
        out["bottom5"] = ind.nsmallest(5, "涨跌幅")[cols].to_dict(orient="records")
        out["inflow_top5"] = ind.nlargest(5, "净流入")[["板块", "涨跌幅", "净流入"]].to_dict(orient="records")
        out["outflow_top5"] = ind.nsmallest(5, "净流入")[["板块", "涨跌幅", "净流入"]].to_dict(orient="records")
        out["median_chg"] = num(ind["涨跌幅"].median())
        out["up_count"] = int((ind["涨跌幅"] > 0).sum())
    spot = safe("stock_zh_a_spot", ak.stock_zh_a_spot)
    if spot is not None and len(spot):
        s = spot.copy()
        s["涨跌幅"] = pd.to_numeric(s["涨跌幅"], errors="coerce")
        s["成交额"] = pd.to_numeric(s["成交额"], errors="coerce")
        out["breadth"] = {"n": int(len(s)), "up": int((s["涨跌幅"] > 0).sum()),
                          "down": int((s["涨跌幅"] < 0).sum()),
                          "flat": int((s["涨跌幅"] == 0).sum()),
                          "median": num(s["涨跌幅"].median()),
                          "limit_up": int((s["涨跌幅"] >= 9.8).sum()),
                          "limit_down": int((s["涨跌幅"] <= -9.8).sum()),
                          "amount_yi": num(s["成交额"].sum() / 1e8, 0),
                          "up_ratio": num((s["涨跌幅"] > 0).mean() * 100, 1)}
    margin = safe("stock_margin_sse", lambda: ak.stock_margin_sse(start_date="20260901", end_date="20261031"))
    if margin is not None and len(margin):
        m = margin.copy()
        m["信用交易日期"] = pd.to_datetime(m["信用交易日期"].astype(str), format="%Y%m%d", errors="coerce")
        m = m.sort_values("信用交易日期")
        b = pd.to_numeric(m["融资余额"], errors="coerce").dropna() / 1e8
        out["margin"] = {"date": str(m["信用交易日期"].iloc[-1].date()), "balance_yi": num(b.iloc[-1], 0)}
        if len(b) > 20:
            out["margin"]["chg_1m_yi"] = num(b.iloc[-1] - b.iloc[-21], 0)
        out["margin"]["high_range_yi"] = num(b.max(), 0)
        out["margin"]["low_range_yi"] = num(b.min(), 0)
    nb = safe("hsgt_hist", lambda: ak.stock_hsgt_hist_em(symbol="北向资金"))
    if nb is not None and len(nb):
        n = nb.copy()
        n["日期"] = pd.to_datetime(n["日期"], errors="coerce")
        valid = n.dropna(subset=["当日成交净买额"])
        out["northbound"] = {"last_valid_date": str(valid["日期"].iloc[-1].date()) if len(valid) else None,
                             "last_valid_net_yi": num(valid["当日成交净买额"].iloc[-1]) if len(valid) else None,
                             "rows": int(len(n)),
                             "note": "近期行净买额为 NaN，日度披露口径可能已变更，未核实"}
    qvix = safe("qvix_300etf", ak.index_option_300etf_qvix)
    if qvix is not None and len(qvix):
        s = pd.to_numeric(qvix["close"], errors="coerce").dropna()
        if len(s):
            out["qvix300"] = {"date": str(qvix["date"].iloc[-1]), "last": num(s.iloc[-1]),
                              "pct_1y": num((s.tail(250) < s.iloc[-1]).mean() * 100, 1),
                              "high1y": num(s.tail(250).max()), "low1y": num(s.tail(250).min())}
    return out


# ---------------------------------------------------------------- 14. 宏观锚
def collect_macro():
    out = {}
    cpi = safe("macro_china_cpi", ak.macro_china_cpi)
    if cpi is not None and len(cpi):
        out["cn_cpi"] = {"month": str(cpi["月份"].iloc[0]),
                         "yoy": num(cpi["全国-同比增长"].iloc[0]),
                         "mom": num(cpi["全国-环比增长"].iloc[0])}
    gdp = safe("macro_china_gdp", ak.macro_china_gdp)
    if gdp is not None and len(gdp):
        out["cn_gdp"] = {"quarter": str(gdp["季度"].iloc[0]),
                         "yoy": num(gdp["国内生产总值-同比增长"].iloc[0])}
    pmi = safe("macro_china_pmi", ak.macro_china_pmi)
    if pmi is not None and len(pmi):
        out["cn_pmi"] = {"month": str(pmi["月份"].iloc[0]),
                         "mfg": num(pmi["制造业-指数"].iloc[0]),
                         "non_mfg": num(pmi["非制造业-指数"].iloc[0])}
    m = safe("macro_china_supply_of_money", ak.macro_china_supply_of_money)
    if m is not None and len(m):
        valid = m.dropna(subset=["货币和准货币（广义货币M2）同比增长"])
        r = valid.iloc[0]
        out["cn_m2"] = {"month": str(r["统计时间"]),
                        "m2_yoy": num(r["货币和准货币（广义货币M2）同比增长"]),
                        "m1_yoy": num(r["货币(狭义货币M1)同比增长"])}
    sf = safe("macro_china_shrzgm", ak.macro_china_shrzgm)
    if sf is not None and len(sf):
        out["cn_tsf"] = {"month": str(sf["月份"].iloc[-1]), "yi": num(sf["社会融资规模增量"].iloc[-1])}
    u = safe("macro_usa_cpi_monthly", ak.macro_usa_cpi_monthly)
    if u is not None and len(u):
        v = u.dropna(subset=["今值"])
        if len(v):
            out["us_cpi"] = {"date": str(v["日期"].iloc[-1]), "mom": num(v["今值"].iloc[-1])}
    return out


# ---------------------------------------------------------------- 15. 相关性
def collect_corr():
    series = {}

    def add(key, df, col="close", dc=None):
        if df is None or not len(df):
            return
        d = df.copy()
        dc = dc or ("date" if "date" in d.columns else "日期")
        if dc not in d.columns or col not in d.columns:
            return
        d[dc] = pd.to_datetime(d[dc], errors="coerce")
        s = pd.to_numeric(d[col], errors="coerce")
        s.index = d[dc]
        s = s.dropna()
        s = s[~s.index.duplicated(keep="last")]
        series[key] = s

    add("沪深300", safe("corr_hs300", lambda: ak.stock_zh_index_daily(symbol="sh000300")))
    add("标普500", safe("corr_spx", lambda: ak.index_us_stock_sina(symbol=".INX")))
    add("恒生指数", safe("corr_hsi", lambda: ak.stock_hk_index_daily_sina(symbol="HSI")))
    add("黄金", safe("corr_gc", lambda: ak.futures_foreign_hist(symbol="GC")))
    add("铜", safe("corr_hg", lambda: ak.futures_foreign_hist(symbol="HG")))
    add("原油", safe("corr_cl", lambda: ak.futures_foreign_hist(symbol="CL")))
    add("中债总财富", safe("corr_cbond", ak.bond_new_composite_index_cbond),
        col="value", dc="date")
    if len(series) < 2:
        return {}
    px = pd.DataFrame(series).sort_index()
    ret = px.pct_change().dropna(how="all")
    out = {"n_days": int(len(ret)), "start": str(ret.index[0].date()) if len(ret) else None}
    for win, label in [(60, "corr60d"), (250, "corr250d")]:
        if len(ret) >= win:
            c = ret.tail(win).corr()
            out[label] = {k: {k2: num(v, 2) for k2, v in row.items()} for k, row in c.iterrows()}
    return out


def main():
    SNAP["meta"] = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "trade_date": "2026-10-30",
                    "generated_weekday": "2026-10-31 周六（A股/港股/美股均休市，读数为上一交易日收盘）",
                    "note": "AKShare/东财/新浪/CBOE/中证/乐咕/集思录 公开接口抓取；取不到的字段为 null，不补数"}
    SNAP["cn_index"] = collect_cn_index()
    SNAP["hk"] = collect_hk()
    SNAP["us"] = collect_us()
    SNAP["global_equity"] = collect_global_equity()
    SNAP["commodity"] = collect_commodity()
    SNAP["rates"] = collect_rates()
    SNAP["fx"] = collect_fx()
    SNAP["funds"] = collect_funds()
    SNAP["etf"] = collect_etf()
    SNAP["reits"] = collect_reits()
    SNAP["primary"] = collect_primary()
    SNAP["crypto"] = collect_crypto()
    SNAP["cn_structure"] = collect_cn_structure()
    SNAP["macro"] = collect_macro()
    SNAP["corr"] = collect_corr()
    path = os.path.join(DATA_DIR, "snapshot-%s.json" % SNAP["meta"]["trade_date"])
    with io.open(path, "w", encoding="utf-8") as f:
        json.dump(SNAP, f, ensure_ascii=False, indent=1, default=str)
    log("WROTE " + path)
    LOG.close()
    print("snapshot ->", path)


if __name__ == "__main__":
    main()
