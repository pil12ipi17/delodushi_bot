import telebot
from telebot import types
from flask import Flask, request
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import requests
import json
from datetime import datetime, timedelta
import random
import config
import re

# --- Инициализация ---
bot = telebot.TeleBot(config.TOKEN)
app = Flask(__name__)

# --- Константы ---
CHANNEL_USERNAME = "@delo_dushi_ai"  # Канал для проверки подписки

# --- Google Sheets ---
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name(config.CREDENTIALS_FILE, scope)
client = gspread.authorize(creds)
sheet = client.open(config.GOOGLE_SHEET_NAME).sheet1

# --- Глобальные данные ---
texts = {}
states = {}
users_sheet = None
products_sheet = None
offers_sheet = None
OFFER_NEXT_AT_COL = None
waiting_for_consultation = set()
ADMIN_BROADCAST_STATE = "broadcast_message"
ADMIN_FILEID_STATE = "get_file_id"
PAID_DOB_STATE = "paid_dob"


# --- Загрузка текстов из таблицы ---
def load_texts():
    global texts
    records = sheet.get_all_records()
    texts = {}
    for r in records:
        t_type = str(r["Type"]).strip()
        key = str(r["Key"]).strip()
        text = str(r["Text"]).strip()
        if not t_type or not key or not text:
            continue
        if t_type not in texts:
            texts[t_type] = {}
        texts[t_type][key] = text
    print(f"[INFO] Загружено {len(records)} строк, {sum(len(v) for v in texts.values())} текстов из Google Sheets.")


load_texts()


def get_text_block(text_type, key="1"):
    return (texts.get(text_type, {}).get(str(key), "") or "").strip()


# --- Проверка подписки на канал ---
def check_subscription(user_id):
    try:
        member = bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in ['creator', 'administrator', 'member']
    except Exception as e:
        print(f"[ERROR] Ошибка проверки подписки для {user_id}: {e}")
        # В случае ошибки API возвращаем True чтобы не блокировать пользователей
        return True


# --- Подсчёт чисел ---
def reduce_to_one(num):
    while num > 9:
        num = sum(int(d) for d in str(num))
    return num


def parse_date(date_str):
    # Строгая проверка формата ДД.ММ.ГГГГ
    pattern = r'^(\d{2})\.(\d{2})\.(\d{4})$'
    match = re.match(pattern, date_str.strip())

    if not match:
        return None

    try:
        day = int(match.group(1))
        month = int(match.group(2))
        year = int(match.group(3))

        # Проверка существования даты через создание объекта datetime
        datetime(year, month, day)

        # Дополнительная проверка разумности года
        current_year = datetime.now().year
        if year < 1900 or year > current_year:
            return None

        return day, month, year
    except ValueError:
        # Дата не существует (например, 31.02.2020)
        return None
    except Exception as e:
        print(f"[ERROR] parse_date: {e}")
        return None


def calc_numbers(date_str):
    parsed = parse_date(date_str)
    if not parsed:
        return None
    day, month, year = parsed
    soul = reduce_to_one(day)
    destiny = reduce_to_one(sum(int(d) for d in f"{day:02d}{month:02d}{year}"))
    return soul, destiny, day


# --- Бесплатный расклад ---
def build_free_reading(soul, destiny, day):
    s1 = texts.get("soul_short", {}).get(str(soul), "—")
    s2 = texts.get("destiny_short", {}).get(str(destiny), "—")
    hint = texts.get("birthday_hint", {}).get(str(day), "—")
    end = texts.get("ending_free", {}).get("1", "")
    msg = (
        f"✨ Число Души ({soul}): {s1}\n\n"
        f"🌙 Число Судьбы ({destiny}): {s2}\n\n"
        f"🎁 Подсказка по дню рождения ({day}): {hint}\n\n"
        f"{end}"
    )
    return msg


# --- Платный расклад ---
def build_full_reading(soul, destiny=None, day=None):
    main_text = texts.get("soul_full", {}).get(str(soul), "")
    if main_text:
        main_text = f"✨ Число Души ({soul}):\n\n{main_text}"
    else:
        main_text = "Полный текст не найден 🌿"

    destiny_text = ""
    if destiny is not None:
        d_text = texts.get("destiny_full", {}).get(str(destiny))
        if d_text:
            destiny_text = f"\n\n🌙 Число Судьбы ({destiny}):\n\n{d_text}"

    birthday_text = ""
    if day:
        b_text = texts.get("birthday_full", {}).get(str(day))
        if b_text:
            birthday_text = f"\n\n🎁 Дополнение по твоему дню рождения ({day}):\n\n{b_text}"

    return main_text + destiny_text + birthday_text


# --- Отправка длинных сообщений ---
def send_long_message(chat_id, text, parse_mode=None):
    MAX_LEN = 4000
    text = text.strip()

    while text:
        if len(text) <= MAX_LEN:
            part = text
            text = ""
        else:
            split_index = text.rfind("\n", 0, MAX_LEN)
            if split_index == -1:
                split_index = text.rfind(" ", 0, MAX_LEN)
            if split_index == -1:
                split_index = MAX_LEN
            part = text[:split_index].strip()
            text = text[split_index:].strip()

        try:
            bot.send_message(chat_id, part, parse_mode=parse_mode)
        except Exception as e:
            print(f"[ERROR] send_long_message failed on part: {e}")


# --- Работа с таблицами ---
def get_sheet(name):
    try:
        return client.open(config.GOOGLE_SHEET_NAME).worksheet(name)
    except Exception as e:
        print(f"[ERROR] Не удалось открыть лист {name}: {e}")
        return None


users_sheet = get_sheet("Users")
products_sheet = get_sheet("Products")
offers_sheet = get_sheet("offers")


