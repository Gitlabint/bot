# main.py
import os
import json
import time
import html
import secrets
import sqlite3
import threading
import requests
from flask import Flask, request, redirect, url_for, session, render_template_string, jsonify, make_response
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

# =========================================================
# Config
# =========================================================
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
FLASK_SECRET     = os.getenv("FLASK_SECRET")
PUBLIC_BASE_URL  = os.getenv("PUBLIC_BASE_URL")  # ex: https://tonbot1.onrender.com
API_SECRET       = os.getenv("API_SECRET")       # pour interroger Bot2
BOT2_URL         = os.getenv("BOT2_URL")         # ex: https://bot2.onrender.com/pioches
BOT_USERNAME     = os.getenv("BOT_USERNAME", "") # si tu veux faire des liens t.me/<bot>

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN manquant")
if not FLASK_SECRET:
    raise RuntimeError("FLASK_SECRET manquant")
if not PUBLIC_BASE_URL:
    raise RuntimeError("PUBLIC_BASE_URL manquant")

# =========================================================
# Flask
# =========================================================
app = Flask(__name__)
app.secret_key = FLASK_SECRET

@app.get("/")
def home():
    return "Bot1 + MiniApp is running!"

@app.get("/ping")
def ping():
    return "OK"

# petite icône (pour futur manifest TON si besoin)
ICON_BYTES = bytes.fromhex(
    "89504E470D0A1A0A0000000D4948445200000001000000010806000000"
    "1F15C4890000000A49444154789C6360000002000154A24F6500000000"
    "49454E44AE426082"
)
@app.get("/static/ton-icon.png")
def ton_icon():
    resp = make_response(ICON_BYTES)
    resp.headers["Content-Type"] = "image/png"
    return resp

