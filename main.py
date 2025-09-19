# main.py
from flask import Flask, request, redirect, url_for, session, render_template_string, jsonify, make_response
from threading import Thread
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
import sqlite3
import random
import string
import os
import requests
import json
import secrets
from dotenv import load_dotenv

# === Config ===
load_dotenv()
app = Flask('')
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "password")
API_SECRET = os.getenv("API_SECRET")

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")  # ex: https://<ngrok>.ngrok-free.app
BOT_USERNAME = os.getenv("BOT_USERNAME", "")    # ex: MonSuperBot (sans @)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

if not PUBLIC_BASE_URL:
    raise ValueError("PUBLIC_BASE_URL must be set")
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN must be set")

# === Home ===
@app.route('/')
def home():
    return "Bot1 is running!"

# -------- petite icône inline pour le manifest --------
ICON_BYTES = bytes.fromhex(
    "89504E470D0A1A0A0000000D4948445200000001000000010806000000"
    "1F15C4890000000A49444154789C6360000002000154A24F6500000000"
    "49454E44AE426082"
)
@app.route('/static/ton-icon.png')
def ton_icon():
    resp = make_response(ICON_BYTES)
    resp.headers["Content-Type"] = "image/png"
    # On peut laisser long cache sur l'icône
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp

# -------- Réponse manifest avec headers anti-cache + CORS --------
def manifest_response():
    payload = {
        "url": PUBLIC_BASE_URL,
        "name": "Endorisum Bot1",
        "iconUrl": f"{PUBLIC_BASE_URL}/static/ton-icon.png",
        "termsOfUseUrl": f"{PUBLIC_BASE_URL}/terms",
        "privacyPolicyUrl": f"{PUBLIC_BASE_URL}/privacy",
    }
    body = json.dumps(payload, ensure_ascii=False)
    resp = make_response(body)
    resp.headers["Content-Type"] = "application/json; charset=utf-8"
    # Anti-cache (certains wallets/webviews sont agressifs)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    # CORS permissif (AU CAS OÙ)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp

# -------- TON Connect manifest + alias/typos --------
@app.route('/ton/manifest.json', methods=['GET', 'OPTIONS'])
def ton_manifest():
    if request.method == 'OPTIONS':
        return ("", 204)
    return manifest_response()

@app.route('/.well-known/tonconnect-manifest.json', methods=['GET', 'OPTIONS'])
def ton_manifest_wellknown():
    if request.method == 'OPTIONS':
        return ("", 204)
    return manifest_response()

@app.route('/manifest.json', methods=['GET', 'OPTIONS'])
def ton_manifest_root_alias():
    if request.method == 'OPTIONS':
        return ("", 204)
    return manifest_response()

@app.route('/ton/manifest.sjon', methods=['GET', 'OPTIONS'])  # typo shield
def ton_manifest_typo_alias():
    if request.method == 'OPTIONS':
        return ("", 204)
    return redirect('/ton/manifest.json', code=302)

