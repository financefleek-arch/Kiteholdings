import os
import json
from flask import Flask, jsonify, request
from flask_cors import CORS
from kiteconnect import KiteConnect
import anthropic
import yfinance as yf
from datetime import datetime

app  = Flask(__name__)
CORS(app)

# ---- LOAD CONFIG FROM ENV ----
ANTHROPIC_KEY  = os.environ.get("ANTHROPIC_KEY")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
USERS          = json.loads(os.environ.get("USER_CONFIG", "{}"))

# In-memory token store per user
token_store = {}  # {"alice": "access_token_xxx", "bob": "access_token_yyy"}

# ---- AUTH ----
def check_auth():
    password = request.headers.get("X-Admin-Password")
    if password != ADMIN_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 401
    return None

def get_kite(username):
    if username not in USERS:
        return None, f"User {username} not found"
    if username not in token_store:
        return None, f"User {username} not logged in to Zerodha"
    kite = KiteConnect(api_key=USERS[username]["api_key"])
    kite.set_access_token(token_store[username])
    return kite, None

# ---- ROUTES ----

@app.route("/api/users")
def list_users():
    auth = check_auth()
    if auth: return auth
    return jsonify({
        "users": [
            {
                "username" : u,
                "connected": u in token_store
            } for u in USERS.keys()
        ]
    })

@app.route("/api/login-url/<username>")
def login_url(username):
    auth = check_auth()
    if auth: return auth
    if username not in USERS:
        return jsonify({"error": f"User {username} not found"}), 404
    kite = KiteConnect(api_key=USERS[username]["api_key"])
    return jsonify({"login_url": kite.login_url(), "username": username})

@app.route("/api/generate-token", methods=["POST"])
def generate_token():
    auth = check_auth()
    if auth: return auth
    data          = request.json
    username      = data.get("username")
    request_token = data.get("request_token")

    if username not in USERS:
        return jsonify({"error": f"User {username} not found"}), 404

    kite    = KiteConnect(api_key=USERS[username]["api_key"])
    session = kite.generate_session(request_token, api_secret=USERS[username]["api_secret"])
    token_store[username] = session["access_token"]

    return jsonify({"status": "success", "message": f"{username} connected to Zerodha!"})

@app.route("/api/portfolio/<username>")
def portfolio(username):
    auth = check_auth()
    if auth: return auth

    kite, error = get_kite(username)
    if error: return jsonify({"error": error}), 401

    try:
        holdings  = kite.holdings()
        positions = kite.positions()["net"]
        margins   = kite.margins()["equity"]
        nifty     = yf.Ticker("^NSEI").history(period="2d")
        vix       = yf.Ticker("^INDIAVIX").history(period="2d")

        return jsonify({
            "username" : username,
            "holdings" : [{
                "symbol"   : h["tradingsymbol"],
                "quantity" : h["quantity"],
                "avg_price": h["average_price"],
                "ltp"      : h["last_price"],
                "pnl"      : round(h["pnl"], 2),
                "pnl_pct"  : round((h["pnl"] / (h["average_price"] * h["quantity"])) * 100, 2) if h["quantity"] > 0 else 0
            } for h in holdings if h["quantity"] > 0],

            "positions": [{
                "symbol"   : p["tradingsymbol"],
                "quantity" : p["quantity"],
                "avg_price": p["average_price"],
                "ltp"      : p["last_price"],
                "pnl"      : round(p["pnl"], 2)
            } for p in positions if p["quantity"] != 0],

            "funds": {
                "available_cash": margins["available"]["cash"],
                "used_margin"   : margins["utilised"]["debits"]
            },

            "market": {
                "nifty_price" : round(nifty["Close"].iloc[-1], 2),
                "nifty_change": round(((nifty["Close"].iloc[-1] - nifty["Close"].iloc[-2]) / nifty["Close"].iloc[-2]) * 100, 2),
                "vix"         : round(vix["Close"].iloc[-1], 2)
            },

            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/analyze", methods=["POST"])
def analyze():
    auth = check_auth()
    if auth: return auth

    data      = request.json
    portfolio = data.get("portfolio")
    username  = portfolio.get("username", "User")
    client    = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    prompt = f"""
You are an expert Indian stock market analyst.
Analyze this portfolio for {username} and give clear actionable advice.

MARKET:
- Nifty 50  : ₹{portfolio['market']['nifty_price']} ({portfolio['market']['nifty_change']}%)
- India VIX : {portfolio['market']['vix']}
- Time      : {portfolio['timestamp']}

HOLDINGS:
{json.dumps(portfolio['holdings'], indent=2)}

POSITIONS:
{json.dumps(portfolio['positions'], indent=2)}

FUNDS:
- Available Cash : ₹{portfolio['funds']['available_cash']}
- Used Margin    : ₹{portfolio['funds']['used_margin']}

Provide:
1. PORTFOLIO HEALTH — Healthy / At Risk / Critical
2. TOP WINNERS — Hold or book profit?
3. TOP LOSERS — Exit, average down, or hold?
4. RISK ASSESSMENT — Overexposed anywhere?
5. IMMEDIATE ACTION — What to do right now?
6. CASH DEPLOYMENT — Opportunities for available cash?
"""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}]
    )
    return jsonify({"analysis": response.content[0].text})

@app.route("/api/status")
def status():
    auth = check_auth()
    if auth: return auth
    return jsonify({
        "users"    : {u: u in token_store for u in USERS.keys()},
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })

if __name__ == "__main__":
    app.run(debug=True)
