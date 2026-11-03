#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cloud_paper.py — 云端模拟雷达 v2.0（双轨制 + 9 维辅助共振）
================================================================================
GitHub Actions 无状态运行 + paper_state.json 持久化。**永不下单**，只做三件事：
    ① 记仓  ①按双轨制找信号，虚拟成交，写进 paper_state.json
    ② 盯仓  ②每次唤醒先重放 15m K 线，判断模拟仓有没有打到止损/止盈/保本/超时
    ③ 推送  ③PushPlus 富文本卡片（标签在最上面，方便人工决定跟不跟）

--------------------------------------------------------------------------------
双轨制（v2 核心）
--------------------------------------------------------------------------------
轨道一 · 结构主导
    主策略 = 1H 海龟汤扫单 + 4 小时内 15m CHoCH 确认（方案 X 原口径，未改）
    再查 9 个辅助策略，**命中 ≥1 个**才触发：
        1 个 → ⚡ B级      2 个 → 🔥 A级      ≥3 个 → 👑 A+级

轨道二 · 纯指标堆积
    主策略不成立（最近 12 根 1H 没有新鲜的扫单+CHoCH），但 9 个辅助同时命中 ≥3 个：
        3 个 → 🧬 3维共振   4 个 → 🧬 4维共振   ≥5 个 → 🌌 5维极限共振
    卡片强制置顶「⚠️ 未出现 1H 扫单结构，属于左侧/指标共振信号」。

9 个辅助策略（全部方向化，除 ATR 是纯波动率滤网）
    1 PA    Al Brooks 价格行为（三选一：信号K质量 / 微型双底双顶 / Always In）
    2 FLOW  订单流：CVD 顺向 + 无否决（含巨鲸/挂单墙）
    3 H4    4H 大周期共振
    4 FIB   斐波那契回撤 0.5–0.786 区
    5 VOL   成交量放大 > 1.5× 前 20 根均量
    6 FUND  资金费率极值（逆向拥挤，±0.05%/8h）
    7 LSR   散户多空比极值（1D 序列的 p10 / p90 分位）
    8 ATR   波动率过滤（ATR14 ≥ 近 100 根中位）
    9 VGAP  成交量缺口（放量位移棒：量 ≥2× 均量 且 振幅 ≥1.5×ATR）

--------------------------------------------------------------------------------
记账规则
--------------------------------------------------------------------------------
· 单笔风险固定 RISK_U（默认 8U），按 R 记：1R = 8U；张数仅供参考。
· **无每日单数上限、无日亏熔断、无同时持仓上限**（v2 特地解除，为极限收集样本）。
· 同品种 **2 小时内不重复触发**（`last_open` 时间戳闸）。
· 出场：1R 推保本 → 结构止盈 / 2.5R → 24 小时超时平仓。
· 出场是**重放式**的：按上次记录之后的 15m K 线逐根补算，隔夜/晚跑都不丢单。
--------------------------------------------------------------------------------

状态文件（version 2）
  {
    "version": 2,
    "date": "2026-11-03",
    "day":  {"opens": 0, "pnl": 0.0},
    "positions": [ {...}, {...} ],          # 列表，允许同品种多笔
    "history":   [ ... ],
    "seen":      {"<inst>|<choch_ts>": <ms>},
    "last_open": {"BTC-USDT-SWAP": <ms>}    # 2 小时冷却
  }

读写三条硬约束
  1. 无状态宿主机：跑完必须 commit+push，否则下一轮读到空文件 → 重复记账。
  2. 并发：workflow 用 concurrency group 串行化，push 前 `git pull --rebase`。
  3. 只追加不覆盖：文件损坏 → 抛错拒跑，绝不用默认值覆盖已有持仓。

环境变量
  PUSHPLUS_TOKEN   必填
  PAPER_STATE      状态文件路径（默认脚本同目录 paper_state.json）
  PAPER_RISK_U     单笔风险，默认 8
  PAPER_FORCE=1    忽略 90 分钟新鲜度窗口（调试用）

用法
  python cloud_paper.py               # 正常跑一轮
  python cloud_paper.py --self-test   # 离线自检，不联网、不写状态
