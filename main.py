# main.py
import os
import sqlite3
import json
import secrets
import requests
from threading import Thread
from flask import (
    Flask, request, redirect, url_for, session,
    render_template_string, jsonify, make_response
)
from telegram import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ---------------------------
# Config / env
# ---------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")  # ex: https://monapp.example.com
FLASK_SECRET = os.getenv("FLASK_SECRET", "please-set-flask-secret")
BOT_USERNAME = os.getenv("BOT_USERNAME", "")
API_SECRET = os.getenv("API_SECRET", "")
TONAPI_KEY = os.getenv("TONAPI_KEY")  # optionnel

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN manquant dans l'environnement.")
if not PUBLIC_BASE_URL:
    raise RuntimeError("PUBLIC_BASE_URL manquant (doit être HTTPS public).")

# ---------------------------
# Flask app
# ---------------------------
app = Flask(__name__)
app.secret_key = FLASK_SECRET

ICON_BYTES = bytes.fromhex(
    "89504E470D0A1A0A0000000D4948445200000001000000010806000000"
    "1F15C4890000000A49444154789C6360000002000154A24F6500000000"
    "49454E44AE426082"
)

@app.route("/static/ton-icon.png")
def ton_icon():
    resp = make_response(ICON_BYTES)
    resp.headers["Content-Type"] = "image/png"
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp

@app.route("/ton/manifest.json")
def ton_manifest():
    manifest = {
        "url": PUBLIC_BASE_URL,
        "name": "Endorisum Bot1",
        "iconUrl": f"{PUBLIC_BASE_URL}/static/ton-icon.png",
        "termsOfUseUrl": f"{PUBLIC_BASE_URL}/terms",
        "privacyPolicyUrl": f"{PUBLIC_BASE_URL}/privacy"
    }
    return jsonify(manifest)

@app.route("/.well-known/tonconnect-manifest.json")
def ton_manifest_wellknown():
    return ton_manifest()

@app.route("/manifest.json")
def ton_manifest_root_alias():
    return ton_manifest()

@app.route("/ton/manifest.sjon")
def ton_manifest_typo_alias():
    return redirect("/ton/manifest.json")

# ---------------------------
# DB init
# ---------------------------
DB_FILE = os.getenv("DB_FILE", "bot.db")
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
c = conn.cursor()
c.execute("""
CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    wallet_address TEXT,
    personal_code TEXT UNIQUE,
    referral_code_used TEXT,
    hat TEXT DEFAULT 'none',
    jacket TEXT DEFAULT 'none',
    pants TEXT DEFAULT 'none',
    shoes TEXT DEFAULT 'none',
    bracelet TEXT DEFAULT 'none'
)
""")
conn.commit()

# ---------------------------
# Helpers
# ---------------------------
def generate_referral_code(length=6):
    import random, string
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

def is_user_registered(tg_id: int) -> bool:
    c.execute("SELECT 1 FROM users WHERE telegram_id=?", (tg_id,))
    return c.fetchone() is not None

def upsert_user_wallet(tg_id: int, address: str):
    c.execute("SELECT personal_code FROM users WHERE telegram_id=?", (tg_id,))
    row = c.fetchone()
    if row:
        c.execute("UPDATE users SET wallet_address=? WHERE telegram_id=?", (address, tg_id))
    else:
        pc = generate_referral_code()
        c.execute("INSERT INTO users (telegram_id, wallet_address, personal_code) VALUES (?, ?, ?)", (tg_id, address, pc))
    conn.commit()

def get_user_row(tg_id: int):
    c.execute("SELECT telegram_id, wallet_address, personal_code, referral_code_used, hat, jacket, pants, shoes, bracelet FROM users WHERE telegram_id=?", (tg_id,))
    r = c.fetchone()
    return r

def set_referral(tg_id: int, referral_code: str):
    """Associer un code de parrainage si l’utilisateur n’est pas encore inscrit"""
    c.execute("SELECT referral_code_used FROM users WHERE telegram_id=?", (tg_id,))
    row = c.fetchone()
    if row is None:
        pc = generate_referral_code()
        c.execute("INSERT INTO users (telegram_id, personal_code, referral_code_used) VALUES (?, ?, ?)",
                  (tg_id, pc, referral_code))
        conn.commit()
    elif not row[0] and referral_code:
        c.execute("UPDATE users SET referral_code_used=? WHERE telegram_id=?", (referral_code, tg_id))
        conn.commit()

# ---------------------------
# TonAPI NFTs
# ---------------------------
def fetch_nfts_for_wallet(address: str):
    if not address or not TONAPI_KEY:
        return []
    try:
        url = f"https://tonapi.io/v2/accounts/{address}/nfts"
        h = {"Authorization": f"Bearer {TONAPI_KEY}"}
        resp = requests.get(url, headers=h, timeout=8)
        if resp.status_code != 200:
            return []
        data = resp.json()
        items = data.get("nft_items") or []
        out = []
        for it in items:
            meta = it.get("metadata") or {}
            image = meta.get("image") or ""
            name = meta.get("name") or "NFT"
            out.append({"name": name, "image": image})
        return out
    except Exception:
        return []

# ---------------------------
# Telegram helpers
# ---------------------------
def send_telegram_message_raw(chat_id: int, text: str, reply_markup: dict = None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        requests.post(url, json=payload, timeout=6)
    except Exception as e:
        print("send_telegram_message_raw error:", e)

# ---------------------------
# Flask API
# ---------------------------
@app.route("/api/me")
def api_me():
    uid = request.args.get("uid", "").strip()
    if not uid.isdigit():
        return jsonify({"registered": False})
    uid_i = int(uid)
    row = get_user_row(uid_i)
    if not row:
        return jsonify({"registered": False})
    telegram_id, wallet, personal_code, referral_code_used, hat, jacket, pants, shoes, bracelet = row
    nfts = fetch_nfts_for_wallet(wallet) if wallet else []
    pioches_total = 0
    return jsonify({
        "registered": True,
        "telegram_id": telegram_id,
        "wallet_address": wallet,
        "personal_code": personal_code,
        "referral_code_used": referral_code_used,
        "pioches_total": pioches_total,
        "avatar": {"hat": hat, "jacket": jacket, "pants": pants, "shoes": shoes, "bracelet": bracelet},
        "nfts": nfts
    })

# ---------------------------
# Telegram bot
# ---------------------------
application = Application.builder().token(TELEGRAM_TOKEN).build()

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    args = context.args

    # Si la commande contient un code de parrainage
    if args:
        referral_code = args[0].strip()
        if referral_code:
            set_referral(uid, referral_code)

    registered = is_user_registered(uid)
    nonce = secrets.token_hex(8)
    if not registered:
        btn = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔗 Connect TON Wallet", web_app=WebAppInfo(url=f"{PUBLIC_BASE_URL}/ton/connect?uid={uid}&nonce={nonce}"))
        ]])
        await update.message.reply_text("Bienvenue — clique pour lier ton wallet TON", reply_markup=btn)
    else:
        btn = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔗 Ouvrir la mini-app", web_app=WebAppInfo(url=f"{PUBLIC_BASE_URL}/app.html?uid={uid}"))
        ]])
        await update.message.reply_text("Tu es inscrit — ouvre la mini-app :", reply_markup=btn)

application.add_handler(CommandHandler("start", start_cmd))

# ---------------------------
# Run Flask + Bot
# ---------------------------
def run_flask():
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))

def run_bot():
    application.run_polling()

if __name__ == "__main__":
    Thread(target=run_flask, daemon=True).start()
    run_bot()
