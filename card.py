# -*- coding: utf-8 -*-
"""A-2 富文本卡片渲染器 —— 本地 monitor_15m 与云端 cloud_ict_e / cloud_paper 共用同一套排版。

设计约束（微信 / PushPlus 环境）：
  · 只用内联 style，不用 <style> 标签、class、id
  · 键值行用 flex 左右分布：宿主就算强行 table-layout:fixed 也不会左右重叠；
    flex 被丢掉时两个 div 会自然上下堆叠，仍然可读，不会压字
  · 卡片自带深色底（#121212）：微信暗色模式下宿主页面是黑的，自带底色才能保证
    亮青 / 亮绿 / 亮红在浅色和深色两种模式下都清楚
  · 颜色只用于文字与单元格底色，丢了也只是变朴素，不会变成乱码
  · 所有正文默认 HTML 转义，绝不把外部文本（新闻标题等）当标签插入

配色规则（暗色模式，色值全部写死，禁止再用 #333 / #666 / #444 这类暗灰）：
  · 方向（做多 / 做空）→ 亮绿 #00FF00 / 亮红 #FF4D4D
  · 数据 / 价格 / 点位（触发位、入场区间、止盈）→ 亮青 #00FFFF
  · 止损位 → 亮黄 #FFD700（同属点位，靠黄色和入场区区分）
  · 辅助说明（分层 / 纯辅助 n 维 / 规则n / 震荡→半仓）→ 浅灰 #B0B0B0
  · 数量（n 张）→ 纯白 #FFFFFF + 粗体

用法：
    c = Card("做多", "BTC · 1H", "Killzone 美盘 · 11-03 02:35 北京")
    c.kv("① 入场价", "77,059.70", note="现价")
    c.kv("④ 仓位", "0.74 张", "qty", note="最大亏损 3.00 U")
    c.flow(True, "订单流验证 · 方向一致，放行", ["CVD +2.14 BTC"])
    c.notes(["4H 上升（HH+HL）"])
    title, body = c.title("☁️ 云端 ICT 哨兵 · BTC 1H"), c.html()
"""
from html import escape

# 方向 → (主色, 符号)
PALETTE = {
    "做多": ("#0E9F6E", "▲"),
    "做空": ("#EA3943", "▼"),
    "否决": ("#6B7280", "✕"),
    "测试": ("#2563EB", "●"),
    "预警": ("#C2410C", "!"),
}

# ------------------------------------------------ 暗色安全色板（强制固定，不随系统主题反转）
BG        = "#121212"   # 卡片底色：写死，浅色 / 深色模式都是这块黑底
BG_META   = "#1C1C1C"   # 表头下的一行元信息
BG_SECT   = "#1E1E1E"   # 分组标题
BORDER    = "#3A3A3A"   # 单元格 / 外框描边
FG        = "#FFFFFF"   # 基础字体：纯白
FG_LABEL  = "#D6D6D6"   # 左侧标签
FG_NOTE   = "#B0B0B0"   # 辅助说明（小注、分层、纯辅助 n 维）
FG_FOOT   = "#B0B0B0"   # 页脚

TONE = {
    "text":   "#00FFFF",   # 数据 / 价格 / 点位（触发位、入场区间、止盈）：亮青
    "gold":   "#FFD700",   # 数据 / 点位（止损）：亮黄
    "long":   "#00FF00",   # 方向：做多（亮绿）
    "short":  "#FF4D4D",   # 方向：做空（亮红）
    "loss":   "#FF4D4D",   # 亏损 / 风险数字：亮红
    "profit": "#00FF00",   # 盈利数字：亮绿
    "muted":  "#B0B0B0",   # 辅助说明：浅灰
    "qty":    "#FFFFFF",   # 数量（张数）：纯白 + 粗体
}

FONT = ("-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB',"
        "'Microsoft YaHei',sans-serif")


def _cell_border(last=False):
    return "" if last else f"border-bottom:1px solid {BORDER};"


