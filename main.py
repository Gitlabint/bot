# main.py
import os
import json
import sqlite3
import secrets
import requests
from threading import Thread
from flask import (
    Flask, request, jsonify, render_template, render_template_string,
    redirect, make_response
)
from telegram import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# =========================
# Config / ENV
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")  # https://ton-domaine (HTTPS)
FLASK_SECRET   = os.getenv("FLASK_SECRET", "please-set-flask-secret")
BOT_USERNAME   = os.getenv("BOT_USERNAME", "")  # sans @, ex: Labail_bot
API_SECRET     = os.getenv("API_SECRET", "")
BOT2_URL       = os.getenv("BOT2_URL", "")      # ex: https://bot2.example.com/pioche
TONAPI_KEY     = os.getenv("TONAPI_KEY", "")    # optionnel (TonAPI)

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN manquant.")
if not PUBLIC_BASE_URL:
    raise RuntimeError("PUBLIC_BASE_URL manquant (doit être HTTPS).")
if not BOT_USERNAME:
    raise RuntimeError("BOT_USERNAME manquant (sans @).")

# =========================
# Flask
# =========================
app = Flask(__name__)
app.secret_key = FLASK_SECRET

@app.route("/")
def root_ok():
    return "OK", 200

# Petite icône inline pour le manifest
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

# Manifest TON Connect
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
def ton_manifest_wk():
    return ton_manifest()

@app.route("/manifest.json")
def ton_manifest_alias():
    return ton_manifest()

@app.route("/ton/manifest.sjon")
def ton_manifest_typo():
    return redirect("/ton/manifest.json", code=302)

# =========================
# DB
# =========================
DB_FILE = os.getenv("DB_FILE", "bot.db")
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
c = conn.cursor()
c.execute("""
CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    username TEXT,
    wallet_address TEXT,
    personal_code TEXT UNIQUE,
    referral_code_used TEXT,      -- code du parrain utilisé (personal_code du parrain)
    trophies_total INTEGER DEFAULT 0,
    hat TEXT DEFAULT 'none',
    jacket TEXT DEFAULT 'none',
    pants TEXT DEFAULT 'none',
    shoes TEXT DEFAULT 'none',
    bracelet TEXT DEFAULT 'metal',
    profile_photo_path TEXT
)
""")
conn.commit()

def generate_referral_code(length=6):
    import random, string
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

def is_user_registered(uid:int) -> bool:
    c.execute("SELECT 1 FROM users WHERE telegram_id=?", (uid,))
    return c.fetchone() is not None

def get_user(uid:int):
    c.execute("""SELECT telegram_id, username, wallet_address, personal_code,
                        referral_code_used, trophies_total, hat, jacket, pants,
                        shoes, bracelet, profile_photo_path
                 FROM users WHERE telegram_id=?""", (uid,))
    return c.fetchone()

def upsert_user(uid:int, username:str=None):
    row = get_user(uid)
    if row:
        if username and username != row[1]:
            c.execute("UPDATE users SET username=? WHERE telegram_id=?", (username, uid))
            conn.commit()
        return
    # create new user with a personal_code (parrainage)
    pc = generate_referral_code()
    c.execute("INSERT INTO users (telegram_id, username, personal_code) VALUES (?, ?, ?)", (uid, username, pc))
    conn.commit()

def set_referral_if_empty(uid:int, code:str):
    if not code:
        return
    # ne pas se parrainer soi-même
    c.execute("SELECT personal_code FROM users WHERE telegram_id=?", (uid,))
    me = c.fetchone()
    if me and me[0] == code:
        return
    # si déjà renseigné, on ne change pas
    c.execute("SELECT referral_code_used FROM users WHERE telegram_id=?", (uid,))
    cur = c.fetchone()
    if cur and cur[0]:
        return
    # vérifier que le code existe
    c.execute("SELECT telegram_id FROM users WHERE personal_code=?", (code,))
    inv = c.fetchone()
    if not inv:
        return
    c.execute("UPDATE users SET referral_code_used=? WHERE telegram_id=?", (code, uid))
    conn.commit()

def set_wallet(uid:int, address:str):
    c.execute("UPDATE users SET wallet_address=? WHERE telegram_id=?", (address, uid))
    conn.commit()

def update_trophies(uid:int, total:int):
    c.execute("UPDATE users SET trophies_total=? WHERE telegram_id=?", (total, uid))
    conn.commit()

def inviter_username_from_code(code:str):
    if not code:
        return None
    c.execute("SELECT username FROM users WHERE personal_code=?", (code,))
    row = c.fetchone()
    return row[0] if row and row[0] else None

