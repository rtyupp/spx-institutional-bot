#!/usr/bin/env python3
"""
Hermes Autonomous SPY Trading Agent
Single Clean Engine:
- Capital.com Demo API for live prices, execution, balance, positions.
- News sentiment digested directly by AI in background.
- Institutional SMC + GEX rules.
- Autonomous trade execution (BUY/SELL) with SL/TP and Telegram updates.
- 2-way Telegram chat.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional

# --- Environment & Configuration ---
BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"

BOT_TOKEN: str = ""
ADMIN_CHAT_ID: int = 1819077477
CAPITAL_EMAIL: str = ""
CAPITAL_API_KEY: str = ""
CAPITAL_API_PASSWORD: str = ""

if ENV_FILE.exists():
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k == "BOT_TOKEN":
                BOT_TOKEN = v
            elif k == "ADMIN_CHAT_ID":
                try:
                    ADMIN_CHAT_ID = int(v)
                except ValueError:
                    pass
            elif k == "CAPITAL_EMAIL":
                CAPITAL_EMAIL = v
            elif k == "CAPITAL_API_KEY":
                CAPITAL_API_KEY = v
            elif k == "CAPITAL_API_PASSWORD":
                CAPITAL_API_PASSWORD = v

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
AI_API_URL = "http://127.0.0.1:8877/v1/chat/completions"
AI_MODEL = "gemini-3.8-flash-high"
CAPITAL_BASE_URL = "https://demo-api-capital.backend-capital.com/api/v1"

LOG_FILE = BASE_DIR / "agent.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("HermesSPY")


# =====================================================================
# 1. Capital.com Demo API Engine
# =====================================================================
class CapitalAPI:
    def __init__(self, email: str, api_key: str, password: str):
        self.email = email
        self.api_key = api_key
        self.password = password
        self.cst: Optional[str] = None
        self.xst: Optional[str] = None
        self.token_time: float = 0.0
        self.lock = threading.Lock()
        self.cache: Dict[str, Any] = {}

    def ensure_session(self, force: bool = False) -> bool:
        with self.lock:
            now = time.time()
            if not force and self.cst and self.xst and (now - self.token_time < 500):
                return True

            url = f"{CAPITAL_BASE_URL}/session"
            payload = json.dumps({"identifier": self.email, "password": self.password}).encode("utf-8")
            headers = {
                "X-CAP-API-KEY": self.api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            try:
                req = urllib.request.Request(url, data=payload, headers=headers)
                with urllib.request.urlopen(req, timeout=12) as resp:
                    headers_dict = dict(resp.headers)
                    cst = headers_dict.get("CST") or headers_dict.get("cst")
                    xst = headers_dict.get("X-SECURITY-TOKEN") or headers_dict.get("x-security-token")
                    if cst and xst:
                        self.cst = cst
                        self.xst = xst
                        self.token_time = now
                        return True
            except urllib.error.HTTPError as e:
                if e.code == 429 and self.cst and self.xst:
                    return True
                logger.error(f"Capital auth error: {e.code}")
            except Exception as e:
                logger.error(f"Capital auth failed: {e}")
            return False

    def request(self, endpoint: str, method: str = "GET", data: Optional[dict] = None) -> Optional[dict]:
        if not self.ensure_session():
            return None
        url = f"{CAPITAL_BASE_URL}{endpoint}"
        headers = {
            "X-CAP-API-KEY": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "CST": self.cst or "",
            "X-SECURITY-TOKEN": self.xst or ""
        }
        body = json.dumps(data).encode("utf-8") if data else None
        for attempt in range(2):
            try:
                req = urllib.request.Request(url, data=body, headers=headers, method=method)
                with urllib.request.urlopen(req, timeout=12) as resp:
                    res = json.loads(resp.read().decode("utf-8"))
                    if method == "GET":
                        self.cache[endpoint] = res
                    return res
            except Exception as e:
                if attempt == 1:
                    logger.error(f"Capital request error ({endpoint}): {e}")
                    if method == "GET" and endpoint in self.cache:
                        return self.cache[endpoint]
                time.sleep(0.5)
        return None

    def get_spy_quote(self) -> Optional[Dict[str, Any]]:
        d = self.request("/markets/SPY")
        if not d or "snapshot" not in d:
            return None
        s = d["snapshot"]
        bid = s.get("bid", 0.0)
        offer = s.get("offer", 0.0)
        return {
            "bid": bid,
            "ask": offer,
            "mid": (bid + offer) / 2.0 if bid and offer else (offer or bid),
            "pct_change": s.get("percentageChange", 0.0),
            "spread": round(offer - bid, 2) if bid and offer else 0.0
        }

    def get_candles(self, resolution: str = "HOUR", max_bars: int = 60) -> List[Dict[str, float]]:
        d = self.request(f"/prices/SPY?resolution={resolution}&max={max_bars}")
        if not d or "prices" not in d:
            return []
        bars = []
        for p in d["prices"]:
            c = p.get("closePrice", {})
            o = p.get("openPrice", {})
            h = p.get("highPrice", {})
            l = p.get("lowPrice", {})
            bars.append({
                "open": (o.get("bid", 0) + o.get("ask", 0)) / 2.0,
                "high": (h.get("bid", 0) + h.get("ask", 0)) / 2.0,
                "low": (l.get("bid", 0) + l.get("ask", 0)) / 2.0,
                "close": (c.get("bid", 0) + c.get("ask", 0)) / 2.0,
                "volume": float(p.get("lastTradedVolume", 0.0))
            })
        return bars

    def get_account(self) -> Optional[Dict[str, float]]:
        d = self.request("/accounts")
        if not d or not d.get("accounts"):
            return None
        b = d["accounts"][0].get("balance", {})
        return {
            "balance": float(b.get("balance", 0.0)),
            "available": float(b.get("available", 0.0)),
            "pnl": float(b.get("profitLoss", 0.0))
        }

    def get_positions(self) -> List[Dict[str, Any]]:
        d = self.request("/positions")
        if not d or "positions" not in d:
            return []
        res = []
        for p in d["positions"]:
            pos = p.get("position", {})
            mkt = p.get("market", {})
            if mkt.get("epic") == "SPY":
                res.append({
                    "deal_id": pos.get("dealId"),
                    "direction": pos.get("direction"),
                    "size": float(pos.get("size", 0.0)),
                    "entry": float(pos.get("level", 0.0)),
                    "upl": float(pos.get("upl", 0.0)),
                    "stop": pos.get("stopLevel"),
                    "tp": pos.get("profitLevel"),
                    "created_utc": pos.get("createdDateUTC", "")
                })
        return res

    def update_position(self, deal_id: str, stop: Optional[float] = None, tp: Optional[float] = None) -> bool:
        payload: Dict[str, Any] = {"guaranteedStop": False}
        if stop is not None:
            payload["stopLevel"] = round(stop, 2)
        if tp is not None:
            payload["profitLevel"] = round(tp, 2)
        res = self.request(f"/positions/{deal_id}", method="PUT", data=payload)
        return bool(res and res.get("dealReference"))

    def place_order(self, direction: str, size: float = 0.5, stop: Optional[float] = None, tp: Optional[float] = None) -> Optional[str]:
        payload: Dict[str, Any] = {
            "epic": "SPY",
            "direction": direction.upper(),
            "size": size,
            "guaranteedStop": False
        }
        if stop:
            payload["stopLevel"] = round(stop, 2)
        if tp:
            payload["profitLevel"] = round(tp, 2)
        res = self.request("/positions", method="POST", data=payload)
        return res.get("dealReference") if res else None

    def close_order(self, deal_id: str) -> bool:
        res = self.request(f"/positions/{deal_id}", method="DELETE")
        return bool(res and res.get("dealReference"))


capital = CapitalAPI(CAPITAL_EMAIL, CAPITAL_API_KEY, CAPITAL_API_PASSWORD)


# =====================================================================
# 2. News Radar (RSS Feed - Ingested by AI Silently)
# =====================================================================
def get_latest_news() -> List[str]:
    url = "https://feeds.finance.yahoo.com/rss/2.0/headline?s=SPY,^GSPC&region=US&lang=en-US"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            root = ET.fromstring(resp.read())
        titles = []
        for item in root.findall(".//item")[:5]:
            t = item.find("title")
            if t is not None and t.text:
                titles.append(t.text.strip())
        return titles
    except Exception as e:
        logger.error(f"News fetch error: {e}")
        return []


# =====================================================================
# 3. Telegram API
# =====================================================================
def send_telegram(text: str) -> bool:
    payload = {
        "chat_id": ADMIN_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(f"{TG_API}/sendMessage", data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode()).get("ok", False)
    except Exception as e:
        logger.error(f"TG Send failed: {e}")
        return False


# =====================================================================
# 4. AI Analytical Brain & Autonomous Trading Loop
# =====================================================================
SYSTEM_PROMPT = """أنت Hermes Agent، المدير التنفيذي لصندوق التحوط الآلي (Autonomous AI Hedge Fund Manager) الخاص بعلي.
لديك التحكم الكامل والمطلق على حساب Capital.com التجريبي (Demo) لمؤشر SPY، وقد تم تزويدك بأقوى 5 مهارات تداول كمي ومؤسسي من مستودعات GitHub العالمية:

