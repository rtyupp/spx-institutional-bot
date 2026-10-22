"""رصد تدفقات الأوبشن ومستويات تعرّض الغاما (GEX) وجدران الكول والبوت وتدفق الصفقات المؤسسية الكبيرة من CBOE SPX/SPY."""
from __future__ import annotations

import datetime as dt
import re
import time
import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/_SPX.json"
_SYM = re.compile(r"^(SPXW?|SPY)(\d{6})([CP])(\d{8})$")
_CACHE: dict[str, tuple[float, dict]] = {}
BLOCK_PREMIUM = 1_000_000.0


def _parse(o: dict, today: dt.date) -> dict | None:
    m = _SYM.match(o.get("option", ""))
    if not m:
        return None
    exp = dt.datetime.strptime(m.group(2), "%y%m%d").date()
    bid, ask = float(o.get("bid") or 0), float(o.get("ask") or 0)
    mid = (bid + ask) / 2 if bid > 0 and ask > 0 else float(o.get("last_trade_price") or 0)
    last = float(o.get("last_trade_price") or mid)
    side = 0
    if bid > 0 and ask > bid:
        pos = (last - bid) / (ask - bid)
        side = 1 if pos >= 0.6 else (-1 if pos <= 0.4 else 0)
    return {
        "sym": o["option"],
        "root": m.group(1),
        "exp": exp,
        "dte": (exp - today).days,
        "t": "C" if m.group(3) == "C" else "P",
        "k": int(m.group(4)) / 1000.0,
        "oi": float(o.get("open_interest") or 0),
        "vol": float(o.get("volume") or 0),
        "gamma": float(o.get("gamma") or 0),
        "iv": float(o.get("iv") or 0),
        "mid": mid,
        "side": side,
    }


def _empty(note: str) -> dict:
    return {
        "ok": False,
        "note": note,
        "spot": 0.0,
        "call_wall": None,
        "put_wall": None,
        "flip": None,
        "max_pain": None,
        "gex": 0.0,
        "gex_regime": "غير متوفر",
        "blocks": [],
        "blocks_bull": 0.0,
        "blocks_bear": 0.0,
        "bias": 0.0,
        "pcr_vol": 1.0,
    }


def fetch_options_flow(force: bool = False) -> dict:
    now = time.time()
    hit = _CACHE.get("opt")
    if hit and not force and (now - hit[0]) < 300:
        return hit[1]
    try:
        r = requests.get(URL, headers=UA, timeout=20)
        r.raise_for_status()
        d = r.json().get("data") or {}
    except Exception as e:
        return _empty(f"CBOE غير متاح: {type(e).__name__}")

    spot = float(d.get("current_price") or 0)
    if spot <= 0 or not d.get("options"):
        return _empty("لا توجد سلسلة أوبشن")

    today = dt.datetime.now(dt.timezone.utc).date()
    cs = [c for c in (_parse(o, today) for o in d["options"]) if c and c["dte"] >= 0]
    near = [c for c in cs if c["dte"] <= 35 and abs(c["k"] / spot - 1) <= 0.10]
    if not near:
        return _empty("لا عقود قريبة")

    nearest = min(c["dte"] for c in near)
    front = [c for c in near if c["dte"] == nearest]

    coi: dict[float, float] = {}
    poi: dict[float, float] = {}
    for c in near:
        (coi if c["t"] == "C" else poi)[c["k"]] = (coi if c["t"] == "C" else poi).get(c["k"], 0) + c["oi"]
    call_wall = max((k for k in coi if k >= spot), key=coi.get, default=None)
    put_wall = max((k for k in poi if k <= spot), key=poi.get, default=None)

    gex_k: dict[float, float] = {}
    for c in near:
        g = c["gamma"] * c["oi"] * 100 * spot * spot * 0.01
        gex_k[c["k"]] = gex_k.get(c["k"], 0) + (g if c["t"] == "C" else -g)
    gex_total = sum(gex_k.values())

    flip = None
    cum = 0.0
    for k in sorted(gex_k):
        prev = cum
        cum += gex_k[k]
        if prev < 0 <= cum or prev > 0 >= cum:
            if abs(k / spot - 1) <= 0.05:
                flip = k

    strikes = sorted({c["k"] for c in front})
    pain = None
    if strikes:
        def loss(s):
            return sum(c["oi"] * max(0.0, (s - c["k"]) if c["t"] == "C" else (c["k"] - s)) for c in front)
        pain = min(strikes, key=loss)

    prem = {"C": 0.0, "P": 0.0}
    vol = {"C": 0.0, "P": 0.0}
    for c in near:
        p = c["vol"] * c["mid"] * 100
        prem[c["t"]] += p
        vol[c["t"]] += c["vol"]

    blocks = []
    for c in near:
        p = c["vol"] * c["mid"] * 100
        if p >= BLOCK_PREMIUM and c["vol"] > max(c["oi"], 100):
            d_ = (1 if c["t"] == "C" else -1) * (c["side"] if c["side"] else 1)
            blocks.append({**c, "premium": p, "dir": d_, "exp": c["exp"].isoformat()})
    blocks.sort(key=lambda b: -b["premium"])

    bull = sum(b["premium"] for b in blocks if b["dir"] > 0)
    bear = sum(b["premium"] for b in blocks if b["dir"] < 0)
    tot = bull + bear
    bias = (bull - bear) / tot if tot > 0 else 0.0
    pcr_vol = vol["P"] / vol["C"] if vol["C"] else 1.0

    res = {
        "ok": True,
        "spot": spot,
        "nearest_dte": nearest,
        "call_wall": call_wall,
        "put_wall": put_wall,
        "flip": flip,
        "max_pain": pain,
        "gex": gex_total,
        "gex_regime": "غاما موجبة (تخميد وارتدادات هادئة)" if gex_total > 0 else "غاما سالبة (زخم سريع وتذبذب حاد)",
        "premium_calls": prem["C"],
        "premium_puts": prem["P"],
        "pcr_vol": round(pcr_vol, 2),
        "blocks": blocks[:8],
        "blocks_bull": bull,
        "blocks_bear": bear,
        "bias": round(bias, 2),
        "ts": now,
    }
    _CACHE["opt"] = (now, res)
    return res


def money(v: float) -> str:
    a = abs(v)
    s = f"{a / 1e9:.2f}B" if a >= 1e9 else f"{a / 1e6:.1f}M" if a >= 1e6 else f"{a / 1e3:.0f}K"
    return ("-" if v < 0 else "") + "$" + s