# =========================
# Bot2: récupération trophées
# =========================
def get_pioches_from_bot2(telegram_id:int) -> int:
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

# =========================
# TonAPI NFTs (optionnel)
# =========================
def fetch_nfts_for_wallet(address:str):
    if not address or not TONAPI_KEY:
        return []
    try:
        url = f"https://tonapi.io/v2/accounts/{address}/nfts"
        headers = {"Authorization": f"Bearer {TONAPI_KEY}"}
        r = requests.get(url, headers=headers, timeout=8)
        if r.status_code != 200:
            return []
        data = r.json()
        items = data.get("nft_items") or data.get("data") or []
        out = []
        for it in items:
            meta = it.get("metadata") or {}
            image = meta.get("image") or it.get("image") or ""
            name  = meta.get("name")  or it.get("name")  or "NFT"
            if image:
                out.append({"name": name, "image": image})
        return out
    except Exception:
        return []

# =========================
# Photo de profil Telegram
# =========================
def get_profile_photo_url(uid:int) -> str | None:
    # si déjà en DB → renvoie l’URL directe
    c.execute("SELECT profile_photo_path FROM users WHERE telegram_id=?", (uid,))
    row = c.fetchone()
    if row and row[0]:
        return f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{row[0]}"

    # sinon on va chercher via Bot API (prend la première photo)
    try:
        # getUserProfilePhotos
        r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUserProfilePhotos",
                         params={"user_id": uid, "limit": 1}, timeout=6)
        jr = r.json()
        photos = (jr.get("result") or {}).get("photos") or []
        if not photos:
            return None
        # getFile pour la plus grande taille
        file_id = photos[0][-1]["file_id"]
        rf = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile",
                          params={"file_id": file_id}, timeout=6).json()
        file_path = (rf.get("result") or {}).get("file_path")
        if not file_path:
            return None
        # on garde en DB
        c.execute("UPDATE users SET profile_photo_path=? WHERE telegram_id=?", (file_path, uid))
        conn.commit()
        return f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
    except Exception:
        return None

# =========================
# Envoi de message brut (HTTP)
# =========================
def send_telegram_message_raw(chat_id:int, text:str, reply_markup:dict|None=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        requests.post(url, json=payload, timeout=6)
    except Exception as e:
        print("send_telegram_message_raw error:", e)

def menu_for(uid:int, registered:bool):
    if not registered:
        nonce = secrets.token_hex(8)
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🔗 Connect TON Wallet",
              web_app=WebAppInfo(url=f"{PUBLIC_BASE_URL}/ton/connect?uid={uid}&nonce={nonce}"))]
        ])
    else:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🔎 Ouvrir la mini-app",
              web_app=WebAppInfo(url=f"{PUBLIC_BASE_URL}/app.html?uid={uid}"))]
        ])