def save_user_data(user_id, username, name, date=None, soul=None, destiny=None, product=None):
    if not users_sheet:
        return
    try:
        records = users_sheet.get_all_records()
        found_row = None
        for i, row in enumerate(records, start=2):
            if str(row.get("User ID")) == str(user_id):
                found_row = i
                break
        if found_row:
            if date: users_sheet.update_cell(found_row, 5, date)
            if soul: users_sheet.update_cell(found_row, 6, soul)
            if destiny: users_sheet.update_cell(found_row, 7, destiny)
            if product:
                current_value = records[found_row - 2].get("Product") or ""
                existing = [p.strip().lower() for p in current_value.split(",") if p.strip()]
                if product.lower() not in existing:
                    merged = ", ".join(filter(None, [current_value.strip(), product]))
                    users_sheet.update_cell(found_row, 8, merged)
        else:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            users_sheet.append_row([now, user_id, username, name, date or "", soul or "", destiny or "", product or ""])
    except Exception as e:
        print(f"[ERROR] save_user_data: {e}")


def get_active_products():
    items = []
    if not products_sheet:
        return items
    try:
        records = products_sheet.get_all_records()
        for r in records:
            if str(r.get("Active")).upper() == "TRUE":
                price_raw = r.get("Price")
                try:
                    price = int(price_raw)
                except Exception:
                    price = 0
                offer_text = (r.get("OfferText") or r.get("Offer Text") or r.get("offer_text") or r.get("offertext") or "").strip()
                items.append({
                    "name": r.get("Name"),
                    "price": price,
                    "description": r.get("Description"),
                    "file_url": (r.get("FileURL") or "").strip(),
                    "delivery_text": (r.get("DeliveryText") or "").strip(),
                    "offer_text": offer_text
                })
    except Exception as e:
        print(f"[ERROR] get_active_products: {e}")
    return items


def format_product_button(product):
    description = product.get("description") or product.get("name") or "Продукт"
    price = product.get("price")
    if price:
        return f"💫 {description} — {price} ₽"
    return f"💫 {description}"


def show_products_list(chat_id, include_full=True):
    try:
        products = get_active_products()
        if not include_full:
            products = [p for p in products if str(p.get("name") or "").lower() != "full_reading"]
        if not products:
            bot.send_message(chat_id, "Пока нет активных продуктов.")
            return
        markup = types.InlineKeyboardMarkup()
        for p in products:
            markup.add(
                types.InlineKeyboardButton(
                    text=format_product_button(p),
                    callback_data=f"buy_{p['name']}"
                )
            )
        bot.send_message(chat_id, "Доступные продукты:", reply_markup=markup)
    except Exception as e:
        print(f"[ERROR] show_products_list: {e}")
        bot.send_message(chat_id, "Не удалось загрузить список продуктов. Попробуйте позже.")


def get_product_by_name(name, active_only=True):
    if not products_sheet:
        return None
    try:
        records = products_sheet.get_all_records()
        for r in records:
            if str(r.get("Name")).strip() == str(name).strip():
                if active_only and str(r.get("Active")).upper() != "TRUE":
                    return None
                price_raw = r.get("Price")
                try:
                    price = int(price_raw)
                except Exception:
                    price = 0
                return {
                    "name": r.get("Name"),
                    "price": price,
                    "description": r.get("Description"),
                    "file_url": (r.get("FileURL") or "").strip(),
                    "delivery_text": (r.get("DeliveryText") or "").strip()
                }
    except Exception as e:
        print(f"[ERROR] get_product_by_name: {e}")
    return None


def deliver_product(user_id, product):
    if not product:
        bot.send_message(user_id, "⚠️ Продукт временно недоступен. Свяжитесь с поддержкой, пожалуйста.")
        return

    delivery_text = product.get("delivery_text")
    if delivery_text:
        send_long_message(user_id, delivery_text)

    file_url = product.get("file_url")
    if file_url:
        try:
            bot.send_document(user_id, file_url, caption=product.get("description") or product.get("name"))
        except Exception as e:
            print(f"[ERROR] deliver_product send_document failed: {e}")
            bot.send_message(
                user_id,
                f"Не удалось автоматически отправить файл. Заберите материал по ссылке:\n{file_url}"
            )
    elif not delivery_text:
        bot.send_message(user_id, "Материал будет отправлен дополнительно. Если его нет в течение часа — напишите нам.")


def get_all_user_ids():
    ids = set()
    if not users_sheet:
        return ids
    try:
        records = users_sheet.get_all_records()
        for row in records:
            uid = row.get("User ID")
            try:
                if uid:
                    ids.add(int(uid))
            except Exception:
                continue
    except Exception as e:
        print(f"[ERROR] get_all_user_ids: {e}")
    return ids


# --- Отложенные офферы (лист offers) ---
def get_offer_cols():
    """
    Возвращает маппинг колонок листа offers в виде имен -> индексов (1-based).
    """
    if not offers_sheet:
        return {}
    try:
        header = offers_sheet.row_values(1)
        lower = [h.lower().strip() for h in header]
        cols = {}
        for name in ["title", "send at", "audience", "text", "text key", "product", "repeat days", "status", "flow", "step", "delay hours", "skip if has product"]:
            if name in lower:
                cols[name] = lower.index(name) + 1
        return cols
    except Exception as e:
        print(f"[ERROR] get_offer_cols: {e}")
        return {}


def resolve_audience(audience_raw, user_records):
    """
    Поддержка простых фильтров: all, ids=1,2,3, has_product=full_reading, stage<=1.
    """
    audience = str(audience_raw or "").strip()
    audience_l = audience.lower()
    targets = []

    def add_user(row):
        try:
            uid = int(row.get("User ID"))
            targets.append(uid)
        except Exception:
            return

    if not audience or audience_l == "all":
        for r in user_records:
            add_user(r)
        return targets

    if audience_l.startswith("ids="):
        ids_part = audience.split("=", 1)[1]
        parts = re.split(r"[,\s;]+", ids_part)
        for p in parts:
            try:
                if p:
                    targets.append(int(p))
            except Exception:
                continue
        return targets

    if audience_l.startswith("has_product="):
        target = audience.split("=", 1)[1].strip().lower()
        for r in user_records:
            prods_raw = str(r.get("Product") or "").lower()
            prods = [p.strip() for p in prods_raw.split(",") if p.strip()]
            if target in prods:
                add_user(r)
        return targets

    if audience_l.startswith("stage<="):
        try:
            limit = int(audience.split("=", 1)[1])
        except Exception:
            return targets
        for r in user_records:
            st = parse_practice_stage(r.get("Practice Stage"))
            if st is not None and st <= limit:
                add_user(r)
        return targets

    # По умолчанию отправляем всем
    for r in user_records:
        add_user(r)
    return targets


