from flask import Flask, request, jsonify, render_template
from threading import Thread
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
import sqlite3, random, string, os, secrets

# --- Flask ---
app = Flask(__name__, static_url_path="/static", static_folder="static", template_folder="templates")
app.secret_key = os.getenv("FLASK_SECRET", "supersecret")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")
BOT_USERNAME = os.getenv("BOT_USERNAME", "MyMiningBot")

if not TELEGRAM_TOKEN or not PUBLIC_BASE_URL:
    raise ValueError("⚠️ Variables d’environnement manquantes")

# --- DB ---
DB_FILE = "bot.db"
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
cursor = conn.cursor()
cursor.execute('''
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
''')
conn.commit()

def generate_referral_code(length=6):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

def add_user(uid, wallet=None, referral=None):
    cursor.execute("SELECT 1 FROM users WHERE telegram_id=?", (uid,))
    if cursor.fetchone(): return
    code = generate_referral_code()
    cursor.execute("""
      INSERT INTO users (telegram_id, wallet_address, personal_code, referral_code_used)
      VALUES (?, ?, ?, ?)
    """, (uid, wallet, code, referral))
    conn.commit()

def get_user(uid):
    cursor.execute("SELECT * FROM users WHERE telegram_id=?", (uid,))
    return cursor.fetchone()

def update_avatar(uid, data):
    cursor.execute("""
        UPDATE users SET hat=?, jacket=?, pants=?, shoes=?, bracelet=? WHERE telegram_id=?
    """, (data.get("hat"), data.get("jacket"), data.get("pants"),
          data.get("shoes"), data.get("bracelet","metal"), uid))
    conn.commit()

def remove_user(uid):
    cursor.execute("DELETE FROM users WHERE telegram_id=?", (uid,))
    conn.commit()

# --- API ---
@app.route("/")
def home():
    return "✅ Bot + MiniApp actif"

@app.route("/app")
def mini_app():
    uid = request.args.get("uid")
    ref = request.args.get("ref")
    return render_template("app.html", uid=uid, ref=ref, bot_username=BOT_USERNAME, public_base=PUBLIC_BASE_URL)

@app.route("/api/me")
def api_me():
    uid = int(request.args.get("uid", "0"))
    user = get_user(uid)
    if not user: return jsonify({"registered": False})

    (telegram_id, wallet, code, ref, hat, jacket, pants, shoes, bracelet) = user
    return jsonify({
        "registered": True,
        "telegram_id": telegram_id,
        "wallet_address": wallet,
        "personal_code": code,
        "referral_code_used": ref,
        "pioches_total": 3,  # exemple statique
        "avatar": {"hat": hat, "jacket": jacket, "pants": pants, "shoes": shoes, "bracelet": bracelet},
        "nfts": [
            {"name":"NFT1","image":f"{PUBLIC_BASE_URL}/static/watch.png"},
            {"name":"NFT2","image":f"{PUBLIC_BASE_URL}/static/watch.png"}
        ]
    })

@app.route("/api/mines")
def api_mines():
    return jsonify({
        "mines": [
            {"title":"Mine 1","url":"https://t.me/mine1"},
            {"title":"Mine 2","url":"https://t.me/mine2"}
        ]
    })

@app.route("/api/avatar/update", methods=["POST"])
def api_avatar_update():
    data = request.get_json()
    uid = int(data.get("uid"))
    update_avatar(uid, data)
    return jsonify({"ok": True})

@app.route("/api/unsubscribe", methods=["POST"])
def api_unsub():
    data = request.get_json()
    uid = int(data.get("uid"))
    remove_user(uid)
    return jsonify({"ok": True})

# --- Telegram Bot ---
def main_menu():
    return ("Bienvenue !", InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Connect TON Wallet", callback_data="connect")]
    ]))

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    add_user(uid)
    text, kb = main_menu()
    await update.message.reply_text(text, reply_markup=kb)

async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    uid = query.from_user.id
    if query.data == "connect":
        nonce = secrets.token_hex(8)
        url = f"{PUBLIC_BASE_URL}/app?uid={uid}&nonce={nonce}"
        btn = InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Ouvrir MiniApp", web_app=WebAppInfo(url=url))]])
        await query.edit_message_text("Ouvre ta MiniApp :", reply_markup=btn)

def run_bot():
    app_tg = Application.builder().token(TELEGRAM_TOKEN).build()
    app_tg.add_handler(CommandHandler("start", start))
    app_tg.add_handler(CallbackQueryHandler(menu_handler))
    app_tg.run_polling()

def run_flask():
    app.run(host="0.0.0.0", port=8080)

def keep_alive():
    Thread(target=run_flask).start()

if __name__ == "__main__":
    keep_alive()
    run_bot()
