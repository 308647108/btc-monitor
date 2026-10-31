#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cloud_monitor.py — BTC/USDT 永续 · 1H 共振盯盘（GitHub Actions 单次执行版）

设计原则
--------
1. 只跑一次：抓最新一根「已收盘」的 1H K 线 → 判断 → 推送 → 退出。没有 while 循环。
2. 交易时段：北京时间 周一至周五 06:00–24:00。
   非交易时段直接静默退出（exit 0），不推送、不报错。
3. 去重：状态文件 .monitor_state.json 记录已报警的 K 线时间戳，
   同一根 1H 只报一次；外加「收盘超过 40 分钟就不再报」的兜底。
4. 推送：PushPlus，token 从环境变量 PUSHPLUS_TOKEN 读取，标题带 [1H级别]。
5. 风控：单笔最大亏损 5U，推送里明确给出「5U 容错空间」和对应止损价。

环境变量
--------
    PUSHPLUS_TOKEN   必填，PushPlus 的 token
    FORCE=1          可选，测试用：跳过时段/时效/去重检查，强制走一遍

退出码
------
    0 = 正常（包含「非交易时段」「无信号」「已报过」等静默情况）
    1 = 配置或网络错误
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

try:
    import requests
except ImportError:
    print("缺少依赖 requests，请先运行：pip install requests")
    sys.exit(1)


# ============================== 基础配置 ==============================
BJT = timezone(timedelta(hours=8))          # 北京时间 UTC+8

INST_ID = "BTC-USDT-SWAP"
CANDLE_URL = "https://www.okx.com/api/v5/market/candles"
OI_URL = "https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume"
OI_CCY = "BTC"
BAR = "1H"
BAR_MS = 60 * 60 * 1000
FETCH_LIMIT = 150

# ---------------------------- 交易时段（北京时间） ----------------------------
SESSION_START_HOUR = 6          # 含 06:00
SESSION_END_HOUR = 24           # 不含 24:00（即到 23:59:59）
SESSION_WEEKDAYS = {0, 1, 2, 3, 4}   # 0=周一 … 4=周五
STALE_MINUTES = 40              # 收盘超过这么多分钟就不再报警（防 GitHub 定时器严重延迟）

# ---------------------------- 风控 ----------------------------
MAX_LOSS_USDT = 5.0             # 单笔最大亏损上限（原来 3U，已放宽到 5U）
BTC_PER_CONTRACT = 0.01         # OKX BTC-USDT-SWAP：1 张 = 0.01 BTC（最小下单单位）
FEE_RATE_RT = 0.001             # 手续费 0.05% × 2（开+平）

# ---------------------------- 结构参数 ----------------------------
PIVOT_K = 2                     # 摆动点：左右各 2 根确认
PIVOT_LOOKBACK = 100            # 在最近 100 根里找摆动点
PREMIUM_LOW = 0.618             # 溢价区下沿（回撤比例）
PREMIUM_HIGH = 0.786            # 溢价区上沿
UPTHRUST_RATIO = 1.5            # 长上影：上影 >= 实体 1.5 倍
MIN_WICK_PCT = 0.0005           # 上影至少占价格 0.05%

# ---------------------------- OI 参数 ----------------------------
OI_FETCH_LIMIT = 300            # 拉 300 根 5m OI（25 小时），聚合成 1H
OI_LOOKBACK = 20
OI_DROP_PCT = 1.0
OI_QUIET_DROP_PCT = 0.6
OI_Z_THRESHOLD = -2.5
OI_RISE_MIN_PCT = 0.05          # 判定「OI 上升」的最小环比增幅

# ---------------------------- 手工钉死关键位 ----------------------------
# 保持 None = 每次运行都从 1H 结构自动计算（推荐，价格跑了也不会失效）
# 想固定死就填数字，例如 MANUAL_CHOCH = 77696.0
MANUAL_CHOCH = None
MANUAL_BREAKDOWN = None
MANUAL_PREMIUM = None           # 填 (下沿, 上沿)，例如 (77348.0, 77388.0)