def send_offer_message(user_id, text, markup=None):
    """
    Отправка оффера с учетом длинных сообщений: если очень длинный текст, кнопка уходит отдельным сообщением.
    """
    try:
        if markup and len(text) <= 3500:
            bot.send_message(user_id, text, reply_markup=markup)
        else:
            send_long_message(user_id, text)
            if markup:
                bot.send_message(user_id, "Предложение:", reply_markup=markup)
        return True
    except Exception as e:
        print(f"[ERROR] send_offer_message to {user_id}: {e}")
        return False


def process_offers():
    """
    Обрабатывает лист offers: отправляет pending-офферы, у которых Send At уже наступило.
    """
    if not offers_sheet:
        return "offers sheet not configured"
    try:
        cols = get_offer_cols()
        status_col = cols.get("status")
        send_col = cols.get("send at")
        repeat_col = cols.get("repeat days")
        records = offers_sheet.get_all_records()
        user_records = users_sheet.get_all_records() if users_sheet else []
        now = datetime.now()

        for idx, row in enumerate(records, start=2):
            # строки, участвующие в flow, пропускаем здесь (ими управляет отдельный обработчик)
            flow_marker = str(row.get("Flow") or row.get("flow") or "").strip()
            if flow_marker:
                continue

            status_raw = str(row.get("Status") or "").strip().lower()
            if status_raw not in ("", "pending"):
                continue

            send_at = parse_practice_datetime(row.get("Send At"))
            if not send_at:
                if status_col:
                    offers_sheet.update_cell(idx, status_col, "error")
                print(f"[ERROR] offer row {idx}: invalid Send At")
                continue

            if send_at > now:
                continue

            text = str(row.get("Text") or "").strip()
            if not text:
                text_key = row.get("Text Key") or row.get("TextKey") or ""
                if text_key:
                    text = get_text_block(str(text_key).strip())
            if not text:
                title = row.get("Title") or ""
                text = str(title).strip()
            if not text:
                if status_col:
                    offers_sheet.update_cell(idx, status_col, "error")
                print(f"[ERROR] offer row {idx}: empty text")
                continue

            audience_raw = row.get("Audience") or "all"
            targets = resolve_audience(audience_raw, user_records)
            if not targets:
                print(f"[DEBUG] offer row {idx}: no audience matched ({audience_raw})")
                continue

            product_name = str(row.get("Product") or "").strip()
            product = get_product_by_name(product_name) if product_name else None
            markup = None
            if product:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton(
                    text=format_product_button(product),
                    callback_data=f"buy_{product['name']}"
                ))

            sent = 0
            for uid in targets:
                if send_offer_message(uid, text, markup=markup):
                    sent += 1

            repeat_days = 0
            try:
                repeat_raw = row.get("Repeat Days")
                if repeat_raw is not None and str(repeat_raw).strip() != "":
                    repeat_days = int(float(str(repeat_raw).replace(",", ".").strip()))
            except Exception:
                repeat_days = 0

            if repeat_days > 0 and send_col and status_col:
                next_at = send_at + timedelta(days=repeat_days)
                offers_sheet.update_cell(idx, send_col, next_at.strftime("%Y-%m-%d %H:%M:%S"))
                offers_sheet.update_cell(idx, status_col, "pending")
            elif status_col:
                offers_sheet.update_cell(idx, status_col, "sent")

            print(f"[INFO] offer row {idx} sent to {sent} users")

    except Exception as e:
        print(f"[ERROR] process_offers: {e}")
        return "error"
    return "ok"


# --- Практики (отложенная отправка) ---
PRACTICE_STAGE_COL = None
PRACTICE_NEXT_AT_COL = None


def init_practice_columns():
    global PRACTICE_STAGE_COL, PRACTICE_NEXT_AT_COL
    if not users_sheet:
        return
    try:
        header = users_sheet.row_values(1)
        header_lower = [h.lower() for h in header]
        updated = False

        if "practice stage" in header_lower:
            PRACTICE_STAGE_COL = header_lower.index("practice stage") + 1
        else:
            header.append("Practice Stage")
            PRACTICE_STAGE_COL = len(header)
            updated = True

        if "practice next at" in header_lower:
            PRACTICE_NEXT_AT_COL = header_lower.index("practice next at") + 1
        else:
            header.append("Practice Next At")
            PRACTICE_NEXT_AT_COL = len(header)
            updated = True

        if updated:
            users_sheet.update("A1", [header])
    except Exception as e:
        print(f"[ERROR] init_practice_columns: {e}")


init_practice_columns()


def parse_practice_stage(raw):
    """
    �?�?�?�?�?�� �?�?��?�? �� Practice Stage: допускаем int/float/str вида "1" или "1.0".
    """
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except Exception:
        try:
            return int(float(str(raw).replace(",", ".").strip()))
        except Exception:
            return None


def parse_practice_datetime(raw):
    """
    �?�?�?��?�?�? Practice Next At �?�?� datetime. �?�?�?�?�?�? ISO, "YYYY-MM-DD HH:MM[:SS]" �?� Google serial.
    """
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw

    if isinstance(raw, (int, float)):
        try:
            return datetime(1899, 12, 30) + timedelta(days=float(raw))
        except Exception:
            pass

    # �?�?�?�?��?�?�� numeric �?� string �? serial
    try:
        if isinstance(raw, str) and raw.replace(".", "", 1).replace(",", "", 1).isdigit():
            return datetime(1899, 12, 30) + timedelta(days=float(raw.replace(",", ".")))
    except Exception:
        pass

    text = str(raw).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except Exception:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "").replace("T", " "))
    except Exception:
        return None


def get_practice_send_time():
    """
    Время отправки практик (hour, minute) из текста practice_time (например, '21:00').
    Если не задано — 10:00.
    """
    raw = get_text_block("practice_time")
    if not raw:
        return 10, 0
    try:
        raw = raw.strip().split()[0]
        parts = raw.split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        if 0 <= hour < 24 and 0 <= minute < 60:
            return hour, minute
    except Exception as e:
        print(f"[ERROR] get_practice_send_time parse failed: {e}")
    return 10, 0


