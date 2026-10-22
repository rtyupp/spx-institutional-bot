"""بوت S&P 500 المؤسسي (SPX Institutional Bot) على تيليجرام.

المميزات:
- رصد تحركات البنوك وصناع السوق (Smart Money Concepts) على فريم الساعة.
- جلب الأسعار اللحظية والشموع من حساب Capital.com التجريبي (US500).
- تحليل تدفقات وسلسلة عقود الأوبشن الحقيقية من CBOE (GEX، الجدران، الصفقات الكبيرة Block/Sweep).
- رسم شارت الشموع اليابانية ومستويات الوقف والهدف تلقائياً عبر Pillow.
- تداول تجريبي اختياري مباشر (فتح وإغلاق وإدارة الوقف).
- فحص دوري مستمر وإرسال تنبيهات تلقائية عند رصد دخول مؤسسي مؤكد.
"""
from __future__ import annotations

import io
import json
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

import requests

import analysis
import capital
import charts
import config
import options_flow

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("spx_bot")

TG_API = f"https://api.telegram.org/bot{config.BOT_TOKEN}"
_RUNNING = True


# =====================================================================
# إدارة الحالة (State Persistence)
# =====================================================================
def load_state() -> dict:
    p = Path(config.STATE_FILE)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("Failed to load state.json")
    return {
        "last_signal_dir": None,
        "last_signal_ts": 0.0,
        "auto_trade": config.CAPITAL_AUTO_TRADE,
        "active_trade_deal_id": None,
    }


def save_state(state: dict) -> None:
    try:
        Path(config.STATE_FILE).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        logger.exception("Failed to save state.json")


# =====================================================================
# وظائف تيليجرام الأساسية (Telegram API Helpers)
# =====================================================================
def send_message(chat_id: int | str, text: str, reply_markup: dict | None = None) -> bool:
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(f"{TG_API}/sendMessage", json=payload, timeout=12)
        return r.json().get("ok", False)
    except Exception as e:
        logger.warning("send_message failed: %s", e)
        return False


def send_photo(chat_id: int | str, photo_bytes: bytes, caption: str, reply_markup: dict | None = None) -> bool:
    data = {
        "chat_id": str(chat_id),
        "caption": caption,
        "parse_mode": "HTML",
    }
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    files = {"photo": ("chart.png", photo_bytes, "image/png")}
    try:
        r = requests.post(f"{TG_API}/sendPhoto", data=data, files=files, timeout=25)
        return r.json().get("ok", False)
    except Exception as e:
        logger.warning("send_photo failed: %s", e)
        return False


def main_keyboard(is_admin: bool = False) -> dict:
    rows = [
        [
            {"text": "📊 التحليل المؤسسي", "callback_data": "cmd_analysis"},
            {"text": "🎯 القرار والاستراتيجية", "callback_data": "cmd_decision"},
        ],
        [
            {"text": "📈 شارت الشموع", "callback_data": "cmd_chart"},
            {"text": "💰 السعر اللحظي", "callback_data": "cmd_price"},
        ],
        [
            {"text": "🐋 صفقات الحيتان (الأوبشن)", "callback_data": "cmd_options"},
        ],
    ]
    if is_admin:
        rows[2].append({"text": "💼 حساب Capital", "callback_data": "cmd_capital"})
    else:
        rows[2].append({"text": "📖 دليل الأوامر", "callback_data": "cmd_help"})
    return {"inline_keyboard": rows}


