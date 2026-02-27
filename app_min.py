# app_min.py
import time
from flask import Flask, request
import telebot

TG_BOT_TOKEN = "8474476409:AAHws0hdlUqvBNOkrlzIz4h0I0v2q11NnOM"
WEBHOOK_HOST = "https://organizing-profession-frog-include.trycloudflare.com"
WEBHOOK_PATH = "/tg/webhook"

bot = telebot.TeleBot(TG_BOT_TOKEN, parse_mode="HTML", threaded=False)
app = Flask(__name__)

@app.route(WEBHOOK_PATH, methods=["POST"])
def tg_webhook():
    data = request.get_data().decode("utf-8")
    print("[RAW UPDATE]", data[:300])  # покажем сырые апдейты
    bot.process_new_updates([telebot.types.Update.de_json(data)])
    return "OK", 200

@bot.message_handler(commands=["start"])
def start(m):
    print("[HANDLER] /start from", m.from_user.id)
    bot.send_message(m.chat.id, "Работает ✅")

def set_webhook():
    url = WEBHOOK_HOST.rstrip("/") + WEBHOOK_PATH
    bot.remove_webhook(); time.sleep(1)
    ok = bot.set_webhook(url=url)
    print("[WEBHOOK]", url, "->", ok)

if __name__ == "__main__":
    set_webhook()
    app.run(host="0.0.0.0", port=8080)
