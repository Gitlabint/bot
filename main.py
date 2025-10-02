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
from telegram import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from telegram.ext import Application, CommandHandler, CallbackQueryHandler

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

# tiny inline PNG icon (1x1 sample) -> remplace si tu veux vrai fichier
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

# manifest required by TON Connect
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

# alias well-known + common typos to reduce 404 problems
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
# Simple TON Connect page (WebApp / stand-alone)
# ---------------------------
CONNECT_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Connect TON Wallet</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <script src="https://unpkg.com/@tonconnect/ui@latest/dist/tonconnect-ui.min.js"></script>
  <style>
    body{font-family:Arial,Helvetica,sans-serif;padding:18px;background:#f7f9fc;color:#0b1220}
    .box{max-width:720px;margin:0 auto;background:#fff;padding:18px;border-radius:10px;box-shadow:0 6px 18px rgba(0,0,0,0.06)}
    button{padding:10px 14px;border-radius:8px;border:none;background:#2b6cff;color:white;cursor:pointer}
    .muted{color:#666;font-size:13px}
  </style>
</head>
<body>
  <div class="box">
    <h2>Connect TON Wallet</h2>
    <p class="muted">Liaison sécurisé de ton wallet TON au bot. Après connexion ton wallet sera enregistré et tu recevras le menu mis à jour dans Telegram.</p>
    <div id="ton-connect"></div>
    <div id="status" style="margin-top:12px;color:green"></div>
    <div id="error" style="margin-top:12px;color:red"></div>
  </div>

  <script>
  (function(){
    const uid = "{{uid}}";
    const nonce = "{{nonce}}";
    const manifest = "{{manifest}}";
    const statusEl = document.getElementById("status");
    const errorEl = document.getElementById("error");

    async function pingManifest(){
      try {
        const r = await fetch(manifest, {cache:'no-store'});
        if (!r.ok) throw new Error("manifest HTTP " + r.status);
        await r.json();
        return true;
      } catch(e) {
        errorEl.textContent = "Manifest inaccessible: " + e.message;
        return false;
      }
    }

    async function init(){
      if (typeof TON_CONNECT_UI === 'undefined' || !TON_CONNECT_UI.TonConnectUI){
        errorEl.textContent = "CDN TON Connect non chargé.";
        return;
      }
      const ok = await pingManifest();
      if (!ok) return;

      const ui = new TON_CONNECT_UI.TonConnectUI({
        manifestUrl: manifest,
        buttonRootId: "ton-connect"
      });

      ui.onStatusChange(async (walletInfo) => {
        if (!walletInfo || !walletInfo.account) return;
        const addr = walletInfo.account.address;
        statusEl.textContent = "Connecté — " + addr;
        try {
          const r = await fetch("/ton/submit", {
            method: "POST",
            headers: {"Content-Type":"application/json"},
            body: JSON.stringify({ uid: uid, nonce: nonce, address: addr })
          });
          const d = await r.json();
          if (d.ok) {
            statusEl.textContent = "Adresse enregistrée ✅. Tu peux fermer cette page.";
            // if in Telegram WebApp try to close
            try { if (window.Telegram && Telegram.WebApp) Telegram.WebApp.close(); } catch(e){}
          } else {
            errorEl.textContent = d.error || "Erreur enregistrement";
          }
        } catch(e) {
          errorEl.textContent = "Erreur réseau lors de l'enregistrement";
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
def ton_connect_page():
    uid = request.args.get("uid", "").strip()
    nonce = request.args.get("nonce", "").strip()
    if not uid.isdigit() or not nonce:
        return "Paramètres invalides", 400
    return render_template_string(
        CONNECT_HTML,
        uid=uid,
        nonce=nonce,
        manifest=f"{PUBLIC_BASE_URL}/ton/manifest.json"
    )

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

# ---------------------------
# TonAPI NFTs (optionnel)
# ---------------------------
def fetch_nfts_for_wallet(address: str):
    if not address:
        return []
    if not TONAPI_KEY:
        return []  # pas de clé → on retourne vide
    try:
        # TonAPI endpoint example (v2). Adapter si tu utilises autre service.
        url = f"https://tonapi.io/v2/accounts/{address}/nfts"
        h = {"Authorization": f"Bearer {TONAPI_KEY}"}
        resp = requests.get(url, headers=h, timeout=8)
        if resp.status_code != 200:
            return []
        data = resp.json()
        items = data.get("nft_items") or data.get("data") or []
        out = []
        for it in items:
            meta = it.get("metadata") or {}
            image = meta.get("image") or it.get("image") or ""
            name = meta.get("name") or it.get("name") or "NFT"
            # avoid returning empty images if absent
            out.append({"name": name, "image": image})
        return out
    except Exception:
        return []

# ---------------------------
# Send Telegram message helper (raw HTTP so we can push menus)
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

def build_menu_markup_dict(is_registered: bool) -> dict:
    if not is_registered:
        return {
            "inline_keyboard": [
                [{"text": "🔗 Connect TON Wallet", "web_app": {"url": f"{PUBLIC_BASE_URL}/ton/connect?uid={{uid}}&nonce={{nonce}}"}}],
                [{"text": "📊 Profil", "callback_data": "profil"}]
            ]
        }
    else:
        return {
            "inline_keyboard": [
                [{"text":"📊 Profil","callback_data":"profil"}, {"text":"⛏️ Mines","callback_data":"mines"}],
                [{"text":"🔗 Ouvrir la mini-app","web_app":{"url": f"{PUBLIC_BASE_URL}/app.html?uid={{uid}}"}}]
            ]
        }

# ---------------------------
# ton/submit (POST) — enregistrement & push menu
# ---------------------------
@app.route("/ton/submit", methods=["POST", "OPTIONS"])
def ton_submit():
    if request.method == "OPTIONS":
        return ("", 204)
    try:
        data = request.get_json() or {}
        uid = int(data.get("uid") or 0)
        addr = (data.get("address") or "").strip()
        if not uid or not addr:
            return jsonify({"ok": False, "error": "missing uid or address"}), 400

        # Upsert wallet
        upsert_user_wallet(uid, addr)

        # Push message to user: menu inscrit (we replace placeholders uid/nonce)
        nonce = secrets.token_hex(8)
        menu_text = f"🔗 Wallet TON reçu: <code>{addr}</code>\n✅ Inscription enregistrée. Voici ton menu :"
        markup = build_menu_markup_dict(True)
        # replace placeholders
        mk = json.loads(json.dumps(markup).replace("{{uid}}", str(uid)).replace("{{nonce}}", nonce))
        mk = json.loads(json.dumps(mk).replace("{{uid}}", str(uid)))  # second pass for app url
        send_telegram_message_raw(uid, menu_text, mk)

        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/ton/submit", methods=["GET"])
def ton_submit_get():
    return ("POST JSON {uid,address}", 405)

# ---------------------------
# Mini-app / API endpoints used by app.html (serve static app separately)
# ---------------------------
# Simple static app.html must be placed in repo under /templates/app.html or generate a simple page here.
# We'll serve the template from repo's templates/app.html if present; otherwise provide a minimal page.
from flask import send_from_directory, render_template

@app.route("/app.html")
def app_html():
    # try to serve templates/app.html from filesystem if exists (better for customization)
    try:
        return render_template("app.html")
    except Exception:
        # fallback minimal page (shouldn't normally happen if you deployed template)
        return "<p>Mini-app manquante. Place ton fichier templates/app.html</p>", 404

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
    # NFTs via TonAPI (if configured)
    nfts = fetch_nfts_for_wallet(wallet) if wallet else []
    # pioches_total fake for now (bot2 integration can call this with API_SECRET)
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

@app.route("/api/mines")
def api_mines():
    # static list of partner mines — personnalise comme tu veux
    mines = [
        {"title":"Mine 1","url":"https://t.me/mine1"},
        {"title":"Mine 2","url":"https://t.me/mine2"},
        {"title":"Mine 3","url":"https://t.me/mine3"}
    ]
    return jsonify({"mines": mines})

@app.route("/api/avatar/update", methods=["POST"])
def api_avatar_update():
    data = request.get_json() or {}
    uid = int(data.get("uid") or 0)
    if not uid:
        return jsonify({"ok": False, "error": "uid required"}), 400
    hat = data.get("hat", "none")
    jacket = data.get("jacket", "none")
    pants = data.get("pants", "none")
    shoes = data.get("shoes", "none")
    bracelet = data.get("bracelet", "none")
    c.execute("""
        UPDATE users SET hat=?, jacket=?, pants=?, shoes=?, bracelet=? WHERE telegram_id=?
    """, (hat, jacket, pants, shoes, bracelet, uid))
    conn.commit()
    return jsonify({"ok": True})

@app.route("/api/unsubscribe", methods=["POST"])
def api_unsubscribe():
    data = request.get_json() or {}
    uid = int(data.get("uid") or 0)
    if not uid:
        return jsonify({"ok": False, "error": "uid required"}), 400
    c.execute("DELETE FROM users WHERE telegram_id=?", (uid,))
    conn.commit()
    # optional: notify user
    try:
        send_telegram_message_raw(uid, "🗑️ Ton compte a été supprimé.", None)
    except:
        pass
    return jsonify({"ok": True})

# ---------------------------
# Small admin pages (optional) - basic authless dashboard (you can protect later)
# ---------------------------
DASH_HTML = """
<!doctype html><html><head><meta charset="utf-8"><title>Dashboard</title></head><body>
<h2>Users</h2><ul>
{% for u in users %}<li>{{u[0]}} — {{u[1]}} — {{u[2]}}</li>{% endfor %}
</ul></body></html>
"""
@app.route("/dashboard")
def dashboard():
    c.execute("SELECT telegram_id, wallet_address, personal_code FROM users")
    users = c.fetchall()
    return render_template_string(DASH_HTML, users=users)

# ---------------------------
# Telegram bot: minimal handlers (bot sends mini-app via web_app buttons)
# ---------------------------
from telegram import Update
from telegram.ext import ContextTypes

application = Application.builder().token(TELEGRAM_TOKEN).build()

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    # build menu: single button to open WebApp connect (if not registered) or open mini-app (if registered)
    registered = is_user_registered(uid)
    nonce = secrets.token_hex(8)
    if not registered:
        btn = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔗 Connect TON Wallet", web_app=WebAppInfo(url=f"{PUBLIC_BASE_URL}/ton/connect?uid={uid}&nonce={nonce}"))]
        ])
        await update.message.reply_text("Bienvenue — clique pour lier ton wallet TON", reply_markup=btn)
    else:
        btn = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔗 Ouvrir la mini-app", web_app=WebAppInfo(url=f"{PUBLIC_BASE_URL}/app.html?uid={uid}"))]
        ])
        await update.message.reply_text("Tu es inscrit — ouvre la mini-app :", reply_markup=btn)

async def callback_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    # pour l'instant on propose seulement d'ouvrir la mini-app
    uid = query.from_user.id
    await query.edit_message_text("Ouvre la mini-app ci-dessous :", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Ouvrir la mini-app", web_app=WebAppInfo(url=f"{PUBLIC_BASE_URL}/app.html?uid={uid}"))]
    ]))

application.add_handler(CommandHandler("start", start_cmd))
application.add_handler(CallbackQueryHandler(callback_menu, pattern=".*"))

# ---------------------------
# Run Flask + Bot
# ---------------------------
def run_flask():
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))

def run_bot():
    # run polling in current thread (Application.run_polling is blocking)
    application.run_polling()

if __name__ == "__main__":
    # start flask in background thread
    Thread(target=run_flask, daemon=True).start()
    run_bot()