# =====================================================================
# صياغة الرسائل والتقارير
# =====================================================================
def format_price_message(mkt: dict) -> str:
    p = mkt.get("price", 0.0)
    chg = mkt.get("change")
    chg_str = f"{chg:+.2f}%" if chg is not None else "—"
    icon = "🟢" if (chg or 0) >= 0 else "🔴"
    spread = mkt.get("spread", 0.0)

    lines = [
        f"🏛 <b>مؤشر S&P 500 (US500) — مباشر</b>",
        f"━━━━━━━━━━━━━━━━━━",
        f"💵 <b>السعر اللحظي:</b> <code>{p:,.2f}</code>",
        f"📊 <b>التغير اليومي:</b> {icon} <code>{chg_str}</code>",
        f"🎯 <b>الطلب (Ask):</b> <code>{mkt.get('offer', 0):,.2f}</code>",
        f"🛡 <b>العرض (Bid):</b> <code>{mkt.get('bid', 0):,.2f}</code>",
        f"⚡️ <b>السبريد:</b> <code>{spread} pts</code>",
        f"📈 <b>أعلى سعر:</b> <code>{mkt.get('high', 0):,.2f}</code>",
        f"📉 <b>أدنى سعر:</b> <code>{mkt.get('low', 0):,.2f}</code>",
        f"━━━━━━━━━━━━━━━━━━",
        f"📡 <i>المصدر: Capital.com Demo API</i>",
    ]
    return "\n".join(lines)


def format_analysis_message(data: dict) -> str:
    mkt = data["market"]
    inst = data["inst"]
    opt = data["options"]

    score = inst["score"]
    direction = inst["direction"]
    dir_ar = "شراء مؤسسي قوي 🟢" if direction == "BUY" else ("بيع وتفريغ مؤسسي 🔴" if direction == "SELL" else "محايد / مراقبة ⚪️")

    lines = [
        f"🏛 <b>التحليل الفني المؤسسي (S&P 500)</b>",
        f"━━━━━━━━━━━━━━━━━━",
        f"💰 <b>السعر:</b> <code>{mkt['price']:,.2f}</code> | <b>قوة الإشارة:</b> <code>{score:+.1f}</code>",
        f"🧭 <b>الاتجاه:</b> <b>{dir_ar}</b>",
        f"📌 <b>النمط المكتشف:</b> {inst['label']}",
        f"",
        f"<b>المؤشرات المؤسسية الرئيسية:</b>",
        f"▫️ متوسط EMA200 (1H): <code>{inst['ema200']:,.1f}</code>",
        f"▫️ معدل الحجم التداولي: <code>{inst['vol_ratio']:.1f}×</code>",
        f"▫️ مؤشر القوة النسبية RSI: <code>{inst['rsi']:.1f}</code>",
        f"▫️ متوسط المدى ATR: <code>{inst['atr']:.1f} pts</code>",
        f"",
    ]

    if inst.get("details"):
        lines.append("<b>الملاحظات والتأكيدات:</b>")
        for d in inst["details"]:
            lines.append(f"• {d}")
        lines.append("")

    if opt.get("ok"):
        lines += [
            f"<b>بيانات سوق الأوبشن (CBOE):</b>",
            f"▫️ نظام الغاما (GEX): {opt.get('gex_regime')}",
            f"▫️ جدار الكول (مقاومة الحيتان): <code>{opt.get('call_wall', 0):,.1f}</code>",
            f"▫️ جدار البوت (دعم الحيتان): <code>{opt.get('put_wall', 0):,.1f}</code>",
            f"▫️ نسبة Put/Call الحجمية: <code>{opt.get('pcr_vol', 1.0):.2f}</code>",
            f"",
        ]

    lines.append("⚠️ <i>تحليل فني لمراقبة حركة السيولة الذكية وليس نصيحة مالية.</i>")
    return "\n".join(lines)


