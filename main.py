# main.py
import os
import json
import time
import secrets
import sqlite3
import threading
from urllib.parse import urlencode

import requests
from flask import (
    Flask, request, jsonify, render_template_string,
    render_template, redirect, url_for, make_response
)
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes
)

# =========================
# ENV & Config
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")  # requis
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")  # requis (https public)
BOT_USERNAME = os.getenv("BOT_USERNAME", "")  # pour liens t.me (facultatif)
API_SECRET = os.getenv("API_SECRET", "changeme")  # utilisé par bot2 (facultatif)
TONAPI_KEY = os.getenv("TONAPI_KEY")  # optionnel pour lister les NFTs

if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN is required")
if not PUBLIC_BASE_URL or not PUBLIC_BASE_URL.startswith("https://"):
    raise ValueError("PUBLIC_BASE_URL must be an https URL")

# =========================
# Flask & DB
# =========================
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "dev-secret")

DB_FILE = "bot.db"
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    wallet_address TEXT,
    personal_code TEXT UNIQUE,
    referral_code_used TEXT,
    hat TEXT DEFAULT 'none',
    jacket TEXT DEFAULT 'none',
    pants TEXT DEFAULT 'none',
    shoes TEXT DEFAULT 'none',
    created_at INTEGER DEFAULT (strftime('%s','now'))
)
""")
conn.commit()

def generate_referral_code(length=6):
    import random, string
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

def is_user_registered(uid:int)->bool:
    cur.execute("SELECT 1 FROM users WHERE telegram_id=?", (uid,))
    return cur.fetchone() is not None

def get_user(uid:int):
    cur.execute("""
        SELECT telegram_id, wallet_address, personal_code, referral_code_used,
               hat, jacket, pants, shoes
        FROM users WHERE telegram_id=?
    """, (uid,))
    row = cur.fetchone()
    if not row:
        return None
    return {
        "telegram_id": row[0],
        "wallet_address": row[1],
        "personal_code": row[2],
        "referral_code_used": row[3],
        "hat": row[4], "jacket": row[5], "pants": row[6], "shoes": row[7],
    }

def upsert_user_wallet(uid:int, address:str):
    cur.execute("SELECT personal_code FROM users WHERE telegram_id=?", (uid,))
    row = cur.fetchone()
    if row:
        cur.execute("UPDATE users SET wallet_address=? WHERE telegram_id=?", (address, uid))
    else:
        pc = generate_referral_code()
        cur.execute("""
          INSERT INTO users (telegram_id, wallet_address, personal_code)
          VALUES (?, ?, ?)
        """, (uid, address, pc))
    conn.commit()

def set_avatar(uid:int, hat:str, jacket:str, pants:str, shoes:str):
    cur.execute("""
      UPDATE users
      SET hat=?, jacket=?, pants=?, shoes=?
      WHERE telegram_id=?
    """, (hat, jacket, pants, shoes, uid))
    conn.commit()

def remove_user(uid:int):
    cur.execute("DELETE FROM users WHERE telegram_id=?", (uid,))
    conn.commit()

# =========================
# Minimal Bot (un bouton)
# =========================
application = Application.builder().token(TELEGRAM_TOKEN).build()

def build_connect_kb(uid:int):
    nonce = secrets.token_hex(8)
    connect_url = f"{PUBLIC_BASE_URL}/ton/connect?uid={uid}&nonce={nonce}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Connect TON Wallet", web_app=WebAppInfo(url=connect_url))]
    ])

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_user_registered(uid):
        # S'il est déjà inscrit → ouvre direct la mini-app
        app_url = f"{PUBLIC_BASE_URL}/app?uid={uid}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🧩 Ouvrir la mini-app", web_app=WebAppInfo(url=app_url))]])
        await update.message.reply_text("Tu es déjà inscrit. Ouvre la mini-app :", reply_markup=kb)
    else:
        await update.message.reply_text(
            "Bienvenue ! Pour commencer, associe ton wallet TON :",
            reply_markup=build_connect_kb(uid)
        )

async def btn_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # On n'affiche plus d'autres menus ici : juste la connexion
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    await q.edit_message_text("Associe ton wallet TON :", reply_markup=build_connect_kb(uid))

application.add_handler(CommandHandler("start", start_cmd))
application.add_handler(CallbackQueryHandler(btn_handler))

# =========================
# TON Connect: manifest & page
# =========================
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
    return jsonify({
        "url": PUBLIC_BASE_URL,
        "name": "Endorisum Bot1",
        "iconUrl": f"{PUBLIC_BASE_URL}/static/ton-icon.png",
        "termsOfUseUrl": f"{PUBLIC_BASE_URL}/terms",
        "privacyPolicyUrl": f"{PUBLIC_BASE_URL}/privacy"
    })

@app.route("/.well-known/tonconnect-manifest.json")
def ton_manifest_alias():
    return ton_manifest()

# Page de connexion TON (dans WebApp)
CONNECT_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Connect TON Wallet</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <script src="https://unpkg.com/@tonconnect/ui@latest/dist/tonconnect-ui.min.js"></script>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; padding: 20px; background:#fafafa;}
    .box { max-width: 560px; margin: 24px auto; padding: 16px; border: 1px solid #e5e5e5; border-radius: 12px; background:white; }
    .row { margin: 12px 0; }
    .center { text-align:center; }
    #error { color:#b00020; display:none; }
  </style>
</head>
<body>
  <div class="box">
    <h2>Connect TON Wallet</h2>
    <p class="row">Clique ci-dessous pour associer ton wallet via TON Connect.</p>
    <div id="ton-connect" class="row center"></div>
    <div id="error" class="row"></div>
  </div>

<script>
 (function(){
   const uid = "{{uid}}";
   const nonce = "{{nonce}}";
   const manifest = "{{manifest}}";
   const mountId = "ton-connect";
   const errorEl = document.getElementById('error');

   function showErr(msg){ errorEl.style.display='block'; errorEl.textContent = "Erreur: " + msg; }

   async function init(){
     if (typeof TON_CONNECT_UI === "undefined" || !TON_CONNECT_UI.TonConnectUI) {
       showErr("La librairie TON Connect UI n'est pas chargée.");
       return;
     }
     let ui;
     try {
       ui = new TON_CONNECT_UI.TonConnectUI({
         manifestUrl: manifest,
         buttonRootId: mountId,
       });
     } catch(e) {
       showErr("TonConnectUI: " + e.message);
       return;
     }

     ui.onStatusChange(async (state) => {
       if (!state || !state.account) return;
       const address = state.account.address;
       try {
         const r = await fetch("/ton/submit", {
           method: "POST",
           headers: { "Content-Type":"application/json" },
           body: JSON.stringify({ uid: uid, nonce: nonce, address: address })
         });
         const data = await r.json();
         if (data.ok) {
           // Ouvre directement la mini-app
           if (window.Telegram && Telegram.WebApp) {
              Telegram.WebApp.openLink("/app?uid=" + uid, { try_instant_view: false });
              setTimeout(() => Telegram.WebApp.close(), 600);
           } else {
              window.location.href = "/app?uid=" + uid;
           }
         } else {
           showErr(data.error || "Enregistrement impossible");
         }
       } catch(e) {
         showErr("Erreur réseau.");
       }
     });
   }
   if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
   else init();
 })();
</script>
</body>
</html>
"""

