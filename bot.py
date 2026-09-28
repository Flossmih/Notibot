import os
import logging
import threading
from datetime import datetime, timedelta
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
import sqlite3

# ========== КОНФИГ ==========
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)

# ========== ВЕБ-СЕРВЕР ДЛЯ RENDER ==========
web_app = Flask(__name__)

@web_app.route('/')
@web_app.route('/health')
def health():
    return "Bot is running"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host='0.0.0.0', port=port, use_reloader=False)

# ========== БАЗА ДАННЫХ ==========
DB_PATH = "notes.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        text TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        text TEXT,
        remind_at TEXT
    )''')
    conn.commit()
    conn.close()

init_db()

def add_note(user_id, text):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO notes (user_id, text) VALUES (?, ?)", (user_id, text))
    conn.commit()
    conn.close()

def get_notes(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, text FROM notes WHERE user_id = ? ORDER BY id", (user_id,))
    rows = c.fetchall()
    conn.close()
    return rows

def delete_note(note_id, user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM notes WHERE id = ? AND user_id = ?", (note_id, user_id))
    conn.commit()
    conn.close()

def add_reminder(user_id, text, remind_at):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO reminders (user_id, text, remind_at) VALUES (?, ?, ?)",
              (user_id, text, remind_at.isoformat()))
    conn.commit()
    conn.close()

def get_due_reminders():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute("SELECT id, user_id, text FROM reminders WHERE remind_at <= ?", (now,))
    rows = c.fetchall()
    conn.close()
    return rows

def delete_reminder(reminder_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
    conn.commit()
    conn.close()

# ========== СОСТОЯНИЯ ==========
user_states = {}

# ========== МЕНЮ ==========
def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Добавить заметку", callback_data="add_note")],
        [InlineKeyboardButton("📋 Мои заметки", callback_data="list_notes")],
        [InlineKeyboardButton("⏰ Напоминание", callback_data="add_reminder")],
        [InlineKeyboardButton("🗑 Удалить заметку", callback_data="delete_note")]
    ])

# ========== КОМАНДЫ ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я бот для заметок и напоминаний.\n\nВыбери действие:",
        reply_markup=main_menu()
    )

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data == "add_note":
        user_states[user_id] = "waiting_note"
        await query.edit_message_text("📝 Напиши текст заметки:")

    elif data == "list_notes":
        notes = get_notes(user_id)
        if not notes:
            await query.edit_message_text("📋 У тебя пока нет заметок.", reply_markup=main_menu())
        else:
            text = "📋 Твои заметки:\n\n"
            for note_id, note_text in notes:
                text += f"{note_id}. {note_text}\n"
            await query.edit_message_text(text, reply_markup=main_menu())

    elif data == "add_reminder":
        user_states[user_id] = "waiting_reminder_text"
        await query.edit_message_text("⏰ Напиши текст напоминания:")

    elif data == "delete_note":
        user_states[user_id] = "waiting_delete_id"
        await query.edit_message_text("🗑 Напиши номер заметки, которую удалить:")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    state = user_states.get(user_id)

    if state == "waiting_note":
        add_note(user_id, text)
        user_states.pop(user_id, None)
        await update.message.reply_text("✅ Заметка добавлена!", reply_markup=main_menu())

    elif state == "waiting_reminder_text":
        user_states[user_id] = {"state": "waiting_reminder_time", "text": text}
        await update.message.reply_text("🕐 Теперь напиши время в формате 18:30 (ЧЧ:ММ).")

    elif isinstance(state, dict) and state.get("state") == "waiting_reminder_time":
        try:
            hour, minute = map(int, text.split(":"))
            now = datetime.now()
            remind_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if remind_at <= now:
                remind_at += timedelta(days=1)
            add_reminder(user_id, state["text"], remind_at)
            user_states.pop(user_id, None)
            await update.message.reply_text(
                f"✅ Напоминание установлено на {remind_at.strftime('%d.%m %H:%M')}",
                reply_markup=main_menu()
            )
        except:
            await update.message.reply_text("❌ Неверный формат. Напиши как 18:30.")

    elif state == "waiting_delete_id":
        try:
            note_id = int(text)
            delete_note(note_id, user_id)
            user_states.pop(user_id, None)
            await update.message.reply_text("✅ Заметка удалена.", reply_markup=main_menu())
        except:
            await update.message.reply_text("❌ Напиши номер заметки (число).")

    else:
        await update.message.reply_text("Используй меню: /start")

# ========== ПРОВЕРКА НАПОМИНАНИЙ ==========
async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
    due = get_due_reminders()
    for reminder_id, user_id, text in due:
        try:
            await context.bot.send_message(user_id, f"⏰ Напоминание:\n\n{text}")
            delete_reminder(reminder_id)
        except Exception as e:
            logging.error(f"Reminder error: {e}")

# ========== ЗАПУСК ==========
if __name__ == '__main__':
    threading.Thread(target=run_flask, daemon=True).start()
    
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(menu_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.job_queue.run_repeating(check_reminders, interval=30, first=10)
    logging.info("Bot started...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
