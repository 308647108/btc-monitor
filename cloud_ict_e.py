#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cloud_ict_e.py — 云端方案 E / ICT 哨兵  v2.0
================================================================================
GitHub Actions 无状态运行，直接把「方案 E」的完整四环节搬到云端：

    结构定方向 → SMC 定位置 → PA 定触发 → 订单流验真

与本地版本的三处差异（都是刻意为之）：
  · 直连 OKX，不走代理（Actions 跑在境外，直连最快）
  · 无后台 AI、无本地状态文件；每小时跑两次，靠「信号根收盘年龄」去重
  · 不再有盈亏比拦截。目标改为「当前方向上最近的结构位」（前高/前低/FVG 边沿）

保留的核心（一根都不能少）：
  · 4H 定调：HH+HL 只做多，LH+LL 只做空，震荡不做
  · Killzone：北京 15:00–18:00 / 20:00–24:00，各自后延 15 分钟容错
  · 1H 扫单：刺破前 24 小时高/低后当根收回区间内
  · CHoCH：扫单后 6 根 1H 内，收盘越过最近的对手摆动位
  · 止损：扫单极点外 0.1%
  · 订单流一票否决：OKX 逐笔 + 盘口深度，真实 CVD / 巨鲸 / 挂单墙

环境变量
  PUSHPLUS_TOKEN    必填，PushPlus 的 token
  FORCE=1           跳过「信号根新鲜度」检查（手动调试用）
  TEST=1            发一条连通性测试推送（不参与信号判定）
  RISK_U            单笔风控，默认 5.0
  FLOW_VETO_MODE    strict（默认，背离即否决）| directional（与本地 15m V7 一致）
  PUSH_VETO         默认 1：被否决的信号也推一条「不交易」回执；0 = 静默
  MAX_NOTIONAL_U    名义仓位硬上限，默认 2000（防止止损过近时算出天量仓位）

用法
  python cloud_ict_e.py
  FORCE=1 python cloud_ict_e.py
  TEST=1  python cloud_ict_e.py