# ---------------------------- 其他 ----------------------------
PUSH_URL = "https://www.pushplus.plus/send"
PUSH_TIMEOUT = 15
STATE_FILE = ".monitor_state.json"
REQUEST_TIMEOUT = 15
RETRIES = 3


# ============================== 工具 ==============================
def log(msg: str) -> None:
    print(f"[{datetime.now(BJT):%Y-%m-%d %H:%M:%S} 北京] {msg}", flush=True)


def _get(url: str, params: dict):
    last_err = None
    headers = {"User-Agent": "btc-cloud-monitor/1.0"}
    for attempt in range(1, RETRIES + 1):
        try:
            r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT, headers=headers)
            r.raise_for_status()
            payload = r.json()
            if payload.get("code") != "0":
                raise RuntimeError(f"OKX 返回错误：{payload}")
            return payload
        except Exception as e:                       # noqa: BLE001
            last_err = e
            if attempt < RETRIES:
                time.sleep(2)
    raise RuntimeError(f"连续 {RETRIES} 次获取失败：{last_err}") from last_err


# ============================== 数据 ==============================
def fetch_candles():
    """返回按时间升序、只含已收盘（confirm=1）的 1H K 线。"""
    payload = _get(CANDLE_URL, {"instId": INST_ID, "bar": BAR, "limit": str(FETCH_LIMIT)})
    rows = []
    for c in payload.get("data", []):
        if len(c) < 9:
            continue
        rows.append({
            "ts": int(c[0]),
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
            "vol": float(c[5]),
            "confirm": c[8],
        })
    rows.sort(key=lambda x: x["ts"])
    return [x for x in rows if x["confirm"] == "1"]


def fetch_oi_1h():
    """拉 5m 的 OI（USD 口径）聚合成 1H，返回 {1H 桶起始 ms: oi_usd}；失败返回 None。"""
    try:
        payload = _get(OI_URL, {"ccy": OI_CCY, "period": "5m", "limit": str(OI_FETCH_LIMIT)})
    except Exception as e:                           # noqa: BLE001
        log(f"OI 获取失败（不含 OI 校验继续）：{e}")
        return None

    raw = []
    for row in payload.get("data", []):
        if len(row) < 2:
            continue
        try:
            raw.append((int(row[0]), float(row[1])))
        except (TypeError, ValueError):
            continue
    if not raw:
        return None

    raw.sort(key=lambda x: x[0])
    buckets = {}
    for ts, oi in raw:
        buckets[ts - (ts % BAR_MS)] = oi
    return buckets


def oi_metrics(oi_map, bar_ts):
    """信号 K 线所属 1H 的 OI 变化；数据不足返回 None。"""
    if not oi_map:
        return None

    now = oi_map.get(bar_ts)
    prev = oi_map.get(bar_ts - BAR_MS)
    if now is None:
        for d in (5 * 60 * 1000, -5 * 60 * 1000, 10 * 60 * 1000):
            if bar_ts + d in oi_map:
                now = oi_map[bar_ts + d]
                break
    if prev is None:
        for d in (5 * 60 * 1000, -5 * 60 * 1000, 10 * 60 * 1000):
            if bar_ts - BAR_MS + d in oi_map:
                prev = oi_map[bar_ts - BAR_MS + d]
                break
    if not now or not prev:
        return None

    chg = (now - prev) / prev * 100.0

    prior = []
    for i in range(1, OI_LOOKBACK + 1):
        t = bar_ts - i * BAR_MS
        a, b = oi_map.get(t), oi_map.get(t - BAR_MS)
        if a and b:
            prior.append((a - b) / b * 100.0)

    z = None
    if len(prior) >= 5:
        mean = sum(prior) / len(prior)
        sd = (sum((x - mean) ** 2 for x in prior) / len(prior)) ** 0.5
        if sd > 1e-9:
            z = (chg - mean) / sd

    is_collapse = (
        chg <= -OI_DROP_PCT
        or (z is not None and z <= OI_Z_THRESHOLD and chg <= -OI_QUIET_DROP_PCT)
    )
    return {"now": now, "prev": prev, "chg_pct": chg, "z": z, "is_collapse": is_collapse}


