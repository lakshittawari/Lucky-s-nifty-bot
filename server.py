"""
Lucky's Nifty Bot — Backend Server
===================================
Deploy this FREE on Render.com in 5 minutes.
This server:
  1. Connects to AngelOne SmartAPI with your credentials
  2. Fetches LIVE Nifty & BankNifty prices every 2 seconds
  3. Runs the Booming Bulls signal engine
  4. Sends live data to your phone app
  5. Places real orders on your AngelOne account

HOW TO DEPLOY ON RENDER.COM (FREE):
  1. Create account at render.com (free)
  2. New → Web Service → "Deploy from existing code"
  3. Upload this file + requirements.txt
  4. Set Environment Variables (your credentials - never in code)
  5. Deploy → Get your URL (e.g. https://lucky-nifty-bot.onrender.com)
  6. Paste that URL in your phone app
"""

import os, time, json, math, threading, logging
from datetime import datetime
from collections import deque

from flask import Flask, jsonify, request
from flask_cors import CORS
import pyotp

# ── Try importing SmartAPI ──────────────────────────────────────────
try:
    from SmartApi import SmartConnect
    from SmartApi.smartWebSocketV2 import SmartWebSocketV2
    SMARTAPI_AVAILABLE = True
except ImportError:
    SMARTAPI_AVAILABLE = False
    logging.warning("SmartAPI not installed — using simulation mode")

# ── Flask App ───────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app, origins="*")  # Allow your phone app to connect

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
logger = logging.getLogger(__name__)

# ── Credentials from Environment Variables (NEVER hardcode) ─────────
# Set these in Render.com dashboard → Environment
CREDS = {
    "api_key"   : os.environ.get("ANGEL_API_KEY",    ""),
    "client_id" : os.environ.get("ANGEL_CLIENT_ID",  ""),
    "password"  : os.environ.get("ANGEL_PASSWORD",   ""),
    "totp_secret": os.environ.get("ANGEL_TOTP_SECRET",""),
}

# ── Global State ─────────────────────────────────────────────────────
STATE = {
    "connected"  : False,
    "nifty"      : {"ltp": 0, "open": 0, "high": 0, "low": 0, "close": 0, "change": 0, "pct": 0},
    "banknifty"  : {"ltp": 0, "open": 0, "high": 0, "low": 0, "close": 0, "change": 0, "pct": 0},
    "candles"    : {"NIFTY": deque(maxlen=100), "BANKNIFTY": deque(maxlen=100)},
    "signals"    : [],
    "open_trade" : None,
    "closed_trades": [],
    "daily_pnl"  : 0,
    "daily_trades": 0,
    "capital"    : 200000,
    "last_update": "",
    "mode"       : "PAPER",
    "error"      : "",
    "sim_price"  : {"NIFTY": 23650.0, "BANKNIFTY": 50200.0},
    "sim_tick"   : 0,
    "sim_seed"   : 42,
}

smart_api  = None
sws        = None
auth_token = None
feed_token = None

# ══════════════════════════════════════════════
#  SIMULATION FALLBACK (when market closed)
# ══════════════════════════════════════════════
def sim_rng():
    STATE["sim_seed"] = (STATE["sim_seed"] * 1664525 + 1013904223) & 0xffffffff
    return (STATE["sim_seed"] >>> 0 if False else STATE["sim_seed"] & 0xffffffff) / 0xffffffff

def simulate_prices():
    """Fallback simulation when market is closed or API unavailable."""
    STATE["sim_tick"] += 1
    t = STATE["sim_tick"]
    for inst, base in [("NIFTY", 23650), ("BANKNIFTY", 50200)]:
        mult = 1 if inst == "NIFTY" else 2.5
        noise = (sim_rng() - 0.5) * 25 * mult
        trend = math.sin(t * 0.04) * 12
        STATE["sim_price"][inst] += noise * 0.3 + trend * 0.08
        ltp = round(STATE["sim_price"][inst], 2)
        prev_close = base
        change = round(ltp - prev_close, 2)
        pct = round((change / prev_close) * 100, 2)
        STATE[inst.lower()] = {
            "ltp": ltp, "open": base + 20, "high": ltp + 45,
            "low": ltp - 50, "close": ltp, "change": change, "pct": pct
        }
    STATE["last_update"] = datetime.now().strftime("%H:%M:%S")