class Card:
    """一张卡片 = 表头 + N 行键值 + 订单流块 + 若干备注 + 页脚。"""

    def __init__(self, side, headline, meta="", title_text=None):
        self.side = side if side in PALETTE else "预警"
        self.headline = headline
        self.meta = meta
        self._title_text = title_text
        self._rows = []
        self._notes = []
        self._footer = []
        self._extra_blocks = []

    # -------------------------------------------------- 构件
    def kv(self, label, value, tone="text", note=None):
        """一行「标签（+小注）｜ 右对齐数值」。

        整行塞进一个 colspan=2 的单元格，内部用 flex 左右分布：
        窄屏 / 宿主强制 table-layout:fixed 时也不会出现左右文字重叠。
        """
        left = (f'<div style="color:{FG_LABEL};font-size:14px;">{escape(str(label))}</div>')
        if note:
            left += (f'<div style="color:{FG_NOTE};font-size:12px;margin-top:3px;">'
                     f'{escape(str(note))}</div>')
        if tone == "side":           # 方向行：做多亮绿 / 做空亮红，自动按卡片方向取色
            tone = {"做多": "long", "做空": "short"}.get(self.side, "muted")
        if tone == "muted":          # 分层 / 纯辅助 n 维：字多，允许换行、字号小一档
            val_style = ("flex:0 1 auto;max-width:60%;text-align:right;"
                         "white-space:normal;word-break:break-word;"
                         "font-weight:bold;font-size:14px;")
        else:                        # 价格 / 区间 / 张数：不换行，避免数字被折断
            val_style = ("flex:0 0 auto;text-align:right;white-space:nowrap;"
                         "font-weight:bold;font-size:17px;")
        self._rows.append(
            f'<tr><td colspan="2" style="padding:0;{_cell_border()}">'
            f'<div style="display:flex;justify-content:space-between;align-items:center;'
            f'padding:10px 14px;">'
            f'<div style="flex:1 1 auto;min-width:0;padding-right:12px;">{left}</div>'
            f'<div style="{val_style}color:{TONE.get(tone, TONE["text"])};">'
            f'{escape(str(value))}</div>'
            f'</div></td></tr>')
        return self

    def flow(self, ok, head, lines, warn=False):
        """订单流整块：暗色底 + 左侧色条 + 标题 + 若干小行。"""
        if ok:
            bg, bar, fg, body = "#0E2419", "#00FF00", "#5BF0A6", "#E8F0EB"
        elif warn:
            bg, bar, fg, body = "#2A210E", "#FFD700", "#FFD75E", "#F0E9DA"
        else:
            bg, bar, fg, body = "#2A1417", "#FF4D4D", "#FF9A9A", "#F2DEDE"
        text = "".join(
            f'<div style="color:{body};font-size:13px;margin-top:4px;">{escape(str(x))}</div>'
            for x in lines)
        self._rows.append(
            f'<tr><td colspan="2" style="background:{bg};border-left:4px solid {bar};'
            f'padding:11px 14px;">'
            f'<div style="color:{fg};font-weight:bold;font-size:14px;">{escape(str(head))}</div>'
            f'{text}</td></tr>')
        return self

    def banner(self, text, tone="warn"):
        """整块警示条（消息面异常 / 未验证），放在表格最上方。"""
        bg, fg = {"warn": ("#2A210E", "#FFD700"),
                  "bad": ("#2A1417", "#FF9A9A"),
                  "info": ("#101E33", "#8CC2FF")}.get(tone, ("#2A210E", "#FFD700"))
        self._rows.insert(
            0,
            f'<tr><td colspan="2" style="background:{bg};color:{fg};padding:10px 14px;'
            f'font-size:14px;font-weight:bold;border-bottom:1px solid {bg};">'
            f'{escape(str(text))}</td></tr>')
        return self

    def risk(self, tags):
        """顶部风险标签条（软性否决）：⚠️ 订单流背离 / 🧱 逆势 / ⚡ 追单。

        tags = [{"head": "⚠️【订单流背离警告】", "tone": "bad"/"warn", "lines": [...]}]
        一次插一行、整块压在表格最上方；调用得越晚越靠上（与 banner 同一规则）。
        只排版、不判定：推不推由调用方决定。
        """
        if not tags:
            return self
        blocks = []
        for i, t in enumerate(tags):
            bg, bar, fg, body = {"bad": ("#2A1417", "#FF4D4D", "#FF9A9A", "#F2DEDE"),
                                 "warn": ("#2A210E", "#FFD700", "#FFD75E", "#F0E9DA")}.get(
                                     t.get("tone", "warn"),
                                     ("#2A210E", "#FFD700", "#FFD75E", "#F0E9DA"))
            text = "".join(
                f'<div style="color:{body};font-size:13px;margin-top:2px;">'
                f'{escape(str(x))}</div>'
                for x in (t.get("lines") or []))
            mt = "" if i == 0 else "margin-top:6px;"
            blocks.append(
                f'<div style="background:{bg};border-left:5px solid {bar};'
                f'padding:9px 12px;{mt}">'
                f'<div style="color:{fg};font-weight:bold;font-size:15px;">'
                f'{escape(str(t.get("head") or ""))}</div>{text}</div>')
        self._rows.insert(
            0,
            '<tr><td colspan="2" style="padding:10px;">'
            f'{"".join(blocks)}</td></tr>')
        return self

    def section(self, text, sub=None):
        """整行分组标题（多品种合推时给每个品种切一刀）。"""
        left = (f'<div style="font-weight:bold;font-size:15px;color:{FG};">'
                f'{escape(str(text))}</div>')
        if sub:
            left += (f'<div style="color:{FG_NOTE};font-size:12px;margin-top:3px;">'
                     f'{escape(str(sub))}</div>')
        self._rows.append(
            f'<tr><td colspan="2" style="background:{BG_SECT};border-top:1px solid {BORDER};'
            f'padding:9px 14px;">{left}</td></tr>')
        return self

    def raw(self, html_fragment):
        """整行插入一段「已经渲染好的」HTML。

        只给内部可信内容用（例如 AI 报告的 md_to_html 结果）。
        外部文本（新闻标题、接口返回）必须先 escape，绝不能走这里。
        """
        self._rows.append(
            f'<tr><td colspan="2" style="padding:12px 14px;font-size:14px;'
            f'line-height:1.7;color:#FFFFFF;">{html_fragment}</td></tr>')
        return self

    def notes(self, lines):
        """表格下方的灰色说明行。"""
        for x in lines:
            if x:
                self._notes.append(str(x))
        return self

    def footer(self, lines):
        for x in lines:
            if x:
                self._footer.append(str(x))
        return self

    # -------------------------------------------------- 输出
    def title(self, text=None):
        if text:
            return text
        if self._title_text:
            return self._title_text
        return f"{self.side} {self.headline}"

    def html(self):
        color, mark = PALETTE[self.side]
        head = f"{mark} {self.side} · {self.headline}"
        meta_row = ""
        if self.meta:
            meta_row = (f'<tr><td colspan="2" style="background:{BG_META};color:{FG_LABEL};'
                        f'padding:8px 14px;font-size:13px;">{escape(self.meta)}</td></tr>')

        out = [
            f'<div style="font-family:{FONT};font-size:15px;line-height:1.6;'
            f'color:{FG};background:{BG};padding:2px;">',
            f'<table width="100%" cellpadding="0" cellspacing="0" '
            f'style="width:100%;border-collapse:collapse;border:1px solid {BORDER};'
            f'background:{BG};">',
            f'<tr><td colspan="2" style="background:{color};color:#ffffff;padding:12px 14px;'
            f'font-size:16px;font-weight:bold;">{escape(head)}</td></tr>',
            meta_row,
        ]
        out += self._rows
        out.append('</table>')

        if self._notes:
            out.append(f'<div style="margin-top:12px;color:{FG_NOTE};font-size:13px;">')
            for x in self._notes:
                out.append(f'<div style="margin-top:5px;">{escape(x)}</div>')
            out.append('</div>')

        if self._footer:
            out.append(f'<div style="margin-top:14px;padding-top:10px;'
                       f'border-top:1px solid {BORDER};color:{FG_FOOT};font-size:12px;">')
            for x in self._footer:
                out.append(f'<div style="margin-top:4px;">{escape(x)}</div>')
            out.append('</div>')

        out.append('</div>')
        return "\n".join(x for x in out if x)