# ============================== 结构 ==============================
def structure(candles):
    """
    找 1H 摆动点，返回按时间排序、HH/LL 交替的 [(index, 'H'/'L', price), ...]。
    """
    seg = candles[-PIVOT_LOOKBACK:]
    off = len(candles) - len(seg)
    k = PIVOT_K

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
            if (x[1] == "H" and x[2] > clean[-1][2]) or (x[1] == "L" and x[2] < clean[-1][2]):
                clean[-1] = x
        else:
            clean.append(x)
    return clean


def build_levels(candles):
    """从 1H 结构算出 CHoCH 位 / 破位位 / 溢价区 / 目标位。"""
    piv = structure(candles)
    if len(piv) < 3:
        return None

    highs = [p for p in piv if p[1] == "H"]
    lows = [p for p in piv if p[1] == "L"]
    if not highs or not lows:
        return None

    last_high = highs[-1][2]
    last_low = lows[-1][2]

    # 溢价区：最近两段摆动构成的腿，取 0.618–0.786 回撤
    p1, p2 = piv[-2], piv[-1]
    leg_hi = max(p1[2], p2[2])
    leg_lo = min(p1[2], p2[2])
    rng = leg_hi - leg_lo
    zone_lo = leg_lo + rng * PREMIUM_LOW
    zone_hi = leg_lo + rng * PREMIUM_HIGH

    return {
        "choch": MANUAL_CHOCH if MANUAL_CHOCH is not None else last_high,
        "breakdown": MANUAL_BREAKDOWN if MANUAL_BREAKDOWN is not None else last_low,
        "zone": MANUAL_PREMIUM if MANUAL_PREMIUM is not None else (zone_lo, zone_hi),
        "leg_hi": leg_hi,
        "leg_lo": leg_lo,
        "pivots": piv,
        "last_high_at": candles[highs[-1][0]]["ts"],
        "last_low_at": candles[lows[-1][0]]["ts"],
    }


def nearest_below(piv, price):
    cand = [p[2] for p in piv if p[1] == "L" and p[2] < price]
    return max(cand) if cand else None


def nearest_above(piv, price):
    cand = [p[2] for p in piv if p[1] == "H" and p[2] > price]
    return min(cand) if cand else None


# ============================== 风控计算 ==============================
def risk_plan(price):
    """
    按 1 张（= 0.01 BTC）反推 5U 容错空间。

        亏损 = 0.01 × 止损距离 + 0.01 × 价格 × 0.001 = 5.0

    → 止损距离 = (5.0 - 0.01 × 价格 × 0.001) / 0.01
    """
    fee = BTC_PER_CONTRACT * price * FEE_RATE_RT
    stop_dist = (MAX_LOSS_USDT - fee) / BTC_PER_CONTRACT
    return {
        "price": price,
        "fee": fee,
        "stop_dist": stop_dist,
        "stop_pct": stop_dist / price * 100.0,
        "stop_short": price + stop_dist,
        "stop_long": price - stop_dist,
        "loss_move": BTC_PER_CONTRACT * stop_dist,
    }