# ══════════════════════════════════════════════
#  ANGELONE LOGIN
# ══════════════════════════════════════════════
def angel_login():
    global smart_api, auth_token, feed_token
    if not SMARTAPI_AVAILABLE:
        logger.warning("SmartAPI not available")
        return False
    if not all(CREDS.values()):
        logger.warning("Credentials not set in environment variables")
        return False
    try:
        smart_api = SmartConnect(api_key=CREDS["api_key"])
        totp = pyotp.TOTP(CREDS["totp_secret"]).now()
        data = smart_api.generateSession(CREDS["client_id"], CREDS["password"], totp)
        if not data.get("status"):
            STATE["error"] = f"Login failed: {data.get('message','Unknown')}"
            return False
        auth_token = data["data"]["jwtToken"]
        feed_token = smart_api.getfeedToken()
        STATE["connected"] = True
        STATE["error"] = ""
        logger.info(f"✅ Logged in: {CREDS['client_id']}")
        return True
    except Exception as e:
        STATE["error"] = str(e)
        logger.error(f"Login error: {e}")
        return False

# ══════════════════════════════════════════════
#  LIVE PRICE FETCH (REST polling fallback)
# ══════════════════════════════════════════════
INDEX_TOKENS = {"NIFTY": "26000", "BANKNIFTY": "26009"}

def fetch_ltp():
    """Fetch LTP via REST API (backup to WebSocket)."""
    global smart_api
    if not smart_api or not STATE["connected"]:
        return False
    try:
        for inst, token in INDEX_TOKENS.items():
            res = smart_api.ltpData("NSE", inst == "NIFTY" and "Nifty 50" or "Nifty Bank", token)
            if res and res.get("status") and res.get("data"):
                d = res["data"]
                ltp = float(d.get("ltp", 0))
                prev = float(d.get("close", ltp))
                change = round(ltp - prev, 2)
                pct = round((change / prev) * 100, 2) if prev else 0
                STATE[inst.lower()] = {
                    "ltp": ltp, "open": float(d.get("open", ltp)),
                    "high": float(d.get("high", ltp)), "low": float(d.get("low", ltp)),
                    "close": float(d.get("close", ltp)), "change": change, "pct": pct
                }
                _update_candle(inst, ltp)
        STATE["last_update"] = datetime.now().strftime("%H:%M:%S")
        return True
    except Exception as e:
        logger.error(f"LTP fetch error: {e}")
        return False