================================================================================
"""
from __future__ import annotations

import datetime
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover
    pass


# ================================================================ 配置
BASE = "https://www.okx.com"
BJT = datetime.timezone(datetime.timedelta(hours=8))

SYMBOLS = {
    "BTC-USDT-SWAP": {"name": "BTC", "ct": 0.01, "lot": 0.01},
    "ETH-USDT-SWAP": {"name": "ETH", "ct": 0.10, "lot": 0.01},
}

RISK_U = float(os.environ.get("RISK_U") or 5.0)
RISK_U_UNVERIFIED = RISK_U / 2.0          # 订单流拉取失败 → 半仓降级
FEE_RT = 0.001                            # 双边 taker 0.05% × 2
MAX_NOTIONAL_U = float(os.environ.get("MAX_NOTIONAL_U") or 2000.0)

# ---- 方案 E 冻结参数（改这里 = 改策略）
SWEEP_LOOKBACK = 24                       # 前 24 根 1H
STOP_BUFFER_PCT = 0.0010                  # 扫单极点外 0.1%
MAX_SETUP_BARS = 6                        # 扫单后 6 根内必须 CHoCH
KILLZONES = ((15, 18, "LDN 欧盘"), (20, 24, "NY 美盘"))
KZ_GRACE_MIN = 15                         # 窗口结束后延容错

PIVOT_K = 2
PIVOT_LOOKBACK = 96
TREND_MIN_PIVOTS = 2

# ---- 订单流
FLOW_WINDOW_MIN = 3                       # 最近 3 分钟逐笔
WHALE_USD = 50_000.0                      # 巨鲸 = 单笔 ≥ 5 万 U
WHALE_VETO_MIN_USD = 100_000.0            # 逆向巨鲸否决下限
WHALE_VETO_MULT = 2.0                     # 逆向 ≥ 2 倍顺向
WALL_BAND_PCT = 0.10                      # 挂单墙统计带（距中间价 ±0.10%）
WALL_VETO_RATIO = 0.25                    # 单档占该侧深度 ≥25% = 墙
BOOK_BAND_PCT = 0.25                      # 盘口失衡统计带
SPREAD_VETO_BPS = 8.0                     # 点差 > 8bps 否决
FLOW_MIN_TRADES = 30                      # 逐笔样本太少 = 未验证

# ---- 止盈
STRUCT_LOOKBACK = 96
FVG_LOOKBACK_1H = 60
FVG_LOOKBACK_4H = 40
MIN_TP_R = 0.5                            # 结构位至少要离入场 0.5R，否则算"贴脸"不算目标
FALLBACK_R = 3.0                          # 一个结构位都没有时的兜底倍数

# ---- 调度
FRESH_MAX_MIN = 30                        # 信号根收盘 ≤30 分钟内才发，避免 :05 / :35 重复
FORCE = os.environ.get("FORCE", "").strip().lower() in ("1", "true", "yes")
TEST = os.environ.get("TEST", "").strip().lower() in ("1", "true", "yes")
FLOW_VETO_MODE = (os.environ.get("FLOW_VETO_MODE") or "strict").strip().lower()
PUSH_VETO = (os.environ.get("PUSH_VETO") or "1").strip() != "0"

BAR_MS = {"1H": 3600_000, "4H": 14400_000}


# ================================================================ HTTP
def _http(url, data=None, timeout=20, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(
                url, data=data,
                headers={"User-Agent": "Mozilla/5.0 cloud-ict-e/2.0",
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")[:160]
            last = f"HTTP {e.code} {body}"
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
        time.sleep(1.0 * (i + 1))
    raise RuntimeError(str(last))


def okx(path, params=None):
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    d = json.loads(_http(url))
    if d.get("code") != "0":
        raise RuntimeError(f"{path} code={d.get('code')} msg={d.get('msg')}")
    return d.get("data") or []


def push_wechat(title, content):
    token = (os.environ.get("PUSHPLUS_TOKEN") or "").strip()
    if not token:
        print("[PUSH] 没有 PUSHPLUS_TOKEN，跳过推送")
        return False
    payload = json.dumps({"token": token, "title": title,
                          "content": content, "template": "html"}).encode("utf-8")
    try:
        d = json.loads(_http("https://www.pushplus.plus/send", data=payload))
    except Exception as e:  # noqa: BLE001
        print(f"[PUSH] 失败：{e}")
        return False
    ok = d.get("code") == 200
    print(f"[PUSH] {'成功' if ok else '失败'}：{title} → {d.get('msg')}")
    return ok


# ================================================================ 行情
def fetch_candles(inst, bar, limit=300):
    """只要已收盘的 K 线，按时间升序返回。"""
    step = BAR_MS[bar]
    rows = okx("/api/v5/market/candles",
               {"instId": inst, "bar": bar, "limit": str(limit)})
    out = []
    for r in rows:
        if len(r) < 9 or r[8] != "1":
            continue
        ts = int(r[0])
        out.append({"ts": ts, "open": float(r[1]), "high": float(r[2]),
                    "low": float(r[3]), "close": float(r[4]),
                    "vol": float(r[5]), "close_ts": ts + step})
    out.sort(key=lambda c: c["ts"])
    return out


def fetch_price(inst):
    d = okx("/api/v5/market/ticker", {"instId": inst})
    if not d:
        raise RuntimeError("ticker 为空")
    return float(d[0]["last"])


def fetch_spec(inst, fallback_ct, fallback_lot):
    try:
        d = okx("/api/v5/public/instruments", {"instType": "SWAP", "instId": inst})
        if d:
            return float(d[0]["ctVal"]), float(d[0]["lotSz"])
    except Exception as e:  # noqa: BLE001
        print(f"[SPEC] {inst} 合约面值取失败（{e}），用内置值")
    return fallback_ct, fallback_lot


# ================================================================ 结构（与 utils.py 同一套算法）
def structure(candles, k=PIVOT_K, lookback=PIVOT_LOOKBACK):
    """最近 lookback 根里找左右各 k 根确认的摆动点，返回时间升序、H/L 交替的
    [(下标, 'H'/'L', 价格), ...]"""
    if not candles:
        return []
    seg = candles[-lookback:]
    off = len(candles) - len(seg)
    piv = []
    for i in range(k, len(seg) - k):
        hi, lo = seg[i]["high"], seg[i]["low"]
        if all(hi >= seg[j]["high"] for j in range(i - k, i + k + 1) if j != i):
            piv.append((off + i, "H", hi))
        if all(lo <= seg[j]["low"] for j in range(i - k, i + k + 1) if j != i):
            piv.append((off + i, "L", lo))
    piv.sort(key=lambda x: x[0])
    clean = []
    for x in piv:
        if clean and clean[-1][1] == x[1]:
            if (x[1] == "H" and x[2] > clean[-1][2]) or \
               (x[1] == "L" and x[2] < clean[-1][2]):
                clean[-1] = x
        else:
            clean.append(x)
    return clean


def nearest_above(piv, price):
    cand = [p[2] for p in piv if p[1] == "H" and p[2] > price]
    return min(cand) if cand else None


def nearest_below(piv, price):
    cand = [p[2] for p in piv if p[1] == "L" and p[2] < price]
    return max(cand) if cand else None


def trend_state(piv):
    """HH+HL → up；LH+LL → down；其余一律 range（从严）。"""
    highs = [p for p in piv if p[1] == "H"]
    lows = [p for p in piv if p[1] == "L"]
    if len(highs) < TREND_MIN_PIVOTS or len(lows) < TREND_MIN_PIVOTS:
        return "range", "摆动点不足"
    hh = highs[-1][2] > highs[-2][2]
    hl = lows[-1][2] > lows[-2][2]
    lh = highs[-1][2] < highs[-2][2]
    ll = lows[-1][2] < lows[-2][2]
    if hh and hl:
        return "up", "HH + HL"
    if lh and ll:
        return "down", "LH + LL"
    return "range", "结构不一致"


def killzone(dt):
    """北京时间 Killzone 标签；窗口结束后再给 15 分钟容错。"""
    if dt is None:
        return None
    m = dt.hour * 60 + dt.minute
    for lo, hi, tag in KILLZONES:
        if lo * 60 <= m < hi * 60 + KZ_GRACE_MIN:
            return tag
    if m < KZ_GRACE_MIN:              # 24:00 之后的后延
        return KILLZONES[-1][2]
    return None


def bjt(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, BJT)


def _t(ms):
    return bjt(ms).strftime("%m-%d %H:%M")


# ================================================================ 信号机（方案 E 冻结逻辑的无状态重放）
class SignalEngine:
    """与归档版 strategy_ict_E.EEngine 逐行等价，只去掉统计计数器。

    有状态是它的本质（扫单后要等 CHoCH）。云端无状态的做法：
    每次运行从「最后 7 根」重新喂一遍，任何 setup 生命周期都不超过 6 根，
    所以起点一定是干净状态。
    """

    def __init__(self):
        self.setup = None

    @staticmethod
    def _j4(h4, bar):
        j = 0
        while j < len(h4) and h4[j]["close_ts"] <= bar["close_ts"]:
            j += 1
        return j

    def observe(self, h1, h4, trace=None):
        i = len(h1) - 1
        bar = h1[i]
        dh = bjt(bar["close_ts"])
        j4 = self._j4(h4, bar)

        # ---- 1) 等待中的 setup：作废 / 超时 / CHoCH 确认
        if self.setup is not None:
            s = self.setup
            sgn = 1 if s["side"] == "做多" else -1
            if (sgn > 0 and bar["close"] < s["ext"]) or \
               (sgn < 0 and bar["close"] > s["ext"]):
                self.setup = None
                return None
            if i - s["j"] > MAX_SETUP_BARS:
                self.setup = None
                return None
            lvl = s["lvl"]
            if (bar["close"] > lvl) if sgn > 0 else (bar["close"] < lvl):
                self.setup = None
                return {"side": s["side"], "ext": s["ext"], "kz": s["kz"],
                        "lvl": lvl, "sweep_ts": s["sweep_ts"],
                        "choch_ts": bar["close_ts"], "prior_hi": s["prior_hi"],
                        "prior_lo": s["prior_lo"], "signal_close": bar["close"]}
            return None

        # ---- 2) 找扫单
        if i < SWEEP_LOOKBACK:
            return None
        prior_hi = max(c["high"] for c in h1[i - SWEEP_LOOKBACK:i])
        prior_lo = min(c["low"] for c in h1[i - SWEEP_LOOKBACK:i])
        long_sweep = bar["low"] < prior_lo and bar["close"] > prior_lo
        short_sweep = bar["high"] > prior_hi and bar["close"] < prior_hi
        if not (long_sweep or short_sweep):
            return None

        kz = killzone(dh)
        if kz is None:
            if trace is not None:
                trace.append(f"{_t(bar['close_ts'])} 扫单但不在 Killzone 内")
            return None

        # ---- 3) 4H 定调
        if j4 < 2 * PIVOT_K + 5:
            return None
        state, _ = trend_state(structure(h4[:j4]))
        if state == "range":
            if trace is not None:
                trace.append("4H 震荡，不做")
            return None
        if state == "up" and long_sweep:
            side, ext = "做多", bar["low"]
        elif state == "down" and short_sweep:
            side, ext = "做空", bar["high"]
        else:
            if trace is not None:
                trace.append("扫单方向与 4H 趋势相反（逆势），不做")
            return None

        piv1 = structure(h1[max(0, i - STRUCT_LOOKBACK + 1):i + 1])
        sgn = 1 if side == "做多" else -1
        lvl = nearest_above(piv1, bar["close"]) if sgn > 0 \
            else nearest_below(piv1, bar["close"])
        if lvl is None:
            if trace is not None:
                trace.append("找不到 CHoCH 触发位（无对手摆动点）")
            return None

        self.setup = {"side": side, "j": i, "ext": ext, "lvl": lvl, "kz": kz,
                      "sweep_ts": bar["close_ts"], "prior_hi": prior_hi,
                      "prior_lo": prior_lo}
        return None


def scan_signal(h1, h4, trace=None):
    """只认「最后一根刚收盘时」触发的 CHoCH，更早的视为已推送过。"""
    if len(h1) < SWEEP_LOOKBACK + 5:
        return None
    eng = SignalEngine()
    start = max(0, len(h1) - 1 - MAX_SETUP_BARS - 1)
    last = None
    for k in range(start, len(h1)):
        sig = eng.observe(h1[:k + 1], h4, trace if k == len(h1) - 1 else None)
        if sig is not None:
            last = sig if k == len(h1) - 1 else None
    return last


# ================================================================ 结构止盈位
def fvg_levels(candles, lookback, tf_name):
    """三根 K 线的缺口边沿。返回 [(价格, 说明), ...]。"""
    seg = candles[-lookback:]
    out = []
    for i in range(2, len(seg)):
        a, c = seg[i - 2], seg[i]
        if c["low"] > a["high"]:            # 看涨缺口
            out.append((a["high"], f"{tf_name} FVG 边沿"))
            out.append((c["low"], f"{tf_name} FVG 边沿"))
        if c["high"] < a["low"]:            # 看跌缺口
            out.append((c["high"], f"{tf_name} FVG 边沿"))
            out.append((a["low"], f"{tf_name} FVG 边沿"))
    return out


def structural_target(h1, h4, price, sgn, risk_px):
    """方向上最近的结构位：1H/4H 前高前低 + FVG 边沿。

    返回 (价格, 来源) 或 (None, "")。
    不设盈亏比门槛——只要求离入场至少 0.5R，否则那不算"目标"算"贴脸"。
    """
    cands = []
    want = "H" if sgn > 0 else "L"
    for p in structure(h1):
        if p[1] == want:
            cands.append((p[2], "1H 前高" if sgn > 0 else "1H 前低"))
    for p in structure(h4):
        if p[1] == want:
            cands.append((p[2], "4H 前高" if sgn > 0 else "4H 前低"))
    cands += fvg_levels(h1, FVG_LOOKBACK_1H, "1H")
    cands += fvg_levels(h4, FVG_LOOKBACK_4H, "4H")

    ok = [(lv, src) for lv, src in cands
          if (lv - price) * sgn >= risk_px * MIN_TP_R]
    if not ok:
        return None, ""
    return min(ok, key=lambda x: abs(x[0] - price))


# ================================================================ 订单流
def _trades(inst, limit=500, after=None, history=False):
    p = {"instId": inst, "limit": str(limit)}
    if history:
        p["type"] = "1"                 # 1 = 永续
    if after:
        p["after"] = after
    path = "/api/v5/market/history-trades" if history else "/api/v5/market/trades"
    return okx(path, p)


def fetch_flow_trades(inst, window_min=FLOW_WINDOW_MIN):
    """最近 window_min 分钟的逐笔（去重、升序）。"""
    cutoff = int(time.time() * 1000) - window_min * 60_000
    rows, seen = [], set()

    def take(batch):
        added = 0
        for r in batch:
            tid = r.get("tradeId")
            if tid is None or tid in seen:
                continue
            seen.add(tid)
            ts = int(r["ts"])
            if ts < cutoff:
                continue
            rows.append({"ts": ts, "px": float(r["px"]),
                         "sz": float(r["sz"]), "side": r["side"]})
            added += 1
        return added

    live = _trades(inst, 500)
    take(live)
    if not live:
        return rows

    after = min((r["tradeId"] for r in live), key=lambda x: int(x))
    for _ in range(8):
        if not rows or min(x["ts"] for x in rows) <= cutoff:
            break
        batch = _trades(inst, 100, after=after, history=True)
        if not batch:
            break
        take(batch)
        nxt = min((r["tradeId"] for r in batch), key=lambda x: int(x))
        if nxt == after:
            break
        after = nxt

    rows.sort(key=lambda x: x["ts"])
    return rows


def fetch_book(inst):
    d = okx("/api/v5/market/books", {"instId": inst, "sz": "50"})
    if not d:
        raise RuntimeError("盘口为空")
    ob = d[0]
    bids = [(float(p), float(s)) for p, s, *_ in ob["bids"]]
    asks = [(float(p), float(s)) for p, s, *_ in ob["asks"]]
    if not bids or not asks:
        raise RuntimeError("盘口单边为空")
    return bids, asks


def _band_usd(levels, ref, pct, above, ct):
    out = 0.0
    for p, s in levels:
        d = (p - ref) / ref * 100.0
        if above and 0 <= d <= pct:
            out += p * s * ct
        elif (not above) and -pct <= d <= 0:
            out += p * s * ct
    return out


def _flow_veto(side, cvd, dpct):
    """V7 方向感知。strict = 用户要求的「背离即否决」；directional = 本地 15m 口径。"""
    sgn = 1 if side == "做多" else -1
    if FLOW_VETO_MODE == "directional":
        if sgn > 0 and dpct > 0 and cvd < 0:
            return "价涨 + CVD 净卖（空头回补的假涨）"
        if sgn < 0 and dpct < 0 and cvd > 0:
            return "价跌 + CVD 净买（有主动买盘托底）"
        return None
    if sgn > 0 and cvd < 0:
        return "CVD 净卖，与做多方向背离"
    if sgn < 0 and cvd > 0:
        return "CVD 净买，与做空方向背离"
    return None


def eval_flow(inst, side, ct):
    """返回完整订单流快照。ok=False 表示未验证（要降级半仓）。"""
    res = {"ok": False, "err": "", "veto": None, "bonus": "", "notes": []}
    try:
        trades = fetch_flow_trades(inst)
        bids, asks = fetch_book(inst)
    except Exception as e:  # noqa: BLE001
        res["err"] = f"{type(e).__name__}: {e}"
        return res
    if len(trades) < FLOW_MIN_TRADES:
        res["err"] = f"逐笔样本过少（{len(trades)} 笔 < {FLOW_MIN_TRADES}）"
        return res

    buy = sum(t["sz"] for t in trades if t["side"] == "buy") * ct
    sell = sum(t["sz"] for t in trades if t["side"] == "sell") * ct
    tot = buy + sell
    if tot <= 0:
        res["err"] = "窗口内成交量为 0"
        return res
    cvd = buy - sell
    p0, p1 = trades[0]["px"], trades[-1]["px"]
    dpct = (p1 - p0) / p0 * 100.0
    cvd_usd = cvd * p1

    wb = ws = 0.0
    wn = 0
    for t in trades:
        usd = t["sz"] * ct * t["px"]
        if usd >= WHALE_USD:
            wn += 1
            if t["side"] == "buy":
                wb += usd
            else:
                ws += usd

    best_bid, best_ask = bids[0][0], asks[0][0]
    mid = (best_bid + best_ask) / 2.0
    spread_bps = (best_ask - best_bid) / mid * 1e4
    bid_usd = _band_usd(bids, mid, BOOK_BAND_PCT, False, ct)
    ask_usd = _band_usd(asks, mid, BOOK_BAND_PCT, True, ct)
    imb = (bid_usd - ask_usd) / (bid_usd + ask_usd) * 100.0 if (bid_usd + ask_usd) else 0.0

    opp = asks if side == "做多" else bids
    opp_band = [(p, s) for p, s in opp if abs(p - mid) / mid * 100 <= WALL_BAND_PCT]
    opp_total = sum(p * s * ct for p, s in opp_band)
    wall = max((p * s * ct for p, s in opp_band), default=0.0)
    wall_ratio = wall / opp_total if opp_total > 0 else 0.0

    sgn = 1 if side == "做多" else -1
    veto = _flow_veto(side, cvd, dpct)
    if veto is None and spread_bps > SPREAD_VETO_BPS:
        veto = f"点差 {spread_bps:.2f} bps 过宽（> {SPREAD_VETO_BPS:.0f} bps）"
    if veto is None and wall_ratio >= WALL_VETO_RATIO:
        veto = f"逆向挂单墙：近 0.1% 单档占该侧 {wall_ratio * 100:.0f}%"
    if veto is None:
        opp_whale = ws if sgn > 0 else wb
        same_whale = wb if sgn > 0 else ws
        if opp_whale >= WHALE_VETO_MIN_USD and \
           opp_whale >= WHALE_VETO_MULT * max(same_whale, 1e-9):
            veto = (f"巨鲸逆向：逆 {opp_whale / 1000:.0f}k U vs "
                    f"顺 {same_whale / 1000:.0f}k U")

    bonus = ""
    if veto is None and FLOW_VETO_MODE == "directional":
        if (sgn > 0 and dpct < 0 and cvd > 0) or (sgn < 0 and dpct > 0 and cvd < 0):
            bonus = "折价/溢价逆向承接，顺流加分"

    res.update({
        "ok": True, "veto": veto, "bonus": bonus,
        "buy": buy, "sell": sell, "cvd": cvd, "cvd_usd": cvd_usd,
        "cvd_ratio": cvd / tot * 100.0, "buy_ratio": buy / tot * 100.0,
        "p0": p0, "p1": p1, "dpct": dpct,
        "whale_buy": wb, "whale_sell": ws, "whale_n": wn,
        "whale_net": wb - ws,
        "mid": mid, "spread_bps": spread_bps,
        "bid_usd": bid_usd, "ask_usd": ask_usd, "imb": imb,
        "wall_ratio": wall_ratio, "n_trades": len(trades),
        "span_min": (trades[-1]["ts"] - trades[0]["ts"]) / 60000.0,
    })
    return res


# ================================================================ 仓位
def size_position(entry, risk_px, budget, ct, lot):
    """按「打止损 = 亏 budget」反推张数；含双边 taker 手续费。"""
    per = ct * risk_px + ct * entry * FEE_RT
    if per <= 0:
        return 0.0, 0.0
    raw = budget / per
    lots = math.floor(raw / lot + 1e-9) * lot
    cap = math.floor((MAX_NOTIONAL_U / (ct * entry)) / lot + 1e-9) * lot
    return round(min(lots, cap), 4), per


# ================================================================ 文案
def _pct(a, b):
    return (b - a) / a * 100.0 if a else 0.0


def build_message(name, sig, plan, flow):
    sgn = 1 if sig["side"] == "做多" else -1
    trend_txt = "上升（HH+HL），只做多" if sgn > 0 else "下降（LH+LL），只做空"
    L = [
        f"<b>☁️ 云端 ICT 哨兵 · {name} 1H {sig['side']}</b>",
        f"信号根 {_t(sig['choch_ts'])}（北京）｜ 4H {trend_txt} ｜ Killzone {sig['kz']}",
        f"扫单 {_t(sig['sweep_ts'])}（极值 {sig['ext']:,.2f}）→ CHoCH 收在 "
        f"{sig['signal_close']:,.2f}，越过 {sig['lvl']:,.2f}",
        "",
        f"<b>① 入场价</b>　{plan['entry']:,.2f}　"
        f"（现价，距信号根收盘 {plan['drift']:+.2f}%）",
        f"<b>② 止损（必须严格执行）</b>　{plan['stop']:,.2f}　"
        f"距入场 {plan['risk_pct']:.2f}%",
        f"<b>③ 参考止盈位</b>　{plan['tp']:,.2f}　"
        f"（{plan['tp_src']}，距入场 {plan['tp_pct']:+.2f}%，约 {plan['rr']:.2f}R）",
        f"<b>④ 建议仓位</b>　{plan['lots']:.2f} 张 ≈ {plan['notional']:,.0f} U 名义　"
        f"｜ 风控 {plan['budget']:.1f}U ｜ 实际最大亏损 {plan['real_risk']:.2f}U",
        f"　　手续费约 {plan['fee']:.2f}U（占 1R 的 {plan['fee_share'] * 100:.0f}%）",
        "",
    ]

    if flow["ok"]:
        whale_txt = (f"净 {flow['whale_net'] / 1000:+.0f}k U"
                     if flow["whale_n"] else "窗口内无 ≥5 万 U 单笔")
        L += [
            f"<b>⑤ 订单流验证（最近 {FLOW_WINDOW_MIN} 分钟，{flow['n_trades']} 笔）</b>",
            f"· CVD 净额 {flow['cvd']:+.3f} {name}"
            f"（{flow['cvd_usd'] / 1000:+.1f}k U，占量 {flow['cvd_ratio']:+.1f}%，"
            f"买占比 {flow['buy_ratio']:.1f}%）",
            f"· 窗口价格 {flow['p0']:,.2f} → {flow['p1']:,.2f}"
            f"（{flow['dpct']:+.2f}%）",
            f"· 盘口失衡 {flow['imb']:+.1f}%（±{BOOK_BAND_PCT}%）"
            f"｜ 点差 {flow['spread_bps']:.2f} bps"
            f"｜ 逆向单档占该侧 {flow['wall_ratio'] * 100:.0f}%",
            f"· 🐋 巨鲸(≥5万U)：买 {flow['whale_buy'] / 1000:.0f}k / "
            f"卖 {flow['whale_sell'] / 1000:.0f}k U → {whale_txt}",
            "· <b>判定：✅ 方向一致，放行</b>",
        ]
        if flow.get("bonus"):
            L.append(f"· {flow['bonus']}")
    else:
        L += [
            "<b>⑤ 【⚠️订单流未验证】</b>",
            f"· 原因：{flow['err']}",
            f"· 已按半仓降级：风控 {plan['budget']:.1f}U（正常 {RISK_U:.1f}U）",
            "· 请自行看盘口，确认有主动买/卖盘再进",
        ]

    L += [
        "",
        "👨‍💻 云端无 AI 动态计算，请收到信号后自行看一眼盘面，"
        "到达参考止盈位附近手动平仓。",
        "仅为提醒，请自行判断，不构成交易建议。",
    ]
    return "<br>".join(L)


def build_veto_message(name, sig, plan, flow):
    sgn = 1 if sig["side"] == "做多" else -1
    L = [
        f"<b>☁️ 云端 ICT 哨兵 · {name} 1H {sig['side']} · ❌ 已否决</b>",
        f"结构条件成立（4H {'多' if sgn > 0 else '空'} + {sig['kz']} + 扫单 + CHoCH），"
        "但订单流一票否决。",
        f"扫单 {_t(sig['sweep_ts'])} 极值 {sig['ext']:,.2f} ｜ "
        f"CHoCH {_t(sig['choch_ts'])} 越过 {sig['lvl']:,.2f}",
        f"参考：入场 {plan['entry']:,.2f} / 止损 {plan['stop']:,.2f} / "
        f"结构位 {plan['tp']:,.2f}",
        "",
        f"<b>否决原因：{flow['veto']}</b>",
        f"· CVD {flow['cvd']:+.3f} {name}（{flow['cvd_usd'] / 1000:+.1f}k U，"
        f"占量 {flow['cvd_ratio']:+.1f}%，买占比 {flow['buy_ratio']:.1f}%）",
        f"· 窗口价格 {flow['p0']:,.2f} → {flow['p1']:,.2f}（{flow['dpct']:+.2f}%）",
        f"· 盘口失衡 {flow['imb']:+.1f}% ｜ 点差 {flow['spread_bps']:.2f} bps ｜ "
        f"逆向单档占该侧 {flow['wall_ratio'] * 100:.0f}%",
        f"· 🐋 买 {flow['whale_buy'] / 1000:.0f}k / 卖 {flow['whale_sell'] / 1000:.0f}k U",
        "",
        "<b>否决 = 这只票今天不碰，不是等待。</b>",
        "仅为提醒，请自行判断，不构成交易建议。",
    ]
    return "<br>".join(L)


def build_test_message(now):
    L = ["<b>☁️ 云端 ICT 哨兵 · 连通性测试</b>",
         f"北京时间 {now:%Y-%m-%d %H:%M} ｜ Killzone：{killzone(now) or '不在窗口内'}"]
    for inst, cfg in SYMBOLS.items():
        name = cfg["name"]
        try:
            px = fetch_price(inst)
            fl = eval_flow(inst, "做多", cfg["ct"])
            if fl["ok"]:
                L.append(f"· {name} {px:,.2f} ｜ CVD {fl['cvd']:+.3f} "
                         f"（买占比 {fl['buy_ratio']:.1f}%）｜ 失衡 {fl['imb']:+.1f}% "
                         f"｜ 点差 {fl['spread_bps']:.2f}bps")
            else:
                L.append(f"· {name} {px:,.2f} ｜ 订单流取失败：{fl['err']}")
        except Exception as e:  # noqa: BLE001
            L.append(f"· {name} 失败：{type(e).__name__}: {e}")
    L += ["", "收到这条说明 OKX 直连、逐笔、盘口、PushPlus 全通。"]
    return "<br>".join(L)


# ================================================================ 主流程
def handle(inst, cfg, now):
    name = cfg["name"]
    h1 = fetch_candles(inst, "1H", 300)
    h4 = fetch_candles(inst, "4H", 200)
    if len(h1) < SWEEP_LOOKBACK + 10 or len(h4) < 2 * PIVOT_K + 10:
        print(f"[SKIP] {name} K 线不足（1H {len(h1)} 根 / 4H {len(h4)} 根）")
        return 0

    bar = h1[-1]
    age = (now - bjt(bar["close_ts"])).total_seconds() / 60.0
    if not FORCE and not (0 <= age <= FRESH_MAX_MIN):
        print(f"[SKIP] {name} 最新收盘根 {_t(bar['close_ts'])} 已 {age:.0f} 分钟，"
              f"超出 {FRESH_MAX_MIN} 分钟时效窗口（防重复推送）")
        return 0

    trace = []
    sig = scan_signal(h1, h4, trace)
    if not sig:
        print(f"[WAIT] {name} {_t(bar['close_ts'])} 收盘无信号"
              + (f"（{'；'.join(trace)}）" if trace else ""))
        return 0

    ct, lot = fetch_spec(inst, cfg["ct"], cfg["lot"])
    price = fetch_price(inst)
    sgn = 1 if sig["side"] == "做多" else -1
    stop = sig["ext"] - sgn * price * STOP_BUFFER_PCT
    risk_px = (price - stop) * sgn
    if risk_px <= 0:
        print(f"[SKIP] {name} 现价 {price:,.2f} 已越过止损 {stop:,.2f}，信号作废")
        return 0

    flow = eval_flow(inst, sig["side"], ct)
    budget = RISK_U if flow["ok"] else RISK_U_UNVERIFIED

    tp, tp_src = structural_target(h1, h4, price, sgn, risk_px)
    if tp is None:
        tp = price + sgn * risk_px * FALLBACK_R
        tp_src = f"无结构位，按 {FALLBACK_R:.0f}R 兜底（仅供参考）"

    lots, _per = size_position(price, risk_px, budget, ct, lot)
    if lots <= 0:
        print(f"[SKIP] {name} 止损 {risk_px:,.2f} 太宽/名义超上限，算不出最小仓位")
        return 0

    fee = lots * ct * price * FEE_RT
    plan = {
        "entry": price,
        "stop": stop,
        "tp": tp,
        "tp_src": tp_src,
        "risk_px": risk_px,
        "risk_pct": risk_px / price * 100.0,
        "tp_pct": (tp - price) / price * 100.0 * sgn,
        "drift": _pct(sig["signal_close"], price),
        "rr": (tp - price) * sgn / risk_px,
        "lots": lots,
        "notional": lots * ct * price,
        "budget": budget,
        "fee": fee,
        "fee_share": (price * FEE_RT) / risk_px,
        "real_risk": lots * ct * risk_px + fee,
    }

    coin = name
    print(f"[SIGNAL] {name} 1H {sig['side']} ｜ 入场 {price:,.2f} 止损 {stop:,.2f} "
          f"目标 {tp:,.2f}（{tp_src}）｜ {lots:.2f} 张 ｜ 风险 {plan['real_risk']:.2f}U")

    if not flow["ok"]:
        print(f"[FLOW] 未验证：{flow['err']} → 半仓 {budget:.1f}U")
        push_wechat(f"☁️【云端ICT哨兵】{name} 1H {sig['side']} · ⚠️订单流未验证",
                    build_message(name, sig, plan, flow))
        return 1

    print(f"[FLOW] CVD {flow['cvd']:+.3f} {coin} / 占量 {flow['cvd_ratio']:+.1f}% / "
          f"买占比 {flow['buy_ratio']:.1f}% ｜ 价 {flow['dpct']:+.2f}% ｜ "
          f"失衡 {flow['imb']:+.1f}% ｜ 点差 {flow['spread_bps']:.2f}bps ｜ "
          f"鲸 买{flow['whale_buy'] / 1000:.0f}k/卖{flow['whale_sell'] / 1000:.0f}k ｜ "
          f"判定 {'否决：' + flow['veto'] if flow['veto'] else '放行'}")

    if flow["veto"]:
        if PUSH_VETO:
            push_wechat(f"☁️【云端ICT哨兵】{name} 1H {sig['side']} · ❌已否决",
                        build_veto_message(name, sig, plan, flow))
        return 0

    push_wechat(f"☁️【云端ICT哨兵】{name} 1H {sig['side']}",
                build_message(name, sig, plan, flow))
    return 1


def main():
    now = datetime.datetime.now(BJT)
    print(f"===== 云端 ICT 哨兵 {now:%Y-%m-%d %H:%M:%S} (北京) "
          f"｜ Killzone {killzone(now) or '非活跃'} "
          f"｜ 模式 {FLOW_VETO_MODE} ｜ 风控 {RISK_U}U =====")

    if TEST:
        push_wechat("☁️【云端ICT哨兵】连通性测试", build_test_message(now))
        return 0

    sent = 0
    for inst, cfg in SYMBOLS.items():
        try:
            sent += handle(inst, cfg, now)
        except Exception as e:  # noqa: BLE001
            print(f"[ERR] {cfg['name']}：{type(e).__name__}: {e}")
    print(f"[DONE] 推送 {sent} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
