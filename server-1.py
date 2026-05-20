import os
import time
import math
import threading
import logging
from datetime import datetime
from collections import deque

from flask import Flask, jsonify, request
from flask_cors import CORS

try:
    import pyotp
    PYOTP_OK = True
except ImportError:
    PYOTP_OK = False

try:
    from SmartApi import SmartConnect
    SMARTAPI_OK = True
except ImportError:
    SMARTAPI_OK = False

app = Flask(__name__)
CORS(app, origins="*")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
logger = logging.getLogger(__name__)

# Credentials from Render environment variables
CREDS = {
    "api_key":    os.environ.get("ANGEL_API_KEY", ""),
    "client_id":  os.environ.get("ANGEL_CLIENT_ID", ""),
    "password":   os.environ.get("ANGEL_PASSWORD", ""),
    "totp_secret":os.environ.get("ANGEL_TOTP_SECRET", ""),
}

INDEX_TOKENS = {
    "NIFTY":     {"token": "26000", "symbol": "Nifty 50"},
    "BANKNIFTY": {"token": "26009", "symbol": "Nifty Bank"},
}

LOT_SIZE = {"NIFTY": 50, "BANKNIFTY": 15}

STATE = {
    "connected":    False,
    "nifty":        {"ltp": 0, "open": 0, "high": 0, "low": 0, "close": 0, "change": 0, "pct": 0},
    "banknifty":    {"ltp": 0, "open": 0, "high": 0, "low": 0, "close": 0, "change": 0, "pct": 0},
    "candles":      {"NIFTY": deque(maxlen=100), "BANKNIFTY": deque(maxlen=100)},
    "signals":      [],
    "open_trade":   None,
    "closed_trades":[],
    "daily_pnl":    0.0,
    "daily_trades": 0,
    "capital":      200000.0,
    "mode":         "PAPER",
    "error":        "",
    "last_update":  "",
    "sim_price":    {"NIFTY": 23650.0, "BANKNIFTY": 50200.0},
    "sim_tick":     0,
    "sim_seed":     12345,
}

smart_api    = None
refresh_tok  = None


# ── Simulation RNG ────────────────────────────
def sim_rng():
    STATE["sim_seed"] = (STATE["sim_seed"] * 1664525 + 1013904223) & 0xFFFFFFFF
    return STATE["sim_seed"] / 0xFFFFFFFF


def simulate_prices():
    STATE["sim_tick"] += 1
    t = STATE["sim_tick"]
    for inst, base in [("NIFTY", 23650), ("BANKNIFTY", 50200)]:
        mult  = 1.0 if inst == "NIFTY" else 2.5
        noise = (sim_rng() - 0.5) * 25 * mult
        trend = math.sin(t * 0.04) * 12
        STATE["sim_price"][inst] += noise * 0.3 + trend * 0.08
        ltp    = round(STATE["sim_price"][inst], 2)
        change = round(ltp - base, 2)
        pct    = round((change / base) * 100, 2)
        STATE[inst.lower()] = {
            "ltp": ltp, "open": base + 20,
            "high": ltp + 45, "low": ltp - 50,
            "close": ltp, "change": change, "pct": pct,
        }
    STATE["last_update"] = datetime.now().strftime("%H:%M:%S")


# ── AngelOne Login ─────────────────────────────
def angel_login():
    global smart_api, refresh_tok
    if not SMARTAPI_OK or not PYOTP_OK:
        logger.warning("SmartAPI or pyotp not installed")
        return False
    if not all(CREDS.values()):
        logger.warning("Credentials missing in environment variables")
        return False
    try:
        smart_api = SmartConnect(api_key=CREDS["api_key"])
        totp      = pyotp.TOTP(CREDS["totp_secret"]).now()
        data      = smart_api.generateSession(
            CREDS["client_id"], CREDS["password"], totp
        )
        if not data.get("status"):
            STATE["error"] = "Login failed: " + str(data.get("message", ""))
            logger.error(STATE["error"])
            return False
        refresh_tok       = data["data"]["refreshToken"]
        STATE["connected"] = True
        STATE["error"]     = ""
        logger.info("Logged in as %s", CREDS["client_id"])
        return True
    except Exception as exc:
        STATE["error"] = str(exc)
        logger.error("Login error: %s", exc)
        return False