def compute_next_practice_datetime(days_from_now=1):
    now = datetime.now()
    hour, minute = get_practice_send_time()
    target_date = (now + timedelta(days=days_from_now)).date()
    return datetime(target_date.year, target_date.month, target_date.day, hour, minute)


def set_user_practice_state(user_id, stage, next_dt):
    """
    Обновляет поля Practice Stage / Practice Next At для пользователя в листе Users.
    """
    if not users_sheet:
        return
    if PRACTICE_STAGE_COL is None or PRACTICE_NEXT_AT_COL is None:
        init_practice_columns()
        if PRACTICE_STAGE_COL is None or PRACTICE_NEXT_AT_COL is None:
            print("[ERROR] practice columns not configured; cannot save state")
            return
    try:
        records = users_sheet.get_all_records()
        found_row = None
        for i, row in enumerate(records, start=2):
            if str(row.get("User ID")) == str(user_id):
                found_row = i
                break
        if not found_row:
            return
        users_sheet.update_cell(found_row, PRACTICE_STAGE_COL, "" if stage is None else stage)
        users_sheet.update_cell(
            found_row,
            PRACTICE_NEXT_AT_COL,
            next_dt.strftime("%Y-%m-%d %H:%M:%S") if next_dt else ""
        )
    except Exception as e:
        print(f"[ERROR] set_user_practice_state: {e}")


def start_practices_for_user(user_id):
    """
    Отправляет practice_1 и планирует дату следующего шага.
    """
    if PRACTICE_STAGE_COL is None or PRACTICE_NEXT_AT_COL is None:
        init_practice_columns()
        if PRACTICE_STAGE_COL is None or PRACTICE_NEXT_AT_COL is None:
            print("[ERROR] practice columns not configured; cannot start practices")
            return

    practice_1 = get_text_block("practice_1")
    if not practice_1:
        return

    send_long_message(user_id, practice_1)
    next_dt = compute_next_practice_datetime(days_from_now=1)
    set_user_practice_state(user_id, stage=1, next_dt=next_dt)
    try:
        human_dt = next_dt.strftime("%d.%m.%Y в %H:%M")
        bot.send_message(user_id, f"Следующая практика будет отправлена {human_dt}.")
    except Exception as e:
        print(f"[ERROR] notify next practice failed: {e}")


def process_practices():
    """
    Планировщик практик: проверяет пользователей и шлет очередное сообщение (вызывается cron-ом через /run_practices).
    """
    if not users_sheet:
        return "practice columns not configured"
    if PRACTICE_STAGE_COL is None or PRACTICE_NEXT_AT_COL is None:
        init_practice_columns()
        if PRACTICE_STAGE_COL is None or PRACTICE_NEXT_AT_COL is None:
            return "practice columns not configured"
    try:
        records = users_sheet.get_all_records()
        now = datetime.now()
        for idx, row in enumerate(records, start=2):
            try:
                uid_raw = row.get("User ID")
                if not uid_raw:
                    continue
                user_id = int(uid_raw)
            except Exception:
                continue

            stage_raw = row.get("Practice Stage")
            next_at_raw = row.get("Practice Next At")
            stage = parse_practice_stage(stage_raw)
            next_at = parse_practice_datetime(next_at_raw)
            if stage is None or not next_at:
                print(f"[DEBUG] practice skip user {user_id}: stage_raw={stage_raw}, next_raw={next_at_raw}")
                continue

            if next_at > now:
                continue

            if stage == 0:
                intro = get_text_block("practice_intro")
                if intro:
                    practices_markup = types.InlineKeyboardMarkup()
                    practices_markup.add(
                        types.InlineKeyboardButton("Начать практики", callback_data="start_practices")
                    )
                    bot.send_message(user_id, intro, reply_markup=practices_markup)
                    print(f"[INFO] practice intro sent to {user_id}")
                set_user_practice_state(user_id, stage=0, next_dt=None)

            elif stage == 1:
                text = get_text_block("practice_2")
                if text:
                    send_long_message(user_id, text)
                next_dt = compute_next_practice_datetime(days_from_now=1)
                set_user_practice_state(user_id, stage=2, next_dt=next_dt)
                try:
                    human_dt = next_dt.strftime("%d.%m.%Y в %H:%M")
                    bot.send_message(user_id, f"Следующее послание придет завтра {human_dt}.")
                except Exception as e:
                    print(f"[ERROR] notify next practice (stage 2) failed: {e}")
                print(f"[INFO] practice day2 sent to {user_id}")

            elif stage == 2:
                day3 = get_text_block("practice_3")
                final = get_text_block("practice_final_offer")
                combined = "\n\n".join(filter(None, [day3, final]))
                if combined:
                    send_long_message(user_id, combined)
                # Дополнительная кнопка на курс guide_2, если активен в Products
                guide_product = get_product_by_name("guide_2")
                if guide_product:
                    markup = types.InlineKeyboardMarkup()
                    markup.add(types.InlineKeyboardButton(
                        text=format_product_button(guide_product),
                        callback_data=f"buy_{guide_product['name']}"
                    ))
                    bot.send_message(user_id, "Если готова продолжить, можешь оформить курс:", reply_markup=markup)
                set_user_practice_state(user_id, stage=3, next_dt=None)
                print(f"[INFO] practice day3+offer sent to {user_id}")

    except Exception as e:
        print(f"[ERROR] process_practices: {e}")
        return "error"
    return "ok"


# --- Ежедневный оффер по отсутствующим продуктам (стартует после free) ---
def init_offer_columns():
    global OFFER_NEXT_AT_COL
    if not users_sheet:
        return
    try:
        header = users_sheet.row_values(1)
        header_lower = [h.lower() for h in header]
        updated = False

        if "offer next at" in header_lower:
            OFFER_NEXT_AT_COL = header_lower.index("offer next at") + 1
        else:
            header.append("Offer Next At")
            OFFER_NEXT_AT_COL = len(header)
            updated = True

        if updated:
            users_sheet.update("A1", [header])
    except Exception as e:
        print(f"[ERROR] init_offer_columns: {e}")