================================================================================
"""
from __future__ import annotations

import argparse
import bisect
import datetime
import json
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from card import Card                      # noqa: E402  与本地共用同一套排版
import cloud_ict_e as base                 # noqa: E402  只借通用件：HTTP/结构/订单流

# V7 必须是「方向感知」口径（价涨+CVD净卖才算多头背离），与 CLAUDE.md 一致。
# cloud_ict_e.py 默认 strict（只看 CVD 符号），照搬会多判背离；只改本模块内存里的常量。
base.FLOW_VETO_MODE = "directional"

BJT = base.BJT
STATE_PATH = os.environ.get("PAPER_STATE") or os.path.join(HERE, "paper_state.json")
VERSION = 2

SYMBOLS = {
    "BTC-USDT-SWAP": {"name": "BTC", "ct": 0.01, "lot": 0.01, "ccy": "BTC"},
    "ETH-USDT-SWAP": {"name": "ETH", "ct": 0.10, "lot": 0.01, "ccy": "ETH"},
    "SOL-USDT-SWAP": {"name": "SOL", "ct": 1.00, "lot": 0.01, "ccy": "SOL"},
}

# ---- 记账（v2：无单数上限 / 无日亏熔断 / 无同时持仓上限）
RISK_U = float(os.environ.get("PAPER_RISK_U") or 8.0)
SAME_SYMBOL_COOLDOWN_MIN = 120
HISTORY_MAX = 500
SEEN_MAX = 800

# ---- 方案 X 冻结参数（轨道一主策略，勿改）
KZ_WINDOWS = ((8, 24),)                   # 北京 08:00–24:00
KZ_GRACE_MIN = 15
SWEEP_LOOKBACK = 20                       # 前 20 根 1H
SETUP_BARS_15M = 16                       # 扫单后 16 根 15m = 4 小时
MAX_HOLD_BARS = 96                        # 24 小时
BE_R = 1.0
STOP_BUFFER_PCT = 0.0010
MIN_TP_R = 2.5
FALLBACK_R = 2.5
M15_LOOKBACK = 96
TARGET_LOOKBACK = 200
CHASE_R = 0.5
MAX_SIGNAL_AGE_MIN = 90
SCAN_H1_BARS = 12
FEE_TAKER = 0.0005
FORCE = os.environ.get("PAPER_FORCE", "").strip().lower() in ("1", "true", "yes")

# ---- 轨道二 / 辅助策略参数
TRACK2_MIN_HITS = 3                       # 纯辅助需要 ≥3 维
TRACK1_MIN_HITS = 1                       # 结构主导 ≥1 维
TRACK2_STOP_ATR = 1.5                     # 轨道二止损 = 1.5×ATR14
FIB_LO, FIB_HI = 0.50, 0.786              # 斐波那契区（v2 改口径）
VOL_EXPAND = 1.5                          # 放量 >1.5× 均量
VGAP_VOL = 2.0                            # 量能断层 ≥2× 均量
VGAP_RANGE_ATR = 1.5                      # 且振幅 ≥1.5×ATR
FUND_EXTREME_PCT = 0.05                   # ±0.05%/8h
LSR_PCTL = 0.10                           # 1D 序列 p10 / p90
AB_BODY_MULT = 1.2                        # Al Brooks 实体 >1.2× 前 5 根均实体
AB_DBL_TOL = 0.002                        # 微型双底/双顶误差 0.2%

# ---- 补丁一：15m 极速单边拦截器（漏洞四）
FAST_BODY_MULT = 2.0                      # 实体 > 2× 前 20 根均实体
FAST_LOOKBACK = 20                        # 均实体回看根数
FAST_WINDOW = 3                           # 最近 3 根里
FAST_MIN_HITS = 2                         # ≥2 根即判定极速单边
FAST_LOCK_BARS = 8                        # 触发后锁 8 根 15m = 2 小时

# ---- 补丁二：轨道 B 的 1H FVG 严格二次确认
FVG_LOOKBACK_1H = 60                      # 扫最近 60 根 1H 的 FVG

AUX_ORDER = ["PA", "FLOW", "H4", "FIB", "VOL", "FUND", "LSR", "ATR", "VGAP"]
AUX_NAME = {
    "PA": "PA(Al Brooks)", "FLOW": "订单流", "H4": "4H 共振", "FIB": "斐波那契 0.5–0.786",
    "VOL": "成交量放大", "FUND": "资金费率极值", "LSR": "散户多空比极值",
    "ATR": "ATR 波动率", "VGAP": "成交量缺口",
}

_CT = {k: v["ct"] for k, v in SYMBOLS.items()}
_NAME = {k: v["name"] for k, v in SYMBOLS.items()}


# ================================================================ 通用
def killzone(dt):
    """北京 08:00–24:00（含 15 分钟容错，次日 00:15 前算昨夜后延）。"""
    if dt is None:
        return None
    m = dt.hour * 60 + dt.minute
    for lo, hi in KZ_WINDOWS:
        if lo * 60 <= m < hi * 60 + KZ_GRACE_MIN:
            return f"{lo:02d}:00-{hi:02d}:00"
    if m < KZ_GRACE_MIN and KZ_WINDOWS[-1][1] == 24:
        return f"{KZ_WINDOWS[-1][0]:02d}:00-24:00"
    return None


def bjt_date(now):
    return now.astimezone(BJT).strftime("%Y-%m-%d")


def fetch_15m(inst, limit=500):
    """只要已收盘的 15m K 线，时间升序。"""
    rows = base.okx("/api/v5/market/candles",
                    {"instId": inst, "bar": "15m", "limit": str(limit)})
    out = []
    for r in rows:
        if len(r) < 9 or r[8] != "1":
            continue
        ts = int(r[0])
        out.append({"ts": ts, "open": float(r[1]), "high": float(r[2]),
                    "low": float(r[3]), "close": float(r[4]),
                    "vol": float(r[5]), "close_ts": ts + 900_000})
    out.sort(key=lambda c: c["ts"])
    return out


def ema_series(vals, n):
    if not vals:
        return []
    k = 2.0 / (n + 1.0)
    out = [float(vals[0])]
    for v in vals[1:]:
        out.append(float(v) * k + out[-1] * (1.0 - k))
    return out


def atr_series(bars, n=14):
    """ Wilder 简化版（简单均值），前 n-1 根为 None。"""
    trs, out = [], []
    for i, b in enumerate(bars):
        if i == 0:
            tr = b["high"] - b["low"]
        else:
            pc = bars[i - 1]["close"]
            tr = max(b["high"] - b["low"], abs(b["high"] - pc), abs(b["low"] - pc))
        trs.append(tr)
        out.append(None if i < n - 1 else sum(trs[i - n + 1:i + 1]) / n)
    return out


def _body(b):
    return abs(b["close"] - b["open"])


def _rng(b):
    return max(b["high"] - b["low"], 1e-12)


# ================================================================ 1. Al Brooks PA
def ab_signal_bar(m15, i, sgn):
    """Al Brooks 条件①：强实体 + 收盘贴边（做多贴最高 1/3，做空贴最低 1/3）。

    这是「信号 K 线」的严格定义，补丁二的 FVG 入场只认这一条。
    """
    if i < 5 or i >= len(m15):
        return False
    b = m15[i]
    avg = sum(_body(x) for x in m15[i - 5:i]) / 5.0
    if avg <= 0:
        return False
    body, rng = _body(b), _rng(b)
    if body <= AB_BODY_MULT * avg:
        return False
    if sgn > 0:
        return b["close"] > b["open"] and b["close"] >= b["high"] - rng / 3.0
    return b["close"] < b["open"] and b["close"] <= b["low"] + rng / 3.0


def albrooks_pa(m15, i, sgn, ema20):
    """Al Brooks 三选一。返回 (bool, 说明)。只吃「i 及之前」的 K 线，无未来函数。"""
    if i < 8 or i >= len(m15):
        return False, "K 线不足"
    b = m15[i]
    prev = m15[i - 5:i]
    avg = sum(_body(x) for x in prev) / 5.0
    bull = b["close"] > b["open"]
    bear = b["close"] < b["open"]
    body = _body(b)

    # 条件 1：信号 K 质量 —— 强实体 + 收盘贴边
    if ab_signal_bar(m15, i, sgn):
        return True, (f"①信号K：实体 {body / avg:.2f}× 均实体，"
                      f"收盘{'在最高' if sgn > 0 else '在最低'} 1/3")

    # 条件 2：微型双底 / 双顶（最近 8 根内两个相近极值 + 当前确认 K 强势）
    seg = m15[i - 7:i + 1]
    if sgn > 0:
        lows = sorted((x["low"] for x in seg))[:2]
        if len(lows) == 2 and lows[0] > 0 and \
           (lows[1] - lows[0]) / lows[0] <= AB_DBL_TOL and bull and body >= avg > 0:
            return True, (f"②微型双底：{lows[0]:,.2f} / {lows[1]:,.2f}"
                          f"（差 {(lows[1] - lows[0]) / lows[0] * 100:.2f}%）+ 阳线")
    else:
        highs = sorted((x["high"] for x in seg), reverse=True)[:2]
        if len(highs) == 2 and highs[0] > 0 and \
           (highs[0] - highs[1]) / highs[0] <= AB_DBL_TOL and bear and body >= avg > 0:
            return True, (f"②微型双顶：{highs[0]:,.2f} / {highs[1]:,.2f}"
                          f"（差 {(highs[0] - highs[1]) / highs[0] * 100:.2f}%）+ 阴线")

    # 条件 3：Always In 顺势背景（近 3 根 ≥2 根同向实体 + 站对 EMA20）
    e = ema20[i] if i < len(ema20) else None
    if e is not None:
        bulls = sum(1 for x in m15[i - 2:i + 1] if x["close"] > x["open"])
        if sgn > 0 and bulls >= 2 and b["close"] > e:
            return True, f"③AlwaysIn：3 根中 {bulls} 根阳线，收盘 {b['close']:,.2f} > EMA20 {e:,.2f}"
        if sgn < 0 and (3 - bulls) >= 2 and b["close"] < e:
            return True, f"③AlwaysIn：3 根中 {3 - bulls} 根阴线，收盘 {b['close']:,.2f} < EMA20 {e:,.2f}"

    return False, "不满足 Al Brooks 三条件"


# ================================================================ 补丁一：极速单边拦截器
def fast_trend_state(m15, i=None):
    """最近 3 根 15m 里有 ≥2 根实体 > 2× 前 20 根均实体 → 极速单边市。

    与 1H ADX 无关：这是价格「一根一根把你甩出去」的形态，ADX 常常还没反应过来。
    """
    n = len(m15)
    i = n - 1 if i is None else i
    if i < FAST_LOOKBACK + FAST_WINDOW - 1:
        return False, "K 线不足"
    w0 = i - FAST_WINDOW + 1                      # 最近 3 根的第一根
    prev = m15[w0 - FAST_LOOKBACK:w0]             # 这 3 根之前的 20 根
    avg = sum(_body(x) for x in prev) / FAST_LOOKBACK
    if avg <= 0:
        return False, "均实体为 0"
    thr = FAST_BODY_MULT * avg
    big = [x for x in m15[i - FAST_WINDOW + 1:i + 1] if _body(x) > thr]
    if len(big) >= FAST_MIN_HITS:
        ups = sum(1 for x in big if x["close"] > x["open"])
        dirn = "向上" if ups > len(big) / 2 else "向下"
        return True, (f"最近 {FAST_WINDOW} 根中 {len(big)} 根实体 > "
                      f"{FAST_BODY_MULT:g}× 前 {FAST_LOOKBACK} 根均实体"
                      f"（{avg:.2f}），{dirn}极速单边")
    return False, ""


def fast_trend_lock(m15):
    """状态一旦触发，向后锁 FAST_LOCK_BARS 根 15m（2 小时）才算解除。

    返回 (locked, 说明)。判定只用已收盘 K 线。
    """
    n = len(m15)
    for end in range(n - 1, max(FAST_LOOKBACK + FAST_WINDOW - 2,
                                n - 1 - FAST_LOCK_BARS), -1):
        ok, why = fast_trend_state(m15, end)
        if ok:
            age = (n - 1) - end
            return True, f"{why}｜{age} 根前触发，锁定 {FAST_LOCK_BARS} 根 / 2 小时"
    return False, ""


def track_a_allowed(m15):
    """轨道 A（1H 扫单 + 15m CHoCH 的震荡海龟汤）是否放行。"""
    locked, why = fast_trend_lock(m15)
    return (not locked), why


# ================================================================ 补丁二：1H FVG 严格二次确认
def fvg_zones(h1, lookback=FVG_LOOKBACK_1H):
    """三根 1H 组成的缺口区。dir=+1 看涨缺口（回踩做多），-1 看跌缺口（反抽做空）。"""
    if len(h1) < 3:
        return []
    seg = h1[-lookback:]
    off = len(h1) - len(seg)
    out = []
    for i in range(2, len(seg)):
        a, c = seg[i - 2], seg[i]
        if c["low"] > a["high"]:
            out.append({"lo": a["high"], "hi": c["low"], "dir": 1,
                        "i": off + i, "ts": c["close_ts"]})
        elif c["high"] < a["low"]:
            out.append({"lo": c["high"], "hi": a["low"], "dir": -1,
                        "i": off + i, "ts": c["close_ts"]})
    return out


def fvg_entry_signal(h1, m15, sgn):
    """轨道 B 的入场结构：1H FVG 回踩 + Al Brooks 强信号K + 下一根 15m 二确。

    返回 (sig|None, 说明)。三条铁律：
      1. 15m 收盘击穿 FVG 下沿（多）/上沿（空）→ 该 FVG 作废，不会再被使用
      2. 信号K必须落在 FVG 区内，且是 Al Brooks 条件① 的强信号K
      3. 必须等下一根 15m 收盘：做多低点不破前低（做空高点不破前高）
    成交价 = 二确根之后那根 15m 的开盘价（与轨道 A 同一口径，不用未来价）。
    """
    if len(h1) < 10 or len(m15) < 10:
        return None, "K 线不足"
    want = "看涨" if sgn > 0 else "看跌"
    zones = [z for z in fvg_zones(h1) if z["dir"] == sgn]
    if not zones:
        return None, f"无同向 1H FVG（{want}缺口）"

    m15_ts = [b["ts"] for b in m15]
    last = m15[-1]
    live = []
    for z in zones:
        k0 = bisect.bisect_left(m15_ts, z["ts"])
        if k0 >= len(m15):
            continue                       # FVG 太新，15m 还没走出来
        broken = False
        for k in range(k0, len(m15)):
            if (sgn > 0 and m15[k]["close"] < z["lo"]) or \
               (sgn < 0 and m15[k]["close"] > z["hi"]):
                broken = True
                break
        if broken:
            continue                       # 击穿作废
        if not (z["lo"] <= last["close"] <= z["hi"]):
            continue                       # 现价不在区内
        live.append((z, k0))
    if not live:
        return None, f"同向 FVG 已被 15m 收盘击穿，或现价不在区内"

    # 从最新的 FVG 往回找；同一区内取最近一次成立的信号K（旧的不复用）
    for z, k0 in reversed(live):
        for j in range(len(m15) - 3, max(k0, 5) - 1, -1):
            b = m15[j]
            if not (z["lo"] <= b["close"] <= z["hi"]):
                continue
            if not ab_signal_bar(m15, j, sgn):
                continue
            c = m15[j + 1]
            if sgn > 0 and c["low"] < b["low"]:
                continue                   # 二确失败：跌破前低
            if sgn < 0 and c["high"] > b["high"]:
                continue                   # 二确失败：突破前高
            return {
                "sgn": sgn, "side": "做多" if sgn > 0 else "做空",
                "sig_i": j, "confirm_i": j + 1,
                "zone": z, "zone_lo": z["lo"], "zone_hi": z["hi"],
                "choch_ts": c["close_ts"], "choch_close": c["close"],
                "fill_ts": m15[j + 2]["ts"], "fill": m15[j + 2]["open"],
                "fill_close_ts": m15[j + 2]["close_ts"],
                "delay_min": int((c["close_ts"] - b["close_ts"]) / 60000),
            }, ""
    return None, "FVG 区内没有 Al Brooks 强信号K + 下一根二确"


# ================================================================ 2. 其余辅助
def fetch_funding_pct(inst):
    """当期资金费率，换算成 8h 口径的百分数。"""
    d = base.okx("/api/v5/public/funding-rate", {"instId": inst})
    if not d:
        return None
    return float(d[0]["fundingRate"]) * 100.0


def fetch_lsr(ccy):
    """散户多空比（账户数）：取 1D 序列，返回 (当前, p10, p90)。"""
    d = base.okx("/api/v5/rubik/stat/contracts/long-short-account-ratio",
                 {"ccy": ccy, "period": "1D"})
    vals = [float(x[1]) for x in d if len(x) >= 2]
    if len(vals) < 30:
        return None
    cur = vals[0]
    s = sorted(vals)
    p10 = s[max(0, int(LSR_PCTL * len(s)))]
    p90 = s[min(len(s) - 1, int((1 - LSR_PCTL) * len(s)))]
    return cur, p10, p90


def fib_zone(h1, price, sgn):
    """最近一段 1H 推动浪的回撤是否落在 0.5–0.786 区。"""
    piv = base.structure(h1[-100:])
    if not piv:
        return False, "无摆动点"
    if sgn > 0:
        lows = [p for p in piv if p[1] == "L"]
        if not lows:
            return False, "无摆动低点"
        L = lows[-1]
        H = next((p for p in reversed(piv) if p[1] == "H" and p[0] > L[0]), None)
        if H is None or H[2] <= L[2]:
            return False, "无有效的上涨浪"
        leg = H[2] - L[2]
        retr = (H[2] - price) / leg
    else:
        highs = [p for p in piv if p[1] == "H"]
        if not highs:
            return False, "无摆动高点"
        H = highs[-1]
        L = next((p for p in reversed(piv) if p[1] == "L" and p[0] > H[0]), None)
        if L is None or H[2] <= L[2]:
            return False, "无有效的下跌浪"
        leg = H[2] - L[2]
        retr = (price - L[2]) / leg
    ok = FIB_LO <= retr <= FIB_HI
    return ok, f"回撤 {retr * 100:.1f}%（区间 {FIB_LO * 100:g}–{FIB_HI * 100:g}%）"


def trend_now(h4):
    if len(h4) < 10:
        return "range", "4H 数据不足"
    return base.trend_state(base.structure(h4))


def trend_at(h4, ts):
    seg = [b for b in h4 if b["close_ts"] <= ts]
    if len(seg) < 10:
        return "range", "4H 数据不足"
    return base.trend_state(base.structure(seg))


def aux_hits(sgn, m15, i, h1, h4, ctx):
    """返回 [(code, 说明), ...]，按 AUX_ORDER 排序。ctx 里放 ema20/atr/fund/lsr/flow。"""
    hits = []
    side = "做多" if sgn > 0 else "做空"
    if i is None or i < 0 or i >= len(m15):
        return hits
    b = m15[i]

    ok, why = albrooks_pa(m15, i, sgn, ctx["ema20"])
    if ok:
        hits.append(("PA", why))

    flow = ctx.get("flow") or {}
    if flow.get("ok") and not flow.get("veto"):
        bullish = flow["cvd"] > 0
        if (sgn > 0) == bullish:
            bonus = f"（{flow['bonus']}）" if flow.get("bonus") else ""
            hits.append(("FLOW", f"CVD {flow['cvd']:+.4f}（占量 {flow['cvd_ratio']:+.1f}% ｜ "
                                 f"买占比 {flow['buy_ratio']:.1f}%）{bonus}"))

    st_h4, why4 = trend_at(h4, b["close_ts"])
    if (sgn > 0 and st_h4 == "up") or (sgn < 0 and st_h4 == "down"):
        hits.append(("H4", why4))

    ok, why = fib_zone(h1, b["close"], sgn)
    if ok:
        hits.append(("FIB", why))

    avg20 = sum(x["vol"] for x in m15[max(0, i - 20):i]) / max(1, len(m15[max(0, i - 20):i]))
    if avg20 > 0 and b["vol"] > VOL_EXPAND * avg20:
        hits.append(("VOL", f"{b['vol']:.0f} vs 20 根均量 {avg20:.0f}"
                            f"（{b['vol'] / avg20:.2f}×）"))

    fund = ctx.get("fund")
    if fund is not None:
        if sgn > 0 and fund <= -FUND_EXTREME_PCT:
            hits.append(("FUND", f"{fund:+.4f}%/8h ≤ -{FUND_EXTREME_PCT:g}%：空头极度拥挤"))
        elif sgn < 0 and fund >= FUND_EXTREME_PCT:
            hits.append(("FUND", f"{fund:+.4f}%/8h ≥ +{FUND_EXTREME_PCT:g}%：多头极度拥挤"))

    lsr = ctx.get("lsr")
    if lsr:
        cur, p10, p90 = lsr
        if sgn > 0 and cur <= p10:
            hits.append(("LSR", f"{cur:.2f} ≤ p10 {p10:.2f}：散户极度偏空"))
        elif sgn < 0 and cur >= p90:
            hits.append(("LSR", f"{cur:.2f} ≥ p90 {p90:.2f}：散户极度偏多"))

    atr = ctx.get("atr") or []
    a_i = atr[i] if i < len(atr) else None
    hist = [x for x in atr[max(0, i - 100):i + 1] if x]
    if a_i and hist:
        med = sorted(hist)[len(hist) // 2]
        if a_i >= med:
            hits.append(("ATR", f"ATR14 {a_i:,.2f} ≥ 近 {len(hist)} 根中位 {med:,.2f}"
                                f"（{a_i / b['close'] * 100:.3f}%）"))

    # 成交量缺口：最近 3 根里有放量位移棒，且方向与本单一致
    for x in m15[max(0, i - 2):i + 1]:
        navg = sum(y["vol"] for y in m15[max(0, i - 20):i]) / max(1, len(m15[max(0, i - 20):i]))
        if a_i and navg > 0 and x["vol"] >= VGAP_VOL * navg and \
           _rng(x) >= VGAP_RANGE_ATR * a_i and \
           ((sgn > 0 and x["close"] > x["open"]) or (sgn < 0 and x["close"] < x["open"])):
            hits.append(("VGAP", f"量 {x['vol']:.0f} = {x['vol'] / navg:.1f}× 均量，"
                                 f"振幅 {_rng(x):,.2f} ≥ {VGAP_RANGE_ATR:g}×ATR"))
            break

    hits.sort(key=lambda h: AUX_ORDER.index(h[0]))
    return hits


def tier_label(n, track):
    """返回 (标题用短标签, 正文用全称)。"""
    if track == 2:
        if n <= 3:
            return "🧬 3维共振", "🧬 纯辅助-3维共振"
        if n == 4:
            return "🧬 4维共振", "🧬 纯辅助-4维共振"
        return "🌌 5维极限共振", "🌌 纯辅助-5维极限共振"
    if n <= 1:
        return "⚡ B级", "⚡ B级"
    if n == 2:
        return "🔥 A级", "🔥 A级"
    return "👑 A+级", "👑 A+级"


# ================================================================ 轨道一：主策略
def find_signals(h1, h4, m15):
    """扫单 → Killzone → 15m 对手摆动位 → 16 根内 CHoCH → 下一根 15m 开盘成交。"""
    out = []
    if len(h1) < SWEEP_LOOKBACK + 5 or len(m15) < M15_LOOKBACK + 2:
        return out
    m15_ts = [b["ts"] for b in m15]
    start = max(SWEEP_LOOKBACK, len(h1) - 1 - SCAN_H1_BARS)
    for i in range(start, len(h1)):
        hb = h1[i]
        prior_hi = max(x["high"] for x in h1[i - SWEEP_LOOKBACK:i])
        prior_lo = min(x["low"] for x in h1[i - SWEEP_LOOKBACK:i])
        long_sweep = hb["low"] < prior_lo and hb["close"] > prior_lo
        short_sweep = hb["high"] > prior_hi and hb["close"] < prior_hi
        if not (long_sweep or short_sweep):
            continue
        kz = killzone(base.bjt(hb["close_ts"]))
        if kz is None:
            continue
        if long_sweep and short_sweep:
            long_sweep = (hb["close"] - hb["low"]) >= (hb["high"] - hb["close"])
            short_sweep = not long_sweep
        sgn = 1 if long_sweep else -1
        ext = hb["low"] if sgn > 0 else hb["high"]

        k0 = bisect.bisect_left(m15_ts, hb["close_ts"])
        if k0 <= 0 or k0 >= len(m15):
            continue
        piv = base.structure(m15[max(0, k0 - M15_LOOKBACK):k0])
        lvl = (base.nearest_above(piv, hb["close"]) if sgn > 0
               else base.nearest_below(piv, hb["close"]))
        if lvl is None:
            continue

        trig = None
        for j in range(k0, min(k0 + SETUP_BARS_15M, len(m15))):
            b = m15[j]
            if (sgn > 0 and b["close"] < ext) or (sgn < 0 and b["close"] > ext):
                trig = None
                break
            if (sgn > 0 and b["close"] > lvl) or (sgn < 0 and b["close"] < lvl):
                trig = j
                break
        if trig is None:
            continue
        fill = trig + 1
        if fill >= len(m15):
            continue

        out.append({
            "inst": None, "side": "做多" if sgn > 0 else "做空", "sgn": sgn,
            "sweep_ts": hb["close_ts"], "ext": ext, "lvl": lvl, "kz": kz,
            "sweep_close": hb["close"],
            "choch_ts": m15[trig]["close_ts"], "choch_close": m15[trig]["close"],
            "trig_i": trig,
            "fill_ts": m15[fill]["ts"], "fill": m15[fill]["open"],
            "fill_close_ts": m15[fill]["close_ts"],
            "delay_min": int((m15[trig]["close_ts"] - hb["close_ts"]) / 60000),
        })
    out.sort(key=lambda s: s["choch_ts"], reverse=True)
    return out


def target_1h(h1, fill_close_ts, price, sgn, risk_px):
    j1 = bisect.bisect_right([b["close_ts"] for b in h1], fill_close_ts)
    piv = base.structure(h1[max(0, j1 - TARGET_LOOKBACK):j1],
                         lookback=TARGET_LOOKBACK) if j1 > 0 else []
    want = "H" if sgn > 0 else "L"
    ok = [p[2] for p in piv if p[1] == want
          and (p[2] - price) * sgn >= risk_px * MIN_TP_R]
    if not ok:
        return None, f"无结构位，按 {FALLBACK_R:g}R 兜底"
    return min(ok, key=lambda x: abs(x - price)), "1H 结构位"


def build_track1_plan(sig, h1, h4, now, m15):
    sgn, entry = sig["sgn"], sig["fill"]
    stop = sig["ext"] - sgn * entry * STOP_BUFFER_PCT
    risk_px = (entry - stop) * sgn
    if risk_px <= 0:
        return None
    tp, tp_src = target_1h(h1, sig["fill_close_ts"], entry, sgn, risk_px)
    if tp is None:
        tp = entry + sgn * FALLBACK_R * risk_px
    state, why = trend_at(h4, sig["sweep_ts"])
    return {
        "entry": entry, "stop": stop, "tp": tp, "tp_src": tp_src,
        "risk_px": risk_px, "risk_pct": risk_px / entry * 100.0,
        "rr": (tp - entry) * sgn / risk_px,
        "trend": state, "trend_why": why,
        "drift_r": (entry - sig["sweep_close"]) * sgn / risk_px,
        "age_min": (now - base.bjt(sig["choch_ts"])).total_seconds() / 60.0,
        "idx": sig["trig_i"], "ts": sig["choch_ts"],
    }


def build_track2_plan(sig, m15, h1, atr, now=None):
    """轨道 B：入场 = 二确后那根 15m 开盘；止损 = 1.5×ATR；止盈 = 1H 结构位 / 2.5R。"""
    i = sig["confirm_i"]
    a = atr[i] if i < len(atr) else None
    if not a:
        return None
    sgn = sig["sgn"]
    entry = sig["fill"]
    risk_px = TRACK2_STOP_ATR * a
    if risk_px <= 0:
        return None
    stop = entry - sgn * risk_px
    tp, tp_src = target_1h(h1, sig["fill_close_ts"], entry, sgn, risk_px)
    if tp is None:
        tp = entry + sgn * FALLBACK_R * risk_px
    return {
        "entry": entry, "stop": stop, "tp": tp, "tp_src": tp_src,
        "risk_px": risk_px, "risk_pct": risk_px / entry * 100.0,
        "rr": (tp - entry) * sgn / risk_px,
        "drift_r": 0.0,
        "age_min": ((now - base.bjt(sig["choch_ts"])).total_seconds() / 60.0
                    if now else 0.0),
        "idx": i, "ts": sig["choch_ts"], "atr": a,
    }


# ================================================================ 持仓结算
def _settle(pos, price, why, ts):
    sgn, risk = pos["sgn"], pos["risk_px"]
    gross_r = (price - pos["entry"]) * sgn / risk
    fee_r = (pos["entry"] + price) * FEE_TAKER / risk
    net_r = gross_r - fee_r
    return {"exit": price, "why": why, "exit_ts": ts,
            "gross_r": round(gross_r, 4), "fee_r": round(fee_r, 4),
            "r": round(net_r, 4), "u": round(net_r * RISK_U, 2),
            "bars": int(pos.get("bars_held") or 0)}


def check_position(pos, m15):
    """逐根重放（同回测顺序：超时 → 保本 → 止损 → 止盈 → 记 1R）。就地推进 pos。"""
    sgn = pos["sgn"]
    held = int(pos.get("bars_held") or 0)
    bars = [b for b in m15 if b["close_ts"] > int(pos.get("last_bar_ts") or 0)]
    for b in bars:
        if held > 0 and held >= MAX_HOLD_BARS:
            return _settle(pos, b["close"], "超时24h", b["close_ts"])
        if held > 0 and pos.get("be"):
            if (sgn > 0 and b["low"] <= pos["entry"]) or \
               (sgn < 0 and b["high"] >= pos["entry"]):
                return _settle(pos, pos["entry"], "保本", b["close_ts"])
        if (sgn > 0 and b["low"] <= pos["stop"]) or \
           (sgn < 0 and b["high"] >= pos["stop"]):
            return _settle(pos, pos["stop"], "止损", b["close_ts"])
        if (sgn > 0 and b["high"] >= pos["tp"]) or \
           (sgn < 0 and b["low"] <= pos["tp"]):
            return _settle(pos, pos["tp"], "止盈", b["close_ts"])
        if not pos.get("be"):
            if (sgn > 0 and b["high"] >= pos["entry"] + BE_R * pos["risk_px"]) or \
               (sgn < 0 and b["low"] <= pos["entry"] - BE_R * pos["risk_px"]):
                pos["be"] = True
                pos["stop"] = pos["entry"]
        pos["last_bar_ts"] = b["close_ts"]
        held += 1
        pos["bars_held"] = held
    return None


# ================================================================ 状态
def default_state():
    return {"version": VERSION, "date": "",
            "day": {"opens": 0, "pnl": 0.0},
            "positions": [], "history": [], "seen": {}, "last_open": {}}


def _migrate_v1(raw):
    """v1 的 positions 是 dict；转成 list，别丢仓。"""
    pos = raw.get("positions")
    if isinstance(pos, dict):
        raw["positions"] = [v for v in pos.values() if isinstance(v, dict)]
    lo = raw.get("last_open")
    if not isinstance(lo, dict):
        raw["last_open"] = {p.get("inst"): int(p.get("opened_ts") or 0)
                            for p in raw.get("positions", []) if p.get("inst")}
    return raw


def load_state(path):
    """坏文件 → 抛 RuntimeError：宁可整轮不跑，也不用空状态覆盖掉持仓。"""
    st = default_state()
    if not os.path.exists(path):
        return st
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"paper_state.json 读取失败（{type(e).__name__}: {e}），"
                           f"拒绝用空状态覆盖，请先人工修复") from e
    if not isinstance(raw, dict):
        raise RuntimeError("paper_state.json 顶层不是对象，拒绝覆盖")
    raw = _migrate_v1(raw)
    st["version"] = VERSION
    st["date"] = str(raw.get("date") or "")
    if isinstance(raw.get("day"), dict):
        for k in st["day"]:
            st["day"][k] = raw["day"].get(k, st["day"][k])
    if isinstance(raw.get("positions"), list):
        st["positions"] = [p for p in raw["positions"] if isinstance(p, dict)]
    if isinstance(raw.get("history"), list):
        st["history"] = raw["history"][-HISTORY_MAX:]
    if isinstance(raw.get("seen"), dict):
        st["seen"] = raw["seen"]
    if isinstance(raw.get("last_open"), dict):
        st["last_open"] = {str(k): int(v or 0) for k, v in raw["last_open"].items()}
    return st


def save_state(path, st):
    st["version"] = VERSION
    st["history"] = st.get("history", [])[-HISTORY_MAX:]
    seen = st.get("seen") or {}
    if len(seen) > SEEN_MAX:
        keep = sorted(seen.items(), key=lambda kv: int(kv[1] or 0))[-SEEN_MAX:]
        st["seen"] = dict(keep)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1, sort_keys=False)
    os.replace(tmp, path)


def roll_day(st, now):
    d = bjt_date(now)
    if st.get("date") != d:
        st["date"] = d
        st["day"] = {"opens": 0, "pnl": 0.0}
    return st


# ================================================================ 卡片
def _px(x):
    return f"{x:,.2f}" if x >= 100 else f"{x:,.3f}"


def push_wechat(title, content):
    return base.push_wechat(title, content)


def _aux_block(hits):
    lines = []
    for k, (code, why) in enumerate(hits, 1):
        lines.append(f"{k}. {AUX_NAME[code]} —— {why}")
    return lines


def build_entry_card(track, tier, n_hits, hits, sig, plan, flow, st, seq,
                     test=False):
    name = _NAME[sig["inst"]]
    side = sig["side"]
    tags = []

    if track == 2:
        tags.append({"head": "⚠️【无1H扫单结构】", "tone": "warn",
                     "lines": ["左侧/指标共振信号：没有 1H 扫单 + 15m CHoCH 确认",
                               "这类样本未经回测验证，只用于收集数据"]})
        if sig.get("fast_lock"):
            tags.append({"head": "🌊【极速单边市】", "tone": "bad",
                         "lines": [sig["fast_lock"], "轨道A（震荡海龟汤）已暂停，本单来自轨道B"]})
    else:
        if (plan["trend"] == "up" and sig["sgn"] < 0) or \
           (plan["trend"] == "down" and sig["sgn"] > 0):
            tags.append({"head": "🧱【逆势】", "tone": "bad",
                         "lines": [f"4H {plan['trend_why']}，本单{side}与 4H 方向相反"]})
        elif plan["trend"] == "range":
            tags.append({"head": "🌫【震荡】", "tone": "warn",
                         "lines": [f"4H {plan['trend_why']}：没有趋势，只有区间"]})
        if plan["drift_r"] >= CHASE_R:
            tags.append({"head": "⚡【追单】", "tone": "warn",
                         "lines": [f"成交价已离扫单收盘 {plan['drift_r']:+.2f}R"
                                   f"（阈值 {CHASE_R:g}R）"]})

    if not flow.get("ok"):
        tags.append({"head": "❓【订单流未验证】", "tone": "warn",
                     "lines": [f"原因：{flow.get('err')}"]})
    elif flow.get("veto"):
        tags.append({"head": "⚠️【订单流背离】", "tone": "bad",
                     "lines": [flow["veto"],
                               f"CVD {flow['cvd']:+.4f}（占量 {flow['cvd_ratio']:+.1f}% ｜ "
                               f"买占比 {flow['buy_ratio']:.1f}%）"]})

    card = Card(side, f"{name} · {'双轨共振' if track == 1 else '纯辅助'}"
                      f" · {n_hits} 维")
    card.banner("🧪 云端模拟盘：不下单、不碰账户，只在自己的账本里虚拟成交", "info")
    if tags:
        card.risk(tags)
    else:
        card.banner("✅ 无风险标签", "info")
    if test:
        card.banner("🔌 连通用测试：手动触发的假信号，不记账、不建仓、不占冷却",
                    "info")

    card.kv("分层", f"{tier}（命中 {n_hits} 维）",
            note="结构主导 ≥1 维触发 ｜ 纯辅助 ≥3 维触发")
    card.kv("① 模拟入场", _px(plan["entry"]),
            note=(f"确认后一根 15m 开盘价，出现在 {plan['age_min']:.0f} 分钟前"
                  if track == 1 else
                  f"信号K→二确K→下一根 15m 开盘价，出现在 {plan['age_min']:.0f} 分钟前"))
    card.kv("② 止损", _px(plan["stop"]), "loss",
            note=(f"扫单极点 {_px(sig['ext'])} 外 0.1% ｜ 距入场 {plan['risk_pct']:.2f}%"
                  if track == 1 else
                  f"{TRACK2_STOP_ATR:g}×ATR14（{_px(plan.get('atr', 0))}）｜ "
                  f"距入场 {plan['risk_pct']:.2f}%"))
    card.kv("③ 结构止盈", _px(plan["tp"]), "profit",
            note=f"{plan['tp_src']} ｜ 约 {plan['rr']:.2f}R")
    card.kv("④ 建议仓位", f"{plan['lots']:.2f} 张",
            note=f"按 {RISK_U:g}U 风险折算 ｜ 账面按 R 记：1R = {RISK_U:g}U")

    if track == 1:
        card.kv("⑤ 结构",
                f"扫单 {base._t(sig['sweep_ts'])} → CHoCH +{sig['delay_min']} 分钟",
                note=f"15m 收盘 {_px(sig['choch_close'])} 越过 {_px(sig['lvl'])}")
    else:
        zb, cb = sig.get("sig_bar"), sig.get("confirm_bar")
        zlo, zhi = sig.get("zone_lo"), sig.get("zone_hi")
        head = (f"1H FVG {_px(zlo)} – {_px(zhi)}" if zlo and zhi
                else "1H FVG 回踩（区间未记录）")
        if zb and cb:
            note = (f"强信号K 收盘 {_px(zb['close'])} → 二确K "
                    f"{'低点不破前低' if sig['sgn'] > 0 else '高点不破前高'}"
                    f"（{_px(cb['low']) if sig['sgn'] > 0 else _px(cb['high'])}）")
        else:
            note = "1H FVG 回踩 + Al Brooks 信号K + 下一根二确"
        card.kv("⑤ 结构", head, note=note)

    card.flow(True, f"⑥ 命中明细（{n_hits}/9 维）", _aux_block(hits))
    card.notes([
        f"今日第 {seq} 单（已解除单数上限）｜ 今日模拟盈亏 {st['day']['pnl']:+.2f}U ｜ "
        f"在手 {len(st['positions'])} 笔",
        "订单流 / 费率 / 多空比是推送时点快照；其余指标按信号当根 15m 计算",
    ])
    card.footer([
        "云端只发雷达、自己记账；实盘做不做、做多少，由你手动决定。",
        "浮盈到 1R 请把止损推到入场价（1 张无法分批时，T1 只作推保本触发点，"
        "不平仓；止损移到入场价后，T2 一次性全部止盈）。",
        "仅为提醒，请自行判断，不构成交易建议。",
    ])
    return card.html()


def build_close_card(pos, ex, st):
    win = ex["u"] >= 0
    card = Card(pos["side"], f"{pos['name']} · 自动平仓",
                f"{pos.get('tier', '')} ｜ {ex['why']} ｜ 持有 {ex['bars']} 根 15m "
                f"（{ex['bars'] * 15 / 60:.1f} 小时）")
    card.banner(f"{'✅ 止盈/保本离场' if win else '❌ 亏损离场'}"
                f"：{ex['u']:+.2f}U（{ex['r']:+.2f}R）",
                "info" if win else "bad")
    card.kv("入场", _px(pos["entry"]))
    card.kv("出场", _px(ex["exit"]), "profit" if win else "loss", note=ex["why"])
    card.kv("毛盈亏", f"{ex['gross_r']:+.2f}R", "profit" if win else "loss")
    card.kv("手续费", f"-{ex['fee_r']:.2f}R", "muted", note="双边 taker 0.05%")
    card.kv("净盈亏", f"{ex['u']:+.2f}U", "profit" if win else "loss",
            note=f"{ex['r']:+.2f}R × {RISK_U:g}U")
    card.notes([f"今日模拟盈亏 {st['day']['pnl']:+.2f}U ｜ 今日已开 "
                f"{st['day']['opens']} 单 ｜ 在手 {len(st['positions'])} 笔"])
    card.footer(["云端记账样本，与你的实盘盈亏无关。",
                 "仅为提醒，请自行判断，不构成交易建议。"])
    return card.html()


# ================================================================ 连通性测试
def _last_price(inst):
    cs = fetch_15m(inst, limit=2)
    return cs[-1]["close"] if cs else None


def build_test_card(st, now, price=None):
    """连通性测试卡片：用一套固定的假做多信号走真实渲染路径。

    只渲染、只推送，不写账本、不建仓、不占同品种冷却。
    """
    inst = "BTC-USDT-SWAP"
    px = price or _safe(_last_price, inst) or 100000.0
    risk_px = px * 0.004                       # 约 0.4%，1 张 ≈ 3.2U
    entry = px
    stop = entry - risk_px
    tp = entry + MIN_TP_R * risk_px
    plan = {
        "entry": entry, "stop": stop, "tp": tp,
        "tp_src": f"测试用 1H 结构位，约 {MIN_TP_R:g}R",
        "risk_px": risk_px, "risk_pct": risk_px / entry * 100.0,
        "rr": MIN_TP_R, "drift_r": 0.0, "age_min": 0.0,
        "idx": 0, "ts": 0, "trend": "up", "trend_why": "4H 上升结构（测试数据）",
        "lots": 0.0,
    }
    plan["lots"] = base.size_position(entry, risk_px, RISK_U,
                                      SYMBOLS[inst]["ct"],
                                      SYMBOLS[inst]["lot"])[0]
    now_ms = int(now.timestamp() * 1000)
    sig = {
        "inst": inst, "side": "做多", "sgn": 1,
        "ext": stop, "lvl": entry - risk_px * 0.5, "kz": "08-24",
        "sweep_ts": now_ms - 45 * 60_000, "choch_ts": now_ms - 30 * 60_000,
        "choch_close": entry, "delay_min": 15,
    }
    flow = {
        "ok": True, "veto": None, "cvd": 12.5, "cvd_ratio": 6.2,
        "buy_ratio": 55.1, "cvd_usd": 1_250_000.0, "p0": entry * 0.997,
        "p1": entry, "dpct": 0.3, "imb": 4.5, "spread_bps": 0.8,
        "wall_ratio": 0.12, "whale_buy": 180_000.0, "whale_sell": 60_000.0,
        "whale_net": 120_000.0, "whale_n": 3,
    }
    hits = [
        ("PA", "① 强信号K：实体 1.8× 前 5 根均实体，收在最高 1/3"),
        ("FLOW", f"CVD +12.5 ｜ 占量 +6.2% ｜ 买占比 55.1%（{_px(entry)} 附近）"),
        ("H4", "4H 上升结构：HH/HL 未被破坏"),
        ("FIB", "回撤 0.618 落在折价区"),
        ("VOL", "量能 1.9× 20 根均量"),
    ]
    return build_entry_card(1, "👑 A+级", len(hits), hits, sig, plan, flow,
                            st, int(st["day"]["opens"]) + 1, test=True)


# ================================================================ 主流程
def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] {fn.__name__}{a} 失败：{type(e).__name__}: {e}")
        return None


def choose_side(h4, m15):
    st, why = trend_now(h4)
    if st == "up":
        return 1, f"4H {why}"
    if st == "down":
        return -1, f"4H {why}"
    e = ema_series([b["close"] for b in m15], 20)[-1]
    return (1, "4H 无趋势，按 15m EMA20 定方向") if m15[-1]["close"] > e else \
           (-1, "4H 无趋势，按 15m EMA20 定方向")


def open_position(st, inst, name, side, sgn, plan, track, tier, hits, flow,
                  seq, now_ms, extra=None):
    ct = SYMBOLS[inst]["ct"]
    lots, _per = base.size_position(plan["entry"], plan["risk_px"], RISK_U, ct,
                                    SYMBOLS[inst]["lot"])
    pos = {
        "id": f"{name}-{plan['ts']}-{seq}",
        "name": name, "inst": inst, "side": side, "sgn": sgn,
        "entry": plan["entry"], "stop": plan["stop"], "tp": plan["tp"],
        "risk_px": plan["risk_px"], "risk_u": RISK_U, "lots": lots, "ct": ct,
        "tp_src": plan["tp_src"], "track": track, "tier": tier,
        "hits": [h[0] for h in hits],
        "opened_ts": now_ms, "signal_ts": plan["ts"],
        "last_bar_ts": (extra or {}).get("last_bar_ts", plan["ts"]),
        "bars_held": 0, "be": False,
        "flow_ok": bool(flow.get("ok")), "flow_veto": flow.get("veto"),
    }
    st["positions"].append(pos)
    st["last_open"][inst] = now_ms
    st["day"]["opens"] = int(st["day"]["opens"]) + 1
    return pos


def handle_symbol(inst, cfg, st, now, now_ms):
    name = cfg["name"]
    h1 = base.fetch_candles(inst, "1H", 300)
    h4 = base.fetch_candles(inst, "4H", 200)
    m15 = fetch_15m(inst)
    if len(h1) < SWEEP_LOOKBACK + 10 or len(h4) < 20 or len(m15) < M15_LOOKBACK:
        print(f"[SKIP] {name} K 线不足（1H {len(h1)} / 4H {len(h4)} / 15m {len(m15)}）")
        return 0

    gap = now_ms - int(st["last_open"].get(inst) or 0)
    if gap < SAME_SYMBOL_COOLDOWN_MIN * 60_000:
        print(f"[COOL] {name} 距上次开仓 {gap / 60000:.0f} 分钟 < "
              f"{SAME_SYMBOL_COOLDOWN_MIN} 分钟")
        return 0

    ema20 = ema_series([b["close"] for b in m15], 20)
    atr = atr_series(m15)

    # ---------- 补丁一：极速单边锁（只封轨道 A，不封轨道 B）
    a_ok, lock_why = track_a_allowed(m15)
    if not a_ok:
        print(f"[🌊] {name} 🌊极速单边市：{lock_why} → 轨道A 暂停接飞刀")

    # ---------- 轨道 A：找新鲜的扫单+CHoCH
    fresh = []
    for s in find_signals(h1, h4, m15):
        s["inst"] = inst
        key = f"{inst}|{s['choch_ts']}"
        if key in st["seen"]:
            continue
        age = (now - base.bjt(s["choch_ts"])).total_seconds() / 60.0
        if age < 0 or (age > MAX_SIGNAL_AGE_MIN and not FORCE):
            st["seen"][key] = now_ms
            print(f"[SKIP] {name} 信号已 {age:.0f} 分钟，超出时效窗")
            continue
        if not a_ok:
            st["seen"][key] = now_ms
            print(f"[🌊] {name} 极速单边锁生效，轨道A 信号作废（不接飞刀）")
            continue
        fresh.append(s)

    if fresh:
        sig = fresh[0]
        plan = build_track1_plan(sig, h1, h4, now, m15)
        if plan is None:
            print(f"[SKIP] {name} 成交价已越过止损，信号作废")
            return 0
        flow = _safe(base.eval_flow, inst, sig["side"], cfg["ct"]) or \
            {"ok": False, "err": "订单流获取失败", "veto": None}
        ctx = {"ema20": ema20, "atr": atr,
               "fund": _safe(fetch_funding_pct, inst),
               "lsr": _safe(fetch_lsr, cfg["ccy"]), "flow": flow}
        hits = aux_hits(sig["sgn"], m15, plan["idx"], h1, h4, ctx)
        st["seen"][f"{inst}|{sig['choch_ts']}"] = now_ms
        if len(hits) < TRACK1_MIN_HITS:
            print(f"[NO ] {name} 主策略成立但辅助命中 0 维（需 ≥{TRACK1_MIN_HITS}）")
            return 0
        short, full = tier_label(len(hits), 1)
        plan["lots"] = base.size_position(plan["entry"], plan["risk_px"],
                                          RISK_U, cfg["ct"], cfg["lot"])[0]
        seq = int(st["day"]["opens"]) + 1
        pos = open_position(st, inst, name, sig["side"], sig["sgn"], plan, 1,
                            full, hits, flow, seq, now_ms,
                            {"last_bar_ts": sig["fill_close_ts"]})
        push_wechat(f"☁️【云端模拟】{short} | {name} {sig['side']}",
                    build_entry_card(1, full, len(hits), hits, sig, plan,
                                     flow, st, seq))
        print(f"[BOOK-A] {name} {sig['side']} {full} | {len(hits)} 维 "
              f"{[h[0] for h in hits]} | 入场 {plan['entry']:,.2f}")
        return 1

    # ---------- 轨道 B：1H FVG 回踩 + Al Brooks 强信号K + 下一根二确
    sgn, why_side = choose_side(h4, m15)
    side = "做多" if sgn > 0 else "做空"
    fvg_sig, fvg_why = fvg_entry_signal(h1, m15, sgn)
    if not fvg_sig:
        print(f"[B- ] {name} 轨道B 无入场结构：{fvg_why}")
        return 0
    key = f"{inst}|FVG|{fvg_sig['choch_ts']}"
    if key in st["seen"]:
        return 0
    age = (now - base.bjt(fvg_sig["choch_ts"])).total_seconds() / 60.0
    if age < 0 or (age > MAX_SIGNAL_AGE_MIN and not FORCE):
        st["seen"][key] = now_ms
        print(f"[B- ] {name} 轨道B 二确已 {age:.0f} 分钟，超出时效窗")
        return 0
    st["seen"][key] = now_ms

    flow = _safe(base.eval_flow, inst, side, cfg["ct"]) or \
        {"ok": False, "err": "订单流获取失败", "veto": None}
    ctx = {"ema20": ema20, "atr": atr,
           "fund": _safe(fetch_funding_pct, inst),
           "lsr": _safe(fetch_lsr, cfg["ccy"]), "flow": flow}
    hits = aux_hits(sgn, m15, fvg_sig["confirm_i"], h1, h4, ctx)
    if len(hits) < TRACK2_MIN_HITS:
        print(f"[B- ] {name} FVG 结构成立但辅助 {len(hits)} 维 "
              f"{[h[0] for h in hits]} < {TRACK2_MIN_HITS}")
        return 0

    plan = build_track2_plan(fvg_sig, m15, h1, atr, now)
    if plan is None:
        print(f"[SKIP] {name} ATR 不足，轨道B 信号放弃")
        return 0
    plan["lots"] = base.size_position(plan["entry"], plan["risk_px"],
                                      RISK_U, cfg["ct"], cfg["lot"])[0]
    short, full = tier_label(len(hits), 2)
    seq = int(st["day"]["opens"]) + 1
    sig = {"inst": inst, "side": side, "sgn": sgn, "ext": None, "lvl": None,
           "sweep_ts": None, "choch_ts": fvg_sig["choch_ts"],
           "choch_close": fvg_sig["choch_close"], "delay_min": fvg_sig["delay_min"],
           "kz": None, "zone_lo": fvg_sig["zone_lo"], "zone_hi": fvg_sig["zone_hi"],
           "sig_bar": m15[fvg_sig["sig_i"]], "confirm_bar": m15[fvg_sig["confirm_i"]],
           "fast_lock": (lock_why if not a_ok else "")}
    open_position(st, inst, name, side, sgn, plan, 2, full, hits, flow, seq,
                  now_ms, {"last_bar_ts": fvg_sig["fill_close_ts"]})
    push_wechat(f"☁️【纯辅助共振】{short} | {name} {side}",
                build_entry_card(2, full, len(hits), hits, sig, plan, flow,
                                 st, seq))
    print(f"[BOOK-B] {name} {side} {full} | {len(hits)} 维 "
          f"{[h[0] for h in hits]} | FVG {sig['zone_lo']:,.2f}-{sig['zone_hi']:,.2f}"
          f" | 入场 {plan['entry']:,.2f}（{why_side}）")
    return 1


def check_positions(st, now_ms):
    sent = 0
    by_inst = {}
    for p in st["positions"]:
        by_inst.setdefault(p["inst"], []).append(p)
    for inst, plist in by_inst.items():
        m15 = _safe(fetch_15m, inst)
        if not m15:
            print(f"[ERR] {_NAME.get(inst, inst)} 取 15m 失败，本轮跳过结算")
            continue
        for pos in list(plist):
            ex = check_position(pos, m15)
            if ex is None:
                continue
            pos = dict(pos)
            pos.update({"exit": ex["exit"], "why": ex["why"], "r": ex["r"],
                        "u": ex["u"], "exit_ts": ex["exit_ts"],
                        "closed_ts": now_ms})
            st["positions"] = [p for p in st["positions"]
                               if p.get("id") != pos.get("id")]
            st["history"].append(pos)
            st["day"]["pnl"] = round(float(st["day"].get("pnl") or 0.0) + ex["u"], 4)
            push_wechat(f"✅【已自动平仓】{pos['name']} {pos['side']} {ex['u']:+.2f}U",
                        build_close_card(pos, ex, st))
            print(f"[CLOSE] {pos['name']} {pos['side']} {ex['why']} "
                  f"{ex['exit']:,.2f} → {ex['u']:+.2f}U（{ex['r']:+.2f}R）")
            sent += 1
    return sent


def print_patches():
    print(f"[PATCH1] 🌊 15m 极速单边拦截器 已激活：最近 {FAST_WINDOW} 根中 "
          f"≥{FAST_MIN_HITS} 根实体 > {FAST_BODY_MULT:g}× 前 {FAST_LOOKBACK} 根均实体 "
          f"→ 强制锁定 {FAST_LOCK_BARS} 根 15m（约 {FAST_LOCK_BARS*15/60:g} 小时）"
          f"，期间轨道A（震荡海龟汤）全部拒单，轨道B 不受影响")
    print(f"[PATCH2] 🧱 轨道B 1H FVG 严格二次确认 已激活：近 {FVG_LOOKBACK_1H} 根 1H 的 "
          f"FVG 回踩 → 区内 Al Brooks 强信号K → 下一根 15m 收盘不破前低/前高才触发；"
          f"任一同向 FVG 被 15m 收盘击穿边沿即作废")


def main(argv=None):
    ap = argparse.ArgumentParser(description="云端模拟雷达 v2（只读行情 + 虚拟记账）")
    ap.add_argument("--self-test", action="store_true", help="离线自检")
    ap.add_argument("--test", action="store_true",
                    help="强制发一张『云端连通用测试』卡片（不记账、不建仓）")
    ap.add_argument("--state", default=STATE_PATH, help="状态文件路径")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()

    # GitHub 上手动触发（Actions → Run workflow）也算测试模式
    test_mode = bool(args.test) or \
        os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"

    now = datetime.datetime.now(BJT)
    now_ms = int(time.time() * 1000)
    print(f"===== 云端模拟雷达 v2 {now:%Y-%m-%d %H:%M:%S} (北京) "
          f"｜ Killzone {killzone(now) or '非活跃'} ｜ {RISK_U:g}U/单 ｜ 无单数/持仓上限"
          f"{' ｜ 🔌 测试模式（手动触发，必发测试卡片）' if test_mode else ''} =====")
    print_patches()

    if not os.environ.get("PUSHPLUS_TOKEN"):
        print("[WARN] 没有 PUSHPLUS_TOKEN，本轮只记账不推送")

    st = load_state(args.state)
    roll_day(st, now)

    sent = check_positions(st, now_ms)

    for inst, cfg in SYMBOLS.items():
        try:
            sent += handle_symbol(inst, cfg, st, now, now_ms)
        except Exception as e:  # noqa: BLE001
            print(f"[ERR] {cfg['name']}：{type(e).__name__}: {e}")

    try:
        save_state(args.state, st)
    except Exception as e:  # noqa: BLE001
        print(f"[ERR] 状态写盘失败：{type(e).__name__}: {e}")
        return 1

    if test_mode:
        html = build_test_card(st, now)
        ok = push_wechat("☁️【云端连通用测试】BTC 做多（假信号）", html)
        print(f"[TEST] 连通性测试卡片 {'已发出' if ok else '未发出'} ｜ "
              f"测试卡片自身不记账、不建仓、不占冷却")
        sent += 1 if ok else 0

    print(f"[DONE] 推送 {sent} 条 ｜ 在手 {len(st['positions'])} 笔 ｜ "
          f"今日 {st['day']['opens']} 单 ｜ 今日 {st['day']['pnl']:+.2f}U")
    return 0


# ================================================================ 自检
def _bar(ts, o, h, l, c, step, v=1.0):
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c, "vol": v,
            "close_ts": ts + step}


def _synth_long():
    """造一组能触发「1H 扫单 + 15m CHoCH」的多头数据（扫单固定在北京 10:00 收盘）。"""
    H, M = 3600_000, 900_000
    sweep_close_ts = int(datetime.datetime(2026, 11, 3, 10, 0,
                                           tzinfo=BJT).timestamp() * 1000)
    sweep_i = 55
    t0 = sweep_close_ts - (sweep_i + 1) * H
    h1 = []
    for i in range(60):
        h1.append(_bar(t0 + i * H, 100.0, 100.5, 99.5, 100.0, H))
    h1[sweep_i] = _bar(t0 + sweep_i * H, 100.0, 100.2, 98.0, 99.8, H)
    for i in range(sweep_i + 1, len(h1)):
        h1[i] = _bar(t0 + i * H, 99.8, 100.0, 99.7, 99.9, H)
    assert h1[sweep_i]["close_ts"] == sweep_close_ts

    m15 = []
    base_ts = t0 + 40 * H
    for i in range(24 * 4 * 2):
        m15.append(_bar(base_ts + i * M, 100.6, 101.0, 100.4, 100.8, M))
    k_piv = next((i for i, b in enumerate(m15)
                  if b["close_ts"] == sweep_close_ts - 8 * M), None)
    if k_piv is not None:
        m15[k_piv] = _bar(m15[k_piv]["ts"], 101.0, 102.0, 100.9, 101.2, M)
        for d in (-2, -1, 1, 2):
            j = k_piv + d
            m15[j] = _bar(m15[j]["ts"], 101.0, 101.3, 100.9, 101.1, M)
    k0 = next(i for i, b in enumerate(m15) if b["ts"] >= sweep_close_ts)
    m15[k0] = _bar(m15[k0]["ts"], 99.9, 101.2, 99.8, 101.0, M)
    m15[k0 + 1] = _bar(m15[k0 + 1]["ts"], 101.0, 102.6, 100.9, 102.4, M)
    m15[k0 + 2] = _bar(m15[k0 + 2]["ts"], 102.35, 102.8, 102.1, 102.5, M)
    return h1, m15, sweep_close_ts, k0


def _synth_fvg(bad_confirm=False):
    """造一组「1H 看涨 FVG + 15m 区内强信号K + 下一根二确」的数据。

    FVG 区间 [100.0, 101.0]，形成于 h1[29] 收盘；15m 从 t0+20h 起 48 根，
    k0=40 是缺口后的第一根；信号K=44，二确K=45，成交根=46。
    """
    H, M = 3600_000, 900_000
    t0 = int(datetime.datetime(2026, 11, 3, 0, 0, tzinfo=BJT).timestamp() * 1000)
    h1 = [_bar(t0 + i * H, 99.5, 100.0, 99.0, 99.8, H) for i in range(28)]
    h1.append(_bar(t0 + 28 * H, 99.8, 100.9, 99.7, 100.8, H))
    h1.append(_bar(t0 + 29 * H, 100.9, 101.5, 101.0, 101.2, H))
    zts = h1[29]["close_ts"]

    base_ts = t0 + 20 * H
    m15 = [_bar(base_ts + i * M, 100.2, 100.5, 100.1, 100.4, M) for i in range(48)]
    m15[44] = _bar(base_ts + 44 * M, 100.30, 100.95, 100.25, 100.90, M)
    m15[45] = _bar(base_ts + 45 * M, 100.90, 101.00,
                   100.20 if bad_confirm else 100.30, 100.80, M)
    m15[46] = _bar(base_ts + 46 * M, 100.85, 100.90, 100.50, 100.70, M)
    m15[47] = _bar(base_ts + 47 * M, 100.70, 100.90, 100.50, 100.80, M)
    return h1, m15, zts


def self_test():
    ok, bad = [], []

    def ck(name, cond, extra=""):
        (ok if cond else bad).append(f"{name}{'' if cond else ' ← ' + str(extra)}")

    def dt(h, m):
        return datetime.datetime(2026, 11, 3, h, m, tzinfo=BJT)

    # 1. Killzone
    ck("KZ 07:50 关闭", killzone(dt(7, 50)) is None)
    ck("KZ 08:00 放行", killzone(dt(8, 0)) is not None)
    ck("KZ 23:59 放行", killzone(dt(23, 59)) is not None)
    ck("KZ 00:10 算后延", killzone(dt(0, 10)) is not None)
    ck("KZ 00:20 关闭", killzone(dt(0, 20)) is None)

    # 2. 主策略信号
    h1, m15, sweep_close_ts, k0 = _synth_long()
    h4 = [_bar(1_790_000_000_000 + i * 14400_000, 90 + i * .5, 91 + i * .5,
               89 + i * .5, 90 + i * .5, 14400_000) for i in range(40)]
    sigs = find_signals(h1, h4, m15)
    ck("主策略识别 1 个信号", len(sigs) == 1, len(sigs))
    ck("主策略方向=做多", sigs and sigs[0]["side"] == "做多")
    ck("成交=确认后一根开盘",
       sigs and abs(sigs[0]["fill"] - m15[k0 + 2]["open"]) < 1e-9)
    m15_bad = [dict(b) for b in m15]
    m15_bad[k0] = dict(m15_bad[k0], close=97.0, low=96.5)
    ck("反向作废不触发", find_signals(h1, h4, m15_bad) == [])

    # 3. Al Brooks 三条件
    M = 900_000
    base = [_bar(i * M, 100, 100.4, 99.8, 100.2, M) for i in range(20)]
    # 条件1：强阳线 + 收盘贴最高 1/3
    b1 = base + [_bar(20 * M, 100.0, 101.0, 99.95, 100.9, M)]
    e1 = ema_series([x["close"] for x in b1], 20)
    ck("PA①信号K 成立", albrooks_pa(b1, 20, 1, e1)[0])
    wick = base + [_bar(20 * M, 100.0, 101.0, 99.5, 100.3, M)]
    _, why_w = albrooks_pa(wick, 20, 1, ema_series([x["close"] for x in wick], 20))
    ck("PA①长上影不算信号K", not why_w.startswith("①"), why_w)
    # 条件2：微型双底 + 阳线
    b2 = [_bar(i * M, 100.2, 100.6, 100.0, 100.3, M) for i in range(20)]
    b2[14] = _bar(14 * M, 100.0, 100.3, 99.00, 100.1, M)
    b2[18] = _bar(18 * M, 100.0, 100.3, 99.01, 100.1, M)
    b2[19] = _bar(19 * M, 100.1, 101.0, 100.05, 100.9, M)
    e2 = ema_series([x["close"] for x in b2], 20)
    hit2, why2 = albrooks_pa(b2, 19, 1, e2)
    ck("PA②微型双底 成立", hit2, why2)
    # 条件3：Always In（3 根中 2 根阳 + 收盘 > EMA20）
    b3 = [_bar(i * M, 100.0, 100.2, 99.9, 100.0, M) for i in range(17)]
    b3 += [_bar(17 * M, 100.0, 101.0, 99.9, 100.8, M),
           _bar(18 * M, 100.8, 101.8, 100.7, 101.6, M),
           _bar(19 * M, 101.6, 102.2, 101.5, 102.0, M)]
    e3 = ema_series([x["close"] for x in b3], 20)
    hit3, why3 = albrooks_pa(b3, 19, 1, e3)
    ck("PA③AlwaysIn 成立", hit3, why3)
    ck("PA 做空不看多头信号", not albrooks_pa(b1, 20, -1, e1)[0])

    # 4. 分层标签
    ck("1 维 → ⚡B", tier_label(1, 1)[1] == "⚡ B级")
    ck("2 维 → 🔥A", tier_label(2, 1)[1] == "🔥 A级")
    ck("3 维 → 👑A+", tier_label(3, 1)[1] == "👑 A+级")
    ck("纯辅助 3 维", tier_label(3, 2)[1] == "🧬 纯辅助-3维共振")
    ck("纯辅助 4 维", tier_label(4, 2)[1] == "🧬 纯辅助-4维共振")
    ck("纯辅助 5 维", tier_label(5, 2)[1] == "🌌 纯辅助-5维极限共振")
    ck("标题短标签风格", tier_label(3, 2)[0] == "🧬 3维共振")

    # 5. ATR / EMA
    a = atr_series(m15)
    ck("ATR 前 13 根为空", a[12] is None and a[13] is not None)
    ck("EMA20 长度对齐", len(ema_series([x["close"] for x in m15], 20)) == len(m15))

    # 6. 轨道 B 计划：FVG 二确入场 + 1.5ATR 止损 + 2.5R 兜底
    h1f, m15f, zts = _synth_fvg()
    fz = fvg_zones(h1f)
    ck("FVG 识别 1 个看涨缺口", len(fz) == 1 and fz[0]["dir"] == 1, fz)
    sig2, why_f = fvg_entry_signal(h1f, m15f, 1)
    ck("FVG 强信号K + 下一根二确 成立", sig2 is not None, why_f)
    if sig2:
        ck("FVG 成交=二确后一根开盘",
           abs(sig2["fill"] - m15f[sig2["confirm_i"] + 1]["open"]) < 1e-9)
        ck("FVG 二确紧跟信号K", sig2["confirm_i"] == sig2["sig_i"] + 1)
        af = atr_series(m15f)
        plan2 = build_track2_plan(sig2, m15f, h1f, af, dt(12, 0))
        ck("轨道二有计划", plan2 is not None)
        if plan2:
            ck("轨道二止损=1.5ATR",
               abs(plan2["risk_px"] - TRACK2_STOP_ATR
                   * af[sig2["confirm_i"]]) < 1e-9)
            ck("轨道二盈亏比≥2.5", plan2["rr"] >= MIN_TP_R - 1e-6, plan2["rr"])
            ck("轨道二入场=二确后开盘", abs(plan2["entry"] - sig2["fill"]) < 1e-9)

    # 6b. 补丁二：跌破作废 / 二确失败
    h1b, m15b, _ = _synth_fvg()
    m15b[42] = _bar(m15b[42]["ts"], 100.3, 100.4, 99.4, 99.5, M)
    sb, why_b = fvg_entry_signal(h1b, m15b, 1)
    ck("补丁二：15m 收盘击穿 FVG 下沿 → 作废", sb is None, why_b)
    h1c, m15c, _ = _synth_fvg(bad_confirm=True)
    sc, why_c = fvg_entry_signal(h1c, m15c, 1)
    ck("补丁二：二确K 跌破信号K 低点 → 不触发", sc is None, why_c)
    h1d, m15d, _ = _synth_fvg()
    sd, _ = fvg_entry_signal(h1d, m15d, -1)
    ck("补丁二：做空不取看涨 FVG", sd is None)

    # 6c. 补丁一：15m 极速单边拦截器
    flat = [_bar(i * M, 100.0, 100.3, 99.9, 100.2, M) for i in range(40)]
    ck("补丁一：常态不锁", fast_trend_state(flat)[0] is False)
    ck("补丁一：常态轨道A 放行", track_a_allowed(flat)[0])
    one = flat + [_bar(40 * M, 100.0, 101.2, 99.9, 101.0, M),
                  _bar(41 * M, 101.0, 101.2, 100.9, 101.1, M),
                  _bar(42 * M, 101.1, 101.3, 101.0, 101.2, M)]
    ck("补丁一：3 根里仅 1 根大实体 → 不触发",
       fast_trend_state(one)[0] is False)
    fast = flat + [_bar(40 * M, 100.0, 101.3, 99.9, 101.2, M),
                   _bar(41 * M, 101.2, 102.5, 101.1, 102.4, M),
                   _bar(42 * M, 102.4, 102.6, 102.3, 102.5, M)]
    f_ok, f_why = fast_trend_state(fast)
    ck("补丁一：3 根里 2 根 >2× 均实体 → 触发", f_ok, f_why)
    ck("补丁一：触发时识别方向", "向上" in f_why, f_why)
    ck("补丁一：触发时锁轨道A", not track_a_allowed(fast)[0])
    old = fast + [_bar((43 + i) * M, 102.5, 102.7, 102.4, 102.6, M)
                  for i in range(10)]
    ck(f"补丁一：{FAST_LOCK_BARS} 根后自动解锁", track_a_allowed(old)[0])
    ck("补丁一：向下极速单边也拦截",
       fast_trend_state(flat + [_bar(40 * M, 100.2, 100.3, 99.0, 99.1, M),
                                _bar(41 * M, 99.1, 99.2, 97.8, 97.9, M),
                                _bar(42 * M, 97.9, 98.0, 97.8, 97.9, M)])[0])

    # 7. 出场引擎
    pos = {"sgn": 1, "entry": 100.0, "stop": 99.0, "tp": 103.0, "risk_px": 1.0,
           "last_bar_ts": 0, "bars_held": 0, "be": False}
    ex = check_position(pos, [_bar(1, 100, 104, 98, 101, M)])
    ck("同根止损优先", ex and ex["why"] == "止损", ex and ex["why"])
    pos = {"sgn": 1, "entry": 100.0, "stop": 99.0, "tp": 103.0, "risk_px": 1.0,
           "last_bar_ts": 0, "bars_held": 0, "be": False}
    ex = check_position(pos, [_bar(1, 100, 103.5, 99.5, 103.2, M)])
    ck("止盈命中", ex and ex["why"] == "止盈", ex and ex["why"])
    ck("止盈净R≈3R-费", ex and 2.8 < ex["r"] < 3.0, ex and ex["r"])
    pos = {"sgn": 1, "entry": 100.0, "stop": 99.0, "tp": 110.0, "risk_px": 1.0,
           "last_bar_ts": 0, "bars_held": 0, "be": False}
    ex = check_position(pos, [_bar(1, 100, 101.2, 99.8, 101.0, M),
                              _bar(2, 101.0, 101.1, 99.9, 100.0, M)])
    ck("1R 后回落=保本", ex and ex["why"] == "保本", ex and ex["why"])
    ck("保本仍扣手续费", ex and ex["r"] < 0, ex and ex["r"])
    pos = {"sgn": 1, "entry": 100.0, "stop": 99.0, "tp": 200.0, "risk_px": 1.0,
           "last_bar_ts": 0, "bars_held": 0, "be": False}
    ex = check_position(pos, [_bar(i, 100.0, 100.5, 99.9, 100.2, M)
                              for i in range(1, 120)])
    ck("96 根超时", ex and ex["why"] == "超时24h", ex and ex["why"])

    # 8. 状态 v2：跨日 / 列表持仓 / 冷却 / 迁移
    st = default_state()
    st["date"] = "2026-11-02"
    st["day"] = {"opens": 9, "pnl": -40.0}
    st["positions"] = [{"id": "a"}, {"id": "b"}]
    roll_day(st, dt(9, 0))
    ck("跨日单数归零", st["day"]["opens"] == 0, st["day"])
    ck("跨日保留多笔持仓", len(st["positions"]) == 2)
    ck("无日亏字段", "halt" not in str(st["day"]))
    ck("无单数上限常量", not hasattr(sys.modules[__name__], "MAX_OPENS_PER_DAY"))
    ck("无日亏熔断常量", not hasattr(sys.modules[__name__], "DAILY_LOSS_LIMIT_U"))

    import tempfile
    p = os.path.join(tempfile.mkdtemp(), "paper_state.json")
    st2 = default_state()
    st2["date"] = "2026-11-03"
    st2["day"]["opens"] = 11
    st2["day"]["pnl"] = -63.5
    st2["positions"] = [{"id": "x", "inst": "BTC-USDT-SWAP", "entry": 1.0},
                        {"id": "y", "inst": "BTC-USDT-SWAP", "entry": 2.0}]
    st2["last_open"] = {"BTC-USDT-SWAP": 123}
    st2["seen"]["k"] = 1
    save_state(p, st2)
    back = load_state(p)
    ck("状态往返：多笔持仓", len(back["positions"]) == 2)
    ck("状态往返：冷却表", back["last_open"]["BTC-USDT-SWAP"] == 123)
    ck("状态往返：无上限计数", back["day"]["opens"] == 11)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "positions": {"BTC-USDT-SWAP": {"id": "z",
                  "inst": "BTC-USDT-SWAP", "opened_ts": 5}}}, f)
    mig = load_state(p)
    ck("v1 dict → v2 list 迁移", isinstance(mig["positions"], list) and
       len(mig["positions"]) == 1)
    with open(p, "w", encoding="utf-8") as f:
        f.write("{坏 JSON")
    try:
        load_state(p)
        ck("坏文件必须报错", False)
    except RuntimeError:
        ck("坏文件必须报错", True)

    # 9. 卡片：轨道二警示置顶 + 标题格式    if sigs:
        sig = dict(sigs[0], inst="BTC-USDT-SWAP")
        plan = build_track1_plan(sig, h1, h4, dt(12, 0), m15)
        plan["lots"] = 0.9
        flow_bad = {"ok": True, "veto": "CVD 净卖，与做多方向背离",
                    "cvd": -1.0, "cvd_ratio": -8.0, "buy_ratio": 46.0,
                    "cvd_usd": -770.0, "p0": 100.0, "p1": 101.0, "dpct": 1.0,
                    "imb": -3.0, "spread_bps": 1.0, "wall_ratio": 0.1,
                    "whale_buy": 1000.0, "whale_sell": 2000.0,
                    "whale_net": -1000.0, "whale_n": 1}
        hits = [("PA", "①信号K"), ("H4", "4H up"), ("ATR", "波动率达标")]
        html = build_entry_card(1, "👑 A+级", 3, hits, sig, plan, flow_bad,
                                st, 1)
        ck("风险标签置顶", 0 < html.find("【订单流背离】") < html.find("模拟入场"))
        ck("正文列出命中项", "1. PA(Al Brooks)" in html and "3. ATR 波动率" in html)
        ck("卡片含 1 张说明", "1 张无法分批" in html)

        p2 = dict(plan, age_min=0.0, atr=12.3)
        html2 = build_entry_card(2, "🧬 纯辅助-4维共振", 4, hits,
                                 dict(sig, sweep_ts=None, lvl=None), p2,
                                 flow_bad, st, 2)
        ck("轨道二警示置顶", 0 < html2.find("无1H扫单结构") < html2.find("模拟入场"))
        ck("轨道二标注左侧信号", "左侧" in html2)

    # 10. 连通性测试卡片：走真实渲染路径，且不写账本
    st_t = default_state()
    st_t["day"]["opens"] = 4
    before = json.dumps(st_t, sort_keys=True)
    html_t = build_test_card(st_t, dt(12, 0), price=100000.0)
    ck("测试卡片有内容", len(html_t) > 500, len(html_t))
    ck("测试卡片标注「连通用测试」", "连通用测试" in html_t)
    ck("测试卡片是完整入场卡片",
       "模拟入场" in html_t and "结构止盈" in html_t and "建议仓位" in html_t)
    ck("测试卡片标题要素（做多/假信号由推送标题给出）", "BTC" in html_t)
    ck("测试卡片不写账本", json.dumps(st_t, sort_keys=True) == before)

    total = len(ok) + len(bad)
    print("=" * 62)
    for x in ok:
        print(f"  ok   {x}")
    for x in bad:
        print(f"  FAIL {x}")
    print("=" * 62)
    print(f"云端模拟雷达 v2 自检 {'通过' if not bad else '失败'} {total} 项，失败 {len(bad)} 项")
    if not bad:
        print_patches()
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