# =========================
# Page TON Connect (WebApp). Redirige vers app.html après enregistrement.
# =========================
CONNECT_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Connect TON Wallet</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <script src="https://unpkg.com/@tonconnect/ui@latest/dist/tonconnect-ui.min.js"></script>
  <style>
    body{font-family:Inter,system-ui,-apple-system,Segoe UI,Roboto,Arial;background:#0f1115;color:#e8eefb;margin:0}
    .box{max-width:720px;margin:24px auto;background:#161a22;border:1px solid #2a3244;border-radius:14px;padding:16px}
    .muted{color:#9aa7bd;font-size:13px}
  </style>
</head>
<body>
  <div class="box">
    <h2>Connect TON Wallet</h2>
    <p class="muted">Connecte ton wallet. Tu seras redirigé automatiquement vers la mini-app.</p>
    <div id="ton-connect"></div>
    <div id="status" style="margin-top:12px;color:#9ae6b4"></div>
    <div id="error" style="margin-top:12px;color:#feb2b2"></div>
  </div>
  <script>
  (function(){
    const uid = "{{uid}}";
    const nonce = "{{nonce}}";
    const ref   = "{{ref}}";
    const manifest = "{{manifest}}";
    const statusEl = document.getElementById("status");
    const errorEl  = document.getElementById("error");

    async function init(){
      if (!window.TON_CONNECT_UI || !TON_CONNECT_UI.TonConnectUI){
        errorEl.textContent = "Librairie TON Connect introuvable.";
        return;
      }
      try{
        const ui = new TON_CONNECT_UI.TonConnectUI({
          manifestUrl: manifest,
          buttonRootId: "ton-connect"
        });
        ui.onStatusChange(async (w)=>{
          if (!w || !w.account) return;
          const addr = w.account.address;
          statusEl.textContent = "Connecté — " + addr;
          try{
            const r = await fetch("/ton/submit", {
              method:"POST", headers:{"Content-Type":"application/json"},
              body: JSON.stringify({ uid, nonce, address: addr, ref })
            });
            const d = await r.json();
            if (d.ok){
              window.location.replace("/app.html?uid="+uid);
            } else {
              errorEl.textContent = d.error || "Erreur enregistrement.";
            }
          }catch(e){
            errorEl.textContent = "Erreur réseau lors de l'enregistrement.";
          }
        });
      }catch(e){ errorEl.textContent = e.message; }
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
    uid   = request.args.get("uid","").strip()
    nonce = request.args.get("nonce","").strip()
    ref   = request.args.get("ref","").strip()
    if not uid.isdigit() or not nonce:
        return "Paramètres invalides", 400
    return render_template_string(
        CONNECT_HTML,
        uid=uid, nonce=nonce, ref=ref,
        manifest=f"{PUBLIC_BASE_URL}/ton/manifest.json"
    )

# =========================
# Enregistrement TON (POST) + auto-ouverture mini-app
# =========================
@app.route("/ton/submit", methods=["POST","OPTIONS"])
def ton_submit():
    if request.method == "OPTIONS":
        return ("", 204)
    try:
        data = request.get_json() or {}
        uid = int(data.get("uid") or 0)
        address = (data.get("address") or "").strip()
        ref = (data.get("ref") or "").strip()
        if not uid or not address:
            return jsonify({"ok": False, "error": "missing uid/address"}), 400

        # init user si besoin
        upsert_user(uid)

        # appliquer parrainage si nouveau et code valide
        set_referral_if_empty(uid, ref)

        # enregistrer l’adresse
        set_wallet(uid, address)

        # récupérer trophies depuis Bot2 & stocker
        total = get_pioches_from_bot2(uid)
        update_trophies(uid, total)

        # pousser un message (au cas où la WebView se ferme)
        send_telegram_message_raw(
            uid,
            f"🔗 Wallet enregistré : <code>{address}</code>\n✅ Bienvenue !",
            reply_markup={"inline_keyboard":[
                [{"text":"🔎 Ouvrir la mini-app","web_app":{"url": f"{PUBLIC_BASE_URL}/app.html?uid={uid}"}}]
            ]}
        )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/ton/submit", methods=["GET"])
def ton_submit_get():
    return ("POST JSON {uid,address,ref}", 405)

# =========================
# API Mini-app
# =========================
@app.route("/app.html")
def app_html():
    return render_template("app.html")  # templates/app.html

@app.route("/api/me")
def api_me():
    uid = request.args.get("uid","").strip()
    if not uid.isdigit():
        return jsonify({"registered": False})
    uid_i = int(uid)
    row = get_user(uid_i)
    if not row:
        return jsonify({"registered": False})

    (telegram_id, username, wallet, personal_code, ref_used,
     trophies_total, hat, jacket, pants, shoes, bracelet, _photo_path) = row

    # rafraîchir trophies depuis Bot2 à l’ouverture de la mini-app
    total = get_pioches_from_bot2(uid_i)
    if total != trophies_total:
        update_trophies(uid_i, total)
        trophies_total = total

    # inviter (username) si parrainage
    invited_by = inviter_username_from_code(ref_used)

    # photo de profil
    photo_url = get_profile_photo_url(uid_i)

    # NFT réels (si clé TONAPI)
    nfts = fetch_nfts_for_wallet(wallet) if wallet else []

    return jsonify({
        "registered": True,
        "bot_username": BOT_USERNAME,
        "telegram_id": telegram_id,
        "username": username,
        "profile_photo_url": photo_url,
        "wallet_address": wallet,
        "personal_code": personal_code,
        "referral_code_used": ref_used,
        "invited_by_username": invited_by,
        "pioches_total": trophies_total,
        "avatar": {"hat": hat, "jacket": jacket, "pants": pants, "shoes": shoes, "bracelet": bracelet},
        "nfts": nfts
    })

@app.route("/api/mines")
def api_mines():
    mines = [
        {"title":"Mine 1","url":"https://t.me/mine1"},
        {"title":"Mine 2","url":"https://t.me/mine2"},
        {"title":"Mine 3","url":"https://t.me/mine3"},
    ]
    return jsonify({"mines": mines})

@app.route("/api/avatar/update", methods=["POST"])
def api_avatar_update():
    data = request.get_json() or {}
    uid = int(data.get("uid") or 0)
    if not uid: return jsonify({"ok": False, "error":"uid required"}), 400
    hat = data.get("hat","none"); jacket=data.get("jacket","none")
    pants=data.get("pants","none"); shoes=data.get("shoes","none")
    bracelet = data.get("bracelet","metal")
    c.execute("""UPDATE users
                 SET hat=?, jacket=?, pants=?, shoes=?, bracelet=?
                 WHERE telegram_id=?""",
              (hat, jacket, pants, shoes, bracelet, uid))
    conn.commit()
    return jsonify({"ok": True})

@app.route("/api/unsubscribe", methods=["POST"])
def api_unsubscribe():
    data = request.get_json() or {}
    uid = int(data.get("uid") or 0)
    if not uid: return jsonify({"ok": False, "error":"uid required"}), 400
    c.execute("DELETE FROM users WHERE telegram_id=?", (uid,))
    conn.commit()
    try:
        send_telegram_message_raw(uid, "🗑️ Ton compte a été supprimé.")
    except: pass
    return jsonify({"ok": True})

# =========================
# Dashboard ultra simple (optionnel)
# =========================
DASH_HTML = """
<!doctype html><html><head><meta charset="utf-8"><title>Dashboard</title></head><body>
<h2>Users</h2>
<table border="1" cellpadding="6">
<tr><th>tg_id</th><th>username</th><th>wallet</th><th>code</th><th>ref_used</th><th>trophies</th></tr>
{% for u in users %}
<tr><td>{{u[0]}}</td><td>{{u[1]}}</td><td>{{u[2]}}</td><td>{{u[3]}}</td><td>{{u[4]}}</td><td>{{u[5]}}</td></tr>
{% endfor %}
</table>
</body></html>
"""
@app.route("/dashboard")
def dashboard():
    c.execute("SELECT telegram_id, username, wallet_address, personal_code, referral_code_used, trophies_total FROM users")
    users = c.fetchall()
    return render_template_string(DASH_HTML, users=users)

# =========================
# Telegram Bot
# =========================
application = Application.builder().token(TELEGRAM_TOKEN).build()

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id
    username = user.username
    # upsert de base (crée personal_code si nouveau)
    upsert_user(uid, username)

    # deep-link /start <payload> ou /start startapp=... → on accepte tout
    payload = ""
    if update.message and update.message.text:
        parts = update.message.text.split(maxsplit=1)
        if len(parts) == 2:
            payload = parts[1].strip()
            # si c’est "startapp=CODE", on isole CODE
            if payload.startswith("startapp="):
                payload = payload.split("=",1)[1]

    # appliquer parrainage si présent
    if payload:
        set_referral_if_empty(uid, payload)

    registered = is_user_registered(uid) and (get_user(uid)[2] is not None)  # wallet présent ?
    if not registered:
        # montrer Connect + ouvrir mini-app automatiquement après /ton/submit
        nonce = secrets.token_hex(8)
        # si parrainage existant, on passe ref dans l’URL pour CONNECT
        c.execute("SELECT referral_code_used FROM users WHERE telegram_id=?", (uid,))
        ref = (c.fetchone() or [None])[0] or ""
        btn = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔗 Connect TON Wallet",
              web_app=WebAppInfo(url=f"{PUBLIC_BASE_URL}/ton/connect?uid={uid}&nonce={nonce}&ref={ref}"))]
        ])
        await update.message.reply_text(
            "Bienvenue 👋 — Connecte ton wallet TON pour t’inscrire.",
            reply_markup=btn
        )
    else:
        btn = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔎 Ouvrir la mini-app",
              web_app=WebAppInfo(url=f"{PUBLIC_BASE_URL}/app.html?uid={uid}"))]
        ])
        await update.message.reply_text("Heureux de te revoir — mini-app ci-dessous :", reply_markup=btn)

# (Callback générique si besoin)
async def any_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    await query.edit_message_text(
        "Ouvre la mini-app ci-dessous :",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔎 Ouvrir la mini-app",
              web_app=WebAppInfo(url=f"{PUBLIC_BASE_URL}/app.html?uid={uid}"))]
        ])
    )

application.add_handler(CommandHandler("start", start_cmd))
application.add_handler(CallbackQueryHandler(any_callback, pattern=".*"))

# =========================
# Run
# =========================
def run_flask():
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","8080")))

def run_bot():
    application.run_polling()

if __name__ == "__main__":
    Thread(target=run_flask, daemon=True).start()
    run_bot()
