import time

from flask import Flask, request
import telebot

import config


WEBHOOK_PATH = "/tg/webhook"

bot = telebot.TeleBot(config.TOKEN, parse_mode="HTML", threaded=False)
app = Flask(__name__)


@app.route(WEBHOOK_PATH, methods=["POST"])
def tg_webhook():
    data = request.get_data().decode("utf-8")
    print("[RAW UPDATE]", data[:300])
    bot.process_new_updates([telebot.types.Update.de_json(data)])
    return "OK", 200


@bot.message_handler(commands=["start"])
def start(message):
    print("[HANDLER] /start from", message.from_user.id)
    bot.send_message(message.chat.id, "Работает OK")


def set_webhook():
    url = config.WEBHOOK_URL.rstrip("/") + WEBHOOK_PATH
    bot.remove_webhook()
    time.sleep(1)
    ok = bot.set_webhook(url=url)
    print("[WEBHOOK]", url, "->", ok)


if __name__ == "__main__":
    set_webhook()
    app.run(host="0.0.0.0", port=8080)
