import os
import io
import contextlib
from dotenv import load_dotenv
import telebot

from generate_trades import generate_trades, TEMPLATE_FILE
from update_salesforce import update_salesforce

load_dotenv()

bot = telebot.TeleBot(os.getenv("TELEGRAM_BOT_TOKEN"))


@bot.message_handler(commands=["generate_trades"])
def handle_generate_trades(message):
    bot.reply_to(message, "Fetching trades from Neon...")
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            generate_trades()
        output = buf.getvalue().strip()
        with open(TEMPLATE_FILE, "rb") as f:
            bot.send_document(message.chat.id, f, caption=output or "Done.")
    except Exception as e:
        bot.reply_to(message, f"Error: {e}")


@bot.message_handler(commands=["update_salesforce"])
def handle_update_salesforce(message):
    msg = bot.reply_to(message, "Please send the Excel file.")
    bot.register_next_step_handler(msg, process_excel_upload)


def process_excel_upload(message):
    if not message.document:
        bot.reply_to(message, "No file received. Please run /update_salesforce and send an Excel file.")
        return
    if not message.document.file_name.lower().endswith(".xlsx"):
        bot.reply_to(message, "Please send an .xlsx file.")
        return

    bot.reply_to(message, "Updating Salesforce...")

    import tempfile
    file_info = bot.get_file(message.document.file_id)
    downloaded = bot.download_file(file_info.file_path)

    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        tmp.write(downloaded)
        tmp_path = tmp.name

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            update_salesforce(tmp_path)
        output = buf.getvalue().strip()
        bot.reply_to(message, output[-4000:] or "Done.")
    except Exception as e:
        bot.reply_to(message, f"Error: {e}")
    finally:
        os.remove(tmp_path)


if __name__ == "__main__":
    print("Bot started. Polling...")
    bot.infinity_polling()