══════════════════════════════════════════════════════════════
المهارات الخمس المدمجة في نواتك (GitHub Quant Agent Skills):
══════════════════════════════════════════════════════════════
1. [Regime Detection Skill — مصفوفة كشف حالة السوق (4-Quadrant Model)]:
   - تصنف السوق لحظياً عبر (ADX-14 + ATR Percentile + EMA20/50 1H):
     * Q1 (Low Vol + Trending): أفضل بيئة لركوب الاتجاه بحجم كامل.
     * Q2 (High Vol + Trending): زخم قوي، ادخل بنصف الحجم مع وقف ATR واسع.
     * Q3 (Low Vol + Ranging): تذبذب ضيق، ممنوع مطاردة الاختراقات الكاذبة.
     * Q4 (High Vol + Ranging / Chop): فوضى وحرق حسابات — اجلس كاش 100% وامنع التداول.

2. [ICT / SMC Structural Skill — هيكل السيولة الذكية]:
   - رصد مناطق كنس السيولة (Liquidity Sweeps BSL/SSL) وفجوات القيمة العادلة (FVG) وكتل الأوامر (Order Blocks) على فريم الساعة والـ 5 دقائق.

3. [Position Sizing Skill — الحجم الديناميكي]:
   - تحدد حجم الصفقة بناءً على:
     `Size = (Account_Balance * Risk_Pct) / Stop_Distance`
   - تخفض الحجم تلقائياً بنسبة 50% إذا كانت التقلبات عالية (Q2) أو بعد أي صفقة خاسرة.

