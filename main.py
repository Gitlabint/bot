# main.py
import os
import sqlite3
import json
import secrets
import requests
from threading import Thread

from flask import (
    Flask, request, jsonify, make_response, redirect,
    render_template, render_template_string, send_from_directory
)
from telegram import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, Update
from telegram.ext import Application, CommandHandler, ContextTypes

# =========================
# Config / env
# =========================
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
PUBLIC_BASE_URL  = os.getenv("PUBLIC_BASE_URL")  # ex: https://monapp.example.com
FLASK_SECRET     = os.getenv("FLASK_SECRET", "please-set-flask-secret")
BOT_USERNAME     = os.getenv("BOT_USERNAME", "")
API_SECRET       = os.getenv("API_SECRET", "")
TONAPI_KEY       = os.getenv("TONAPI_KEY")       # optionnel pour NFTs
BOT2_URL         = os.getenv("BOT2_URL")         # optionnel pour pioches réelles

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN manquant dans l'environnement.")
if not PUBLIC_BASE_URL:
    raise RuntimeError("PUBLIC_BASE_URL manquant (doit être HTTPS public).")

# =========================
# Flask app
# =========================
app = Flask(__name__)
app.secret_key = FLASK_SECRET

# --- Health & favicon (évite les 404 bruyants) ---
@app.route("/")
def home():
    return (
        "<h3>OK</h3>"
        '<p>Manifest: <a href="/ton/manifest.json">/ton/manifest.json</a></p>'
        '<p>Mini-app: /app.html?uid=VOTRE_TELEGRAM_ID</p>',
        200
    )

@app.route("/favicon.ico")
def favicon():
    return ("", 204)

# --- petite icône inline pour le manifest ---
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

# --- TON Connect manifest (+ alias & anti-typo) ---
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

@app.route("/ton/manifest.sjon")  # bouclier typo
def ton_manifest_typo_alias():
    return redirect("/ton/manifest.json", code=302)

# =========================
# DB init
# =========================
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
    bracelet TEXT DEFAULT 'metal'
)
""")
conn.commit()

# =========================
# Helpers
# =========================
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
        c.execute(
            "INSERT INTO users (telegram_id, wallet_address, personal_code) VALUES (?, ?, ?)",
            (tg_id, address, pc)
        )
    conn.commit()

def set_referral_if_new(tg_id: int, referral_code: str | None):
    """Enregistre un parrain si l'utilisateur est nouveau OU si pas encore de parrain."""
    if not referral_code:
        return
    c.execute("SELECT referral_code_used FROM users WHERE telegram_id=?", (tg_id,))
    row = c.fetchone()
    if row is None:
        pc = generate_referral_code()
        c.execute(
            "INSERT INTO users (telegram_id, personal_code, referral_code_used) VALUES (?, ?, ?)",
            (tg_id, pc, referral_code)
        )
        conn.commit()
    elif not row[0]:
        c.execute("UPDATE users SET referral_code_used=? WHERE telegram_id=?", (referral_code, tg_id))
        conn.commit()

def get_user_row(tg_id: int):
    c.execute("""
        SELECT telegram_id, wallet_address, personal_code, referral_code_used,
               hat, jacket, pants, shoes, bracelet
        FROM users WHERE telegram_id=?""", (tg_id,))
    return c.fetchone()

def get_pioches_from_bot2(telegram_id: int) -> int:
    """Optionnel: récupère les pioches depuis Bot2 si BOT2_URL + API_SECRET sont configurés."""
    if not BOT2_URL or not API_SECRET:
        return 0
    try:
        r = requests.get(BOT2_URL, params={"telegram_id": telegram_id, "secret": API_SECRET}, timeout=6)
        if r.status_code == 200:
            data = r.json()
            return int(data.get("total_pioches", 0))
    except Exception:
        pass
    return 0

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
            if image:
                out.append({"name": name, "image": image})
        return out
    except Exception:
        return []

def send_telegram_message_raw(chat_id: int, text: str, reply_markup: dict | None = None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        requests.post(url, json=payload, timeout=6)
    except Exception as e:
        print("send_telegram_message_raw error:", e)

# =========================
# TON Connect page (WebApp ou standalone)
# =========================
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
    .muted{color:#666;font-size:13px}
  </style>
</head>
<body>
  <div class="box">
    <h2>Connect TON Wallet</h2>
    <p class="muted">Connecte ton wallet via TON Connect. Une fois connecté, le bot mettra à jour ton menu automatiquement.</p>
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

# =========================
# Mini-app (app.html) + APIs
# =========================
@app.route("/app.html")
def app_html():
    """
    Sert templates/app.html si présent. Si le fichier manque -> 404 explicite.
    """
    tpl_path = os.path.join(os.getcwd(), "templates")
    app_file = os.path.join(tpl_path, "app.html")
    if os.path.isfile(app_file):
        return send_from_directory(tpl_path, "app.html")
    return "<p>Mini-app manquante. Place ton fichier <code>templates/app.html</code>.</p>", 404

@app.route("/api/config")
def api_config():
    return jsonify({
        "bot_username": BOT_USERNAME,
        "public_base_url": PUBLIC_BASE_URL
    })

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
    pioches_total = get_pioches_from_bot2(uid_i)
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
    hat      = data.get("hat", "none")
    jacket   = data.get("jacket", "none")
    pants    = data.get("pants", "none")
    shoes    = data.get("shoes", "none")
    bracelet = data.get("bracelet", "metal")
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
    try:
        send_telegram_message_raw(uid, "🗑️ Ton compte a été supprimé.")
    except:
        pass
    return jsonify({"ok": True})

# --- ton/submit (POST) — enregistrement & push menu inscrit ---
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

        upsert_user_wallet(uid, addr)

        # push menu minimal : ouvrir la mini-app
        btn = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔗 Ouvrir la mini-app", web_app=WebAppInfo(url=f"{PUBLIC_BASE_URL}/app.html?uid={uid}"))
        ]])
        send_telegram_message_raw(
            uid,
            f"🔗 Wallet TON reçu: <code>{addr}</code>\n✅ Inscription enregistrée.",
            reply_markup={"inline_keyboard": [[
                {"text": "🔗 Ouvrir la mini-app", "web_app": {"url": f"{PUBLIC_BASE_URL}/app.html?uid={uid}"}}
            ]]}
        )

        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/ton/submit", methods=["GET"])
def ton_submit_get():
    return ("POST JSON {uid,address}", 405)

# =========================
# Dashboard simple (optionnel)
# =========================
DASH_HTML = """
<!doctype html><html><head><meta charset="utf-8"><title>Dashboard</title></head><body>
<h2>Users</h2><ul>
{% for u in users %}<li>{{u[0]}} — {{u[1]}} — {{u[2]}} — RefUsed: {{u[3]}}</li>{% endfor %}
</ul></body></html>
"""
@app.route("/dashboard")
def dashboard():
    c.execute("SELECT telegram_id, wallet_address, personal_code, referral_code_used FROM users")
    users = c.fetchall()
    return render_template_string(DASH_HTML, users=users)

# =========================
# Telegram bot
# =========================
application = Application.builder().token(TELEGRAM_TOKEN).build()

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    # Intercepter /start <codeParrain>
    if context.args:
        referral_code = context.args[0].strip()
        if referral_code:
            set_referral_if_new(uid, referral_code)

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

# =========================
# Run Flask + Bot
# =========================
def run_flask():
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))

def run_bot():
    application.run_polling()

if __name__ == "__main__":
    # Démarre Flask dans un thread, puis le bot
    Thread(target=run_flask, daemon=True).start()
    run_bot()