def set_user_offer_next_at(user_id, next_dt):
    if not users_sheet:
        return
    if OFFER_NEXT_AT_COL is None:
        init_offer_columns()
        if OFFER_NEXT_AT_COL is None:
            print("[ERROR] offer column not configured; cannot save next at")
            return
    try:
        records = users_sheet.get_all_records()
        found_row = None
        for i, row in enumerate(records, start=2):
            if str(row.get("User ID")) == str(user_id):
                found_row = i
                break
        if not found_row:
            return
        users_sheet.update_cell(
            found_row,
            OFFER_NEXT_AT_COL,
            next_dt.strftime("%Y-%m-%d %H:%M:%S") if next_dt else ""
        )
    except Exception as e:
        print(f"[ERROR] set_user_offer_next_at: {e}")


def process_daily_offers():
    """
    Раз в сутки предлагает случайный отсутствующий продукт (если стартовали после free).
    """
    if not users_sheet:
        return "offer columns not configured"
    if OFFER_NEXT_AT_COL is None:
        init_offer_columns()
        if OFFER_NEXT_AT_COL is None:
            return "offer columns not configured"
    try:
        records = users_sheet.get_all_records()
        products = get_active_products()
        now = datetime.now()

        for idx, row in enumerate(records, start=2):
            try:
                uid_raw = row.get("User ID")
                if not uid_raw:
                    continue
                user_id = int(uid_raw)
            except Exception:
                continue

            next_raw = row.get("Offer Next At")
            next_at = parse_practice_datetime(next_raw)
            if not next_at or next_at > now:
                continue

            owned_raw = str(row.get("Product") or "").lower()
            owned = {p.strip() for p in owned_raw.split(",") if p.strip()}
            missing = [p for p in products if str(p.get("name") or "").lower() not in owned]
            if not missing:
                # нет чего предлагать, проверим завтра
                set_user_offer_next_at(user_id, now + timedelta(hours=24))
                continue

            with_offer = [p for p in missing if p.get("offer_text")]
            pool = with_offer or missing
            product = random.choice(pool)

            offer_text = product.get("offer_text") or ""
            if offer_text:
                print(f"[INFO] daily offer text from OfferText for {user_id}: {offer_text}")
            text = offer_text or product.get("description") or product.get("name") or "Предложение"
            print(f"[DEBUG] daily offer product payload for {user_id}: {product}")
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton(
                text=format_product_button(product),
                callback_data=f"buy_{product['name']}"
            ))
            send_offer_message(user_id, text, markup=markup)
            print(f"[INFO] daily offer sent to {user_id} for {product.get('name')}")

            # следующий раз через 24 часа
            set_user_offer_next_at(user_id, now + timedelta(hours=24))

    except Exception as e:
        print(f"[ERROR] process_daily_offers: {e}")
        return "error"
    return "ok"

def create_payment(amount, description, user_id, metadata=None):
    url = "https://api.yookassa.ru/v3/payments"
    headers = {
        "Idempotence-Key": str(user_id) + "_" + datetime.now().strftime("%H%M%S"),
        "Content-Type": "application/json"
    }
    payment_metadata = {"user_id": str(user_id)}
    if metadata:
        payment_metadata.update(metadata)
    data = {
        "amount": {"value": str(amount), "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": "https://t.me/" + bot.get_me().username},
        "description": description,
        "metadata": payment_metadata
    }
    response = requests.post(url, auth=(str(config.SHOP_ID), config.PAYMENT_TOKEN), headers=headers,
                             data=json.dumps(data))
    res_json = response.json()
    return res_json.get("confirmation", {}).get("confirmation_url"), res_json.get("id")

def start_full_payment(user_id):
    """
    Создаёт платёж на полный расклад. Предполагается, что дата рождения уже сохранена.
    """
    product = get_product_by_name("full_reading", active_only=False)
    amount = product["price"] if product and product.get("price") else 300
    description = product.get("description") if product else "Полный нумерологический расклад"

    url, pay_id = create_payment(
        amount,
        description,
        user_id,
        metadata={"product_name": "full_reading"}
    )
    if url:
        bot.send_message(
            user_id,
            f"💳 Перейди по ссылке для оплаты:\n{url}\n\nПосле оплаты расклад придёт автоматически."
        )
    else:
        bot.send_message(user_id, "⚠️ Ошибка при создании платежа.")



# --- Главное меню ---
def main_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("🔹 Бесплатный расклад", "💫 Мои продукты")
    markup.add("📞 Консультация", "ℹ️ Поддержка")
    markup.add("🛒 Список продуктов")
    return markup


# --- Команды ---
@bot.message_handler(commands=["start"])
def start_message(message):
    user_id = message.from_user.id

    # Проверяем подписку на канал
    if not check_subscription(user_id):
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("📢 Подписаться на канал", url="https://t.me/delo_dushi_ai"))
        markup.add(types.InlineKeyboardButton("✅ Проверить подписку", callback_data="check_subscription"))

        bot.send_message(
            message.chat.id,
            "🌸 Чтобы получить свой персональный нумерологический расклад, подпишись на наш канал!\n\n"
            "После подписки нажми кнопку «Проверить подписку» ниже 👇",
            reply_markup=markup
        )
        return

    # Если подписан - показываем приветствие и меню
    greet = texts.get("greeting", {}).get("1", "💬 Приветствую! Напиши свою дату рождения в формате ДД.ММ.ГГГГ.")
    bot.send_message(message.chat.id, greet, reply_markup=main_menu())
    save_user_data(message.from_user.id, message.from_user.username, message.from_user.first_name)


@bot.callback_query_handler(func=lambda c: c.data == "check_subscription")
def check_sub_callback(callback_query):
    user_id = callback_query.from_user.id

    if check_subscription(user_id):
        bot.answer_callback_query(callback_query.id, "✅ Отлично! Подписка подтверждена")
        greet = texts.get("greeting", {}).get("1", "💬 Приветствую! Напиши свою дату рождения в формате ДД.ММ.ГГГГ.")
        bot.send_message(callback_query.message.chat.id, greet, reply_markup=main_menu())
        save_user_data(user_id, callback_query.from_user.username, callback_query.from_user.first_name)
    else:
        bot.answer_callback_query(
            callback_query.id,
            "❌ Подписка не найдена. Пожалуйста, подпишитесь на канал сначала.",
            show_alert=True
        )


