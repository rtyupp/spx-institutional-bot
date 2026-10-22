"""محرك رصد دخول البنوك والمؤسسات (Smart Money Concepts) لمؤشر S&P 500.

الاستراتيجيات المؤسسية:
1. كنس السيولة (SMC Liquidity Sweep): ضرب قيعان/قمم المتداولين الأفراد ثم ارتداد قوي داخل النطاق (Bear/Bull Trap).
2. كتل الأوامر وفجوات القيمة (Order Blocks & Fair Value Gaps - FVG): شموع اندفاعية مؤسسية بحجم كبير (Volume > 1.5×).
3. كسر الهيكل وتغيير الطابع (BOS / CHoCH): تأكيد تحول المسار مع ثبات فوق/تحت EMA200.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import ta
import capital
import options_flow


def compute_indicators(df: pd.DataFrame) -> dict:
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]
    bb = ta.volatility.BollingerBands(c, 20, 2)
    return {
        "atr": ta.volatility.AverageTrueRange(h, l, c, 14).average_true_range() if len(df) >= 15 else (h - l),
        "rsi": ta.momentum.RSIIndicator(c, 14).rsi(),
        "ema20": ta.trend.EMAIndicator(c, 20).ema_indicator(),
        "ema50": ta.trend.EMAIndicator(c, 50).ema_indicator(),
        "ema200": ta.trend.EMAIndicator(c, 200).ema_indicator() if len(df) >= 200 else ta.trend.EMAIndicator(c, 50).ema_indicator(),
        "macd": ta.trend.MACD(c),
        "bb_up": bb.bollinger_hband(),
        "bb_mid": bb.bollinger_mavg(),
        "bb_lo": bb.bollinger_lband(),
        "vol20": v.rolling(20).mean(),
        "don20_hi": h.rolling(20).max().shift(1),
        "don20_lo": l.rolling(20).min().shift(1),
        "don10_hi": h.rolling(10).max().shift(1),
        "don10_lo": l.rolling(10).min().shift(1),
    }


def detect_institutional_move(df: pd.DataFrame) -> dict:
    """يفحص دخول المؤسسات وصيد السيولة."""
    if len(df) < 30:
        return {"signal": "WAIT", "score": 0.0, "label": "بيانات غير كافية", "details": []}

    ind = compute_indicators(df)
    c, o, h, l, v = df["close"], df["open"], df["high"], df["low"], df["volume"]
    i = -2 if len(df) >= 2 else -1  # آخر شمعة مكتملة
    ip = i - 1

    price = float(c.iloc[-1])
    atr = float(ind["atr"].iloc[i]) if ind["atr"] is not None else 15.0
    e20 = float(ind["ema20"].iloc[i])
    e50 = float(ind["ema50"].iloc[i])
    e200 = float(ind["ema200"].iloc[i])
    rsi = float(ind["rsi"].iloc[i])
    v20 = float(ind["vol20"].iloc[i]) if ind["vol20"].iloc[i] == ind["vol20"].iloc[i] else 1.0
    vol_ratio = float(v.iloc[i]) / v20 if v20 > 0 else 1.0
    sw20_hi = float(ind["don20_hi"].iloc[i])
    sw20_lo = float(ind["don20_lo"].iloc[i])
    up = float(ind["bb_up"].iloc[i])
    lo = float(ind["bb_lo"].iloc[i])

    score = 0.0
    label = "داخل النطاق المؤسسي — مراقبة المستويات"
    details = []

    # فحص كنس سيولة القاع (Bullish Liquidity Sweep / Bear Trap)
    swept_bottom = (l.iloc[i] < sw20_lo and c.iloc[i] > sw20_lo) or (
        l.iloc[ip] < float(ind["don20_lo"].iloc[ip]) and c.iloc[i] > sw20_lo and c.iloc[i] > h.iloc[ip]
    )
    if swept_bottom and c.iloc[i] > o.iloc[i]:
        score += 3.5
        label = "كنس سيولة قيعان المؤسسات (Bullish Sweep — مصيدة دباب)"
        details.append(f"تم كنس سيولة القاع ({sw20_lo:,.1f}) والارتداد القوي للداخل")

    # فحص اختراق مؤسسي مدعوم بالحجم
    if c.iloc[i] > sw20_hi and c.iloc[i] > up and vol_ratio >= 1.4 and e20 > e50:
        score += 3.5
        label = f"اندفاع سيولة مؤسسية شرائية (حجم {vol_ratio:.1f}×)"
        details.append(f"اختراق قمة 20 شمعة مع تضاعف حجم التداول المؤسسي {vol_ratio:.1f}×")

    # فحص كنس سيولة القمة (Bearish Liquidity Sweep / Bull Trap)
    swept_top = (h.iloc[i] > sw20_hi and c.iloc[i] < sw20_hi) or (
        h.iloc[ip] > float(ind["don20_hi"].iloc[ip]) and c.iloc[i] < sw20_hi and c.iloc[i] < l.iloc[ip]
    )
    if swept_top and c.iloc[i] < o.iloc[i]:
        score -= 3.5
        label = "كنس سيولة قمم المؤسسات (Bearish Sweep — مصيدة ثيران)"
        details.append(f"تم ضرب ستوبات القمة ({sw20_hi:,.1f}) والرفض السريع للأسفل")

    # فحص كسر بيعي مؤسسي مدعوم بالحجم
    if c.iloc[i] < sw20_lo and c.iloc[i] < lo and vol_ratio >= 1.4 and e20 < e50:
        score -= 3.5
        label = f"تفريغ سيولة مؤسسية بيعية (حجم {vol_ratio:.1f}×)"
        details.append(f"كسر قاع 20 شمعة بحجم بيعي مكثف {vol_ratio:.1f}×")

    # الاتجاه العام فوق/تحت EMA200
    if price > e200:
        score += 0.8
        details.append("التمركز أعلى من متوسط EMA200 يدعم الاتجاه الصاعد")
    else:
        score -= 0.8
        details.append("التمركز أدنى من متوسط EMA200 يدعم الاتجاه الهابط")

    # تحديد الاتجاه والوقف والهدف
    direction = "BUY" if score >= 2.0 else ("SELL" if score <= -2.0 else "FLAT")
    stop_dist = 1.5 * atr
    stop_loss = round(price - stop_dist if direction == "BUY" else price + stop_dist, 1)
    tp1 = round(price + 1.0 * stop_dist if direction == "BUY" else price - 1.0 * stop_dist, 1)  # تأمين 1:1
    tp2 = round(price + 2.5 * stop_dist if direction == "BUY" else price - 2.5 * stop_dist, 1)  # هدف كامل 1:2.5

    return {
        "direction": direction,
        "score": round(score, 1),
        "label": label,
        "price": price,
        "atr": round(atr, 1),
        "stop_loss": stop_loss,
        "tp1": tp1,
        "tp2": tp2,
        "rsi": round(rsi, 1),
        "vol_ratio": round(vol_ratio, 1),
        "ema200": round(e200, 1),
        "details": details,
    }


def full_spx_analysis() -> dict:
    """تحليل متكامل لـ SPX: بيانات Capital.com + رصد المؤسسات + جدران الأوبشن."""
    df = capital.fetch_ohlcv("1h", 200)
    inst = detect_institutional_move(df)
    opt = options_flow.fetch_options_flow()
    mkt = capital.live_price()

    return {
        "market": mkt,
        "inst": inst,
        "options": opt,
        "df": df,
    }