# -------- Page Connect (WebApp Telegram + TON Connect UI) --------
CONNECT_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Connect TON Wallet</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <!-- Telegram WebApp SDK -->
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <!-- TON Connect UI -->
  <script src="https://unpkg.com/@tonconnect/ui@latest/dist/tonconnect-ui.min.js"></script>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; padding: 20px; background:#fafafa;}
    .box { max-width: 560px; margin: 24px auto; padding: 16px; border: 1px solid #e5e5e5; border-radius: 12px; background:white; }
    .row { margin: 12px 0; }
    button { padding: 10px 14px; cursor: pointer; border-radius:8px; border:1px solid #ddd; background:#fff; }
    #error { color:#b00020; display:none; }
    #status { color:#333; }
    .hint { color:#666; font-size:14px; }
    .center { text-align:center; }
    code { background:#f2f2f2; padding:2px 4px; border-radius:4px; }
  </style>
</head>
<body>
  <div class="box">
    <h2>Connect TON Wallet</h2>
    <p class="hint">Clique sur le bouton ci-dessous pour connecter ton wallet via TON Connect.</p>

    <div id="ton-connect" class="row center"></div>
    <div class="row center">
      <button id="openModalBtn" style="display:none;">🔗 Ouvrir la liste des wallets</button>
    </div>

    <div id="status" class="row"></div>
    <div id="error" class="row"></div>

    <div class="row hint">Manifest utilisé : <code id="mf"></code></div>
    <div class="row hint">Cette page est ouverte dans Telegram (WebApp). Elle se fermera automatiquement quand l'inscription sera validée.</div>

    <div class="row center hint">
      <a id="fallbackLink" href="#" target="_blank" rel="noreferrer" style="display:none;">Essayer l’ouverture directe du wallet</a>
    </div>
  </div>

  <script>
    (function(){
      const uid = "{{uid}}";
      const nonce = "{{nonce}}";
      const manifestAbs = "{{ manifest_abs }}"; // sans ?v
      const manifestWithV = manifestAbs + "?v=" + encodeURIComponent(nonce); // cache-buster
      const mountId = "ton-connect";
      const statusEl = document.getElementById("status");
      const errorEl  = document.getElementById("error");
      const openBtn  = document.getElementById("openModalBtn");
      const fallback = document.getElementById("fallbackLink");
      document.getElementById("mf").textContent = manifestWithV;

      function showError(msg) {
        errorEl.style.display = "block";
        errorEl.textContent = "Erreur: " + msg;
      }
      function showStatus(msg) { statusEl.textContent = msg; }
      function buildFallbackUniversalLink() {
        const url = "https://tonkeeper.com/"; // fallback générique
        fallback.href = url;
        fallback.style.display = "inline-block";
      }

      async function pingManifest() {
        try {
          const r = await fetch(manifestWithV, {cache: "no-store"});
          if (!r.ok) throw new Error("manifest HTTP " + r.status);
          await r.json();
          return true;
        } catch(e) {
          showError("Manifest injoignable (" + e.message + "). Vérifie PUBLIC_BASE_URL & HTTPS.");
          return false;
        }
      }

      async function closeWebAppSoon() {
        try {
          if (window.Telegram && Telegram.WebApp) {
            Telegram.WebApp.HapticFeedback.impactOccurred('light');
            setTimeout(() => Telegram.WebApp.close(), 400);
          } else {
            setTimeout(() => window.close(), 400);
          }
        } catch(e) {}
      }

      async function init() {
        if (typeof TON_CONNECT_UI === "undefined" || !TON_CONNECT_UI.TonConnectUI) {
          buildFallbackUniversalLink();
          showError("La librairie TON Connect UI n'a pas été chargée (CDN).");
          return;
        }
        const ok = await pingManifest();
        if (!ok) {
          buildFallbackUniversalLink();
          openBtn.style.display = "inline-block";
        }

        let ui;
        try {
          ui = new TON_CONNECT_UI.TonConnectUI({
            manifestUrl: manifestWithV, // cache-buster actif
            buttonRootId: mountId,
          });
        } catch (e) {
          showError("Instantiation TonConnectUI a échoué: " + e.message);
          buildFallbackUniversalLink();
          openBtn.style.display = "inline-block";
          return;
        }

        setTimeout(() => {
          const container = document.getElementById(mountId);
          if (!container || container.children.length === 0) {
            openBtn.style.display = "inline-block";
          }
        }, 600);

        openBtn.addEventListener("click", () => {
          try { ui.openModal(); } catch(e) {}
        });

        ui.onStatusChange(async (walletInfo) => {
          if (!walletInfo || !walletInfo.account) return;
          const address = walletInfo.account.address;
          showStatus("Connecté ✔ Adresse: " + address);

          try {
            const r = await fetch("/ton/submit", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ uid: uid, nonce: nonce, address: address })
            });
            const data = await r.json();
            if (data.ok) {
              showStatus("Connecté ✔ Adresse: " + address + " — Enregistré ✅");
              await closeWebAppSoon(); // retourne au chat Telegram
            } else {
              showError(data.error || "Enregistrement impossible");
            }
          } catch (e) {
            showError("Erreur réseau lors de l'enregistrement.");
          }
        });
      }

      if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
      } else {
        init();
      }
    })();
  </script>