@bot.message_handler(commands=["menu"])
def show_menu(message):
    bot.send_message(message.chat.id, "🌸 Главное меню:", reply_markup=main_menu())


@bot.message_handler(commands=["reload_texts"])
def reload_texts_cmd(message):
    if message.from_user.id in config.ADMIN_IDS:
        load_texts()
        bot.reply_to(message, "✅ Тексты обновлены из Google Sheets.")
    else:
        bot.reply_to(message, "⛔ У вас нет прав на эту команду.")


# --- Пользовательские кнопки ---
@bot.message_handler(func=lambda m: m.text == "🔹 Бесплатный расклад")
def menu_free(message):
    bot.send_message(message.chat.id, "✨ Введи свою дату рождения в формате ДД.ММ.ГГГГ.",
                     reply_markup=types.ReplyKeyboardRemove())


@bot.message_handler(func=lambda m: m.text == "💫 Мои продукты")


@bot.message_handler(func=lambda m: m.text == "🛒 Список продуктов")
def menu_products_list(message):
    show_products_list(message.chat.id, include_full=True)

@bot.message_handler(commands=["products"])
def products_cmd(message):
    show_products_list(message.chat.id, include_full=True)

def menu_products(message):
    try:
        records = users_sheet.get_all_records()
        row = next((r for r in records if str(r.get("User ID")) == str(message.from_user.id)), None)
        if row and row.get("Product"):
            bot.send_message(message.chat.id, f"💎 Твои приобретённые продукты:\n{row.get('Product')}")
        else:
            bot.send_message(message.chat.id, "🌿 У тебя пока нет приобретённых продуктов.")
    except Exception as e:
        bot.send_message(message.chat.id, "⚠️ Ошибка при загрузке данных.")
        print(f"[ERROR] menu_products: {e}")


@bot.message_handler(func=lambda m: m.text == "📞 Консультация")
def menu_consultation(message):
    bot.send_message(message.chat.id, "💌 Напиши свой запрос, и я передам его Екатерине лично.")
    waiting_for_consultation.add(message.from_user.id)


@bot.callback_query_handler(func=lambda c: c.data == "consultation")
def consultation_callback(callback_query):
    user_id = callback_query.from_user.id
    bot.answer_callback_query(callback_query.id)
    bot.send_message(user_id, "💌 Напиши свой запрос, и я передам его Екатерине лично.")
    waiting_for_consultation.add(user_id)


@bot.message_handler(func=lambda m: m.from_user.id in waiting_for_consultation and not m.text.startswith('/'))
def handle_consultation_message(message):
    user_id = message.from_user.id
    username = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
    text = message.text.strip()

    # Отправка Екатерине
    for admin_id in config.ADMIN_IDS:
        bot.send_message(admin_id, f"✉️ Новый запрос на консультацию:\n\nОт: {username} (ID: {user_id})\n\n{text}")

    save_user_data(user_id, message.from_user.username, message.from_user.first_name, product="Consultation Request")
    bot.send_message(user_id, "✨ Спасибо! Екатерина получила твой запрос 🌸", reply_markup=main_menu())
    waiting_for_consultation.discard(user_id)


@bot.message_handler(func=lambda m: m.text == "ℹ️ Поддержка")
def menu_support(message):
    support_text = texts.get("support", {}).get("1", "📩 По вопросам поддержки напиши: @dombiznesa")
    bot.send_message(message.chat.id, support_text)


@bot.message_handler(func=lambda m: states.get(m.from_user.id) == PAID_DOB_STATE)
def handle_paid_dob(message):
    date_str = message.text.strip()

    if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', date_str):
        bot.send_message(
            message.chat.id,
            "❌ Неверный формат. Введите дату рождения в формате ДД.ММ.ГГГГ\n\nНапример: 15.06.1990"
        )
        return

    result = calc_numbers(date_str)
    if not result:
        bot.send_message(
            message.chat.id,
            "❌ Некорректная дата. Проверьте правильность даты и введите в формате ДД.ММ.ГГГГ\n\nНапример: 15.06.1990"
        )
        return

    soul, destiny, day = result
    save_user_data(message.from_user.id, message.from_user.username, message.from_user.first_name, date_str, soul, destiny)
    states.pop(message.from_user.id, None)

    bot.send_message(
        message.chat.id,
        f"✨ Число Души: {soul}\n🌙 Число Судьбы: {destiny}\n\nГотово! Ниже — ссылка на оплату полного расклада."
    )
    start_full_payment(message.from_user.id)


@bot.message_handler(commands=["cancel"])
def cancel_state(message):
    if states.pop(message.from_user.id, None):
        bot.reply_to(message, "Отменено.")
    else:
        bot.reply_to(message, "Нет активных действий для отмены.")


@bot.message_handler(content_types=['text', 'photo', 'document'], func=lambda m: states.get(m.from_user.id) == ADMIN_BROADCAST_STATE)
def handle_admin_broadcast_message(message):
    user_id = message.from_user.id
    if user_id not in config.ADMIN_IDS:
        states.pop(user_id, None)
        return

    recipients = get_all_user_ids()
    if not recipients:
        bot.reply_to(message, "Список получателей пуст.")
        states.pop(user_id, None)
        return

    sent = 0
    failed = 0
    caption = getattr(message, "caption", None)

    for uid in recipients:
        try:
            if message.content_type == "text":
                bot.send_message(uid, message.text)
            elif message.content_type == "photo":
                file_id = message.photo[-1].file_id
                bot.send_photo(uid, file_id, caption=caption)
            elif message.content_type == "document":
                bot.send_document(uid, message.document.file_id, caption=caption)
            else:
                continue
            sent += 1
        except Exception as e:
            failed += 1
            print(f"[ERROR] broadcast to {uid} failed: {e}")

    states.pop(user_id, None)
    bot.reply_to(message, f"Готово! ✅ {sent} получателей, ошибок: {failed}.")


