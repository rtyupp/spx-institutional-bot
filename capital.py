"""بوابة الاتصال بـ Capital.com Demo REST API لمؤشر S&P 500 (US500)."""
from __future__ import annotations

import logging
import threading
import time
import httpx
import pandas as pd
import config

BASE_URL = "https://demo-api-capital.backend-capital.com/api/v1"
EPIC = "US500"

RESOLUTION_MAP = {
    "1m": "MINUTE",
    "5m": "MINUTE_5",
    "15m": "MINUTE_15",
    "30m": "MINUTE_30",
    "1h": "HOUR",
    "4h": "HOUR_4",
    "1d": "DAY",
}

_CLIENT = httpx.Client(timeout=httpx.Timeout(14.0, connect=6.0))
_AUTH_LOCK = threading.Lock()
_SESSION: dict[str, str | float] = {"cst": "", "xst": "", "ts": 0.0, "fail_until": 0.0}
_SESSION_TTL = 540.0  # 9 دقائق

_PRICE_CACHE: dict[str, tuple[float, dict]] = {}
_OHLCV_CACHE: dict[tuple[str, int], tuple[float, pd.DataFrame]] = {}


def is_configured() -> bool:
    return bool(config.CAPITAL_EMAIL and config.CAPITAL_API_KEY and config.CAPITAL_API_PASSWORD)


def _auth_headers(force: bool = False) -> dict[str, str]:
    if not is_configured():
        raise RuntimeError("بيانات حساب Capital.com غير مضبوطة")
    with _AUTH_LOCK:
        now = time.time()
        if not force and _SESSION["cst"] and (now - float(_SESSION["ts"])) < _SESSION_TTL:
            return {
                "CST": str(_SESSION["cst"]),
                "X-SECURITY-TOKEN": str(_SESSION["xst"]),
                "Content-Type": "application/json",
            }
        if now < float(_SESSION.get("fail_until", 0.0)):
            if _SESSION["cst"]:
                return {
                    "CST": str(_SESSION["cst"]),
                    "X-SECURITY-TOKEN": str(_SESSION["xst"]),
                    "Content-Type": "application/json",
                }
            raise RuntimeError("Capital.com rate-limit cooldown")

        r = _CLIENT.post(
            f"{BASE_URL}/session",
            headers={"X-CAP-API-KEY": config.CAPITAL_API_KEY, "Content-Type": "application/json"},
            json={
                "identifier": config.CAPITAL_EMAIL,
                "password": config.CAPITAL_API_PASSWORD,
                "encryptedPassword": False,
            },
        )
        if r.status_code == 429:
            _SESSION["fail_until"] = now + 30.0
            if _SESSION["cst"]:
                return {
                    "CST": str(_SESSION["cst"]),
                    "X-SECURITY-TOKEN": str(_SESSION["xst"]),
                    "Content-Type": "application/json",
                }
        r.raise_for_status()
        cst = r.headers.get("CST", "")
        xst = r.headers.get("X-SECURITY-TOKEN", "")
        if not cst or not xst:
            raise RuntimeError("فشل الحصول على رموز الجلسة من Capital.com")
        _SESSION["cst"] = cst
        _SESSION["xst"] = xst
        _SESSION["ts"] = now
        return {"CST": cst, "X-SECURITY-TOKEN": xst, "Content-Type": "application/json"}


def _request(method: str, path: str, **kwargs) -> httpx.Response:
    headers = _auth_headers(force=False)
    r = _CLIENT.request(method, f"{BASE_URL}{path}", headers=headers, **kwargs)
    if r.status_code == 401:
        headers = _auth_headers(force=True)
        r = _CLIENT.request(method, f"{BASE_URL}{path}", headers=headers, **kwargs)
    r.raise_for_status()
    return r


def live_price() -> dict:
    """جلب سعر SPX (US 500) اللحظي المباشر والسبريد والتغير اليومي."""
    now = time.time()
    cached = _PRICE_CACHE.get(EPIC)
    if cached and (now - cached[0]) < 2.0:
        return cached[1]

    data = _request("GET", f"/markets/{EPIC}").json()
    snap = data.get("snapshot") or {}
    bid = float(snap.get("bid") or 0.0)
    offer = float(snap.get("offer") or bid)
    price = bid if bid > 0 else offer
    pct_chg = snap.get("percentageChange")
    res = {
        "price": price,
        "bid": bid,
        "offer": offer,
        "spread": round(offer - bid, 2),
        "mid": (bid + offer) / 2.0 if (bid and offer) else price,
        "change": float(pct_chg) if pct_chg is not None else None,
        "high": float(snap.get("high") or 0.0),
        "low": float(snap.get("low") or 0.0),
        "ts": now,
        "exchange": "Capital.com (US500)",
    }
    _PRICE_CACHE[EPIC] = (now, res)
    return res


