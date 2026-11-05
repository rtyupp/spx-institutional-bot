"""رسم شارت الشموع اليابانية لمؤشر SPX مع تحديد مناطق السيولة والوقف والأهداف المؤسسية عبر Pillow."""
from __future__ import annotations

import io
import pandas as pd
from PIL import Image, ImageDraw, ImageFont


def render_spx_chart(df: pd.DataFrame, plan: dict, live_price: float | None = None) -> bytes:
    """رسم الشارت خفيف وسريع وبلا استهلاك للذاكرة (Pillow)."""
    sub = df.tail(60).copy().reset_index(drop=True)
    w, h = 950, 520
    im = Image.new("RGB", (w, h), color=(14, 18, 24))
    draw = ImageDraw.Draw(im)

    font_title = ImageFont.load_default()
    font_main = ImageFont.load_default()

    # الإطار وحدود الرسم
    pad_left, pad_right, pad_top, pad_bottom = 65, 85, 50, 45
    pw = w - pad_left - pad_right
    ph = h - pad_top - pad_bottom

    highs = sub["high"].values
    lows = sub["low"].values
    min_p, max_p = float(lows.min()), float(highs.max())
    span = max(max_p - min_p, 1.0)
    min_p -= span * 0.05
    max_p += span * 0.05
    span = max_p - min_p

    def to_y(p: float) -> float:
        return pad_top + ph * (1.0 - (p - min_p) / span)

    # شبكة الأسعار الأفقية
    for lvl in range(int(min_p // 10 * 10), int(max_p // 10 * 10 + 20), 20):
        y = to_y(lvl)
        if pad_top <= y <= pad_top + ph:
            draw.line([(pad_left, y), (w - pad_right, y)], fill=(30, 36, 46), width=1)
            draw.text((w - pad_right + 8, y - 6), f"{lvl:.1f}", fill=(120, 130, 145), font=font_main)

    # رسم الشموع اليابانية
    n = len(sub)
    col_w = pw / max(n, 1)
    body_w = max(col_w * 0.65, 2)

    for idx, row in sub.iterrows():
        x_center = pad_left + idx * col_w + col_w / 2
        o, c, hi, lo = float(row["open"]), float(row["close"]), float(row["high"]), float(row["low"])
        bullish = c >= o
        col = (46, 204, 113) if bullish else (231, 76, 60)

        # الفتيل
        draw.line([(x_center, to_y(hi)), (x_center, to_y(lo))], fill=col, width=1)
        # الجسم
        y_top = min(to_y(o), to_y(c))
        y_bot = max(to_y(o), to_y(c))
        if y_bot - y_top < 1:
            y_bot = y_top + 1
        draw.rectangle([x_center - body_w / 2, y_top, x_center + body_w / 2, y_bot], fill=col)

    # رسم مستويات الصفقة المؤسسية (Entry, SL, TP1, TP2) إن وجدت
    direction = plan.get("direction")
    if direction in ("BUY", "SELL"):
        entry = plan.get("price")
        sl = plan.get("stop_loss")
        tp1 = plan.get("tp1")
        tp2 = plan.get("tp2")

        if entry and min_p <= entry <= max_p:
            y = to_y(entry)
            draw.line([(pad_left, y), (w - pad_right, y)], fill=(52, 152, 219), width=2)
            draw.text((pad_left + 10, y - 14), f"ENTRY: {entry:.1f}", fill=(52, 152, 219), font=font_main)

        if sl and min_p <= sl <= max_p:
            y = to_y(sl)
            draw.line([(pad_left, y), (w - pad_right, y)], fill=(231, 76, 60), width=2)
            draw.text((pad_left + 10, y - 14), f"STOP LOSS: {sl:.1f}", fill=(231, 76, 60), font=font_main)

        if tp2 and min_p <= tp2 <= max_p:
            y = to_y(tp2)
            draw.line([(pad_left, y), (w - pad_right, y)], fill=(46, 204, 113), width=2)
            draw.text((pad_left + 10, y - 14), f"TARGET TP2 (1:2.5): {tp2:.1f}", fill=(46, 204, 113), font=font_main)

    # خط السعر اللحظي المباشر
    cur = live_price or float(sub["close"].iloc[-1])
    if min_p <= cur <= max_p:
        y = to_y(cur)
        draw.line([(pad_left, y), (w - pad_right, y)], fill=(241, 196, 15), width=1)
        draw.rectangle([(w - pad_right, y - 9), (w - 5, y + 9)], fill=(241, 196, 15))
        draw.text((w - pad_right + 4, y - 6), f"{cur:.1f}", fill=(14, 18, 24), font=font_main)

    # عنوان الشارت العلوي
    title = f"S&P 500 (US 500) Institutional Chart - 1H | Price: {cur:,.1f}"
    draw.text((pad_left, 15), title, fill=(255, 255, 255), font=font_title)
    sub_title = f"Setup: {plan.get('label', 'SMC Liquidity & Order Flow')} | Direction: {direction}"
    draw.text((pad_left, 30), sub_title, fill=(180, 190, 205), font=font_main)

    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()