@app.route("/ton/connect")
def ton_connect():
    uid = (request.args.get("uid") or "").strip()
    nonce = (request.args.get("nonce") or "").strip()
    if not uid.isdigit() or not nonce:
        return "Paramètres invalides", 400
    return render_template_string(CONNECT_HTML, uid=uid, nonce=nonce, manifest=f"{PUBLIC_BASE_URL}/ton/manifest.json")

@app.route("/ton/submit", methods=["POST"])
def ton_submit():
    try:
        data = request.get_json(force=True)
        uid = int(data.get("uid") or 0)
        address = (data.get("address") or "").strip()
        if not uid or not address:
            return jsonify({"ok": False, "error": "missing uid/address"}), 400
        upsert_user_wallet(uid, address)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# =========================
# Mini-app Web (profil, mines, settings, unsubscribe)
# =========================
@app.route("/app")
def webapp():
    """Mini-app Web (HTML dans templates/app.html)"""
    uid = (request.args.get("uid") or "").strip()
    if not uid.isdigit():
        return "Bad uid", 400
    return render_template("app.html", uid=uid, public_base=PUBLIC_BASE_URL)

def fetch_pioches_from_bot2(telegram_id:int):
    """Optionnel: si ton Bot2 expose une API GET ?telegram_id=&secret= """
    url = os.getenv("BOT2_URL")  # exemple: https://bot2.example.com/trophies
    secret = API_SECRET
    if not url:
        return 0, []
    try:
        r = requests.get(url, params={"telegram_id": telegram_id, "secret": secret}, timeout=5)
        if r.status_code == 200:
            data = r.json()
            return data.get("total_pioches", 0), data.get("details", [])
    except Exception as e:
        print("Bot2 API error:", e)
    return 0, []