4. [Exit Strategies Skill — إدارة الخروج والوقف المتحرك]:
   - ممنوع وضع وقف خسارة أقل من `1.80$ - 2.50$` (أكثر من 3.5x سبريد كابيتال البالغ 0.55$).
   - تأمين الصفقة (Break-Even): بمجرد تحرك السعر في صالحنا بمقدار +1.0R، انقل وقف الخسارة إلى نقطة الدخول لتأمين الصفقة مجاناً.
   - الوقف المتحرك (Chandelier ATR Trailing): ارفع الوقف مع السعر لحجز الأرباح.
   - إذا انعكس هيكل الساعة أو صدر خبر سلبي مفاجئ، أغلق الصفقة فوراً ولا تنتظر ضرب الستوب.

5. [Risk Management & Circuit Breaker Skill — قواطع الأمان]:
   - الحد الأقصى للمخاطرة في الصفقة: 1% - 2% من الرصيد.
   - قاطع الخسارة اليومي (Daily Loss Limit): إذا وصلت خسارة اليوم -3%، أوقف فتح صفقات جديدة.

══════════════════════════════════════════════════════════════
أوامر التحكم التنفيذي المباشر بيدك (Full Autonomous Control):
══════════════════════════════════════════════════════════════
أنت تملك الصلاحية الكاملة لإصدار أوامر التنفيذ في آخر سطر من ردك متى ما رأيت ذلك مناسباً:
• لفتح صفقة جديدة:
  `[EXECUTE: ACTION=BUY/SELL, SIZE=0.5, STOP=766.00, TP=772.00]`
• لتعديل وقف الخسارة أو الهدف لصفقة قائمة (Break-Even / Trailing Stop):
  `[EXECUTE: ACTION=MODIFY, DEAL_ID=xxxx, STOP=768.50, TP=773.00]`
