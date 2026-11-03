# -*- coding: utf-8 -*-
"""A-2 富文本卡片渲染器 —— 本地 monitor_15m 与云端 cloud_ict_e 共用同一套排版。

设计约束（微信 / PushPlus 环境）：
  · 只用内联 style，不用 <style> 标签、class、id
  · 不用 flex / grid / box-shadow / border-radius 做关键布局（公众号会丢掉）
  · 颜色只用于文字与单元格底色，丢了也只是变朴素，不会变成乱码
  · 所有正文默认 HTML 转义，绝不把外部文本（新闻标题等）当标签插入

用法：
    c = Card("做多", "BTC · 1H", "Killzone 美盘 · 11-03 02:35 北京")
    c.kv("① 入场价", "77,059.70", note="现价")
    c.flow(True, "订单流验证 · 方向一致，放行", ["CVD +2.14 BTC"])
    c.notes(["4H 上升（HH+HL）"])
    title, body = c.title("☁️ 云端 ICT 哨兵 · BTC 1H"), c.html()
"""
from html import escape

# 方向 → (主色, 符号)
PALETTE = {
    "做多": ("#16C784", "▲"),
    "做空": ("#EA3943", "▼"),
    "否决": ("#6B7280", "✕"),
    "测试": ("#2563EB", "●"),
    "预警": ("#F59E0B", "!"),
}

TONE = {
    "text":   "#111827",
    "loss":   "#DC2626",
    "profit": "#059669",
    "muted":  "#6B7280",
}

FONT = ("-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB',"
        "'Microsoft YaHei',sans-serif")


def _cell_border(last=False):
    return "" if last else "border-bottom:1px solid #EDF1F6;"


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
        """一行「标签（+小注）｜ 右对齐数值」。"""
        left = (f'<div style="color:#8B95A7;font-size:14px;">{escape(str(label))}</div>')
        if note:
            left += (f'<div style="color:#AEB7C2;font-size:12px;margin-top:3px;">'
                     f'{escape(str(note))}</div>')
        self._rows.append(
            f'<tr>'
            f'<td style="padding:10px 14px;vertical-align:middle;{_cell_border()}">{left}</td>'
            f'<td style="padding:10px 14px;vertical-align:middle;text-align:right;'
            f'font-weight:bold;font-size:17px;{_cell_border()}'
            f'color:{TONE.get(tone, TONE["text"])};">{escape(str(value))}</td>'
            f'</tr>')
        return self

    def flow(self, ok, head, lines, warn=False):
        """订单流整块：淡底色 + 左侧色条 + 标题 + 若干小行。"""
        if ok:
            bg, bar, fg = "#F0FDF6", "#16C784", "#0E9F6E"
        elif warn:
            bg, bar, fg = "#FFF7ED", "#F59E0B", "#B45309"
        else:
            bg, bar, fg = "#FEF2F2", "#EF4444", "#B91C1C"
        body = "".join(
            f'<div style="color:#3F4A5A;font-size:13px;margin-top:4px;">{escape(str(x))}</div>'
            for x in lines)
        self._rows.append(
            f'<tr><td colspan="2" style="background:{bg};border-left:4px solid {bar};'
            f'padding:11px 14px;">'
            f'<div style="color:{fg};font-weight:bold;font-size:14px;">{escape(str(head))}</div>'
            f'{body}</td></tr>')
        return self

    def banner(self, text, tone="warn"):
        """整块警示条（消息面异常 / 未验证），放在表格最上方。"""
        bg, fg = {"warn": ("#FFF7ED", "#B45309"),
                  "bad": ("#FEF2F2", "#B91C1C"),
                  "info": ("#EFF6FF", "#1D4ED8")}.get(tone, ("#FFF7ED", "#B45309"))
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
            bg, bar, fg = {"bad": ("#FEF2F2", "#EF4444", "#B91C1C"),
                           "warn": ("#FFF7ED", "#F59E0B", "#B45309")}.get(
                               t.get("tone", "warn"), ("#FFF7ED", "#F59E0B", "#B45309"))
            body = "".join(
                f'<div style="color:#3F4A5A;font-size:13px;margin-top:2px;">'
                f'{escape(str(x))}</div>'
                for x in (t.get("lines") or []))
            mt = "" if i == 0 else "margin-top:6px;"
            blocks.append(
                f'<div style="background:{bg};border-left:5px solid {bar};'
                f'padding:9px 12px;{mt}">'
                f'<div style="color:{fg};font-weight:bold;font-size:15px;">'
                f'{escape(str(t.get("head") or ""))}</div>{body}</div>')
        self._rows.insert(
            0,
            '<tr><td colspan="2" style="padding:10px;">'
            f'{"".join(blocks)}</td></tr>')
        return self

    def section(self, text, sub=None):
        """整行分组标题（多品种合推时给每个品种切一刀）。"""
        left = (f'<div style="font-weight:bold;font-size:15px;color:#111827;">'
                f'{escape(str(text))}</div>')
        if sub:
            left += (f'<div style="color:#8B95A7;font-size:12px;margin-top:3px;">'
                     f'{escape(str(sub))}</div>')
        self._rows.append(
            f'<tr><td colspan="2" style="background:#F7F9FC;border-top:1px solid #E4E9F0;'
            f'padding:9px 14px;">{left}</td></tr>')
        return self

    def raw(self, html_fragment):
        """整行插入一段「已经渲染好的」HTML。

        只给内部可信内容用（例如 AI 报告的 md_to_html 结果）。
        外部文本（新闻标题、接口返回）必须先 escape，绝不能走这里。
        """
        self._rows.append(
            f'<tr><td colspan="2" style="padding:12px 14px;font-size:14px;'
            f'line-height:1.7;color:#3F4A5A;">{html_fragment}</td></tr>')
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
            meta_row = (f'<tr><td colspan="2" style="background:#F3F6FA;color:#5B6675;'
                        f'padding:8px 14px;font-size:13px;">{escape(self.meta)}</td></tr>')

        out = [
            f'<div style="font-family:{FONT};font-size:15px;line-height:1.6;color:#111827;">',
            f'<table width="100%" cellpadding="0" cellspacing="0" '
            f'style="width:100%;border-collapse:collapse;border:1px solid #E4E9F0;">',
            f'<tr><td colspan="2" style="background:{color};color:#ffffff;padding:12px 14px;'
            f'font-size:16px;font-weight:bold;">{escape(head)}</td></tr>',
            meta_row,
        ]
        out += self._rows
        out.append('</table>')

        if self._notes:
            out.append('<div style="margin-top:12px;color:#5B6675;font-size:13px;">')
            for x in self._notes:
                out.append(f'<div style="margin-top:5px;">{escape(x)}</div>')
            out.append('</div>')

        if self._footer:
            out.append('<div style="margin-top:14px;padding-top:10px;'
                       'border-top:1px solid #EDF1F6;color:#8B95A7;font-size:12px;">')
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
    c.kv("④ 仓位", "0.65 张", note="名义 500 U · 风控 5.0 U")
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