def format_decision_message(inst: dict) -> str:
    d = inst["direction"]
    p = inst["price"]
    sl = inst["stop_loss"]
    tp1 = inst["tp1"]
    tp2 = inst["tp2"]

    if d == "FLAT":
        return (
            "⚖️ <b>القرار الاستثماري اللحظي: محايد (انتظار)</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"💵 السعر الحالي: <code>{p:,.2f}</code>\n"
            f"📊 قوة الإشارة: <code>{inst['score']:+.1f}</code> (أقل من حد الدخول 2.0)\n\n"
            f"💡 <b>التوصية:</b> لا توجد فرصة مؤسسية مكتملة الشروط حالياً.\n"
            f"• راقب مستويات كنس السيولة حول القمم والقيعان.\n"
            f"• انتظر اختراق مدعوم بحجم مؤسسي أو نموذج Liquidity Sweep مؤكد."
        )

    action_text = "🟢 <b>فرصة شراء مؤسسية (LONG)</b>" if d == "BUY" else "🔴 <b>فرصة بيع وتفريغ مؤسسي (SHORT)</b>"
    risk = abs(p - sl)
    reward1 = abs(tp1 - p)
    reward2 = abs(tp2 - p)

    return (
        f"{action_text}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>النموذج:</b> {inst['label']}\n"
        f"💵 <b>سعر الدخول المقترح:</b> <code>{p:,.2f}</code>\n"
        f"🛑 <b>وقف الخسارة (SL):</b> <code>{sl:,.2f}</code> (مخاطرة: {risk:.1f} pts)\n"
        f"🎯 <b>الهدف الأول (TP1 - تأمين):</b> <code>{tp1:,.2f}</code> (عائد 1:1 = {reward1:.1f} pts)\n"
        f"🚀 <b>الهدف الثاني (TP2 - رئيسي):</b> <code>{tp2:,.2f}</code> (عائد 1:2.5 = {reward2:.1f} pts)\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💡 <i>خطة إدارة الصفقة: عند ملامسة الهدف الأول TP1، قم بنقل الوقف فوراً إلى نقطة الدخول (Break-Even) لحجز الأرباح وتأمين الصفقة بالكامل.</i>"
    )


def format_options_message(opt: dict) -> str:
    if not opt.get("ok"):
        return f"⚠️ <b>سوق الأوبشن:</b> غير متاح حالياً ({opt.get('note', 'خطأ في الاتصال')})"

    lines = [
        f"🐋 <b>رصد تمركزات الحيتان في الأوبشن (CBOE SPX)</b>",
        f"━━━━━━━━━━━━━━━━━━",
        f"💵 <b>سعر المؤشر المرجعي:</b> <code>{opt['spot']:,.2f}</code>",
        f"🛡 <b>جدار البوت (Put Wall):</b> <code>{opt.get('put_wall', 0):,.1f}</code>",
        f"🧱 <b>جدار الكول (Call Wall):</b> <code>{opt.get('call_wall', 0):,.1f}</code>",
        f"📍 <b>مستوى ماكس بين (Max Pain):</b> <code>{opt.get('max_pain', 0):,.1f}</code>",
        f"🌊 <b>حالة تعرّض الغاما:</b> {opt.get('gex_regime')}",
        f"⚖��� <b>نسبة Put/Call:</b> <code>{opt.get('pcr_vol', 1.0):.2f}</code>",
        f"",
    ]

    blocks = opt.get("blocks") or []
    if blocks:
        lines.append("<b>أكبر الصفقات المؤسسية المرصودة اليوم (≥ $1M):</b>")
        for i, b in enumerate(blocks[:5], 1):
            d_icon = "🟢" if b["dir"] > 0 else "🔴"
            t_name = "CALL" if b["t"] == "C" else "PUT"
            lines.append(
                f"{i}. {d_icon} <b>{t_name} Strike {b['k']:.0f}</b> — {options_flow.money(b['premium'])} "
                f"(حجم: {int(b['vol']):,} عقد | انتهاء: {b['exp']})"
            )
        lines.append("")

    return "\n".join(lines)


def format_capital_message(acc: dict) -> str:
    lines = [
        f"💼 <b>حساب Capital.com التجريبي (Demo)</b>",
        f"━━━━━━━━━━━━━━━━━━",
        f"🆔 <b>رقم الحساب:</b> <code>{acc.get('accountId')}</code>",
        f"💵 <b>الرصيد الإجمالي:</b> <code>${acc.get('balance', 0):,.2f}</code>",
        f"📊 <b>الربح/الخسارة اللحظية:</b> <code>${acc.get('profitLoss', 0):+,.2f}</code>",
        f"🟢 <b>الرصيد المتاح:</b> <code>${acc.get('available', 0):,.2f}</code>",
        f"",
    ]

    positions = acc.get("positions") or []
    if positions:
        lines.append(f"<b>الصفقات المفتوحة ({len(positions)}):</b>")
        for p in positions:
            side_icon = "🟢 BUY" if p.get("direction") == "BUY" else "🔴 SELL"
            upl = p.get("upl", 0.0)
            lines.append(
                f"• {side_icon} <b>{p.get('name', 'US500')}</b> | الحجم: {p.get('size')}\n"
                f"   الدخول: <code>{p.get('level')}</code> | PnL: <code>${upl:+,.2f}</code> | الصفقة: <code>{p.get('dealId')}</code>"
            )
    else:
        lines.append("<i>لا توجد صفقات مفتوحة حالياً.</i>")

    return "\n".join(lines)


