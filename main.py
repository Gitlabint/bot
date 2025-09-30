# main.py
import os
import json
import time
import secrets
import sqlite3
import threading
from datetime import datetime

import requests
from flask import Flask, request, jsonify, render_template_string, redirect, url_for, session, make_response
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# =========================
# Configuration (ENV)
# =========================
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN")
FLASK_SECRET    = os.getenv("FLASK_SECRET", "changeme")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")  # ex: https://yourapp.onrender.com
BOT_USERNAME    = os.getenv("BOT_USERNAME", "") # sans @ (pour lien t.me, optionnel)
API_SECRET      = os.getenv("API_SECRET", "")
BOT2_URL        = os.getenv("BOT2_URL", "")     # ex: https://bot2.yourdomain.com/pioches (GET ?telegram_id=&secret=)

if not TELEGRAM_TOKEN:  raise SystemExit("Missing TELEGRAM_TOKEN")
if not PUBLIC_BASE_URL: raise SystemExit("Missing PUBLIC_BASE_URL (public https for TON Connect)")

# =========================
# Flask app + DB
# =========================
app = Flask(__name__)
app.secret_key = FLASK_SECRET

DB_FILE = "bot.db"
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
cur  = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS users (
    telegram_id    INTEGER PRIMARY KEY,
    username       TEXT,
    first_name     TEXT,
    last_name      TEXT,
    wallet_address TEXT,
    personal_code  TEXT UNIQUE,
    referral_code_used TEXT,
    avatar_json    TEXT DEFAULT '{}',
    created_at     INTEGER
)
""")
conn.commit()

def now_ts() -> int: return int(time.time())

def generate_referral_code(n=6):
    import string, random
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=n))

def upsert_user_from_telegram(u):
    """Enregistre basiquement l’utilisateur côté bot (sans wallet)."""
    tid = u.id
    cur.execute("SELECT telegram_id FROM users WHERE telegram_id=?", (tid,))
    if cur.fetchone() is None:
        cur.execute("""INSERT INTO users(telegram_id, username, first_name, last_name, personal_code, created_at)
                       VALUES(?,?,?,?,?,?)""",
                    (tid, u.username, u.first_name, u.last_name, generate_referral_code(), now_ts()))
        conn.commit()
    else:
        cur.execute("""UPDATE users SET username=?, first_name=?, last_name=? WHERE telegram_id=?""",
                    (u.username, u.first_name, u.last_name, tid))
        conn.commit()

def set_user_wallet(telegram_id: int, wallet: str):
    cur.execute("SELECT telegram_id FROM users WHERE telegram_id=?", (telegram_id,))
    if cur.fetchone() is None:
        cur.execute("""INSERT INTO users(telegram_id, wallet_address, personal_code, created_at)
                       VALUES(?,?,?,?)""",
                    (telegram_id, wallet, generate_referral_code(), now_ts()))
    else:
        cur.execute("UPDATE users SET wallet_address=? WHERE telegram_id=?", (wallet, telegram_id))
    conn.commit()

def is_registered(uid: int) -> bool:
    cur.execute("SELECT 1 FROM users WHERE telegram_id=? AND wallet_address IS NOT NULL", (uid,))
    return cur.fetchone() is not None

def get_user_profile(uid: int):
    cur.execute("""SELECT username, first_name, last_name, wallet_address, personal_code, referral_code_used, avatar_json
                   FROM users WHERE telegram_id=?""", (uid,))
    r = cur.fetchone()
    if not r: return None
    username, fn, ln, wallet, pcode, ref_used, avatar_json = r
    try:
        avatar = json.loads(avatar_json or "{}")
    except Exception:
        avatar = {}
    return {
        "telegram_id": uid,
        "username": username,
        "first_name": fn,
        "last_name": ln,
        "wallet": wallet,
        "personal_code": pcode,
        "referral_code_used": ref_used,
        "avatar": avatar
    }

def update_avatar(uid: int, avatar_dict: dict):
    cur.execute("UPDATE users SET avatar_json=? WHERE telegram_id=?",
                (json.dumps(avatar_dict or {}), uid))
    conn.commit()

def send_message(chat_id: int, text: str, reply_markup: dict | None = None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        requests.post(url, json=payload, timeout=8)
    except Exception as e:
        print("send_message error:", e)

def get_pioches_from_bot2(uid: int):
    if not (BOT2_URL and API_SECRET):
        return 0, []
    try:
        r = requests.get(BOT2_URL, params={"telegram_id": uid, "secret": API_SECRET}, timeout=6)
        if r.status_code == 200:
            data = r.json()
            return data.get("total_pioches", 0), data.get("details", [])
    except Exception as e:
        print("get_pioches_from_bot2 error:", e)
    return 0, []

# =========================
# Telegram BOT (PTB v20)
# =========================
application: Application | None = None

def main_menu_buttons(user_id: int) -> InlineKeyboardMarkup:
    # Bot ≠ Mini App. Ici : un seul bouton "Se connecter" ou "Ouvrir la Mini App"
    if is_registered(user_id):
        # Ouvre la Mini App
        url_app = f"{PUBLIC_BASE_URL}/app?uid={user_id}"
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("▶️ Ouvrir la Mini App", web_app=WebAppInfo(url=url_app))]
        ])
    else:
        # Connect TON wallet via WebApp
        nonce = secrets.token_hex(8)
        url_connect = f"{PUBLIC_BASE_URL}/ton/connect?uid={user_id}&nonce={nonce}"
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🔗 Se connecter avec TON Wallet", web_app=WebAppInfo(url=url_connect))]
        ])

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user_from_telegram(u)
    kb = main_menu_buttons(u.id)
    await update.message.reply_text(
        "Bienvenue ! Utilise le bouton ci-dessous pour te connecter ou ouvrir la Mini App.",
        reply_markup=kb
    )

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # On ne met plus de menu inline côté bot, tout se passe en Mini App.
    query = update.callback_query
    await query.answer()
    u = query.from_user
    upsert_user_from_telegram(u)
    kb = main_menu_buttons(u.id)
    await query.edit_message_text("Utilise le bouton ci-dessous :", reply_markup=kb)

def run_bot():
    global application
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CallbackQueryHandler(menu_callback))
    # Run polling dans ce thread
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

# =========================
# TON Connect: manifest & connect page
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
def ton_manifest_wk():
    return ton_manifest()

@app.route("/manifest.json")
def ton_manifest_alias():
    return ton_manifest()

@app.route("/ton/connect")
def ton_connect_page():
    uid = request.args.get("uid", "").strip()
    nonce = request.args.get("nonce", "").strip()
    if not uid.isdigit() or not nonce:
        return "Paramètres invalides", 400
    return render_template_string(CONNECT_HTML,
                                  uid=uid,
                                  nonce=nonce,
                                  manifest_abs=f"{PUBLIC_BASE_URL}/ton/manifest.json")

@app.route("/ton/submit", methods=["POST", "OPTIONS"])
def ton_submit():
    if request.method == "OPTIONS":
        return ("", 204)
    try:
        data = request.get_json() or {}
        uid = int(data.get("uid") or 0)
        address = (data.get("address") or "").strip()
        if not uid or not address:
            return jsonify({"ok": False, "error": "missing"}), 400

        set_user_wallet(uid, address)

        # Pousse un bouton qui ouvre la Mini App directement
        kb = {
            "inline_keyboard": [
                [{"text": "▶️ Ouvrir la Mini App", "web_app": {"url": f"{PUBLIC_BASE_URL}/app?uid={uid}"}}]
            ]
        }
        send_message(uid, f"✅ Wallet lié : {address}\nOuvre la Mini App :", kb)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# =========================
# Mini App (WebApp Telegram)
# =========================
@app.route("/app")
def mini_app():
    uid = request.args.get("uid", "").strip()
    if not uid.isdigit():
        return "Invalid uid", 400
    return render_template_string(MINI_APP_HTML, uid=uid)

# -------- API Mini App --------
@app.route("/api/profile")
def api_profile():
    uid = request.args.get("uid", "").strip()
    if not uid.isdigit():
        return jsonify({"ok": False, "error": "invalid uid"}), 400
    uid_i = int(uid)
    p = get_user_profile(uid_i)
    if not p:
        return jsonify({"ok": False, "error": "not found"}), 404
    total, details = get_pioches_from_bot2(uid_i)
    p["trophies_total"] = total
    p["trophies_details"] = details
    return jsonify({"ok": True, "profile": p})

@app.route("/api/update_avatar", methods=["POST"])
def api_update_avatar():
    data = request.get_json() or {}
    uid = data.get("uid")
    avatar = data.get("avatar")
    try:
        uid = int(uid)
    except:
        return jsonify({"ok": False, "error": "invalid uid"}), 400
    if not isinstance(avatar, dict):
        return jsonify({"ok": False, "error": "invalid avatar"}), 400
    update_avatar(uid, avatar)
    return jsonify({"ok": True})

@app.route("/api/mines")
def api_mines():
    # Liste simple, édite à volonté
    mines = [
        {"name": "Mine 1", "url": "https://t.me/mine1"},
        {"name": "Mine 2", "url": "https://t.me/mine2"},
        {"name": "Mine 3", "url": "https://t.me/mine3"},
    ]
    return jsonify({"ok": True, "mines": mines})

# Pages placeholder (manifest refs)
@app.route("/terms")
def terms(): return "Terms of Use", 200

@app.route("/privacy")
def privacy(): return "Privacy Policy", 200

@app.route("/ping")
def ping(): return "pong", 200

# =========================
# HTML: TON Connect (WebApp)
# =========================
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
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; padding: 18px; background:#0b0f17; color:#e6edf3; }
    .card { max-width: 600px; margin: 0 auto; background:#111827; border:1px solid #1f2937; border-radius: 14px; padding: 18px; }
    h2 { margin: 0 0 8px 0; }
    .muted { color:#9aa4b2; }
    .status { margin: 12px 0; }
    .cta { margin-top: 16px; }
    button { padding: 10px 14px; border-radius: 10px; border:1px solid #334155; background:#0ea5e9; color:white; cursor:pointer; }
    .ghost { background:transparent; border:1px solid #334155; color:#e6edf3; margin-left: 8px; }
    code { background:#0f172a; padding:3px 6px; border-radius:6px; }
  </style>
</head>
<body>
  <div class="card">
    <h2>Connect TON Wallet</h2>
    <div class="muted">Manifest: <code id="mf"></code></div>

    <div id="ton-ui" class="cta"></div>
    <button id="open" class="ghost" style="display:none;">Ouvrir la liste des wallets</button>

    <div id="status" class="status"></div>
    <div id="error"  class="status" style="color:#f87171;"></div>
  </div>

<script>
(function(){
  const uid = "{{uid}}";
  const nonce = "{{nonce}}";
  const manifestAbs = "{{manifest_abs}}";
  document.getElementById("mf").textContent = manifestAbs;

  const statusEl = document.getElementById("status");
  const errorEl  = document.getElementById("error");
  const openBtn  = document.getElementById("open");

  function setStatus(s){ statusEl.textContent = s; }
  function setError(e){ errorEl.textContent  = e; }

  async function checkManifest(){
    try{
      const r = await fetch(manifestAbs, {cache:"no-store"});
      if(!r.ok) throw new Error("HTTP "+r.status);
      await r.json();
      return true;
    }catch(e){
      setError("Manifest injoignable : " + e.message);
      return false;
    }
  }

  async function closeWebAppSoon(){
    try{
      if(window.Telegram && Telegram.WebApp){
        Telegram.WebApp.HapticFeedback.impactOccurred('light');
        setTimeout(()=>Telegram.WebApp.close(), 400);
      } else {
        setTimeout(()=>window.close(), 400);
      }
    }catch(e){}
  }

  async function init(){
    setStatus("Chargement…");
    if(!window.TON_CONNECT_UI || !TON_CONNECT_UI.TonConnectUI){
      setError("Librairie TON Connect UI introuvable.");
      return;
    }
    const ok = await checkManifest();
    if(!ok){ openBtn.style.display = "inline-block"; }

    let ui;
    try {
      ui = new TON_CONNECT_UI.TonConnectUI({
        manifestUrl: manifestAbs,
        buttonRootId: "ton-ui"
      });
    } catch(e) {
      setError("TonConnectUI init: " + e.message);
      return;
    }

    setTimeout(()=>{
      const mount = document.getElementById("ton-ui");
      if(!mount || mount.children.length===0) openBtn.style.display = "inline-block";
    }, 600);

    openBtn.addEventListener("click", ()=>{ try{ ui.openModal(); }catch(e){} });

    ui.onStatusChange(async (walletInfo)=>{
      if(!walletInfo || !walletInfo.account) return;
      const address = walletInfo.account.address;
      setStatus("Wallet connecté: " + address + " — enregistrement…");
      try{
        const r = await fetch("/ton/submit", {
          method: "POST",
          headers: {"Content-Type":"application/json"},
          body: JSON.stringify({ uid: uid, nonce: nonce, address: address })
        });
        const data = await r.json();
        if(data.ok){
          setStatus("Inscription enregistrée ✅");
          await closeWebAppSoon();
        } else {
          setError("Erreur: " + (data.error || "enregistrement"));
        }
      }catch(e){
        setError("Échec réseau: " + e.message);
      }
    });
  }

  if(document.readyState==="loading") document.addEventListener("DOMContentLoaded", init);
  else init();

})();
</script>
</body>
</html>
"""

