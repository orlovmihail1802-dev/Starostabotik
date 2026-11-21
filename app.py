import os, re, json, time, hmac, hashlib, random, sqlite3, threading, uuid, base64
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import parse_qsl

import requests
import telebot
from telebot import types
from flask import Flask, request, jsonify, send_from_directory, g

# ---------------- настройки (из переменных Railway) ----------------
BASE = os.path.dirname(os.path.abspath(__file__))
TOKEN = os.environ.get("TOKEN", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
_env_model = (os.environ.get("GEMINI_MODEL") or "").strip()
GEMINI_MODELS = list(dict.fromkeys([
    m for m in [
        _env_model,
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-flash-latest",
    ] if m
]))
DATA_DIR = os.environ.get("DATA_DIR") or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.join(BASE, "data")
TZ = int(os.environ.get("TZ_OFFSET", "3"))          # часовой пояс (Москва = 3)
LIMIT = int(os.environ.get("ABSENCE_LIMIT", "3"))    # пропусков до "на грани"
WEBAPP_URL = (os.environ.get("WEBAPP_URL") or
              ("https://" + os.environ["RAILWAY_PUBLIC_DOMAIN"] if os.environ.get("RAILWAY_PUBLIC_DOMAIN") else "")).rstrip("/")

UPLOADS = os.path.join(DATA_DIR, "uploads")
os.makedirs(UPLOADS, exist_ok=True)
DB = os.path.join(DATA_DIR, "starosta2.db")

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 15 * 1024 * 1024
BOT_USER = ""

SCHEMA = """
CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY, name TEXT, owner INTEGER, student_code TEXT, helper_code TEXT);
CREATE TABLE IF NOT EXISTS members(uid INTEGER PRIMARY KEY, gid INTEGER, role TEXT, name TEXT);
CREATE TABLE IF NOT EXISTS students(id INTEGER PRIMARY KEY, gid INTEGER, name TEXT);
CREATE TABLE IF NOT EXISTS lessons(id INTEGER PRIMARY KEY, gid INTEGER, day TEXT, start TEXT, finish TEXT, subject TEXT, room TEXT);
CREATE TABLE IF NOT EXISTS att(lesson INTEGER, student INTEGER, status INTEGER, PRIMARY KEY(lesson, student));
CREATE TABLE IF NOT EXISTS hw(id INTEGER PRIMARY KEY, gid INTEGER, lesson INTEGER, text TEXT, link TEXT, photo TEXT, created TEXT, author TEXT);
CREATE TABLE IF NOT EXISTS materials(id INTEGER PRIMARY KEY, gid INTEGER, subject TEXT, kind TEXT, title TEXT, content TEXT);
"""


def init_db():
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    con.commit()
    con.close()


def q(sql, args=(), write=False):
    con = sqlite3.connect(DB, timeout=15)
    con.row_factory = sqlite3.Row
    try:
        cur = con.execute(sql, args)
        if write:
            con.commit()
            return cur.lastrowid
        return [dict(r) for r in cur.fetchall()]
    finally:
        con.close()


def one(sql, args=()):
    r = q(sql, args)
    return r[0] if r else None


def now():
    return datetime.utcnow() + timedelta(hours=TZ)


def new_code():
    return "".join(random.choices("ABCDEFGHJKLMNPQRSTUVWXYZ23456789", k=6))


# ---------------- проверка, что запрос из Telegram ----------------
def check_init(raw):
    try:
        data = dict(parse_qsl(raw, keep_blank_values=True))
        h = data.pop("hash")
        s = "\n".join(f"{k}={data[k]}" for k in sorted(data))
        key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(key, s.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, h):
            return None
        if time.time() - int(data.get("auth_date", 0)) > 86400 * 30:
            return None
        return json.loads(data["user"])
    except Exception:
        return None


def api(need=None):
    """need: None - любой, 'member' - в группе, 'edit' - староста/помощник, 'owner' - староста"""
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            u = check_init(request.headers.get("X-Init-Data", ""))
            if not u:
                return jsonify(error="Открой приложение из Telegram"), 401
            g.u = u
            g.m = one("SELECT * FROM members WHERE uid=?", (u["id"],))
            if need and not g.m:
                return jsonify(error="Нет доступа"), 403
            if need == "edit" and g.m["role"] not in ("owner", "helper"):
                return jsonify(error="Нужны права старосты"), 403
            if need == "owner" and g.m["role"] != "owner":
                return jsonify(error="Только для старосты"), 403
            return f(*a, **k)
        return w
    return deco


def save_photo(fs):
    if not fs:
        return None
    data = fs.read()
    if not data:
        return None
    fn = uuid.uuid4().hex + ".jpg"
    with open(os.path.join(UPLOADS, fn), "wb") as f:
        f.write(data)
    return fn


def drop_file(fn):
    if fn:
        try:
            os.remove(os.path.join(UPLOADS, fn))
        except Exception:
            pass


def open_kb(text="📱 Открыть журнал"):
    kb = types.InlineKeyboardMarkup()
    if WEBAPP_URL:
        kb.add(types.InlineKeyboardButton(text, web_app=types.WebAppInfo(WEBAPP_URL)))
    return kb


def notify(gid, text, skip=None):
    def run():
        for m in q("SELECT uid FROM members WHERE gid=?", (gid,)):
            if m["uid"] == skip:
                continue
            try:
                bot.send_message(m["uid"], text, reply_markup=open_kb())
            except Exception:
                pass
    threading.Thread(target=run, daemon=True).start()


# ---------------- ИИ: расписание по фото ----------------
PROMPT = """Это фото расписания занятий студентов на неделю. Извлеки все пары.
Верни ТОЛЬКО JSON-массив объектов такого вида:
{"day": 0, "start": "09:00", "end": "10:30", "subject": "Математика", "room": "301"}
day: номер дня недели, 0 = понедельник ... 6 = воскресенье.
start и end в формате HH:MM (если времени нет, пустая строка).
room: аудитория или пустая строка. subject: название предмета без ФИО преподавателя.
Если на фото две недели (числитель и знаменатель), возьми только текущую, а если непонятно, первую.
Не выдумывай пары, которых нет на фото."""


def norm_t(s):
    m = re.search(r"(\d{1,2})[:.](\d{2})", str(s or ""))
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else ""


RETRY_CODES = {429, 500, 502, 503, 504}


def ai_schedule(img):
    if not GEMINI_KEY:
        raise RuntimeError("Не задан ключ GEMINI_API_KEY в настройках Railway")
    body = {