# =====================================================================
# معالجة الأوامر الواردة من المستخدم (Command Dispatcher)
# =====================================================================
def handle_message(msg: dict, state: dict) -> None:
    chat_id = msg.get("chat", {}).get("id")
    user_id = msg.get("from", {}).get("id") or chat_id
    text = (msg.get("text") or "").strip()
    if not chat_id or not text:
        return

    is_admin = bool(user_id and user_id == config.ADMIN_CHAT_ID)
    raw_cmd = text.split()[0].lower()
    cmd = raw_cmd.split("@")[0]  # إزالة @username للأوامر في المجموعات

    if cmd in ("/start", "ابدأ"):
        intro = (
            "🏛 <b>مرحباً بك في بوت S&P 500 المؤسسي</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "بوت عام مفتوح للجميع لمراقبة مؤشر S&P 500 ورصد دخول البنوك وصناع السوق "
            "(Smart Money Concepts) وتدفقات عقود الأوبشن الضخمة والشارتات اللحظية.\n\n"
            "اختر ما تريد من الأزرار بالأسفل أو أرسل الأمر مباشرة:"
        )
        send_message(chat_id, intro, reply_markup=main_keyboard(is_admin=is_admin))

    elif cmd in ("/price", "/السعر"):
        try:
            mkt = capital.live_price()
            send_message(chat_id, format_price_message(mkt))
        except Exception as e:
            send_message(chat_id, f"❌ تعذر جلب السعر: {e}")

    elif cmd in ("/analysis", "/التحليل"):
        try:
            data = analysis.full_spx_analysis()
            send_message(chat_id, format_analysis_message(data))
        except Exception as e:
            logger.exception("analysis command failed")
            send_message(chat_id, f"❌ حدث خطأ أثناء التحليل: {e}")

    elif cmd in ("/decision", "/القرار"):
        try:
            data = analysis.full_spx_analysis()
            send_message(chat_id, format_decision_message(data["inst"]))
        except Exception as e:
            send_message(chat_id, f"❌ خطأ: {e}")

    elif cmd in ("/chart", "/الشارت"):
        try:
            data = analysis.full_spx_analysis()
            img = charts.render_spx_chart(data["df"], data["inst"], data["market"]["price"])
            cap_text = f"📊 <b>شارت SPX المؤسسي (1H)</b>\nالسعر: <code>{data['market']['price']:,.1f}</code> | النمط: {data['inst']['label']}"
            send_photo(chat_id, img, cap_text)
        except Exception as e:
            logger.exception("chart command failed")
            send_message(chat_id, f"❌ تعذر رسم الشارت: {e}")

    elif cmd in ("/options", "/الأوبشن"):
        try:
            opt = options_flow.fetch_options_flow()
            send_message(chat_id, format_options_message(opt))
        except Exception as e:
            send_message(chat_id, f"❌ خطأ في فحص الأوبشن: {e}")

    elif cmd in ("/capital", "/الحساب"):
        if not is_admin:
            send_message(chat_id, "🔒 هذا الأمر مخصص لإدارة الحساب التجريبي للمسؤول فقط.\nيمكنك استخدام باقي الأوامر العامة: /analysis, /chart, /price, /options")
            return
        try:
            acc = capital.account_summary()
            send_message(chat_id, format_capital_message(acc))
        except Exception as e:
            send_message(chat_id, f"❌ تعذر جلب بيانات الحساب: {e}")

    elif cmd in ("/buy", "/شراء"):
        if not is_admin:
            send_message(chat_id, "🔒 أوامر التداول مخصصة للمسؤول فقط.")
            return
        try:
            parts = text.split()
            size = float(parts[1]) if len(parts) > 1 else config.TRADE_SIZE
            data = analysis.full_spx_analysis()
            inst = data["inst"]
            res = capital.open_trade(
                direction="BUY",
                size=size,
                stop_level=inst.get("stop_loss"),
                profit_level=inst.get("tp2"),
            )
            state["active_trade_deal_id"] = res.get("dealId")
            save_state(state)
            send_message(
                chat_id,
                f"🟢 <b>تم فتح صفقة شراء (BUY) تجريبية بنجاح!</b>\n"
                f"• المؤشر: US500\n"
                f"• الحجم: {res['size']}\n"
                f"• سعر الدخول: <code>{res['level']:,.1f}</code>\n"
                f"• الوقف: <code>{res.get('stopLevel')}</code>\n"
                f"• الهدف: <code>{res.get('profitLevel')}</code>\n"
                f"• رقم الصفقة: <code>{res.get('dealId')}</code>",
            )
        except Exception as e:
            send_message(chat_id, f"❌ فشل فتح الصفقة: {e}")

    elif cmd in ("/sell", "/بيع"):
        if not is_admin:
            send_message(chat_id, "🔒 أوامر التداول مخصصة للمسؤول فقط.")
            return
        try:
            parts = text.split()
            size = float(parts[1]) if len(parts) > 1 else config.TRADE_SIZE
            data = analysis.full_spx_analysis()
            inst = data["inst"]
            res = capital.open_trade(
                direction="SELL",
                size=size,
                stop_level=inst.get("stop_loss"),
                profit_level=inst.get("tp2"),
            )
            state["active_trade_deal_id"] = res.get("dealId")
            save_state(state)
            send_message(
                chat_id,
                f"🔴 <b>تم فتح صفقة بيع (SELL) تجريبية بنجاح!</b>\n"
                f"• المؤشر: US500\n"
                f"• الحجم: {res['size']}\n"
                f"• سعر الدخول: <code>{res['level']:,.1f}</code>\n"
                f"• الوقف: <code>{res.get('stopLevel')}</code>\n"
                f"• الهدف: <code>{res.get('profitLevel')}</code>\n"
                f"• رقم الصفقة: <code>{res.get('dealId')}</code>",
            )
        except Exception as e:
            send_message(chat_id, f"❌ فشل فتح الصفقة: {e}")

    elif cmd in ("/closeall", "/إغلاق"):
        if not is_admin:
            send_message(chat_id, "🔒 أوامر التداول مخصصة للمسؤول فقط.")
            return
        try:
            count = capital.close_all()
            state["active_trade_deal_id"] = None
            save_state(state)
            send_message(chat_id, f"✅ تم إغلاق <b>{count}</b> صفقة مفتوحة على US500.")
        except Exception as e:
            send_message(chat_id, f"❌ خطأ أثناء الإغلاق: {e}")

    elif cmd in ("/autotrade", "/التداول_التلقائي"):
        if not is_admin:
            send_message(chat_id, "🔒 هذا الأمر مخصص للمسؤول فقط.")
            return
        parts = text.split()
        if len(parts) > 1:
            val = parts[1].lower() in ("on", "1", "true", "تفعيل")
            state["auto_trade"] = val
            save_state(state)
            status_txt = "مفعّل 🟢" if val else "معطّل 🔴"
            send_message(chat_id, f"⚙️ وضع التداول التجريبي التلقائي أصبح: <b>{status_txt}</b>")
        else:
            cur = "مفعّل 🟢" if state.get("auto_trade") else "معطّل 🔴"
            send_message(chat_id, f"وضع التداول التلقائي الحالي: <b>{cur}</b>\nلتغييره: <code>/autotrade on</code> أو <code>/autotrade off</code>")

    elif cmd in ("/selftest", "/فحص"):
        send_message(chat_id, "⏳ جاري فحص جميع الأنظمة والاتصالات...")
        res_lines = ["🧪 <b>نتائج الفحص الذاتي الشامل:</b>"]
        try:
            m = capital.live_price()
            res_lines.append(f"✅ Capital Price: <code>{m['price']:,.1f}</code> (سبريد: {m['spread']})")
        except Exception as e:
            res_lines.append(f"❌ Capital Price: {e}")

        try:
            df = capital.fetch_ohlcv("1h", 50)
            res_lines.append(f"✅ Capital OHLCV: {len(df)} شمعة")
        except Exception as e:
            res_lines.append(f"❌ Capital OHLCV: {e}")

        try:
            acc = capital.account_summary()
            res_lines.append(f"✅ Capital Account: رصيد <code>${acc['balance']:,.2f}</code>")
        except Exception as e:
            res_lines.append(f"❌ Capital Account: {e}")

        try:
            opt = options_flow.fetch_options_flow()
            res_lines.append(f"✅ CBOE Options: OK (Spot: {opt['spot']:,.1f})")
        except Exception as e:
            res_lines.append(f"❌ CBOE Options: {e}")

        try:
            data = analysis.full_spx_analysis()
            img = charts.render_spx_chart(data["df"], data["inst"], data["market"]["price"])
            res_lines.append(f"✅ Pillow Chart: تم توليد الشارت بنجاح ({len(img):,} bytes)")
        except Exception as e:
            res_lines.append(f"❌ Pillow Chart: {e}")

        send_message(chat_id, "\n".join(res_lines))

    elif cmd in ("/help", "/مساعدة"):
        h_text = (
            "📖 <b>دليل أوامر بوت SPX المؤسسي:</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "/start - القائمة التفاعلية الرئيسية\n"
            "/price - سعر S&P 500 اللحظي والسبريد\n"
            "/analysis - التحليل الفني المؤسسي الشامل\n"
            "/decision - التوصية الاستثمارية والوقف والهدف\n"
            "/chart - رسم شارت الشموع ومستويات الصيد\n"
            "/options - رصد كبار المتداولين وجدران الأوبشن\n"
            "/capital - كشف رصيد وصفقات حساب Capital التجريبي\n"
            "/buy [حجم] - فتح صفقة شراء تجريبية\n"
            "/sell [حجم] - فتح صفقة بيع تجريبية\n"
            "/closeall - إغلاق جميع صفقات المؤشر فوراً\n"
            "/autotrade [on/off] - تفعيل/تعطيل التداول الآلي\n"
            "/selftest - فحص صحة جميع الاتصالات والمصادر\n"
        )
        send_message(chat_id, h_text)


