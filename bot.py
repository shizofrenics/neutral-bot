"""Телеграм-бот записи в мотомастерскую «Нейтраль».

Только стандартная библиотека Python 3.8+, ничего устанавливать не нужно.
Запуск: python bot.py   (токен и ID администратора лежат в файле config.env)
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
BOOKINGS_FILE = os.path.join(BASE, "bookings.json")

# ---------- настройки мастерской (те же, что на сайте) ----------
SHOP_NAME = "Нейтраль"
ADDRESS = "ул. Заводская, 7, гараж 12"
PHONE = "+7 912 345-67-89"
# день недели (0 = понедельник) -> (открытие, закрытие). Пн и Вс выходные.
SCHEDULE = {1: (10, 20), 2: (10, 20), 3: (10, 20), 4: (10, 20), 5: (11, 18)}
DAYS_AHEAD = 14          # на сколько дней вперёд можно записаться
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

SERVICES = [
    ("Замена масла и фильтров", "от 1 800 ₽"),
    ("Замена цепи и звёзд", "2 500 ₽"),
    ("Регулировка клапанов", "от 4 000 ₽"),
    ("Тормоза: колодки, прокачка, шланги", "от 900 ₽"),
    ("Карбюраторы: чистка и синхронизация", "3 500 ₽"),
    ("Диагностика электрики", "1 000 ₽"),
    ("Шиномонтаж и балансировка", "от 400 ₽"),
    ("Консервация на зиму (акция)", "3 000 ₽"),
    ("Другое / не знаю что с мотоциклом", "диагностика по факту"),
]


# ---------- настройки из .env ----------
def load_env(path=os.path.join(BASE, "config.env")):
    env = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("BOT_TOKEN", "ADMIN_CHAT_ID"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


# ---------- хранилище записей ----------
def load_bookings():
    if not os.path.exists(BOOKINGS_FILE):
        return []
    with open(BOOKINGS_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_booking(b):
    items = load_bookings()
    items.append(b)
    with open(BOOKINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def taken_slots(day_iso):
    return {b["time"] for b in load_bookings() if b["date"] == day_iso}


# ---------- расписание ----------
def working_days(today=None, n=DAYS_AHEAD):
    today = today or date.today()
    return [today + timedelta(days=i) for i in range(0, n + 1)
            if (today + timedelta(days=i)).weekday() in SCHEDULE]


def free_slots(day, now=None):
    """Свободные часы на день. Сегодня — только те, до которых больше часа."""
    now = now or datetime.now()
    start, end = SCHEDULE[day.weekday()]
    busy = taken_slots(day.isoformat())
    out = []
    for h in range(start, end):
        label = "%02d:00" % h
        if label in busy:
            continue
        if day == now.date() and datetime(day.year, day.month, day.day, h) < now + timedelta(hours=1):
            continue
        out.append(label)
    return out


def fmt_day(d):
    return "%s %s" % (WEEKDAYS[d.weekday()], d.strftime("%d.%m"))


# ---------- Telegram API ----------
class Api:
    def __init__(self, token):
        self.url = "https://api.telegram.org/bot%s/" % token

    def call(self, method, **params):
        data = json.dumps(params).encode("utf-8")
        req = urllib.request.Request(self.url + method, data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            print("Ошибка Telegram %s: %s" % (e.code, body), file=sys.stderr)
            return {"ok": False}

    def send(self, chat_id, text, kb=None):
        p = {"chat_id": chat_id, "text": text}
        if kb:
            p["reply_markup"] = kb
        return self.call("sendMessage", **p)


def inline(rows):
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


def main_menu():
    return inline([[("Записаться", "start")], [("Прайс", "price"), ("Как добраться", "where")]])


# ---------- логика диалога ----------
class Bot:
    def __init__(self, api, admin_id=None):
        self.api = api
        self.admin_id = admin_id
        self.state = {}   # chat_id -> {"step": ..., данные}

    def handle(self, upd):
        if "callback_query" in upd:
            cq = upd["callback_query"]
            self.api.call("answerCallbackQuery", callback_query_id=cq["id"])
            self.on_callback(cq["message"]["chat"]["id"], cq["data"], cq["from"])
        elif "message" in upd:
            self.on_message(upd["message"])

    # --- текст и команды ---
    def on_message(self, m):
        chat = m["chat"]["id"]
        text = (m.get("text") or "").strip()
        st = self.state.get(chat, {})

        if text.startswith("/start") or text.startswith("/menu"):
            self.state.pop(chat, None)
            self.api.send(chat, "Здравствуйте! Это «%s», мастерская мотоциклов. "
                                "Помогу записаться на ремонт и расскажу про цены." % SHOP_NAME, main_menu())
        elif text.startswith("/price"):
            self.send_price(chat)
        elif text.startswith("/where"):
            self.send_where(chat)
        elif text.startswith("/cancel"):
            self.state.pop(chat, None)
            self.api.send(chat, "Запись отменена. Если передумаете, напишите /start.")
        elif text.startswith("/list") and self.is_admin(chat):
            self.send_list(chat)
        elif st.get("step") == "name":
            st["name"] = text[:60] or "без имени"
            st["step"] = "phone"
            kb = {"keyboard": [[{"text": "Отправить мой номер", "request_contact": True}]],
                  "resize_keyboard": True, "one_time_keyboard": True}
            self.api.send(chat, "Оставьте телефон: нажмите кнопку или напишите номер.", kb)
        elif st.get("step") == "phone":
            phone = (m.get("contact") or {}).get("phone_number") or text
            digits = "".join(c for c in phone if c.isdigit())
            if len(digits) < 10:
                self.api.send(chat, "Не похоже на номер телефона. Напишите его ещё раз, например +7 912 345-67-89.")
                return
            st["phone"] = phone
            st["step"] = "bike"
            self.api.send(chat, "Какой мотоцикл? Например: Honda CB400, 2003.",
                          {"remove_keyboard": True})
        elif st.get("step") == "bike":
            st["bike"] = text[:80] or "не указан"
            self.finish(chat, st, m["from"])
        elif m.get("contact") and st.get("step") == "phone":
            pass
        else:
            self.api.send(chat, "Выберите действие:", main_menu())

    # --- кнопки ---
    def on_callback(self, chat, data, user):
        if data == "start":
            rows = [[(s + " · " + p, "svc:%d" % i)] for i, (s, p) in enumerate(SERVICES)]
            self.state[chat] = {"step": "service"}
            self.api.send(chat, "Что нужно сделать?", inline(rows))
        elif data == "price":
            self.send_price(chat)
        elif data == "where":
            self.send_where(chat)
        elif data.startswith("svc:"):
            i = int(data[4:])
            self.state[chat] = {"step": "day", "service": SERVICES[i][0]}
            days = working_days()
            rows, row = [], []
            for d in days:
                if free_slots(d):
                    row.append((fmt_day(d), "day:" + d.isoformat()))
                if len(row) == 3:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)
            if not rows:
                self.api.send(chat, "На ближайшие две недели свободных окон нет. Позвоните: " + PHONE)
                return
            self.api.send(chat, "Выберите день:", inline(rows))
        elif data.startswith("day:"):
            st = self.state.get(chat)
            if not st:
                return self.api.send(chat, "Начнём заново: /start")
            d = date.fromisoformat(data[4:])
            slots = free_slots(d)
            if not slots:
                return self.api.send(chat, "На этот день уже всё занято, выберите другой: /start")
            st["date"] = d.isoformat()
            st["step"] = "time"
            rows = [[(s, "time:" + s) for s in slots[i:i + 4]] for i in range(0, len(slots), 4)]
            self.api.send(chat, "Во сколько приедете, %s?" % fmt_day(d), inline(rows))
        elif data.startswith("time:"):
            st = self.state.get(chat)
            if not st or "date" not in st:
                return self.api.send(chat, "Начнём заново: /start")
            t = data[5:]
            if t in taken_slots(st["date"]):
                return self.api.send(chat, "Это время только что заняли. Начнём заново: /start")
            st["time"] = t
            st["step"] = "name"
            self.api.send(chat, "Как к вам обращаться?")

    # --- завершение записи ---
    def finish(self, chat, st, user):
        if st["time"] in taken_slots(st["date"]):
            self.state.pop(chat, None)
            return self.api.send(chat, "Это время только что заняли. Начнём заново: /start")
        booking = {
            "created": datetime.now().isoformat(timespec="seconds"),
            "date": st["date"], "time": st["time"], "service": st["service"],
            "name": st["name"], "phone": st["phone"], "bike": st["bike"],
            "chat_id": chat, "username": user.get("username", ""),
        }
        save_booking(booking)
        self.state.pop(chat, None)
        d = date.fromisoformat(booking["date"])
        self.api.send(chat, "Готово, записал вас:\n%s, %s\n%s\nМотоцикл: %s\n\n"
                            "Адрес: %s\nЕсли планы изменятся, позвоните: %s"
                      % (fmt_day(d), booking["time"], booking["service"], booking["bike"], ADDRESS, PHONE))
        if self.admin_id:
            self.api.send(self.admin_id, "Новая запись\n%s, %s\n%s\n%s, %s\n%s%s" % (
                fmt_day(d), booking["time"], booking["service"], booking["name"],
                booking["phone"], booking["bike"],
                ("\n@" + booking["username"]) if booking["username"] else ""))

    # --- вспомогательное ---
    def is_admin(self, chat):
        return self.admin_id is not None and str(chat) == str(self.admin_id)

    def send_price(self, chat):
        lines = ["%s: %s" % (s, p) for s, p in SERVICES[:-1]]
        self.api.send(chat, "Цены (запчасти отдельно, точную стоимость назовём после диагностики):\n\n"
                      + "\n".join(lines), inline([[("Записаться", "start")]]))

    def send_where(self, chat):
        self.api.send(chat, "%s\nРаботаем вт–пт 10:00–20:00, сб 11:00–18:00. Пн и вс выходные.\n"
                            "Телефон: %s" % (ADDRESS, PHONE), inline([[("Записаться", "start")]]))

    def send_list(self, chat):
        today = date.today().isoformat()
        items = sorted((b for b in load_bookings() if b["date"] >= today),
                       key=lambda b: (b["date"], b["time"]))
        if not items:
            return self.api.send(chat, "Ближайших записей нет.")
        lines = ["%s %s, %s, %s, %s" % (fmt_day(date.fromisoformat(b["date"])), b["time"],
                                         b["service"], b["name"], b["phone"]) for b in items]
        self.api.send(chat, "Записи:\n\n" + "\n".join(lines))


def run():
    env = load_env()
    token = env.get("BOT_TOKEN")
    if not token or token.startswith("ВСТАВЬТЕ"):
        sys.exit("Нет токена. Откройте файл config.env и вставьте токен от @BotFather.")
    api = Api(token)
    me = api.call("getMe")
    if not me.get("ok"):
        sys.exit("Токен не подошёл. Проверьте config.env.")
    print("Бот запущен: @%s. Остановить: Ctrl+C" % me["result"]["username"])
    bot = Bot(api, env.get("ADMIN_CHAT_ID") or None)
    offset = 0
    while True:
        try:
            res = api.call("getUpdates", offset=offset, timeout=30, allowed_updates=["message", "callback_query"])
            for upd in res.get("result", []):
                offset = upd["update_id"] + 1
                try:
                    bot.handle(upd)
                except Exception as e:  # один сбойный апдейт не должен ронять бота
                    print("Ошибка обработки:", repr(e), file=sys.stderr)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print("Нет связи с Telegram, пробую снова через 5 секунд:", e, file=sys.stderr)
            time.sleep(5)


if __name__ == "__main__":
    run()