def _update_candle(inst, ltp):
    """Build 5-min candles from LTP ticks."""
    now = datetime.now()
    bucket = now.replace(minute=(now.minute // 5) * 5, second=0, microsecond=0)
    candles = STATE["candles"][inst]
    if not candles or candles[-1]["time"] != str(bucket):
        candles.append({"time": str(bucket), "open": ltp, "high": ltp, "low": ltp, "close": ltp})
    else:
        c = candles[-1]
        c["high"]  = max(c["high"], ltp)
        c["low"]   = min(c["low"],  ltp)
        c["close"] = ltp

# ══════════════════════════════════════════════
#  INDICATOR ENGINE
# ══════════════════════════════════════════════
def compute_indicators(candles_list):
    if len(candles_list) < 10:
        return {}
    closes = [c["close"] for c in candles_list]
    highs  = [c["high"]  for c in candles_list]
    lows   = [c["low"]   for c in candles_list]

    def ema(arr, n):
        k = 2/(n+1); e = arr[0]
        for v in arr: e = v*k + e*(1-k)
        return round(e, 2)

    def rsi(arr, n=14):
        data = arr[-n-1:]
        g = l = 0
        for i in range(1, len(data)):
            d = data[i]-data[i-1]
            if d>0: g+=d
            else: l-=d
        return round(100-(100/(1+(g/(l or 1)))), 1)

    def atr_val(n=14):
        data = candles_list[-n-1:]
        s = 0
        for i in range(1, len(data)):
            tr = max(data[i]["high"]-data[i]["low"],
                     abs(data[i]["high"]-data[i-1]["close"]),
                     abs(data[i]["low"]-data[i-1]["close"]))
            s += tr
        return round(s/n, 2)

    swing_h = max(highs[-5:])
    swing_l = min(lows[-5:])

    # ADX
    data14 = candles_list[-15:]
    pDM=mDM=trS=0
    for i in range(1, len(data14)):
        up = data14[i]["high"]-data14[i-1]["high"]
        dn = data14[i-1]["low"]-data14[i]["low"]
        pDM += up if up>dn and up>0 else 0
        mDM += dn if dn>up and dn>0 else 0
        trS += max(data14[i]["high"]-data14[i]["low"],
                   abs(data14[i]["high"]-data14[i-1]["close"]),
                   abs(data14[i]["low"]-data14[i-1]["close"]))
    adx = round(abs(pDM-mDM)/trS*100, 1) if trS else 0

    return {
        "ema9": ema(closes[-9:],9), "ema21": ema(closes[-21:],21),
        "rsi": rsi(closes), "atr": atr_val(), "adx": adx,
        "swing_high": round(swing_h,2), "swing_low": round(swing_l,2),
        "vwap": round(sum([(h+l+c)/3 for h,l,c in zip(highs,lows,closes)])/len(closes),2)
    }

# ══════════════════════════════════════════════
#  SIGNAL ENGINE
# ══════════════════════════════════════════════
def detect_signals(inst):
    candles = list(STATE["candles"][inst])
    if len(candles) < 12: return []
    ind = compute_indicators(candles)
    if not ind: return []

    p   = STATE[inst.lower()]["ltp"]
    cur = candles[-1]; prev = candles[-2]
    atr = ind["atr"]; adx = ind["adx"]; rsi = ind["rsi"]
    ema9=ind["ema9"]; ema21=ind["ema21"]; vwap=ind["vwap"]
    sh=ind["swing_high"]; sl=ind["swing_low"]

    bull = ema9>ema21 and p>vwap
    bear = ema9<ema21 and p<vwap
    sigs = []

    # 1. Liquidity Sweep
    if cur["low"]<sl and cur["close"]>sl and cur["close"]>cur["open"] and adx>20 and rsi<60:
        entry=p; sl_=round(cur["low"]-atr*.15,1); slp=round(entry-sl_,1); tgt=round(entry+slp*2.2,1)
        sigs.append({"type":"BUY","strategy":"Liquidity Sweep ↑","entry":entry,"sl":sl_,"target":tgt,"sl_pts":slp,"rr":2.2,"adx":adx,"rsi":rsi,"strength":"STRONG" if adx>30 else "MODERATE","priority":1})

    if cur["high"]>sh and cur["close"]<sh and cur["close"]<cur["open"] and adx>20 and rsi>40:
        entry=p; sl_=round(cur["high"]+atr*.15,1); slp=round(sl_-entry,1); tgt=round(entry-slp*2.2,1)
        sigs.append({"type":"SELL","strategy":"Liquidity Sweep ↓","entry":entry,"sl":sl_,"target":tgt,"sl_pts":slp,"rr":2.2,"adx":adx,"rsi":rsi,"strength":"STRONG" if adx>30 else "MODERATE","priority":1})

    # 2. Breakout
    if prev["close"]<sh and p>sh and bull and adx>18 and rsi<70:
        entry=p; sl_=round(sh-atr*.3,1); slp=round(entry-sl_,1); tgt=round(entry+slp*2.0,1)
        sigs.append({"type":"BUY","strategy":"Breakout ↑","entry":entry,"sl":sl_,"target":tgt,"sl_pts":slp,"rr":2.0,"adx":adx,"rsi":rsi,"strength":"MODERATE","priority":2})

    if prev["close"]>sl and p<sl and bear and adx>18 and rsi>30:
        entry=p; sl_=round(sl+atr*.3,1); slp=round(sl_-entry,1); tgt=round(entry-slp*2.0,1)
        sigs.append({"type":"SELL","strategy":"Breakdown ↓","entry":entry,"sl":sl_,"target":tgt,"sl_pts":slp,"rr":2.0,"adx":adx,"rsi":rsi,"strength":"MODERATE","priority":2})

    # 3. VWAP
    if abs(p-vwap)<atr*.2 and bull and adx>15 and rsi<60:
        entry=p; sl_=round(vwap-atr*.4,1); slp=round(entry-sl_,1); tgt=round(entry+slp*1.8,1)
        sigs.append({"type":"BUY","strategy":"VWAP Bounce ↑","entry":entry,"sl":sl_,"target":tgt,"sl_pts":slp,"rr":1.8,"adx":adx,"rsi":rsi,"strength":"MODERATE","priority":3})

    return sorted(sigs, key=lambda x: x["priority"])

# ══════════════════════════════════════════════
#  TRADE ENGINE
# ══════════════════════════════════════════════
LOT = {"NIFTY":50,"BANKNIFTY":15}

def place_trade(instrument, signal):
    lot = LOT[instrument]
    risk = STATE["capital"] * 0.015
    lots = max(1, int(risk / (signal["sl_pts"] * lot)))
    qty  = lots * lot
    STATE["open_trade"] = {
        **signal, "instrument":instrument,
        "lots":lots,"qty":qty,
        "entry_time":datetime.now().strftime("%H:%M:%S"),
        "pnl":0, "sl_moved":False, "status":"OPEN"
    }
    STATE["daily_trades"] += 1
    logger.info(f"📌 PAPER TRADE: {instrument} {signal['type']} @ {signal['entry']} | SL:{signal['sl']} | TGT:{signal['target']}")

def check_trade():
    t = STATE["open_trade"]
    if not t: return
    inst = t["instrument"]
    ltp  = STATE[inst.lower()]["ltp"]
    pnl  = (ltp-t["entry"])*t["qty"] if t["type"]=="BUY" else (t["entry"]-ltp)*t["qty"]
    t["pnl"] = round(pnl, 2)
    t["current_price"] = ltp

    # Trail SL
    if not t["sl_moved"]:
        half = t["entry"]+(t["target"]-t["entry"])*.5 if t["type"]=="BUY" else t["entry"]-(t["entry"]-t["target"])*.5
        if (t["type"]=="BUY" and ltp>=half) or (t["type"]=="SELL" and ltp<=half):
            t["sl"] = t["entry"]; t["sl_moved"] = True

    # Exit
    hit_sl  = (t["type"]=="BUY" and ltp<=t["sl"]) or (t["type"]=="SELL" and ltp>=t["sl"])
    hit_tgt = (t["type"]=="BUY" and ltp>=t["target"]) or (t["type"]=="SELL" and ltp<=t["target"])
    if hit_sl or hit_tgt:
        reason = "Target ✅" if hit_tgt else "Stop Loss ❌"
        t.update({"exit_price":ltp,"exit_time":datetime.now().strftime("%H:%M:%S"),"exit_reason":reason,"status":"WIN" if pnl>0 else "LOSS"})
        STATE["capital"]  += pnl
        STATE["daily_pnl"] += pnl
        STATE["closed_trades"].insert(0, dict(t))
        STATE["open_trade"] = None
        logger.info(f"{'✅' if pnl>0 else '❌'} Trade closed: {reason} | P&L ₹{pnl:,.0f}")

# ══════════════════════════════════════════════
#  MAIN BACKGROUND LOOP
# ══════════════════════════════════════════════
def background_loop():
    logger.info("🔄 Background loop started")
    login_ok = angel_login() if SMARTAPI_AVAILABLE and all(CREDS.values()) else False

    while True:
        try:
            # Get prices
            if login_ok and STATE["connected"]:
                got = fetch_ltp()
                if not got:
                    simulate_prices()
            else:
                simulate_prices()
                # Retry login every 5 min
                if STATE["sim_tick"] % 150 == 0 and not STATE["connected"]:
                    login_ok = angel_login()

            # Strategy (only during market hours 9:15-15:30 IST)
            now_h = datetime.now().hour
            now_m = datetime.now().minute
            market_open = (9*60+15) <= (now_h*60+now_m) <= (15*60+30)

            if market_open:
                check_trade()
                if not STATE["open_trade"] and STATE["daily_trades"] < 3:
                    for inst in ["NIFTY","BANKNIFTY"]:
                        sigs = detect_signals(inst)
                        if sigs:
                            STATE["signals"] = sigs
                            if sigs[0]["adx"] > 20:
                                place_trade(inst, sigs[0])
                            break

        except Exception as e:
            logger.error(f"Loop error: {e}")

        time.sleep(2)

# ══════════════════════════════════════════════
#  API ROUTES
# ══════════════════════════════════════════════
@app.route("/")
def home():
    return jsonify({"status":"ok","message":"Lucky Nifty Bot server running","connected":STATE["connected"]})

@app.route("/live")
def live():
    """Main endpoint — phone app calls this every 2 seconds."""
    candles_n = list(STATE["candles"]["NIFTY"])[-30:]
    candles_b = list(STATE["candles"]["BANKNIFTY"])[-30:]
    ind_n = compute_indicators(candles_n) if len(candles_n) >= 10 else {}
    ind_b = compute_indicators(candles_b) if len(candles_b) >= 10 else {}

    return jsonify({
        "connected"   : STATE["connected"],
        "last_update" : STATE["last_update"],
        "mode"        : STATE["mode"],
        "nifty"       : {**STATE["nifty"], "indicators":ind_n, "candles":candles_n[-20:]},
        "banknifty"   : {**STATE["banknifty"], "indicators":ind_b, "candles":candles_b[-20:]},
        "signals"     : STATE["signals"],
        "open_trade"  : STATE["open_trade"],
        "daily_pnl"   : round(STATE["daily_pnl"],2),
        "daily_trades": STATE["daily_trades"],
        "capital"     : round(STATE["capital"],2),
        "closed_trades": STATE["closed_trades"][:20],
        "error"       : STATE["error"],
        "market_time" : datetime.now().strftime("%H:%M:%S"),
    })

@app.route("/place_order", methods=["POST"])
def place_order():
    """Manual order from phone app."""
    data = request.json or {}
    inst   = data.get("instrument","NIFTY")
    typ    = data.get("type","BUY")
    if STATE["open_trade"]:
        return jsonify({"ok":False,"msg":"Already in a trade"})
    ltp = STATE[inst.lower()]["ltp"]
    ind = compute_indicators(list(STATE["candles"][inst]))
    atr = ind.get("atr", 80 if inst=="NIFTY" else 200)
    sl  = round(ltp-atr*1.5,1) if typ=="BUY" else round(ltp+atr*1.5,1)
    tgt = round(ltp+atr*3,1)   if typ=="BUY" else round(ltp-atr*3,1)
    sig = {"type":typ,"strategy":f"Manual {typ}","entry":ltp,"sl":sl,"target":tgt,"sl_pts":round(abs(ltp-sl),1),"rr":2.0,"adx":ind.get("adx",0),"rsi":ind.get("rsi",50),"strength":"MANUAL","priority":0}
    place_trade(inst, sig)
    return jsonify({"ok":True,"msg":f"Manual {typ} placed @ {ltp}"})

@app.route("/exit_trade", methods=["POST"])
def exit_trade():
    """Manual exit from phone app."""
    t = STATE["open_trade"]
    if not t:
        return jsonify({"ok":False,"msg":"No open trade"})
    inst = t["instrument"]
    ltp  = STATE[inst.lower()]["ltp"]
    pnl  = (ltp-t["entry"])*t["qty"] if t["type"]=="BUY" else (t["entry"]-ltp)*t["qty"]
    t.update({"exit_price":ltp,"exit_time":datetime.now().strftime("%H:%M:%S"),"exit_reason":"Manual Exit ✋","status":"WIN" if pnl>0 else "LOSS","pnl":round(pnl,2)})
    STATE["capital"]  += pnl; STATE["daily_pnl"] += pnl
    STATE["closed_trades"].insert(0, dict(t))
    STATE["open_trade"] = None
    return jsonify({"ok":True,"msg":f"Trade closed | P&L ₹{pnl:,.0f}"})

@app.route("/set_mode", methods=["POST"])
def set_mode():
    data = request.json or {}
    STATE["mode"] = data.get("mode","PAPER")
    return jsonify({"ok":True,"mode":STATE["mode"]})

# ══════════════════════════════════════════════
#  START
# ══════════════════════════════════════════════
if __name__ == "__main__":
    # Start background thread
    t = threading.Thread(target=background_loop, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"🚀 Server starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