def handle_callback(cb: dict, state: dict) -> None:
    cb_id = cb.get("id")
    data = cb.get("data", "")
    msg = cb.get("message", {})
    chat_id = msg.get("chat", {}).get("id")

    try:
        requests.post(f"{TG_API}/answerCallbackQuery", json={"callback_query_id": cb_id}, timeout=6)
    except Exception:
        pass

    cmd_map = {
        "cmd_analysis": "/analysis",
        "cmd_decision": "/decision",
        "cmd_chart": "/chart",
        "cmd_price": "/price",
        "cmd_options": "/options",
        "cmd_capital": "/capital",
        "cmd_help": "/help",
    }
    target_cmd = cmd_map.get(data)
    if target_cmd and chat_id:
        handle_message({"chat": {"id": chat_id}, "text": target_cmd}, state)


# =====================================================================
# حلقة المراقبة الآلية واقتناص الصفقات المؤسسية (Signal Watcher)
# =====================================================================
def signal_watcher_loop(state: dict) -> None:
    logger.info("Signal watcher loop started (interval: %ds, cooldown: %dm)", config.SIGNAL_INTERVAL_SEC, config.SIGNAL_COOLDOWN_MIN)
    target_chat = config.CHANNEL_ID or config.ADMIN_CHAT_ID

    while _RUNNING:
        try:
            data = analysis.full_spx_analysis()
            inst = data["inst"]
            score = inst.get("score", 0.0)
            direction = inst.get("direction", "FLAT")
            now = time.time()

            # التحقق من وجود إشارة مؤسسية قوية
            if direction in ("BUY", "SELL") and abs(score) >= config.MIN_SIGNAL_SCORE:
                last_dir = state.get("last_signal_dir")
                last_ts = float(state.get("last_signal_ts") or 0.0)
                cooldown_sec = config.SIGNAL_COOLDOWN_MIN * 60

                # فحص التهدئة: لا نكرر نفس التنبيه إلا بعد مرور فترة التهدئة أو إذا تغير الاتجاه
                if (direction != last_dir) or (now - last_ts >= cooldown_sec):
                    logger.info("🔥 New Institutional Signal detected: %s (score: %.1f)", direction, score)
                    state["last_signal_dir"] = direction
                    state["last_signal_ts"] = now

                    alert_msg = (
                        f"🚨 <b>تنبيه صيد مؤسسي جديد (S&P 500)</b>\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"{format_decision_message(inst)}"
                    )

                    # رسم الشارت وإرساله مع التنبيه
                    try:
                        chart_bytes = charts.render_spx_chart(data["df"], inst, data["market"]["price"])
                        send_photo(target_chat, chart_bytes, alert_msg)
                    except Exception:
                        send_message(target_chat, alert_msg)

                    # إذا كان التداول التلقائي مفعل، فتح الصفقة بالحساب التجريبي
                    if state.get("auto_trade"):
                        try:
                            # التأكد أولاً إن لم تكن هناك صفقة مفتوحة بالفعل
                            if not capital.has_open_position():
                                res = capital.open_trade(
                                    direction=direction,
                                    size=config.TRADE_SIZE,
                                    stop_level=inst.get("stop_loss"),
                                    profit_level=inst.get("tp2"),
                                )
                                state["active_trade_deal_id"] = res.get("dealId")
                                send_message(
                                    target_chat,
                                    f"⚡️ <b>تم تنفيذ الصفقة تلقائياً على Capital.com Demo</b>\n"
                                    f"• الاتجاه: {direction}\n"
                                    f"• السعر: <code>{res['level']:,.1f}</code>\n"
                                    f"• الصفقة: <code>{res.get('dealId')}</code>",
                                )
                        except Exception as te:
                            logger.exception("Auto trade execution error: %s", te)
                            send_message(target_chat, f"⚠️ تعذر التنفيذ التلقائي: {te}")

                    save_state(state)

        except Exception as e:
            logger.exception("Error in signal watcher: %s", e)

        # انتظار الفاصل الزمني مع التحقق من استمرار التشغيل
        for _ in range(config.SIGNAL_INTERVAL_SEC):
            if not _RUNNING:
                break
            time.sleep(1)