# --- Дата рождения ---
@bot.message_handler(content_types=['document','photo','video','audio','voice','animation','video_note','sticker','text'], func=lambda m: states.get(m.from_user.id) == ADMIN_FILEID_STATE)
def handle_admin_fileid_message(message):
    uid = message.from_user.id
    if uid not in config.ADMIN_IDS:
        states.pop(uid, None)
        return

    def reply(text):
        try:
            bot.reply_to(message, text)
        except Exception as e:
            print(f"[ERROR] reply in fileid helper: {e}")

    ct = message.content_type
    if ct == 'document' and getattr(message, 'document', None):
        name = getattr(message.document, 'file_name', '') or ''
        reply(f"file_id: {message.document.file_id}\nfile_name: {name}")
    elif ct == 'photo' and getattr(message, 'photo', None):
        reply(f"file_id: {message.photo[-1].file_id} (photo)")
    elif ct == 'video' and getattr(message, 'video', None):
        reply(f"file_id: {message.video.file_id} (video)")
    elif ct == 'audio' and getattr(message, 'audio', None):
        reply(f"file_id: {message.audio.file_id} (audio)")
    elif ct == 'voice' and getattr(message, 'voice', None):
        reply(f"file_id: {message.voice.file_id} (voice)")
    elif ct == 'animation' and getattr(message, 'animation', None):
        reply(f"file_id: {message.animation.file_id} (animation)")
    elif ct == 'video_note' and getattr(message, 'video_note', None):
        reply(f"file_id: {message.video_note.file_id} (video_note)")
    elif ct == 'sticker' and getattr(message, 'sticker', None):
        reply(f"file_id: {message.sticker.file_id} (sticker)")
    else:
        reply("Пришлите документ/фото/видео/аудио — я отвечу его file_id. Для выхода отправьте /cancel.")

@bot.message_handler(func=lambda m: not m.text.startswith('/') and m.from_user.id not in waiting_for_consultation and states.get(m.from_user.id) not in (ADMIN_BROADCAST_STATE, ADMIN_FILEID_STATE, PAID_DOB_STATE))
def handle_date(message):
    date_str = message.text.strip()

    # Проверяем формат даты
    if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', date_str):
        bot.send_message(
            message.chat.id,
            "❌ Неверный формат. Введите дату рождения в формате ДД.ММ.ГГГГ\n\nНапример: 15.06.1990"
        )
        return

    result = calc_numbers(date_str)
    if not result:
        bot.send_message(
            message.chat.id,
            "❌ Некорректная дата. Проверьте правильность даты и введите в формате ДД.ММ.ГГГГ\n\nНапример: 15.06.1990"
        )
        return

    soul, destiny, day = result
    pre_free = get_text_block("pre_free_info")
    if pre_free:
        send_long_message(message.chat.id, pre_free)
    msg = build_free_reading(soul, destiny, day)
    markup = types.InlineKeyboardMarkup()
    full_product = get_product_by_name("full_reading")
    if full_product:
        offer_text = format_product_button(full_product)
    else:
        offer_text = texts.get("offer", {}).get("full_reading", "💫 Получить полный расклад")
    markup.add(types.InlineKeyboardButton(
        text=offer_text,
        callback_data=f"pay_{soul}_{day}"
    ))
    save_user_data(message.from_user.id, message.from_user.username, message.from_user.first_name, date_str, soul,
                   destiny)
    # старт ежедневного оффера (через 24 часа)
    set_user_offer_next_at(message.from_user.id, datetime.now() + timedelta(hours=24))
    bot.send_message(message.chat.id, msg, reply_markup=markup)


# --- Оплата ---


@bot.callback_query_handler(func=lambda c: c.data == "list_products")
def show_products_cb(callback_query):
    bot.answer_callback_query(callback_query.id)
    show_products_list(callback_query.from_user.id, include_full=True)

@bot.callback_query_handler(func=lambda c: c.data == "start_practices")
def start_practices_cb(callback_query):
    user_id = callback_query.from_user.id
    bot.answer_callback_query(callback_query.id)
    start_practices_for_user(user_id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("pay_"))
def handle_payment(callback_query):
    user_id = callback_query.from_user.id
    # Всегда запрашиваем дату рождения перед оплатой платного расклада.
    states[user_id] = PAID_DOB_STATE
    bot.answer_callback_query(callback_query.id)
    bot.send_message(
        user_id,
        "Для платного расклада введите дату рождения в формате ДД.ММ.ГГГГ."
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("buy_"))
def handle_additional_product(callback_query):
    user_id = callback_query.from_user.id
    product_name = callback_query.data.split("buy_", 1)[1]
    product = get_product_by_name(product_name)

    # Для платного расклада всегда запрашиваем дату рождения (строгий ввод), как в бесплатном.
    if product_name == "full_reading":
        states[user_id] = PAID_DOB_STATE
        bot.answer_callback_query(callback_query.id)
        bot.send_message(
            user_id,
            "Для платного расклада введите дату рождения в формате ДД.ММ.ГГГГ."
        )
        return

    if not product:
        bot.answer_callback_query(callback_query.id, "Продукт временно недоступен.", show_alert=True)
        return

    if not product.get("price"):
        bot.answer_callback_query(callback_query.id, "Цена не задана. Свяжитесь с поддержкой.", show_alert=True)
        return

    url, pay_id = create_payment(
        product["price"],
        product.get("description") or product["name"],
        user_id,
        metadata={"product_name": product["name"]}
    )
    if url:
        bot.send_message(
            user_id,
            f"💳 Перейди по ссылке для оплаты «{product.get('description') or product_name}»:\n{url}\n"
            "После оплаты материал придёт автоматически."
        )
    else:
        bot.send_message(user_id, "⚠️ Ошибка при создании платежа.")