# ---------------------------------------------------------------- 演示
if __name__ == "__main__":
    c = Card("做多", "BTC · 1H", "Killzone 美盘 · 11-03 02:35 北京",
             title_text="☁️【云端ICT哨兵】BTC 1H 做多")
    c.kv("① 入场价", "77,059.70", note="现价，距信号根收盘 +0.12%")
    c.kv("② 止损（严格执行）", "76,240.30", "loss", note="距入场 1.06% · 触发即离场，不扛单")
    c.kv("③ 参考止盈", "79,180.00", "profit", note="1H 前高 · 约 3.02R")
    c.kv("④ 仓位", "0.65 张", "qty", note="名义 500 U · 风控 5.0 U")
    c.kv("分层", "🧬 纯辅助-3维（命中 3/9 维）", "muted", note="轨道二｜纯辅助（左侧/指标共振）")
    c.flow(True, "⑤ 订单流验证 · 方向一致，放行", [
        "CVD +2.14 BTC（+16.5k U · 占量 +18.4% · 买占比 58.3%）",
        "盘口失衡 +12.7% ｜ 点差 0.82 bps ｜ 🐋 巨鲸 买 182k / 卖 41k U",
    ])
    c.notes(["4H 上升（HH+HL） · 扫单 76,240 → CHoCH 收在 76,890"])
    c.footer(["👨‍💻 到达参考止盈位附近请手动平仓；止损只可上移，不可下移",
              "仅为提醒，请自行判断，不构成交易建议。"])
    print(c.title())
    print()
    print(c.html())