# =====================================================================
# نقطة الدخول وحلقة Polling الرئيسية
# =====================================================================
def main() -> None:
    global _RUNNING
    if not config.BOT_TOKEN:
        logger.error("BOT_TOKEN is not configured!")
        sys.exit(1)

    state = load_state()

    def handle_sig(sig, frame):
        global _RUNNING
        logger.info("Signal %s received, shutting down gracefully...", sig)
        _RUNNING = False

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    # تشغيل مراقب الإشارات في مسار خلفي منفصل
    watcher_th = threading.Thread(target=signal_watcher_loop, args=(state,), daemon=True)
    watcher_th.start()

    logger.info("Bot polling started for @krmalk_dad_bot...")
    offset = 0

    while _RUNNING:
        try:
            url = f"{TG_API}/getUpdates"
            params = {"offset": offset, "timeout": 15, "allowed_updates": ["message", "callback_query"]}
            resp = requests.get(url, params=params, timeout=20)
            if resp.status_code == 409:
                logger.warning("409 Conflict: Another instance is polling. Waiting 10s...")
                time.sleep(10)
                continue
            if resp.status_code != 200:
                time.sleep(3)
                continue

            data = resp.json()
            if not data.get("ok"):
                time.sleep(3)
                continue

            for upd in data.get("result", []):
                offset = max(offset, upd["update_id"] + 1)
                if "message" in upd:
                    handle_message(upd["message"], state)
                elif "callback_query" in upd:
                    handle_callback(upd["callback_query"], state)

        except requests.exceptions.RequestException:
            time.sleep(4)
        except Exception as e:
            logger.exception("Polling error: %s", e)
            time.sleep(3)

    logger.info("Bot terminated cleanly.")


if __name__ == "__main__":
    main()
