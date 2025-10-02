from flask import Flask, request, render_template, jsonify
from threading import Thread
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
import sqlite3, random, string, os, secrets, json

# --- Flask setup ---
app = Flask(__name__, static_url_path="/static", static_folder="static")
app.secret_key = os.getenv("FLASK_SECRET", "supersecret")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")
BOT_USERNAME = os.getenv("BOT_USERNAME", "MyMiningBot")

if not TELEGRAM_TOKEN or not PUBLIC_BASE_URL:
    raise ValueError("⚠️ Variables d’environnement manquantes")

# --- Database ---
DB_FILE = "bot.db"
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
cursor = conn.cursor()
cursor.execute('''
CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    wallet_address TEXT,
    personal_code TEXT UNIQUE,
    referral_code_used TEXT,
    avatar_style TEXT DEFAULT 'default'
)
''')
conn.commit()

def generate_referral_code(length=6):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

def add_user(telegram_id, wallet_address=None, referral=None):
    personal_code = generate_referral_code()
    cursor.execute("""
        INSERT OR IGNORE INTO users (telegram_id, wallet_address, personal_code, referral_code_used)
        VALUES (?, ?, ?, ?)
    """, (telegram_id, wallet_address, personal_code, referral))
    conn.commit()

def get_user(user_id):
    cursor.execute("SELECT telegram_id, wallet_address, personal_code, referral_code_used, avatar_style FROM users WHERE telegram_id=?", (user_id,))
    return cursor.fetchone()

def update_avatar(uid, style):
    cursor.execute("UPDATE users SET avatar_style=? WHERE telegram_id=?", (style, uid))
    conn.commit()

# --- Flask routes ---
@app.route("/")
def home():
    return "✅ Bot avec MiniApp fonctionne"

@app.route("/app")
def mini_app():
    uid = request.args.get("uid")
    ref = request.args.get("ref")
    return render_template("app.html", uid=uid, ref=ref, public_base=PUBLIC_BASE_URL)

@app.route("/user/<int:uid>")
def get_user_data(uid):
    row = get_user(uid)
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify({
        "telegram_id": row[0],
        "wallet": row[1],
        "personal_code": row[2],
        "referral_code_used": row[3],
        "avatar_style": row[4]
    })

@app.route("/update_avatar", methods=["POST"])
def update_avatar_route():
    data = request.get_json()
    uid = int(data.get("uid"))
    style = data.get("avatar_style")
    update_avatar(uid, style)
    return jsonify({"ok": True})

# --- Bot menus ---
def main_menu():
    return ("Bienvenue !", InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Connect TON Wallet", callback_data="connect")]
    ]))

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    add_user(user_id)  # crée l’utilisateur si pas déjà en DB
    text, kb = main_menu()
    await update.message.reply_text(text, reply_markup=kb)

async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    action = query.data

    if action == "connect":
        nonce = secrets.token_hex(8)
        connect_url = f"{PUBLIC_BASE_URL}/app?uid={user_id}&nonce={nonce}"
        btn = InlineKeyboardMarkup([[
            InlineKeyboardButton("🚀 Ouvrir MiniApp", web_app=WebAppInfo(url=connect_url))
        ]])
        await query.edit_message_text("Clique pour ouvrir la MiniApp :", reply_markup=btn)

# --- Run bot ---
def run_bot():
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(menu_handler))
    application.run_polling()

# --- Run Flask ---
def run():
    app.run(host="0.0.0.0", port=8080)

def keep_alive():
    Thread(target=run).start()

if __name__ == "__main__":
    keep_alive()
    run_bot()