# ============================== 信号判定 ==============================
def evaluate(candles, levels, oi):
    """返回命中信号 dict，或 None。优先级：破位 > CHoCH > 溢价区。"""
    sig_bar = candles[-1]
    prev = candles[-2]
    o, h, l, c = sig_bar["open"], sig_bar["high"], sig_bar["low"], sig_bar["close"]
    pc = prev["close"]
    body = abs(c - o)
    upper = h - max(o, c)

    base = {
        "close": c, "open": o, "high": h, "low": l,
        "prev_close": pc, "body": body, "upper": upper,
        "lower": min(o, c) - l, "vol": sig_bar["vol"],
        "bar_ts": sig_bar["ts"],
        "bar_start": datetime.fromtimestamp(sig_bar["ts"] / 1000, BJT),
    }

    # ---- 规则3：收盘跌破结构低点 ----
    if c < levels["breakdown"] and pc >= levels["breakdown"]:
        return dict(base, key="breakdown", rule="规则3 · 破位（结构失效）",
                    side="做空", level=levels["breakdown"])

    # ---- 规则2：收盘站上结构高点（CHoCH）----
    if c > levels["choch"] and pc <= levels["choch"]:
        return dict(base, key="choch", rule="规则2 · CHoCH 结构反转",
                    side="做多", level=levels["choch"])

    # ---- 规则1：进入溢价区 + 阴线/长上影 + OI 上升 ----
    z_lo, z_hi = levels["zone"]
    if h >= z_lo and l <= z_hi:
        bearish = c < o
        long_upper = upper >= UPTHRUST_RATIO * max(body, 1e-9) and upper >= c * MIN_WICK_PCT
        oi_up = bool(oi) and oi["chg_pct"] >= OI_RISE_MIN_PCT
        if (bearish or long_upper) and oi_up:
            return dict(base, key="short_zone", rule="规则1 · 溢价区 Upthrust",
                        side="做空", level=(z_lo + z_hi) / 2, zone=(z_lo, z_hi))
        reasons = []
        if not (bearish or long_upper):
            reasons.append("K线不是阴线/长上影")
        if not oi_up:
            reasons.append("OI 未上升" if oi else "OI 数据缺失")
        log(f"  · 触及溢价区 {z_lo:,.0f}–{z_hi:,.0f}，但 {'、'.join(reasons)}，不报")
    return None