# =========================
# HTML: MINI APP (Profil/Miner/Referral/Settings)
# =========================
MINI_APP_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Endorisum Mini App</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    :root{ --bg:#0b0f17; --card:#111827; --muted:#9aa4b2; --text:#e6edf3; --primary:#0ea5e9; --border:#1f2937; }
    body{ margin:0; background:var(--bg); color:var(--text); font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; }
    .wrap{ max-width:900px; margin:0 auto; padding:16px; }
    header{ display:flex; align-items:center; justify-content:space-between; margin-bottom:12px; }
    .tabs{ display:flex; gap:8px; margin:10px 0 16px; }
    .tab{ padding:8px 12px; border:1px solid var(--border); border-radius:10px; cursor:pointer; color:var(--muted); }
    .tab.active{ background:var(--primary); color:white; border-color:transparent; }
    .card{ background:var(--card); border:1px solid var(--border); border-radius:14px; padding:16px; margin-bottom:12px; }
    .row{ margin:8px 0; }
    .muted{ color:var(--muted); }
    .grid{ display:grid; grid-template-columns: repeat(auto-fit, minmax(220px,1fr)); gap:12px; }
    button{ padding:10px 14px; border-radius:10px; border:1px solid var(--border); background:#0ea5e9; color:#fff; cursor:pointer; }
    select{ padding:8px; border-radius:8px; border:1px solid var(--border); background:#0b1220; color:#fff; }
    .avatar{ display:flex; align-items:center; gap:14px; }
    .avatar-box{ width:96px; height:96px; border-radius:50%; background:linear-gradient(145deg,#0f172a,#0b1220); border:1px solid var(--border); position:relative; display:flex; align-items:center; justify-content:center; font-weight:700; }
    .badge{ display:inline-block; padding:3px 8px; border-radius:999px; border:1px solid var(--border); color:var(--muted); }
    a.mlink{ color:#93c5fd; text-decoration:none; }
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <div><strong>Endorisum</strong> <span class="muted">Mini App</span></div>
      <div class="badge" id="uidbadge"></div>
    </header>

    <div class="tabs">
      <div class="tab active" data-tab="profile">👤 Profil</div>
      <div class="tab" data-tab="miner">⛏️ Miner</div>
      <div class="tab" data-tab="referral">🤝 Referral</div>
      <div class="tab" data-tab="settings">⚙️ Paramètres</div>
    </div>

    <!-- Profil -->
    <section class="card" id="tab-profile">
      <div class="avatar">
        <div class="avatar-box" id="avatarCircle">⛏️</div>
        <div>
          <div id="name" style="font-size:18px;font-weight:700;"></div>
          <div class="muted row">Wallet: <span id="wallet">—</span></div>
          <div class="row">Trophées ⛏️: <span id="trophies">0</span></div>
        </div>
      </div>

      <div class="row muted" style="margin-top:12px;">Personnalise ton avatar :</div>
      <div class="grid">
        <div>
          <div class="muted">Chapeau</div>
          <select id="pick-hat">
            <option value="">— Aucun —</option>
            <option value="casquette">Casquette</option>
            <option value="cowboy">Cowboy</option>
            <option value="miner">Casque de mineur</option>
          </select>
        </div>
        <div>
          <div class="muted">Tenue</div>
          <select id="pick-shirt">
            <option value="">— Basique —</option>
            <option value="red">Rouge</option>
            <option value="blue">Bleu</option>
            <option value="black">Noir</option>
          </select>
        </div>
        <div>
          <div class="muted">Skin</div>
          <select id="pick-skin">
            <option value="">— Classique —</option>
            <option value="robot">Robot</option>
            <option value="alien">Alien</option>
            <option value="human">Humain</option>
          </select>
        </div>
      </div>
      <div class="row">
        <button id="saveAvatar">💾 Sauvegarder l’avatar</button>
      </div>
    </section>

    <!-- Miner -->
    <section class="card" id="tab-miner" style="display:none;">
      <div class="row muted">Choisis une mine partenaire :</div>
      <div id="mines" class="grid"></div>
    </section>

    <!-- Referral -->
    <section class="card" id="tab-referral" style="display:none;">
      <div class="row">Cette fonctionnalité arrive bientôt.</div>
    </section>

    <!-- Settings -->
    <section class="card" id="tab-settings" style="display:none;">
      <div class="row">Rien ici pour l’instant 🙂</div>
    </section>
  </div>

<script>
(function(){
  const uid = new URLSearchParams(location.search).get("uid");
  if(!uid){ alert("No uid"); }

  document.getElementById("uidbadge").textContent = "UID " + uid;

  const tabs = document.querySelectorAll(".tab");
  const sections = {
    profile: document.getElementById("tab-profile"),
    miner: document.getElementById("tab-miner"),
    referral: document.getElementById("tab-referral"),
    settings: document.getElementById("tab-settings"),
  };
  tabs.forEach(t=>{
    t.addEventListener("click", ()=>{
      tabs.forEach(x=>x.classList.remove("active"));
      t.classList.add("active");
      for(const k in sections){ sections[k].style.display="none"; }
      const tab = t.dataset.tab;
      sections[tab].style.display="block";
    });
  });

  async function loadProfile(){
    const r = await fetch("/api/profile?uid="+uid);
    const data = await r.json();
    if(!data.ok){ alert("Profil introuvable"); return; }
    const p = data.profile;
    const name = [p.first_name || "", p.last_name || ""].join(" ").trim() || ("@" + (p.username||""));
    document.getElementById("name").textContent = name || "Utilisateur";
    document.getElementById("wallet").textContent = p.wallet || "—";
    document.getElementById("trophies").textContent = p.trophies_total || 0;

    const av = p.avatar || {};
    document.getElementById("pick-hat").value = av.hat || "";
    document.getElementById("pick-shirt").value = av.shirt || "";
    document.getElementById("pick-skin").value = av.skin || "";
    renderAvatar(av);
  }

  function renderAvatar(av){
    const box = document.getElementById("avatarCircle");
    let txt = "⛏️";
    if(av.skin==="robot") txt = "🤖";
    else if(av.skin==="alien") txt = "👽";
    else txt = "⛏️";
    if(av.hat==="cowboy") txt = "🤠";
    else if(av.hat==="miner") txt = "⛑️";
    else if(av.hat==="casquette") txt = "🧢";
    box.textContent = txt;
  }

  document.getElementById("saveAvatar").addEventListener("click", async ()=>{
    const av = {
      hat: document.getElementById("pick-hat").value,
      shirt: document.getElementById("pick-shirt").value,
      skin: document.getElementById("pick-skin").value
    };
    await fetch("/api/update_avatar", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ uid: uid, avatar: av })
    });
    renderAvatar(av);
  });

  async function loadMines(){
    const r = await fetch("/api/mines");
    const data = await r.json();
    const wrap = document.getElementById("mines");
    wrap.innerHTML = "";
    (data.mines||[]).forEach(m=>{
      const a = document.createElement("a");
      a.href = m.url;
      a.target = "_blank";
      a.className = "mlink card";
      a.style.display = "block";
      a.style.padding = "12px";
      a.textContent = "⛏️ " + m.name;
      wrap.appendChild(a);
    });
  }

  loadProfile();
  loadMines();

  // Adapter le thème au WebApp Telegram
  if(window.Telegram && Telegram.WebApp){
    Telegram.WebApp.ready();
    Telegram.WebApp.expand();
  }
})();
</script>
</body>
</html>
"""

# =========================
# Lancement
# =========================
def run_flask():
    # Sur Render, port fourni par PORT
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    # Lance Flask + Bot en parallèle
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()
    run_bot()