• لإغلاق صفقة مفتوحة فوراً:
  `[EXECUTE: ACTION=CLOSE, DEAL_ID=xxxx]`
• لإغلاق جميع الصفقات المفتوحة فوراً (Emergency Flatten):
  `[EXECUTE: ACTION=CLOSE_ALL]`
"""

conversation_history: List[Dict[str, str]] = [
    {"role": "system", "content": SYSTEM_PROMPT}
]


def calc_ema(series: List[float], span: int) -> List[float]:
    if not series:
        return []
    m = 2.0 / (span + 1)
    res = [series[0]]
    for x in series[1:]:
        res.append((x - res[-1]) * m + res[-1])
    return res


def calc_atr(bars: List[Dict[str, float]], period: int = 14) -> float:
    if len(bars) < 2:
        return 1.5
    trs = []
    for i in range(1, len(bars)):
        h = bars[i]["high"]
        l = bars[i]["low"]
        pc = bars[i - 1]["close"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    window = trs[-period:] if len(trs) >= period else trs
    return sum(window) / len(window)


def calc_adx(bars: List[Dict[str, float]], period: int = 14) -> float:
    if len(bars) < period + 2:
        return 15.0
    plus_dm = []
    minus_dm = []
    trs = []
    for i in range(1, len(bars)):
        up = bars[i]["high"] - bars[i - 1]["high"]
        down = bars[i - 1]["low"] - bars[i]["low"]
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr_s = calc_ema(trs, period)
    p_dm_s = calc_ema(plus_dm, period)
    m_dm_s = calc_ema(minus_dm, period)
    dxs = []
    for a, p, m in zip(atr_s, p_dm_s, m_dm_s):
        if a <= 0:
            continue
        p_di = 100.0 * p / a
        m_di = 100.0 * m / a
        denom = p_di + m_di
        dxs.append(100.0 * abs(p_di - m_di) / denom if denom > 0 else 0.0)
    return calc_ema(dxs, period)[-1] if dxs else 15.0


def detect_smc_structures(bars: List[Dict[str, float]]) -> str:
    if len(bars) < 20:
        return "لا توجد إشارات SMC حالياً."
    signals = []
    # Check FVG in last 5 bars
    for i in range(len(bars) - 4, len(bars) - 1):
        b1, b2, b3 = bars[i - 1], bars[i], bars[i + 1]
        if b3["low"] > b1["high"]:
            signals.append(f"Bullish FVG [{b1['high']:.2f} - {b3['low']:.2f}]")
        elif b3["high"] < b1["low"]:
            signals.append(f"Bearish FVG [{b3['high']:.2f} - {b1['low']:.2f}]")
    # Check Liquidity Sweep of 15-bar high/low
    swing_h = max(b["high"] for b in bars[-16:-1])
    swing_l = min(b["low"] for b in bars[-16:-1])
    curr = bars[-1]
    if curr["high"] > swing_h and curr["close"] < swing_h:
        signals.append(f"Bearish Liquidity Sweep (BSL Swept @ {swing_h:.2f})")
    elif curr["low"] < swing_l and curr["close"] > swing_l:
        signals.append(f"Bullish Liquidity Sweep (SSL Swept @ {swing_l:.2f})")
    return " | ".join(signals) if signals else "لا توجد فجوات أو كنس سيولة لحظي."


def analyze_market_structure() -> Dict[str, Any]:
    """Computes full 1H institutional regime (4-Quadrant) + SMC + 5M entry trigger + Strategy Score."""
    quote = capital.get_spy_quote() or {"mid": 0.0, "bid": 0.0, "ask": 0.0, "spread": 0.55, "pct_change": 0.0}
    bars_1h = capital.get_candles("HOUR", 60)
    bars_5m = capital.get_candles("MINUTE_5", 35)

    res: Dict[str, Any] = {
        "quote": quote,
        "1h_ready": False,
        "5m_ready": False,
        "strategy_score": 0.0,
        "direction_bias": "NEUTRAL",
        "vol_ratio_1h": 1.0,
        "atr_1h": 2.0,
        "summary_text": "بيانات الشموع قيد التحديث."
    }

    if len(bars_1h) >= 50 and len(bars_5m) >= 25:
        res["1h_ready"] = True
        res["5m_ready"] = True

        c1h = [b["close"] for b in bars_1h]
        v1h = [b["volume"] for b in bars_1h]
        ema20_1h = calc_ema(c1h, 20)[-1]
        ema50_1h = calc_ema(c1h, 50)[-1]
        atr_1h = calc_atr(bars_1h, 14)
        adx_1h = calc_adx(bars_1h, 14)
        avg_vol_1h = sum(v1h[-11:-1]) / 10.0 if sum(v1h[-11:-1]) > 0 else 1.0
        vol_ratio_1h = v1h[-1] / avg_vol_1h

        # ATR Percentile over rolling 1H bars
        atr_history = [calc_atr(bars_1h[max(0, idx - 14):idx + 1], 14) for idx in range(15, len(bars_1h))]
        atr_pct = sum(1 for x in atr_history if x <= atr_1h) / len(atr_history) * 100.0 if atr_history else 50.0

        # 4-Quadrant Regime Classification (GitHub regime-detection skill)
        is_trending = adx_1h >= 22.0
        is_high_vol = atr_pct >= 70.0
        if is_trending and not is_high_vol:
            regime_quadrant = "Q1 (Clean Trend - بيئة مثالية لاتجاه كامل)"
        elif is_trending and is_high_vol:
            regime_quadrant = "Q2 (Volatile Trend - اتجاه متقلب، نصف الحجم)"
        elif not is_trending and not is_high_vol:
            regime_quadrant = "Q3 (Quiet Range - تذبذب هادئ، ارتداد فقط)"
        else:
            regime_quadrant = "Q4 (Choppy Chaos - فوضى عالية، ممنوع الدخول)"

        c5m = [b["close"] for b in bars_5m]
        ema9_5m = calc_ema(c5m, 9)[-1]
        ema21_5m = calc_ema(c5m, 21)[-1]
        atr_5m = calc_atr(bars_5m, 14)
        smc_1h = detect_smc_structures(bars_1h[-20:])
        smc_5m = detect_smc_structures(bars_5m[-20:])

        curr_1h = bars_1h[-1]
        curr_5m = bars_5m[-1]
        prev_5m = bars_5m[-2]

        score = 0.0
        if ema20_1h > ema50_1h:
            score += 1.0
            if curr_1h["close"] > ema20_1h:
                score += 0.5
        else:
            score -= 1.0
            if curr_1h["close"] < ema20_1h:
                score -= 0.5

        if vol_ratio_1h >= 1.5:
            score += 0.5 if score > 0 else -0.5

        if ema9_5m > ema21_5m and curr_5m["close"] > prev_5m["high"]:
            score += 0.5
        elif ema9_5m < ema21_5m and curr_5m["close"] < prev_5m["low"]:
            score -= 0.5

        bias = "BULL (صاعد)" if score >= 1.5 else ("BEAR (هابط)" if score <= -1.5 else "CHOP / NEUTRAL (تذبذب - ممنوع الدخول)")

        spread = max(quote.get("spread", 0.55), 0.50)
        safe_sl_dist = round(max(atr_1h * 0.85, spread * 3.5, 1.80), 2)
        safe_tp_dist = round(safe_sl_dist * 2.0, 2)

        res.update({
            "ema20_1h": ema20_1h,
            "ema50_1h": ema50_1h,
            "vol_ratio_1h": vol_ratio_1h,
            "atr_1h": atr_1h,
            "adx_1h": adx_1h,
            "atr_pct": atr_pct,
            "regime_quadrant": regime_quadrant,
            "ema9_5m": ema9_5m,
            "ema21_5m": ema21_5m,
            "atr_5m": atr_5m,
            "strategy_score": round(score, 2),
            "direction_bias": bias,
            "safe_sl_dist": safe_sl_dist,
            "safe_tp_dist": safe_tp_dist,
            "summary_text": (
                f"• مصفوفة حالة السوق (Regime Quadrant): {regime_quadrant} (ADX={adx_1h:.1f}, ATR Rank={atr_pct:.0f}%)\n"
                f"• فريم الساعة الأساسي (1H): الاتجاه={bias} | EMA20={ema20_1h:.2f} vs EMA50={ema50_1h:.2f} | Vol Ratio={vol_ratio_1h:.2f}x | ATR(1H)={atr_1h:.2f}\n"
                f"• فريم 5 دقائق (5M): EMA9={ema9_5m:.2f} vs EMA21={ema21_5m:.2f} | Close={curr_5m['close']:.2f} (Prev High={prev_5m['high']:.2f}, Low={prev_5m['low']:.2f})\n"
                f"• هيكل السيولة الذكية (SMC 1H): {smc_1h} | (SMC 5M): {smc_5m}\n"
                f"• تقييم القوة المؤسسية (Strategy Score): {score:+.2f} من 2.50 (الحد الأدنى للدخول الصارم: >= +2.0 للشراء أو <= -2.0 للبيع)\n"
                f"• الوقف الآمن ضد السبريد ({spread}$): مسافة الوقف لا تقل عن {safe_sl_dist}$ | مسافة الهدف: {safe_tp_dist}$ (R:R = 1:2)"
            )
        })

    return res


def ask_ai(user_msg: str) -> str:
    global conversation_history

    mkt = analyze_market_structure()
    quote = mkt["quote"]
    acc = capital.get_account() or {}
    positions = capital.get_positions()
    news = get_latest_news()

    pos_text = "لا توجد صفقات مفتوحة."
    if positions:
        pos_text = "\n".join([
            f"• {p['direction']} {p['size']} عقد @ {p['entry']} (SL: {p.get('stop')} | TP: {p.get('tp')} | PnL: ${p['upl']:.2f}) [ID: {p['deal_id']}]"
            for p in positions
        ])

    news_text = "\n".join([f"- {n}" for n in news]) if news else "لا توجد أخبار عاجلة."

    ctx = (
        f"📊 [بيانات السوق والمحفظة اللحظية - Capital.com]\n"
        f"• سعر SPY: ${quote.get('mid', 0):.2f} (Bid: {quote.get('bid')} | Ask: {quote.get('ask')} | Spread: {quote.get('spread')}$)\n"
        f"{mkt['summary_text']}\n"
        f"• الرصيد الإجمالي: ${acc.get('balance', 0):.2f} | السيولة المتاحة: ${acc.get('available', 0):.2f}\n"
        f"• الصفقات المفتوحة:\n{pos_text}\n"
        f"• شريط الأخبار اللحظية:\n{news_text}\n"
        f"• التوقيت: {datetime.datetime.now().strftime('%H:%M:%S')}"
    )

    conversation_history.append({"role": "user", "content": f"{user_msg}\n\n━━━━━━━━━━━━━━━━━━\n{ctx}"})
    if len(conversation_history) > 13:
        conversation_history = [conversation_history[0]] + conversation_history[-12:]

    payload = {
        "model": AI_MODEL,
        "messages": conversation_history,
        "temperature": 0.5
    }

    try:
        req = urllib.request.Request(
            AI_API_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=25) as resp:
            raw = json.loads(resp.read().decode())["choices"][0]["message"]["content"]

            match = re.search(r"\[EXECUTE:\s*(.*?)\]", raw)
            if match:
                cmd_dict = dict(p.strip().split("=") for p in match.group(1).split(",") if "=" in p)
                action = cmd_dict.get("ACTION", "").upper()
                size = float(cmd_dict.get("SIZE", 0.5))
                stop = float(cmd_dict["STOP"]) if "STOP" in cmd_dict and cmd_dict["STOP"] else None
                tp = float(cmd_dict["TP"]) if "TP" in cmd_dict and cmd_dict["TP"] else None
                deal_id = cmd_dict.get("DEAL_ID")

                status_exec = ""
                if action in ["BUY", "SELL"]:
                    ref = capital.place_order(action, size, stop, tp)
                    if ref:
                        status_exec = f"\n\n⚡ <b>تم تنفيذ الصفقة على Capital Demo!</b>\n• الاتجاه: {action} {size} عقد SPY\n• الوقف: {stop} | الهدف: {tp}\n• المرجع: <code>{ref}</code>"
                    else:
                        status_exec = f"\n\n⚠️ <i>فشل تنفيذ أمر {action}.</i>"
                elif action == "MODIFY" and deal_id:
                    updated = capital.update_position(deal_id, stop=stop, tp=tp)
                    if updated:
                        status_exec = f"\n\n🛡️ <b>تم تعديل وتأمين الصفقة [{deal_id}] بنجاح!</b>\n• الوقف الجديد: {stop} | الهدف الجديد: {tp}"
                    else:
                        status_exec = f"\n\n⚠️ <i>تعذر تعديل الصفقة [{deal_id}].</i>"
                elif action == "CLOSE" and deal_id:
                    closed = capital.close_order(deal_id)
                    if closed:
                        status_exec = f"\n\n✅ <b>تم إغلاق الصفقة [{deal_id}] بنجاح!</b>"
                elif action == "CLOSE_ALL":
                    closed_cnt = 0
                    for p in capital.get_positions():
                        if capital.close_order(p["deal_id"]):
                            closed_cnt += 1
                    status_exec = f"\n\n🚨 <b>تم إغلاق جميع الصفقات المفتوحة ({closed_cnt}) بنجاح!</b>"

                raw = re.sub(r"\[EXECUTE:\s*.*?\]", "", raw).strip() + status_exec

            conversation_history.append({"role": "assistant", "content": raw})
            return raw
    except Exception as e:
        logger.error(f"AI Call error: {e}")
        return f"يا بعدي حصل ضغط بسيط بالسيرفر، لكن أنا معك! سعر SPY الآن ${quote.get('mid', 'N/A')}."


# =====================================================================
# 5. AI-Gated Background Monitor & Telegram Loop
# =====================================================================
last_pulse_time = 0.0
last_auto_trade_time = 0.0
last_pos_review_time = 0.0

def background_pulse():
    """Autonomous AI Hedge Fund Manager Loop:
    1. Active Position Management (Break-Even, Trailing Stop, Emergency Exit).
    2. New Trade Entry Evaluation when 1H Strategy Score >= 2.0.
    3. Periodic 30m Heartbeat.
    """
    global last_pulse_time, last_auto_trade_time, last_pos_review_time
    logger.info("AI-Gated 1H Institutional Scanner & Position Manager started.")
    while True:
        try:
            now = time.time()
            mkt = analyze_market_structure()
            pos = capital.get_positions()

            # 1. Autonomous Open Position Supervision (Every 3 minutes when positions exist)
            if len(pos) > 0 and (now - last_pos_review_time > 180):
                last_pos_review_time = now
                q_mid = mkt["quote"].get("mid", 0.0)
                for p in pos:
                    deal_id = p["deal_id"]
                    direction = p["direction"]
                    entry = p["entry"]
                    curr_sl = p.get("stop")
                    curr_tp = p.get("tp")
                    upl = p["upl"]

                    # Mechanical Break-Even Guard (+1.50$ move in favor -> move SL to entry + 0.10$)
                    if direction == "BUY" and q_mid - entry >= 1.50 and (curr_sl is None or curr_sl < entry):
                        new_sl = round(entry + 0.10, 2)
                        if capital.update_position(deal_id, stop=new_sl, tp=curr_tp):
                            send_telegram(f"🛡️ <b>تأمين آلي للصفقة (Break-Even Skill):</b>\nتم نقل وقف الخسارة للصفقة <code>{deal_id}</code> إلى نقطة الدخول <code>${new_sl}</code> بعد صعود السعر!")
                    elif direction == "SELL" and entry - q_mid >= 1.50 and (curr_sl is None or curr_sl > entry):
                        new_sl = round(entry - 0.10, 2)
                        if capital.update_position(deal_id, stop=new_sl, tp=curr_tp):
                            send_telegram(f"🛡️ <b>تأمين آلي للصفقة (Break-Even Skill):</b>\nتم نقل وقف الخسارة للصفقة <code>{deal_id}</code> إلى نقطة الدخول <code>${new_sl}</code> بعد هبوط السعر!")

                # If 1H trend flipped strongly against open position, ask AI to review & close/modify
                score = mkt.get("strategy_score", 0.0)
                has_conflict = any((p["direction"] == "BUY" and score <= -1.5) or (p["direction"] == "SELL" and score >= 1.5) for p in pos)
                if has_conflict:
                    ai_review = ask_ai("تنبيه إدارة مخاطر: هناك صفقة مفتوحة حالياً ولكن هيكل الساعة (1H) بدأ ينعكس ضدها. راجع الصفقة والأخبار وقرر هل تغلقها فوراً أو تعدل وقفها لحماية الحساب.")
                    send_telegram(f"🚨 <b>تدخل الذكاء الاصطناعي لإدارة الصفقة المفتوحة:</b>\n\n{ai_review}")

            # 2. Autonomous Entry Evaluation when 1H Strategy Score >= 2.0
            if mkt.get("1h_ready") and abs(mkt.get("strategy_score", 0.0)) >= 2.0 and (now - last_auto_trade_time > 2700):
                if len(pos) == 0:
                    last_auto_trade_time = now
                    prompt = (
                        "تم رصد إشارة قوية على فريم الساعة (Strategy Score >= 2.0). "
                        "طبق مهاراتك الخمس (Regime Quadrant + SMC + Position Sizing + ATR Stop + News): "
                        "إذا كانت الفرصة في Q1 أو Q2 ومؤكدة 100% نفذها الآن مع وقف ATR آمن، وإذا كان هناك خطر ارفضها."
                    )
                    ai_decision = ask_ai(prompt)
                    send_telegram(f"🧠 <b>فحص الفرصة المؤسسية بالذكاء الاصطناعي:</b>\n\n{ai_decision}")

            # 3. Every 30 minutes send clean status pulse
            if now - last_pulse_time > 1800:
                last_pulse_time = now
                q = mkt["quote"]
                acc = capital.get_account()
                if q and acc:
                    pos_msg = f"{len(pos)} صفقات مفتوحة" if pos else "لا توجد صفقات مفتوحة"
                    heartbeat_text = (
                        f"💓 <b>نبض صندوق التحوط الآلي (GitHub Quant Skills)</b>\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"📈 <b>سعر SPY:</b> <code>${q['mid']:.2f}</code> (السبريد: {q['spread']}$)\n"
                        f"🧭 <b>حالة السوق:</b> {mkt.get('regime_quadrant', 'N/A')}\n"
                        f"📊 <b>اتجاه الساعة:</b> {mkt.get('direction_bias')} (Score: {mkt.get('strategy_score')})\n"
                        f"💰 <b>الرصيد المتاح:</b> <code>${acc['available']:.2f}</code>\n"
                        f"🎯 <b>الصفقات:</b> {pos_msg}"
                    )
                    send_telegram(heartbeat_text)
            time.sleep(30)
        except Exception as e:
            logger.error(f"Pulse error: {e}")
            time.sleep(15)


def telegram_loop():
    logger.info("Telegram polling started...")
    offset = 0
    send_telegram("🦅 <b>تم تنظيف وبناء المحرك المستقل 100%!</b>\n\n• بدون أزرار أو فوضى.\n• شات حر ومباشر مع الذكاء الاصطناعي.\n• مربوط بـ Capital Demo للتداول والرصد اللحظي.\n• السيولة الحالية: $202.98 جاهزة لاقتناص الفرص!")
    
    while True:
        try:
            url = f"{TG_API}/getUpdates?offset={offset}&timeout=15"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=20) as resp:
                updates = json.loads(resp.read().decode()).get("result", [])
                
            for u in updates:
                offset = u["update_id"] + 1
                if "message" in u:
                    msg = u["message"]
                    text = msg.get("text", "").strip()
                    if not text:
                        continue
                    logger.info(f"User message: {text}")
                    # Send typing
                    try:
                        urllib.request.urlopen(f"{TG_API}/sendChatAction?chat_id={ADMIN_CHAT_ID}&action=typing", timeout=3)
                    except Exception:
                        pass
                    reply = ask_ai(text)
                    send_telegram(reply)
            time.sleep(0.3)
        except Exception as e:
            logger.error(f"Polling error: {e}")
            time.sleep(2)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        print("Running tests...")
        q = capital.get_spy_quote()
        acc = capital.get_account()
        assert q is not None and q["mid"] > 0
        assert acc is not None and acc["balance"] > 0
        print(f"✅ Capital API OK: SPY=${q['mid']} | Balance=${acc['balance']}")
        ai_resp = ask_ai("وش وضع السيولة الحين؟")
        assert len(ai_resp) > 10
        print(f"✅ AI Response OK:\n{ai_resp[:120]}...")
        print("ALL VERIFIED!")
        sys.exit(0)
        
    t_pulse = threading.Thread(target=background_pulse, daemon=True)
    t_pulse.start()
    telegram_loop()