def fetch_ohlcv(timeframe: str = "1h", limit: int = 250) -> pd.DataFrame:
    """جلب الشموع التاريخية واللحظية لمؤشر US500."""
    res = RESOLUTION_MAP.get(timeframe, "HOUR")
    max_bars = min(max(int(limit), 20), 1000)
    cache_key = (res, max_bars)
    now = time.time()
    cached = _OHLCV_CACHE.get(cache_key)
    if cached and (now - cached[0]) < 15.0:
        return cached[1].copy()

    data = _request("GET", f"/prices/{EPIC}", params={"resolution": res, "max": max_bars}).json()
    prices = data.get("prices") or []
    if not prices:
        raise RuntimeError("لم يرجع Capital.com شموعاً لـ US500")

    def _bid(d: dict | None) -> float:
        if not d:
            return 0.0
        b = d.get("bid")
        a = d.get("ask")
        return float(b if b is not None else (a or 0.0))

    rows = []
    for p in prices:
        ts_str = p.get("snapshotTimeUTC") or p.get("snapshotTime")
        rows.append((
            ts_str,
            _bid(p.get("openPrice")),
            _bid(p.get("highPrice")),
            _bid(p.get("lowPrice")),
            _bid(p.get("closePrice")),
            float(p.get("lastTradedVolume") or 0.0),
        ))

    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"])
    _OHLCV_CACHE[cache_key] = (now, df)
    return df.copy()


def account_summary() -> dict:
    """بيانات الرصيد والصفقات المفتوحة على الحساب التجريبي."""
    acc_data = _request("GET", "/accounts").json()
    acc = (acc_data.get("accounts") or [{}])[0]
    bal = acc.get("balance") or {}

    pos_data = _request("GET", "/positions").json()
    positions = []
    for item in pos_data.get("positions") or []:
        p = item.get("position") or {}
        m = item.get("market") or {}
        positions.append({
            "dealId": p.get("dealId"),
            "epic": m.get("epic"),
            "name": m.get("instrumentName"),
            "direction": p.get("direction"),
            "size": p.get("size"),
            "level": p.get("level"),
            "stopLevel": p.get("stopLevel"),
            "profitLevel": p.get("profitLevel"),
            "upl": p.get("upl", 0.0),
        })

    return {
        "accountId": acc.get("accountId", "—"),
        "balance": float(bal.get("balance", 0.0)),
        "profitLoss": float(bal.get("profitLoss", 0.0)),
        "available": float(bal.get("available", 0.0)),
        "positions": positions,
    }


def has_open_position() -> dict | None:
    try:
        s = account_summary()
        for p in s["positions"]:
            if p["epic"] == EPIC:
                return p
    except Exception:
        pass
    return None


def open_trade(direction: str, size: float = 0.1, stop_level: float | None = None, profit_level: float | None = None) -> dict:
    """فتح صفقة BUY أو SELL تجريبية على مؤشر US 500."""
    side = "BUY" if direction.upper() in ("BUY", "LONG") else "SELL"
    mkt = live_price()
    ref_price = mkt["offer"] if side == "BUY" else mkt["bid"]

    payload: dict = {
        "epic": EPIC,
        "direction": side,
        "size": float(size or 0.1),
        "guaranteedStop": False,
        "trailingStop": False,
    }
    if stop_level:
        st = round(float(stop_level), 1)
        if (side == "BUY" and st < ref_price) or (side == "SELL" and st > ref_price):
            payload["stopLevel"] = st
    if profit_level:
        tp = round(float(profit_level), 1)
        if (side == "BUY" and tp > ref_price) or (side == "SELL" and tp < ref_price):
            payload["profitLevel"] = tp

    r = _request("POST", "/positions", json=payload).json()
    deal_ref = r.get("dealReference", "")
    conf = {}
    if deal_ref:
        try:
            conf = _request("GET", f"/confirms/{deal_ref}").json()
        except Exception:
            logging.exception("Failed to get confirm for %s", deal_ref)

    if conf.get("dealStatus") == "REJECTED":
        raise RuntimeError(f"رُفض الأمر: {conf.get('reason')}")

    deal_id = conf.get("dealId")
    for ad in conf.get("affectedDeals") or []:
        if ad.get("status") in ("OPENED", "AMENDED"):
            deal_id = ad.get("dealId") or deal_id

    return {
        "ok": True,
        "epic": EPIC,
        "direction": side,
        "size": float(size or 0.1),
        "level": float(conf.get("level") or ref_price),
        "dealId": deal_id,
        "stopLevel": payload.get("stopLevel"),
        "profitLevel": payload.get("profitLevel"),
    }


def close_position(deal_id: str) -> dict:
    r = _request("DELETE", f"/positions/{deal_id}").json()
    return {"ok": True, "dealReference": r.get("dealReference")}


def close_all() -> int:
    s = account_summary()
    closed = 0
    for p in s["positions"]:
        if p["epic"] == EPIC:
            try:
                close_position(p["dealId"])
                closed += 1
            except Exception:
                pass
    return closed