# =========================================================
# SQLite
# =========================================================
DB_FILE = "bot.db"
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
cur  = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    wallet_address TEXT,
    personal_code TEXT UNIQUE,
    referral_code_used TEXT,
    avatar_skin TEXT DEFAULT 'rookie'
)
""")
conn.commit()

def generate_referral_code(length=6):
    import random, string
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

def is_user_registered(user_id: int) -> bool:
    cur.execute("SELECT 1 FROM users WHERE telegram_id=?", (user_id,))
    return cur.fetchone() is not None

def ensure_user_row(user_id: int):
    if not is_user_registered(user_id):
        cur.execute("INSERT OR IGNORE INTO users (telegram_id, personal_code) VALUES (?, ?)",
                    (user_id, generate_referral_code()))
        conn.commit()

def set_wallet(user_id: int, address: str):
    ensure_user_row(user_id)
    cur.execute("UPDATE users SET wallet_address=? WHERE telegram_id=?", (address, user_id))
    conn.commit()

def get_user_row(user_id: int):
    cur.execute("SELECT wallet_address, personal_code, referral_code_used, avatar_skin FROM users WHERE telegram_id=?",
                (user_id,))
    return cur.fetchone()

def set_avatar_skin(user_id: int, skin: str):
    ensure_user_row(user_id)
    cur.execute("UPDATE users SET avatar_skin=? WHERE telegram_id=?", (skin, user_id))
    conn.commit()

# =========================================================
# Intégration Bot2 (pioches)
# =========================================================
def get_pioches_from_bot2(telegram_id: int):
    if not BOT2_URL or not API_SECRET:
        return 0, []
    try:
        r = requests.get(BOT2_URL, params={"telegram_id": telegram_id, "secret": API_SECRET}, timeout=5)
        if r.status_code == 200:
            data = r.json()
            return data.get("total_pioches", 0), data.get("details", [])
    except Exception as e:
        print("Bot2 API error:", e)
    return 0, []

# =========================================================
# Mini-App (Telegram WebApp)
# =========================================================
APP_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Endorisum — MiniApp</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial;padding:16px;background:#0e0f13;color:#e6e6e6}
    .wrap{max-width:680px;margin:0 auto}
    .card{background:#16181e;border:1px solid #22252d;border-radius:14px;padding:16px;margin-bottom:14px;box-shadow:0 2px 8px rgba(0,0,0,.4)}
    h1{margin:0 0 10px;font-size:20px}
    .muted{color:#a6a8ad;font-size:14px}
    .row{display:flex;gap:12px;flex-wrap:wrap}
    .btn{padding:10px 14px;border:1px solid #2d313a;border-radius:10px;background:#1b1f27;color:#e6e6e6;cursor:pointer}
    .btn:active{transform:scale(.98)}
    .grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
    .avatar{display:flex;align-items:center;gap:14px}
    .avatar-badge{width:64px;height:64px;border-radius:12px;display:flex;align-items:center;justify-content:center;background:#101218;border:1px solid #2b2f39}
    .stat{display:flex;justify-content:space-between;margin:3px 0}
    code{background:#0b0c10;color:#c6d0f5;padding:2px 4px;border-radius:4px}
    .small{font-size:12px;color:#9aa0a6}
  </style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <h1>🏠 Endorisum</h1>
    <div class="muted">Menu rapide</div>
    <div class="row" style="margin-top:10px">
      <button class="btn" onclick="goProfile()">👤 Profil</button>
      <button class="btn" onclick="goMines()">🏭 Mines</button>
      <button class="btn" onclick="goReferral()">🤝 Referral</button>
    </div>
  </div>

  <div class="card" id="profileCard" style="display:none">
    <h1>👤 Profil</h1>
    <div class="avatar">
      <div class="avatar-badge" id="avatarBox">⛏️</div>
      <div>
        <div id="uidLine" class="small"></div>
        <div id="walletLine" class="small"></div>
        <div id="codeLine" class="small"></div>
      </div>
    </div>
    <div style="margin-top:10px">
      <div class="stat"><span>⛏️ Pioches totales</span><b id="statsPicks">0</b></div>
    </div>
  </div>

  <div class="card" id="skinsCard" style="display:none">
    <h1>🎨 Avatar</h1>
    <div class="muted">Choisis ton skin :</div>
    <div class="grid" style="margin-top:10px">
      <button class="btn" onclick="chooseSkin('rookie')">Rookie</button>
      <button class="btn" onclick="chooseSkin('miner')">Miner</button>
      <button class="btn" onclick="chooseSkin('explorer')">Explorer</button>
      <button class="btn" onclick="chooseSkin('pro')">Pro</button>
    </div>
  </div>

  <div class="card" id="minesCard" style="display:none">
    <h1>🏭 Mines</h1>
    <div class="row">
      <a class="btn" href="https://t.me/mine1" target="_blank">Mine 1</a>
      <a class="btn" href="https://t.me/mine2" target="_blank">Mine 2</a>
      <a class="btn" href="https://t.me/mine3" target="_blank">Mine 3</a>
    </div>
  </div>

  <div class="card" id="refCard" style="display:none">
    <h1>🤝 Referral</h1>
    <div class="muted">Bientôt disponible.</div>
  </div>

  <div class="card">
    <button class="btn" onclick="backToStart()">⬅️ Retour</button>
  </div>
</div>

<script>
  const tg = window.Telegram ? Telegram.WebApp : null;
  if (tg) { tg.expand(); }

  const Q = new URLSearchParams(location.search);
  const uid = Q.get("uid");

  function iconForSkin(s) {
    if (s === "miner") return "⛏️";
    if (s === "explorer") return "🧭";
    if (s === "pro") return "💎";
    return "🙂";
  }

  async function fetchProfile() {
    const r = await fetch(`/api/profile?uid=${encodeURIComponent(uid)}`, {cache:"no-store"});
    if (!r.ok) return null;
    return await r.json();
  }

  async function chooseSkin(s) {
    const r = await fetch(`/api/avatar`, {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ uid: uid, skin: s })
    });
    const data = await r.json().catch(()=>({}));
    if (data && data.ok) {
      document.getElementById("avatarBox").textContent = iconForSkin(s);
    }
  }

  function showOnly(id) {
    const cards = ["profileCard","skinsCard","minesCard","refCard"];
    cards.forEach(c => document.getElementById(c).style.display = "none");
    document.getElementById(id).style.display = "block";
  }

  async function goProfile() {
    const p = await fetchProfile();
    if (!p) return;
    document.getElementById("uidLine").textContent    = "ID: " + p.telegram_id;
    document.getElementById("walletLine").textContent = "Wallet: " + (p.wallet || "—");
    document.getElementById("codeLine").textContent   = "Code: " + (p.personal_code || "—");
    document.getElementById("statsPicks").textContent = p.total_pioches || 0;
    document.getElementById("avatarBox").textContent  = iconForSkin(p.avatar_skin || "rookie");
    showOnly("profileCard");
    document.getElementById("skinsCard").style.display = "block";
  }

  function goMines()    { showOnly("minesCard"); }
  function goReferral() { showOnly("refCard");   }
  function backToStart(){ if (tg) tg.close(); else location.href="/"; }

  // auto-open profil au lancement
  goProfile();
</script>
</body>
</html>
"""

@app.get("/app")
def mini_app():
    uid = request.args.get("uid", "").strip()
    if not uid.isdigit():
        return "Missing uid", 400
    return render_template_string(APP_HTML)

# --- API mini-app
@app.get("/api/profile")
def api_profile():
    uid = request.args.get("uid", "").strip()
    if not uid.isdigit():
        return jsonify({"ok": False, "error": "bad uid"}), 400
    tid = int(uid)
    ensure_user_row(tid)
    row = get_user_row(tid)  # wallet, code, ref, skin
    wallet, code, ref, skin = row if row else (None, None, None, "rookie")
    total, details = get_pioches_from_bot2(tid)
    return jsonify({
        "ok": True,
        "telegram_id": tid,
        "wallet": wallet,
        "personal_code": code,
        "referral_code_used": ref,
        "avatar_skin": skin,
        "total_pioches": total,
        "details": details[:20]
    })