# ============================== 去重 ==============================
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(bar_ts, bar_start, key):
    data = {
        "last_alert_bar_ts": bar_ts,
        "last_alert_bar_start_bjt": bar_start.strftime("%Y-%m-%d %H:%M"),
        "last_alert_rule": key,
        "updated_at_bjt": datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        log(f"已写入去重状态：{STATE_FILE}")
    except OSError as e:
        log(f"写入状态失败（下次可能重复报警）：{e}")


# ============================== 推送 ==============================
def build_message(sig, levels, oi, risk):
    side = sig["side"]
    target = (nearest_below(levels["pivots"], sig["close"]) if side == "做空"
              else nearest_above(levels["pivots"], sig["close"]))
    target_txt = f"{target:,.1f}" if target else "无近端结构目标，按 5U 止损管理"

    if sig["key"] == "breakdown":
        invalid = f"1H 收盘重新站回 {levels['breakdown']:,.1f} 上方"
    elif sig["key"] == "choch":
        invalid = f"1H 收盘跌回 {levels['choch']:,.1f} 下方"
    else:
        invalid = f"1H 收盘站上 {levels['choch']:,.1f}（结构转多）"

    rows = [
        f"<b>{sig['rule']}</b>",
        "",
        f"<b>方向</b>：{side}",
        f"<b>关键位</b>：{sig['level']:,.1f}",
        f"<b>失效位</b>：{invalid}",
        f"<b>目标位</b>：{target_txt}",
        "",
        f"<b>【5U 容错空间】</b>",
        f"止损距离：<b>±{risk['stop_dist']:,.1f} U</b>（{risk['stop_pct']:.2f}%）",
        f"做空止损价：{risk['stop_short']:,.1f}",
        f"做多止损价：{risk['stop_long']:,.1f}",
        f"核算：0.01×{risk['stop_dist']:,.1f} + 0.01×{risk['price']:,.0f}×0.001 "
        f"= {risk['loss_move']:.2f} + {risk['fee']:.2f} = {MAX_LOSS_USDT:.2f} U",
        "",
        f"1H 收盘：{sig['bar_start']:%Y-%m-%d %H:%M} 北京",
        f"O {sig['open']:,.1f} / H {sig['high']:,.1f} / L {sig['low']:,.1f} / C {sig['close']:,.1f}",
        f"实体 {sig['body']:,.1f} ｜ 上影 {sig['upper']:,.1f} ｜ 下影 {sig['lower']:,.1f}",
        f"成交量 {sig['vol']:,.0f}",
    ]

    if oi is None:
        rows.append("OI：数据缺失，请手动核对")
    else:
        z_txt = f"{oi['z']:+.2f}σ" if oi["z"] is not None else "σ 样本不足"
        arrow = "↑" if oi["chg_pct"] >= 0 else "↓"
        rows.append(f"OI：{oi['prev'] / 1e8:.2f} 亿 → {oi['now'] / 1e8:.2f} 亿 USD"
                    f"（{arrow}{abs(oi['chg_pct']):.2f}%，{z_txt}）")
        if sig["key"] == "choch" and (oi["is_collapse"] or oi["chg_pct"] < 0):
            rows.append("⚠ 警惕骗炮，OI未跟上 —— 更像空头回补，不是新多进场")
        if sig["key"] == "breakdown" and oi["is_collapse"]:
            rows.append("⚠ OI 剧烈下降，可能是多头止损砸出来的，急跌后易反抽，别追空")

    rows += ["", "单笔止损上限 5U ｜ 1 张 = 0.01 BTC",
             "仅为提醒，请自行判断，不构成交易建议。"]

    title = f"[1H级别] {side} · {sig['rule']}"
    return title, "<br>".join(rows)


def build_test_message(candle, levels, oi, risk):
    """FORCE=1 且没有信号时发这条：证明管道通了，顺带把当前盯的位打出来。"""
    start = datetime.fromtimestamp(candle["ts"] / 1000, BJT)
    z_lo, z_hi = levels["zone"]
    rows = [
        "<b>测试运行（FORCE=1）</b>",
        "推送管道已连通。本轮没有命中共振信号，属正常情况。",
        "",
        "<b>当前盯的 1H 关键位</b>",
        f"CHoCH 位（收盘站上 → 做多）：{levels['choch']:,.1f}",
        f"破位位（收盘跌破 → 做空）：{levels['breakdown']:,.1f}",
        f"溢价区（做空伏击带）：{z_lo:,.1f} – {z_hi:,.1f}",
        "",
        f"1H 收盘：{start:%Y-%m-%d %H:%M} 北京",
        f"O {candle['open']:,.1f} / H {candle['high']:,.1f} / "
        f"L {candle['low']:,.1f} / C {candle['close']:,.1f}",
        "",
        "<b>【5U 容错空间】</b>",
        f"止损距离：<b>±{risk['stop_dist']:,.1f} U</b>（{risk['stop_pct']:.2f}%）",
        f"做空止损价：{risk['stop_short']:,.1f}",
        f"做多止损价：{risk['stop_long']:,.1f}",
        f"核算：0.01×{risk['stop_dist']:,.1f} + 0.01×{risk['price']:,.0f}×0.001 "
        f"= {MAX_LOSS_USDT:.2f} U",
    ]
    if oi is not None:
        rows.append(f"OI 环比：{oi['chg_pct']:+.2f}%")
    rows += ["", "单笔止损上限 5U ｜ 1 张 = 0.01 BTC",
             "仅为提醒，请自行判断，不构成交易建议。"]
    return "[1H级别] 测试运行 · 当前无信号", "<br>".join(rows)


def push_wechat(title, content):
    token = os.environ.get("PUSHPLUS_TOKEN", "").strip()
    if not token:
        log("缺少环境变量 PUSHPLUS_TOKEN，无法推送")
        return False

    payload = {"token": token, "title": title, "content": content, "template": "html"}
    headers = {"Content-Type": "application/json;charset=utf-8"}
    try:
        r = requests.post(PUSH_URL, json=payload, timeout=PUSH_TIMEOUT, headers=headers)
        r.raise_for_status()
        data = r.json()
    except Exception as e:                           # noqa: BLE001
        log(f"推送异常：{e}")
        return False

    if data.get("code") == 200:
        log(f"推送成功（流水号 {data.get('data', '-')}）")
        return True
    log(f"推送失败：code={data.get('code')} msg={data.get('msg')}")
    return False


# ============================== 主流程 ==============================
def main() -> int:
    force = os.environ.get("FORCE", "").strip() == "1"

    try:
        candles = fetch_candles()
    except Exception as e:                           # noqa: BLE001
        log(f"K 线获取失败：{e}")
        return 1

    if len(candles) < PIVOT_LOOKBACK // 2:
        log(f"K 线数量不足（{len(candles)}），退出")
        return 1

    sig_bar = candles[-1]
    bar_start = datetime.fromtimestamp(sig_bar["ts"] / 1000, BJT)
    bar_close = bar_start + timedelta(hours=1)
    age_min = (datetime.now(timezone.utc) - bar_close.astimezone(timezone.utc)).total_seconds() / 60

    log(f"最新已收盘 1H：{bar_start:%Y-%m-%d %H:%M} 北京｜收 {sig_bar['close']:,.1f}｜"
        f"距今 {age_min:.0f} 分钟")

    # ---------- 1. 交易时段 ----------
    if not force:
        if bar_start.weekday() not in SESSION_WEEKDAYS:
            log("非交易日（周末），静默退出")
            return 0
        if not (SESSION_START_HOUR <= bar_start.hour < SESSION_END_HOUR):
            log(f"非交易时段（北京 {bar_start:%H:%M}，监控范围 "
                f"{SESSION_START_HOUR:02d}:00–{SESSION_END_HOUR:02d}:00），静默退出")
            return 0

    # ---------- 2. 时效 ----------
    if not force and age_min > STALE_MINUTES:
        log(f"该 K 线已收盘 {age_min:.0f} 分钟（> {STALE_MINUTES}），判定为定时器延迟，静默退出")
        return 0

    # ---------- 3. 结构 & 信号 ----------
    levels = build_levels(candles)
    if levels is None:
        log("结构点不足，无法计算关键位，退出")
        return 0

    z_lo, z_hi = levels["zone"]
    log(f"关键位：CHoCH {levels['choch']:,.1f}｜破位 {levels['breakdown']:,.1f}｜"
        f"溢价区 {z_lo:,.1f}–{z_hi:,.1f}")

    oi = oi_metrics(fetch_oi_1h(), sig_bar["ts"])
    if oi is not None:
        log(f"OI 环比 {oi['chg_pct']:+.3f}%"
            + (f"（z={oi['z']:+.2f}σ）" if oi["z"] is not None else ""))

    sig = evaluate(candles, levels, oi)
    if sig is None:
        log(f"无共振信号（收盘 {sig_bar['close']:,.1f}）")
        if force:
            # 手动测试时也要有一条消息，方便确认整条链路是通的
            risk = risk_plan(sig_bar["close"])
            t, c = build_test_message(sig_bar, levels, oi, risk)
            return 0 if push_wechat(t, c) else 1
        return 0

    # ---------- 4. 去重 ----------
    state = load_state()
    if not force and state.get("last_alert_bar_ts") == sig_bar["ts"]:
        log(f"这根 1H（{bar_start:%m-%d %H:%M}）已经报过，跳过")
        return 0

    # ---------- 5. 风控 & 推送 ----------
    risk = risk_plan(sig["close"])
    log(f"命中 {sig['rule']}｜方向 {sig['side']}｜关键位 {sig['level']:,.1f}")
    log(f"5U 容错空间：±{risk['stop_dist']:,.1f} U（{risk['stop_pct']:.2f}%）"
        f"｜空单止损 {risk['stop_short']:,.1f}｜多单止损 {risk['stop_long']:,.1f}")

    title, content = build_message(sig, levels, oi, risk)
    if not push_wechat(title, content):
        return 1

    # 测试模式不写去重状态，否则会把正式那次的报警吞掉
    if not force:
        save_state(sig_bar["ts"], bar_start, sig["key"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