# --- Webhook от ЮKassa ---
@app.route("/webhook", methods=["POST"])
def yookassa_webhook():
    data = request.json
    try:
        if data and data.get("event") == "payment.succeeded":
            payment = data.get("object", {})
            metadata = payment.get("metadata", {}) or {}
            user_id = metadata.get("user_id")
            product_name = metadata.get("product_name", "full_reading")

            if user_id:
                if product_name == "full_reading":
                    bot.send_message(user_id, "✔ Оплата получена! Формирую твой расклад...")

                    records = users_sheet.get_all_records()
                    row = next((r for r in records if str(r.get("User ID")) == str(user_id)), None)
                    soul, destiny, day = 1, 1, 1
                    if row:
                        date_str = str(row.get("Date of Birth") or "")
                        numbers = calc_numbers(date_str) if date_str else None
                        if numbers:
                            soul, destiny, day = numbers
                        else:
                            try:
                                soul = int(row.get("Soul Num") or soul)
                            except Exception:
                                pass
                            try:
                                destiny = int(row.get("Destiny Num") or destiny)
                            except Exception:
                                pass
                            try:
                                if date_str and "." in date_str:
                                    day = int(date_str.split(".")[0])
                            except Exception:
                                pass

                    pre_full = get_text_block("pre_full_info")
                    if pre_full:
                        send_long_message(user_id, pre_full)

                    msg = build_full_reading(soul, destiny, day)
                    send_long_message(user_id, f"✨ Твой полный нумерологический расклад:\n\n{msg}")

                    save_user_data(user_id, "", "", product="full_reading")

                    # Приглашение к практическому руководству через 24 часа
                    try:
                        next_intro = datetime.now() + timedelta(hours=24)
                        set_user_practice_state(user_id, stage=0, next_dt=next_intro)
                    except Exception as e:
                        print(f"[ERROR] schedule practice intro: {e}")

                    offers = get_active_products()
                    offers = [o for o in offers if str(o.get("name") or "").lower() != "full_reading"]
                    if offers:
                        markup = types.InlineKeyboardMarkup()
                        for o in offers:
                            markup.add(types.InlineKeyboardButton(
                                text=f"💫 {o['description']} — {o['price']} ₽",
                                callback_data=f"buy_{o['name']}"
                            ))
                        markup.add(types.InlineKeyboardButton("📞 Консультация", callback_data="consultation"))
                        bot.send_message(user_id, "Хочешь продолжить? 💫 Выбери продукт ниже:", reply_markup=markup)
                else:
                    product = get_product_by_name(product_name, active_only=False)
                    if product:
                        bot.send_message(
                            user_id,
                            f"✔ Оплата получена! Отправляю «{product.get('description') or product_name}»."
                        )
                        deliver_product(user_id, product)
                        save_user_data(user_id, "", "", product=product_name)
                    else:
                        bot.send_message(
                            user_id,
                            "Оплата получена, но продукт не найден. Напишите, пожалуйста, в поддержку."
                        )

    except Exception as e:
        print(f"[ERROR] Webhook processing failed: {e}")

    return "ok", 200



# --- Админ-панель ---
@bot.message_handler(commands=["admin"])
def admin_panel(message):
    if message.from_user.id not in config.ADMIN_IDS:
        bot.send_message(message.chat.id, "⛔ У тебя нет доступа.")
        return
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📊 Статистика", callback_data="admin_stats"))
    markup.add(types.InlineKeyboardButton("🔁 Обновить тексты", callback_data="admin_reload"))
    markup.add(types.InlineKeyboardButton("👥 Пользователи", callback_data="admin_users"))
    markup.add(types.InlineKeyboardButton("📨 Рассылка", callback_data="admin_broadcast"))
    markup.add(types.InlineKeyboardButton("file_id", callback_data="admin_fileid"))
    bot.send_message(message.chat.id, "⚙️ Панель администратора:", reply_markup=markup)


@bot.callback_query_handler(func=lambda c: c.data.startswith("admin_"))
def handle_admin_panel(callback_query):
    user_id = callback_query.from_user.id
    action = callback_query.data.split("_", 1)[1]
    if user_id not in config.ADMIN_IDS:
        bot.answer_callback_query(callback_query.id, "Нет доступа.")
        return

    if action == "stats":
        users = users_sheet.get_all_records()
        total = len(users)
        paid = len([u for u in users if str(u.get("Product")).lower() == "full_reading"])
        consults = len([u for u in users if str(u.get("Product")).lower() == "consultation request"])
        bot.send_message(user_id,
                         f"📊 Статистика:\n👥 Пользователей: {total}\n💎 Оплат: {paid}\n💌 Консультаций: {consults}")

    elif action == "reload":
        load_texts()
        bot.send_message(user_id, "✅ Тексты обновлены из Google Sheets.")

    elif action == "users":
        link = client.open(config.GOOGLE_SHEET_NAME).url
        bot.send_message(user_id, f"👥 Список пользователей:\n{link}")

    
    elif action == "fileid":
        states[user_id] = ADMIN_FILEID_STATE
        bot.send_message(
            user_id,
            "file_id helper mode enabled.\n"
            "Send a document/photo/video/audio - I'll reply with its file_id.\n"
            "Use /cancel to exit."
        )

    elif action == "broadcast":
        states[user_id] = ADMIN_BROADCAST_STATE
        bot.send_message(
            user_id,
            "📨 Пришли текст или документ для рассылки всем пользователям.\n"
            "Чтобы отменить, отправь /cancel."
        )


# --- Webhook Telegram ---
@app.route("/" + config.TOKEN, methods=["POST"])
def telegram_webhook():
    update = telebot.types.Update.de_json(request.stream.read().decode("utf-8"))
    bot.process_new_updates([update])
    return "ok", 200


@app.route("/run_practices", methods=["GET"])
def run_practices():
    result = process_practices()
    offers_result = process_offers()
    daily_offer = process_daily_offers()
    return f"practices: {result}; offers: {offers_result}; daily_offer: {daily_offer}", 200


@app.route("/")
def index():
    return "Bot running!", 200


# --- Установка webhook ---
bot.remove_webhook()
bot.set_webhook(url=config.WEBHOOK_URL + "/" + config.TOKEN)
print(f"[INFO] Webhook установлен на {config.WEBHOOK_URL}/{config.TOKEN}")

# --- Запуск ---
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