@app.post("/api/avatar")
def api_avatar():
    data = request.get_json() or {}
    uid  = str(data.get("uid","")).strip()
    skin = (data.get("skin") or "rookie").strip()
    if not uid.isdigit():
        return jsonify({"ok": False, "error":"bad uid"}), 400
    if skin not in {"rookie","miner","explorer","pro"}:
        return jsonify({"ok": False, "error":"bad skin"}), 400
    set_avatar_skin(int(uid), skin)
    return jsonify({"ok": True})

# =========================================================
# Telegram Bot (polling avec anti-conflit et keepalive)
# =========================================================
def main_menu(is_registered: bool, user_id: int):
    webapp_url = f"{PUBLIC_BASE_URL}/app?uid={user_id}"
    if not is_registered:
        # Menu public (sans inscription via wallet ici ; tu peux ajouter TON Connect si besoin)
        txt = "Bienvenue dans Endorisum !\n\nQue souhaites-tu faire ?"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🕹️ Ouvrir la mini-app", web_app=WebAppInfo(url=webapp_url))],
            [InlineKeyboardButton("📊 Profil", callback_data="profil")],
            [InlineKeyboardButton("🤝 Referral", callback_data="referral")],
            [InlineKeyboardButton("🏭 Mines", callback_data="mines")],
        ])
        return txt, kb
    else:
        txt = "Bienvenue dans Endorisum !\n\nQue souhaites-tu faire ?"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🕹️ Ouvrir la mini-app", web_app=WebAppInfo(url=webapp_url))],
            [InlineKeyboardButton("📊 Profil", callback_data="profil"),
             InlineKeyboardButton("🏭 Mines", callback_data="mines")],
            [InlineKeyboardButton("🤝 Referral", callback_data="referral")],
        ])
        return txt, kb

def profile_text(user_id: int):
    row = get_user_row(user_id)
    if not row:
        return "❌ Tu n'es pas encore inscrit."
    wallet, code, ref, skin = row
    total, _ = get_pioches_from_bot2(user_id)
    icon = {"rookie":"🙂","miner":"⛏️","explorer":"🧭","pro":"💎"}.get(skin or "rookie", "🙂")
    return (
        f"{icon} <b>Profil</b>\n"
        f"• ID: <code>{user_id}</code>\n"
        f"• Wallet: <code>{html.escape(wallet or '—')}</code>\n"
        f"• Code: <code>{html.escape(code or '—')}</code>\n"
        f"• Pioches: <b>{total}</b>\n\n"
        f"👉 Pour personnaliser ton avatar et voir plus d’infos, ouvre la mini-app."
    )

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user_row(u.id)
    text, kb = main_menu(is_user_registered(u.id), u.id)
    await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")

async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id
    data = q.data

    if data == "profil":
        txt = profile_text(user_id)
        # bouton ouvrir mini-app
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🕹️ Ouvrir la mini-app", web_app=WebAppInfo(url=f"{PUBLIC_BASE_URL}/app?uid={user_id}"))],
            [InlineKeyboardButton("⬅️ Retour", callback_data="back_main")]
        ])
        await q.edit_message_text(txt, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)

    elif data == "mines":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Mine 1", url="https://t.me/mine1"),
             InlineKeyboardButton("Mine 2", url="https://t.me/mine2")],
            [InlineKeyboardButton("⬅️ Retour", callback_data="back_main")]
        ])
        await q.edit_message_text("⛏️ Choisis une mine partenaire :", reply_markup=kb)

    elif data == "referral":
        await q.edit_message_text("🤝 Parrainage — bientôt disponible.", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Retour", callback_data="back_main")]
        ]))

    elif data == "back_main":
        txt, kb = main_menu(is_user_registered(user_id), user_id)
        await q.edit_message_text(txt, reply_markup=kb)

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Pas de saisie texte nécessaire pour ce bot
    pass

# =========================================================
# Keepalive (anti-sommeil)
# =========================================================
def keep_alive():
    def _loop():
        while True:
            try:
                requests.get(f"{PUBLIC_BASE_URL}/ping", timeout=5)
            except Exception:
                pass
            time.sleep(240)  # toutes les ~4 minutes
    t = threading.Thread(target=_loop, daemon=True)
    t.start()

# =========================================================
# Lancement
# =========================================================
def run_bot():
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CallbackQueryHandler(on_menu))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # anti-conflit : s'assurer qu'aucun webhook n'est actif, et drop des updates en attente
    async def _run():
        await application.bot.delete_webhook(drop_pending_updates=True)
        await application.run_polling()

    import asyncio
    asyncio.run(_run())

def run_flask():
    app.run(host="0.0.0.0", port=8080)

if __name__ == "__main__":
    keep_alive()
    threading.Thread(target=run_flask, daemon=True).start()
    run_bot()