</body>
</html>
"""

def _manifest_abs_url():
    return f"{PUBLIC_BASE_URL}/ton/manifest.json"

@app.route("/ton/connect")
def ton_connect_page():
    uid = request.args.get("uid", "").strip()
    nonce = request.args.get("nonce", "").strip()
    if not uid.isdigit() or not nonce:
        return "Paramètres invalides", 400
    return render_template_string(CONNECT_HTML, uid=uid, nonce=nonce, manifest_abs=_manifest_abs_url())

# -------- Login --------
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form.get('username') == ADMIN_USER and request.form.get('password') == ADMIN_PASS:
            session['admin'] = True
            return redirect(url_for('dashboard'))
        return render_template_string(LOGIN_TEMPLATE, error="Mauvais identifiants")
    return render_template_string(LOGIN_TEMPLATE)

@app.route('/logout')
def logout():
    session.pop('admin', None)
    return redirect(url_for('login'))

# -------- Dashboard --------
@app.route('/dashboard')
def dashboard():
    if not session.get('admin'):
        return redirect(url_for('login'))

    cursor.execute("SELECT telegram_id, wallet_address, personal_code, referral_code_used FROM users")
    users = cursor.fetchall()
    users_data = []
    for u in users:
        telegram_id, wallet, personal_code, ref_used = u
        total_pioches, _ = get_pioches_from_bot2(telegram_id)
        users_data.append({
            "telegram_id": telegram_id,
            "wallet": wallet,
            "personal_code": personal_code,
            "referral_code_used": ref_used or "Aucun",
            "total_pioches": total_pioches
        })

    return render_template_string(DASHBOARD_TEMPLATE, users=users_data)

@app.route('/user/<int:telegram_id>')
def user_detail(telegram_id):
    if not session.get('admin'):
        return redirect(url_for('login'))

    total, details = get_pioches_from_bot2(telegram_id)
    cursor.execute("SELECT wallet_address, personal_code, referral_code_used FROM users WHERE telegram_id=?", (telegram_id,))
    row = cursor.fetchone()
    return render_template_string(DETAIL_TEMPLATE,
        telegram_id=telegram_id,
        wallet=row[0] if row else None, personal_code=row[1] if row else None, ref_used=row[2] if row else None,
        total_pioches=total, details=details
    )

# -------- API pour notif depuis Bot2 --------
@app.route('/notify_user', methods=["POST"])
def notify_user():
    data = request.get_json()
    if not data or data.get("secret") != API_SECRET:
        return {"error": "unauthorized"}, 403
    telegram_id = data.get("telegram_id")
    if not telegram_id:
        return {"error": "telegram_id required"}, 400
    send_telegram_message(int(telegram_id), "✅ Ton bloc a bien été miné suite à ta pioche !")
    return {"status": "ok"}

# === Flask setup ===
app.secret_key = os.getenv("FLASK_SECRET")
if not app.secret_key:
    raise ValueError("FLASK_SECRET must be set in environment variables")

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    Thread(target=run).start()

# === SQLite ===
DB_FILE = "bot.db"
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
cursor = conn.cursor()
cursor.execute('''
CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    wallet_address TEXT,
    personal_code TEXT UNIQUE,
    referral_code_used TEXT
)
''')
conn.commit()

# === Utils ===
def generate_referral_code(length=6):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

def add_user(user_data):
    personal_code = generate_referral_code()
    cursor.execute('''
    INSERT OR IGNORE INTO users (telegram_id, wallet_address, personal_code, referral_code_used)
    VALUES (?, ?, ?, ?)
    ''', (
        user_data["telegram_id"],
        user_data.get("wallet_id"),
        personal_code,
        user_data.get("referral_code_used")
    ))
    conn.commit()

def is_user_registered(user_id):
    cursor.execute("SELECT 1 FROM users WHERE telegram_id=?", (user_id,))
    return cursor.fetchone() is not None

def remove_user(user_id):
    cursor.execute("DELETE FROM users WHERE telegram_id=?", (user_id,))
    conn.commit()

def get_personal_code(user_id):
    cursor.execute("SELECT personal_code FROM users WHERE telegram_id=?", (user_id,))
    row = cursor.fetchone()
    return row[0] if row else None

# === Appel API Bot2 ===
def get_pioches_from_bot2(telegram_id):
    url = os.getenv("BOT2_URL")
    secret = API_SECRET
    try:
        resp = requests.get(url, params={"telegram_id": telegram_id, "secret": secret}, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("total_pioches", 0), data.get("details", [])
    except Exception as e:
        print("Erreur API Bot2:", e)
    return 0, []

def send_telegram_message(user_id, text, reply_markup=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": user_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print("Erreur envoi message:", e)

# === Helpers pour pousser le menu via HTTP (pas PTB) ===
def main_menu_text_only(is_registered: bool) -> str:
    return "Bienvenue dans le bot de minage !\n\nQue souhaites-tu faire ?"

def build_menu_markup_dict(is_registered: bool) -> dict:
    if not is_registered:
        return {
            "inline_keyboard": [
                [{"text": "🚀 Inscription", "callback_data": "inscription"}],
                [{"text": "🤝 Referral", "callback_data": "referral"}],
                [{"text": "🏭 Mines", "callback_data": "mines"}],
                [{"text": "📊 Profil", "callback_data": "profil"}],
                [{"text": "❌ Désinscription", "callback_data": "unsubscribe"}],
            ]
        }
    else:
        return {
            "inline_keyboard": [
                [{"text": "🤝 Referral", "callback_data": "referral"}, {"text": "🏭 Mines", "callback_data": "mines"}],
                [{"text": "📊 Profil", "callback_data": "profil"}, {"text": "⚙️ Paramètres", "callback_data": "settings"}],
                [{"text": "❌ Désinscription", "callback_data": "unsubscribe"}],
            ]
        }

# === Templates HTML simples ===
LOGIN_TEMPLATE = '''
<h2>Connexion Admin</h2>
{% if error %}<p style="color:red">{{ error }}</p>{% endif %}
<form method="post">
<input name="username" placeholder="user"><br>
<input name="password" type="password" placeholder="pass"><br>
<input type="submit" value="Connexion">
</form>
'''

DASHBOARD_TEMPLATE = '''
<h2>Dashboard</h2>
<a href="/logout">Déconnexion</a>
<table border=1>
<tr><th>ID</th><th>Wallet</th><th>Code</th><th>Referral</th><th>Pioches</th><th>Action</th></tr>
{% for u in users %}
<tr>
<td>{{u.telegram_id}}</td><td>{{u.wallet}}</td><td>{{u.personal_code}}</td>
<td>{{u.referral_code_used}}</td><td>{{u.total_pioches}}</td>
<td><a href="/user/{{u.telegram_id}}">Détails</a></td>
</tr>
{% endfor %}
</table>
'''

DETAIL_TEMPLATE = '''
<h2>Détail utilisateur {{telegram_id}}</h2>
<p>Wallet: {{wallet}}<br>Code: {{personal_code}}<br>Referral: {{ref_used}}<br>Total Pioches: {{total_pioches}}</p>
<table border=1>
<tr><th>Chat</th><th>Message</th><th>Timestamp</th><th>Delta</th></tr>
{% for d in details %}
<tr><td>{{d.chat_id}}</td><td>{{d.message_id}}</td><td>{{d.timestamp}}</td><td>{{d.delta}}</td></tr>
{% endfor %}
</table>
'''

# === Enregistrement côté serveur (POST) — pousse le menu inscrit immédiatement ===
@app.route("/ton/submit", methods=["POST", "OPTIONS"])
def ton_submit():
    if request.method == "OPTIONS":
        # CORS preflight (si nécessaire)
        resp = make_response("", 204)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return resp

    try:
        data = request.get_json() or {}
        uid = int(data.get("uid") or 0)
        address = (data.get("address") or "").strip()
        if not uid or not address:
            return jsonify({"ok": False, "error": "missing"}), 400

        # Enregistrement en BDD
        cursor.execute("SELECT personal_code FROM users WHERE telegram_id=?", (uid,))
        row = cursor.fetchone()
        if row:
            cursor.execute("UPDATE users SET wallet_address=? WHERE telegram_id=?", (address, uid))
        else:
            pc = generate_referral_code()
            cursor.execute("""
                INSERT INTO users (telegram_id, wallet_address, personal_code, referral_code_used)
                VALUES (?, ?, ?, ?)
            """, (uid, address, pc, None))
        conn.commit()

        # Pousser directement le menu "inscrit"
        menu_text = main_menu_text_only(True)
        menu_kb   = build_menu_markup_dict(True)
        final_text = f"🔗 Wallet TON reçu: {address}\n✅ Inscription enregistrée.\n\n{menu_text}"
        send_telegram_message(uid, final_text, menu_kb)

        # CORS permissif dans la réponse JSON
        resp = make_response(json.dumps({"ok": True}))
        resp.headers["Content-Type"] = "application/json; charset=utf-8"
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp
    except Exception as e:
        resp = make_response(json.dumps({"ok": False, "error": str(e)}))
        resp.headers["Content-Type"] = "application/json; charset=utf-8"
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp, 500

# GET informatif pour éviter 405 si ouvert dans le navigateur
@app.route("/ton/submit", methods=["GET"])
def ton_submit_get():
    return ("This endpoint accepts POST with JSON {uid, address}.", 405)

# === Menus dynamiques Bot Telegram (PTB) ===
def main_menu(is_registered):
    if not is_registered:
        return ("Bienvenue dans le bot de minage !\n\nQue souhaites-tu faire ?",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🚀 Inscription", callback_data="inscription")],
                [InlineKeyboardButton("🤝 Referral", callback_data="referral")],
                [InlineKeyboardButton("🏭 Mines", callback_data="mines")],
                [InlineKeyboardButton("📊 Profil", callback_data="profil")],
                [InlineKeyboardButton("❌ Désinscription", callback_data="unsubscribe")]
            ]))
    else:
        return ("Bienvenue dans le bot de minage !\n\nQue souhaites-tu faire ?",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🤝 Referral", callback_data="referral"), InlineKeyboardButton("🏭 Mines", callback_data="mines")],
                [InlineKeyboardButton("📊 Profil", callback_data="profil"), InlineKeyboardButton("⚙️ Paramètres", callback_data="settings")],
                [InlineKeyboardButton("❌ Désinscription", callback_data="unsubscribe")]
            ]))

def settings_menu():
    return ("⚙️ Paramètres", InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Time Zone", callback_data="timezone"), InlineKeyboardButton("🇺🇸 Language", callback_data="language")],
        [InlineKeyboardButton("💱 Currency", callback_data="currency")],
        [InlineKeyboardButton("⬅️ Retour", callback_data="back_main")]
    ]))

def mines_menu():
    return ("⛏️ Choisis une mine partenaire :", InlineKeyboardMarkup([
        [InlineKeyboardButton("Mine 1", url="https://t.me/mine1")],
        [InlineKeyboardButton("Mine 2", url="https://t.me/mine2")],
        [InlineKeyboardButton("Mine 3", url="https://t.me/mine3")],
        [InlineKeyboardButton("⬅️ Retour", callback_data="back_main")]
    ]))

def referral_menu():
    return ("🤝 Parrainage\n\nCette fonctionnalité sera bientôt disponible.", InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Retour", callback_data="back_main")]
    ]))

def profil_menu(wallet, personal_code, ref_used, total_pioches):
    return (f"👤 Profil utilisateur\n\n💼 Wallet : {wallet or '—'}\n🎁 Code de parrainage : {personal_code}\n👥 Code utilisé : {ref_used or 'Aucun'}\n⛏️ Pioches totales : {total_pioches}",
        InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Retour", callback_data="back_main")]]))

# === Bot Telegram ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    is_registered = is_user_registered(user_id)
    text, keyboard = main_menu(is_registered)
    await update.message.reply_text(text, reply_markup=keyboard)

async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    action = query.data

    if action == "inscription":
        # Ouvre la page TON Connect dans la WebApp Telegram
        nonce = secrets.token_hex(8)
        connect_url = f"{PUBLIC_BASE_URL}/ton/connect?uid={user_id}&nonce={nonce}"
        btn = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔗 Connect TON Wallet", web_app=WebAppInfo(url=connect_url))],
            [InlineKeyboardButton("⬅️ Retour", callback_data="back_main")]
        ])
        await query.edit_message_text(
            "Inscription via TON Connect.\nClique sur le bouton pour lier ton wallet.",
            reply_markup=btn
        )

    elif action == "referral":
        text, keyboard = referral_menu()
        await query.edit_message_text(text, reply_markup=keyboard)

    elif action == "mines":
        text, keyboard = mines_menu()
        await query.edit_message_text(text, reply_markup=keyboard)

    elif action == "profil":
        cursor.execute("SELECT wallet_address, personal_code, referral_code_used FROM users WHERE telegram_id=?", (user_id,))
        row = cursor.fetchone()
        if row:
            wallet, personal_code, ref_used = row
            total_pioches, _ = get_pioches_from_bot2(user_id)
            text, keyboard = profil_menu(wallet, personal_code, ref_used, total_pioches)
            await query.edit_message_text(text, reply_markup=keyboard)
        else:
            await query.edit_message_text("❌ Tu n'es pas encore inscrit.", reply_markup=main_menu(False)[1])

    elif action == "unsubscribe":
        await query.edit_message_text(
            "⚠️ Es-tu sûr de vouloir te désinscrire ? Tu perdras tous tes rewards.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Oui, je confirme", callback_data="confirm_unsubscribe")],
                [InlineKeyboardButton("❌ Annuler", callback_data="back_main")]
            ])
        )

    elif action == "confirm_unsubscribe":
        remove_user(user_id)
        text, keyboard = main_menu(False)
        await query.edit_message_text("🗑️ Tu as bien été désinscrit.", reply_markup=keyboard)

    elif action == "settings":
        text, keyboard = settings_menu()
        await query.edit_message_text(text, reply_markup=keyboard)

    elif action == "back_main":
        text, keyboard = main_menu(is_user_registered(user_id))
        await query.edit_message_text(text, reply_markup=keyboard)

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # pas de saisie manuelle de wallet ; on ne fait rien ici
    pass

def start_bot():
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(menu_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    application.run_polling()

# === Lancement ===
if __name__ == "__main__":
    app.secret_key = os.getenv("FLASK_SECRET")
    if not app.secret_key:
        raise ValueError("FLASK_SECRET must be set in environment variables")
    keep_alive()  # Flask
    start_bot()   # Telegram