# ── Fetch live LTP from AngelOne ──────────────
def fetch_ltp():
    global smart_api
    if not smart_api or not STATE["connected"]:
        return False
    try:
        for inst, info in INDEX_TOKENS.items():
            res = smart_api.ltpData("NSE", info["symbol"], info["token"])
            if res and res.get("status") and res.get("data"):
                d      = res["data"]
                ltp    = float(d.get("ltp", 0))
                prev   = float(d.get("close", ltp))
                change = round(ltp - prev, 2)
                pct    = round((change / prev) * 100, 2) if prev else 0.0
                STATE[inst.lower()] = {
                    "ltp":    ltp,
                    "open":   float(d.get("open", ltp)),
                    "high":   float(d.get("high", ltp)),
                    "low":    float(d.get("low", ltp)),
                    "close":  float(d.get("close", ltp)),
                    "change": change,
                    "pct":    pct,
                }
                _update_candle(inst, ltp)
        STATE["last_update"] = datetime.now().strftime("%H:%M:%S")
        return True
    except Exception as exc:
        logger.error("LTP fetch error: %s", exc)
        STATE["connected"] = False
        return False


def _update_candle(inst, ltp):
    now    = datetime.now()
    bucket = now.replace(
        minute=(now.minute // 5) * 5, second=0, microsecond=0
    ).strftime("%H:%M")
    candles = STATE["candles"][inst]
    if not candles or candles[-1]["time"] != bucket:
        candles.append({
            "time": bucket, "open": ltp,
            "high": ltp, "low": ltp, "close": ltp,
        })
    else:
        c = candles[-1]
        c["high"]  = max(c["high"], ltp)
        c["low"]   = min(c["low"],  ltp)
        c["close"] = ltp


# ── Indicators ────────────────────────────────
def compute_indicators(candles_list):
    if len(candles_list) < 10:
        return {}
    closes = [c["close"] for c in candles_list]
    highs  = [c["high"]  for c in candles_list]
    lows   = [c["low"]   for c in candles_list]

    def ema(arr, n):
        k = 2.0 / (n + 1)
        e = arr[0]
        for v in arr:
            e = v * k + e * (1 - k)
        return round(e, 2)

    def rsi(arr, n=14):
        data = arr[-(n + 1):]
        g = l = 0.0
        for i in range(1, len(data)):
            d = data[i] - data[i - 1]
            if d > 0:
                g += d
            else:
                l -= d
        return round(100 - (100 / (1 + g / (l or 1))), 1)

    def atr_calc(n=14):
        data = candles_list[-(n + 1):]
        s = 0.0
        for i in range(1, len(data)):
            tr = max(
                data[i]["high"] - data[i]["low"],
                abs(data[i]["high"] - data[i - 1]["close"]),
                abs(data[i]["low"]  - data[i - 1]["close"]),
            )
            s += tr
        return round(s / n, 2)

    def adx_calc(n=14):
        data = candles_list[-(n + 1):]
        p_dm = m_dm = tr_s = 0.0
        for i in range(1, len(data)):
            up = data[i]["high"] - data[i - 1]["high"]
            dn = data[i - 1]["low"] - data[i]["low"]
            p_dm += up if (up > dn and up > 0) else 0
            m_dm += dn if (dn > up and dn > 0) else 0
            tr_s += max(
                data[i]["high"] - data[i]["low"],
                abs(data[i]["high"] - data[i - 1]["close"]),
                abs(data[i]["low"]  - data[i - 1]["close"]),
            )
        return round(abs(p_dm - m_dm) / tr_s * 100, 1) if tr_s else 0.0

    vwap_val = round(
        sum((h + l + c) / 3 for h, l, c in zip(highs, lows, closes)) / len(closes), 2
    )

    return {
        "ema9":       ema(closes[-9:],  9),
        "ema21":      ema(closes[-21:], 21),
        "rsi":        rsi(closes),
        "atr":        atr_calc(),
        "adx":        adx_calc(),
        "swing_high": round(max(highs[-5:]), 2),
        "swing_low":  round(min(lows[-5:]),  2),
        "vwap":       vwap_val,
    }


# ── Signal Engine ─────────────────────────────
def detect_signals(inst):
    candles = list(STATE["candles"][inst])
    if len(candles) < 12:
        return []
    ind = compute_indicators(candles)
    if not ind:
        return []

    p    = STATE[inst.lower()]["ltp"]
    cur  = candles[-1]
    prev = candles[-2]
    atr  = ind["atr"]
    adx  = ind["adx"]
    rsi  = ind["rsi"]
    ema9 = ind["ema9"]
    ema21= ind["ema21"]
    vwap = ind["vwap"]
    sh   = ind["swing_high"]
    sl   = ind["swing_low"]

    bull = ema9 > ema21 and p > vwap
    bear = ema9 < ema21 and p < vwap
    sigs = []

    # 1. Liquidity Sweep BUY
    if (cur["low"] < sl and cur["close"] > sl
            and cur["close"] > cur["open"] and adx > 20 and rsi < 60):
        entry = p
        sl_   = round(cur["low"] - atr * 0.15, 1)
        slp   = round(entry - sl_, 1)
        tgt   = round(entry + slp * 2.2, 1)
        sigs.append({
            "type": "BUY", "strategy": "Liquidity Sweep",
            "entry": entry, "sl": sl_, "target": tgt,
            "sl_pts": slp, "rr": 2.2, "adx": adx, "rsi": rsi,
            "strength": "STRONG" if adx > 30 else "MODERATE", "priority": 1,
        })

    # 1b. Liquidity Sweep SELL
    if (cur["high"] > sh and cur["close"] < sh
            and cur["close"] < cur["open"] and adx > 20 and rsi > 40):
        entry = p
        sl_   = round(cur["high"] + atr * 0.15, 1)
        slp   = round(sl_ - entry, 1)
        tgt   = round(entry - slp * 2.2, 1)
        sigs.append({
            "type": "SELL", "strategy": "Liquidity Sweep",
            "entry": entry, "sl": sl_, "target": tgt,
            "sl_pts": slp, "rr": 2.2, "adx": adx, "rsi": rsi,
            "strength": "STRONG" if adx > 30 else "MODERATE", "priority": 1,
        })

    # 2. Breakout BUY
    if (prev["close"] < sh and p > sh
            and bull and adx > 18 and rsi < 70):
        entry = p
        sl_   = round(sh - atr * 0.3, 1)
        slp   = round(entry - sl_, 1)
        tgt   = round(entry + slp * 2.0, 1)
        sigs.append({
            "type": "BUY", "strategy": "Breakout",
            "entry": entry, "sl": sl_, "target": tgt,
            "sl_pts": slp, "rr": 2.0, "adx": adx, "rsi": rsi,
            "strength": "MODERATE", "priority": 2,
        })

    # 2b. Breakdown SELL
    if (prev["close"] > sl and p < sl
            and bear and adx > 18 and rsi > 30):
        entry = p
        sl_   = round(sl + atr * 0.3, 1)
        slp   = round(sl_ - entry, 1)
        tgt   = round(entry - slp * 2.0, 1)
        sigs.append({
            "type": "SELL", "strategy": "Breakdown",
            "entry": entry, "sl": sl_, "target": tgt,
            "sl_pts": slp, "rr": 2.0, "adx": adx, "rsi": rsi,
            "strength": "MODERATE", "priority": 2,
        })

    # 3. VWAP Bounce
    if (abs(p - vwap) < atr * 0.2 and bull and adx > 15 and rsi < 60):
        entry = p
        sl_   = round(vwap - atr * 0.4, 1)
        slp   = round(entry - sl_, 1)
        tgt   = round(entry + slp * 1.8, 1)
        sigs.append({
            "type": "BUY", "strategy": "VWAP Bounce",
            "entry": entry, "sl": sl_, "target": tgt,
            "sl_pts": slp, "rr": 1.8, "adx": adx, "rsi": rsi,
            "strength": "MODERATE", "priority": 3,
        })

    return sorted(sigs, key=lambda x: x["priority"])


# ── Trade Engine ──────────────────────────────
def place_trade(inst, signal):
    lot   = LOT_SIZE[inst]
    risk  = STATE["capital"] * 0.015
    lots  = max(1, int(risk / (signal["sl_pts"] * lot)))
    qty   = lots * lot
    STATE["open_trade"] = {
        **signal,
        "instrument":  inst,
        "lots":        lots,
        "qty":         qty,
        "entry_time":  datetime.now().strftime("%H:%M:%S"),
        "current_price": signal["entry"],
        "pnl":         0.0,
        "sl_moved":    False,
        "status":      "OPEN",
    }
    STATE["daily_trades"] += 1
    logger.info("TRADE %s %s @ %.2f SL:%.2f TGT:%.2f",
                inst, signal["type"], signal["entry"], signal["sl"], signal["target"])


def check_trade():
    t = STATE["open_trade"]
    if not t:
        return
    inst = t["instrument"]
    ltp  = STATE[inst.lower()]["ltp"]
    if ltp <= 0:
        return

    pnl = ((ltp - t["entry"]) * t["qty"] if t["type"] == "BUY"
           else (t["entry"] - ltp) * t["qty"])
    t["pnl"]           = round(pnl, 2)
    t["current_price"] = ltp

    # Trail SL to break-even at 50% target
    if not t["sl_moved"]:
        if t["type"] == "BUY":
            half = t["entry"] + (t["target"] - t["entry"]) * 0.5
            if ltp >= half:
                t["sl"] = t["entry"]
                t["sl_moved"] = True
        else:
            half = t["entry"] - (t["entry"] - t["target"]) * 0.5
            if ltp <= half:
                t["sl"] = t["entry"]
                t["sl_moved"] = True

    hit_sl  = ((t["type"] == "BUY"  and ltp <= t["sl"]) or
               (t["type"] == "SELL" and ltp >= t["sl"]))
    hit_tgt = ((t["type"] == "BUY"  and ltp >= t["target"]) or
               (t["type"] == "SELL" and ltp <= t["target"]))

    if hit_sl or hit_tgt:
        reason = "Target ✅" if hit_tgt else "Stop Loss ❌"
        t.update({
            "exit_price": ltp,
            "exit_time":  datetime.now().strftime("%H:%M:%S"),
            "exit_reason": reason,
            "status":     "WIN" if pnl > 0 else "LOSS",
        })
        STATE["capital"]   += pnl
        STATE["daily_pnl"] += pnl
        STATE["closed_trades"].insert(0, dict(t))
        STATE["open_trade"] = None
        logger.info("CLOSED %s | PnL ₹%.0f", reason, pnl)


# ── Background Loop ───────────────────────────
def background_loop():
    logger.info("Background loop started")
    login_ok = angel_login()
    retry_counter = 0

    while True:
        try:
            if login_ok and STATE["connected"]:
                if not fetch_ltp():
                    simulate_prices()
            else:
                simulate_prices()
                retry_counter += 1
                if retry_counter % 150 == 0:   # retry login every ~5 min
                    login_ok = angel_login()

            # Strategy engine — only during market hours
            now     = datetime.now()
            minutes = now.hour * 60 + now.minute
            market  = (9 * 60 + 15) <= minutes <= (15 * 60 + 30)

            if market:
                check_trade()
                if not STATE["open_trade"] and STATE["daily_trades"] < 3:
                    for inst in ["NIFTY", "BANKNIFTY"]:
                        sigs = detect_signals(inst)
                        if sigs and sigs[0]["adx"] > 20:
                            STATE["signals"] = sigs
                            place_trade(inst, sigs[0])
                            break

        except Exception as exc:
            logger.error("Loop error: %s", exc)

        time.sleep(2)


# ── Routes ────────────────────────────────────
@app.route("/")
def home():
    return jsonify({
        "status":    "ok",
        "message":   "Lucky Nifty Bot running",
        "connected": STATE["connected"],
        "version":   "2.0",
    })


@app.route("/ping")
def ping():
    return jsonify({"pong": True})


@app.route("/live")
def live():
    candles_n = list(STATE["candles"]["NIFTY"])[-30:]
    candles_b = list(STATE["candles"]["BANKNIFTY"])[-30:]
    ind_n = compute_indicators(candles_n) if len(candles_n) >= 10 else {}
    ind_b = compute_indicators(candles_b) if len(candles_b) >= 10 else {}
    return jsonify({
        "connected":    STATE["connected"],
        "last_update":  STATE["last_update"],
        "mode":         STATE["mode"],
        "nifty":        {**STATE["nifty"],     "indicators": ind_n, "candles": candles_n[-20:]},
        "banknifty":    {**STATE["banknifty"], "indicators": ind_b, "candles": candles_b[-20:]},
        "signals":      STATE["signals"],
        "open_trade":   STATE["open_trade"],
        "daily_pnl":    round(STATE["daily_pnl"], 2),
        "daily_trades": STATE["daily_trades"],
        "capital":      round(STATE["capital"], 2),
        "closed_trades": STATE["closed_trades"][:20],
        "error":        STATE["error"],
        "market_time":  datetime.now().strftime("%H:%M:%S"),
    })


@app.route("/place_order", methods=["POST"])
def place_order():
    data = request.get_json() or {}
    inst = data.get("instrument", "NIFTY")
    typ  = data.get("type", "BUY")
    if STATE["open_trade"]:
        return jsonify({"ok": False, "msg": "Already in a trade"})
    ltp = STATE[inst.lower()]["ltp"]
    ind = compute_indicators(list(STATE["candles"][inst]))
    atr = ind.get("atr", 80 if inst == "NIFTY" else 200)
    sl  = round(ltp - atr * 1.5, 1) if typ == "BUY" else round(ltp + atr * 1.5, 1)
    tgt = round(ltp + atr * 3.0, 1) if typ == "BUY" else round(ltp - atr * 3.0, 1)
    sig = {
        "type": typ, "strategy": "Manual " + typ,
        "entry": ltp, "sl": sl, "target": tgt,
        "sl_pts": round(abs(ltp - sl), 1), "rr": 2.0,
        "adx": ind.get("adx", 0), "rsi": ind.get("rsi", 50),
        "strength": "MANUAL", "priority": 0,
    }
    place_trade(inst, sig)
    return jsonify({"ok": True, "msg": "Manual {} placed @ {:.2f}".format(typ, ltp)})


@app.route("/exit_trade", methods=["POST"])
def exit_trade():
    t = STATE["open_trade"]
    if not t:
        return jsonify({"ok": False, "msg": "No open trade"})
    inst = t["instrument"]
    ltp  = STATE[inst.lower()]["ltp"]
    pnl  = ((ltp - t["entry"]) * t["qty"] if t["type"] == "BUY"
            else (t["entry"] - ltp) * t["qty"])
    pnl  = round(pnl, 2)
    t.update({
        "exit_price":  ltp,
        "exit_time":   datetime.now().strftime("%H:%M:%S"),
        "exit_reason": "Manual Exit",
        "status":      "WIN" if pnl > 0 else "LOSS",
        "pnl":         pnl,
    })
    STATE["capital"]   += pnl
    STATE["daily_pnl"] += pnl
    STATE["closed_trades"].insert(0, dict(t))
    STATE["open_trade"] = None
    return jsonify({"ok": True, "msg": "Closed | PnL Rs {:.0f}".format(pnl)})


@app.route("/set_mode", methods=["POST"])
def set_mode():
    data = request.get_json() or {}
    STATE["mode"] = data.get("mode", "PAPER")
    return jsonify({"ok": True, "mode": STATE["mode"]})


# ── Entry point ───────────────────────────────
if __name__ == "__main__":
    thread = threading.Thread(target=background_loop, daemon=True)
    thread.start()
    port = int(os.environ.get("PORT", 5000))
    logger.info("Server starting on port %d", port)
    app.run(host="0.0.0.0", port=port, debug=False)