def fetch_ton_nfts(address:str):
    """Optionnel via TONAPI (si TONAPI_KEY). Retourne une liste simple {name,image}."""
    if not TONAPI_KEY or not address:
        return []
    try:
        # Endpoint public TonAPI (v2)
        url = f"https://tonapi.io/v2/accounts/{address}/nfts"
        r = requests.get(url, headers={"Authorization": f"Bearer {TONAPI_KEY}"}, params={"limit": 24}, timeout=7)
        if r.status_code != 200:
            return []
        data = r.json()
        items = data.get("nft_items", []) or data.get("nfts", []) or []
        out = []
        for it in items:
            name = it.get("metadata", {}).get("name") or it.get("name") or "NFT"
            img = (it.get("previews") or [{}])[-1].get("url") if it.get("previews") else it.get("metadata", {}).get("image")
            out.append({"name": name, "image": img})
        return out
    except Exception as e:
        print("TONAPI error:", e)
        return []

@app.get("/api/me")
def api_me():
    uid = (request.args.get("uid") or "").strip()
    if not uid.isdigit():
        return jsonify({"error":"bad uid"}), 400
    uid = int(uid)
    u = get_user(uid)
    if not u:
        return jsonify({"registered": False})
    total, details = fetch_pioches_from_bot2(uid)
    nfts = fetch_ton_nfts(u["wallet_address"])
    return jsonify({
        "registered": True,
        "telegram_id": uid,
        "wallet_address": u["wallet_address"],
        "personal_code": u["personal_code"],
        "referral_code_used": u["referral_code_used"],
        "avatar": {
            "hat": u["hat"], "jacket": u["jacket"],
            "pants": u["pants"], "shoes": u["shoes"],
        },
        "pioches_total": total,
        "nfts": nfts,
    })

@app.post("/api/avatar/update")
def api_avatar_update():
    data = request.get_json(force=True)
    uid = int(data.get("uid") or 0)
    if not uid:
        return jsonify({"ok": False, "error":"bad uid"}), 400
    hat = (data.get("hat") or "none")
    jacket = (data.get("jacket") or "none")
    pants = (data.get("pants") or "none")
    shoes = (data.get("shoes") or "none")
    set_avatar(uid, hat, jacket, pants, shoes)
    return jsonify({"ok": True})

@app.get("/api/mines")
def api_mines():
    # Liste statique de liens Telegram (tu peux mettre depuis DB si tu veux)
    mines = [
        {"title":"Mine 1", "url":"https://t.me/mine1"},
        {"title":"Mine 2", "url":"https://t.me/mine2"},
        {"title":"Mine 3", "url":"https://t.me/mine3"},
    ]
    return jsonify({"mines": mines})

@app.post("/api/unsubscribe")
def api_unsubscribe():
    data = request.get_json(force=True)
    uid = int(data.get("uid") or 0)
    if not uid:
        return jsonify({"ok": False, "error":"bad uid"}), 400
    remove_user(uid)
    return jsonify({"ok": True})

@app.get("/health")
def health():
    return "ok"

# =========================
# Run (Flask + Bot)
# =========================
def run_flask():
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))

def run_bot():
    # IMPORTANT: pas d'asyncio.run ici (évite le bug "loop already running")
    application.run_polling(close_loop=False)

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    run_bot()
