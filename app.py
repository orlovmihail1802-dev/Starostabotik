import os, re, json, time, hmac, hashlib, random, sqlite3, threading, uuid, base64
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import parse_qsl

import telebot
from telebot import types
from flask import Flask, request, jsonify, send_from_directory, g

# ---------------- настройки (из переменных Railway) ----------------
BASE = os.path.dirname(os.path.abspath(__file__))
TOKEN = os.environ.get("TOKEN", "")
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



# ---------------- расписание из текста + расписание звонков ----------------
DAY_NAMES = {
    "понедельник": 0, "пн": 0,
    "вторник": 1, "вт": 1,
    "среда": 2, "ср": 2,
    "четверг": 3, "чт": 3,
    "пятница": 4, "пт": 4,
    "суббота": 5, "сб": 5,
}

# Время указывается для всей пары: от начала первого занятия до конца второго.
BELL_TIMES = {
    "monday": {
        1: ("08:55", "10:30"), 2: ("10:40", "12:20"),
        3: ("12:50", "14:25"), 4: ("14:35", "16:10"),
        5: ("16:15", "17:50"), 6: ("17:55", "19:30"),
    },
    "weekday": {
        1: ("08:00", "09:35"), 2: ("09:45", "11:25"),
        3: ("11:55", "13:30"), 4: ("13:40", "15:15"),
        5: ("15:20", "16:55"), 6: ("17:00", "18:35"),
    },
    "saturday": {
        1: ("08:00", "09:10"), 2: ("09:20", "10:30"),
        3: ("11:00", "12:10"), 4: ("12:20", "13:30"),
        5: ("13:40", "14:50"), 6: ("15:00", "16:10"),
    },
}

def bell_time(day_index, pair_no):
    if day_index == 0:
        table = BELL_TIMES["monday"]
    elif day_index == 5:
        table = BELL_TIMES["saturday"]
    else:
        table = BELL_TIMES["weekday"]
    return table.get(pair_no, ("", ""))


def parse_schedule_text(raw):
    """Формат: день недели, затем пары по строкам. Номер пары можно указать явно."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("Вставь текст расписания.")
    items = []
    current_day = None
    next_pair = 1
    for original in raw.replace("\r", "").split("\n"):
        line = original.strip()
        if not line:
            continue
        clean = line.strip(" #*•\t")
        low = clean.lower().replace("ё", "е").strip(":")
        if low in DAY_NAMES:
            current_day = DAY_NAMES[low]
            next_pair = 2 if current_day == 5 else 1
            continue
        # Поддержка заголовков вида «Понедельник:»
        day_match = re.match(r"^(понедельник|вторник|среда|четверг|пятница|суббота|пн|вт|ср|чт|пт|сб)\s*[:\-]?\s*$", low)
        if day_match:
            current_day = DAY_NAMES[day_match.group(1)]
            next_pair = 2 if current_day == 5 else 1
            continue
        if current_day is None:
            continue
        if low.startswith(("расписание", "неделя", "день")):
            continue

        # Отдельный классный час в понедельник — до первой пары.
        if current_day == 0 and "классный час" in low:
            subject = re.sub(r"^\s*(?:[-•*]|\d+[.)])\s*", "", clean).strip()
            items.append({"day": current_day, "start": "08:15", "finish": "08:45",
                          "subject": subject[:80], "room": ""})
            continue

        pair_match = re.match(r"^\s*(?:пара\s*)?([1-6])\s*(?:пара|[.)]|:|-)\s*", clean, re.I)
        if pair_match:
            pair_no = int(pair_match.group(1))
            rest = clean[pair_match.end():].strip()
        else:
            roman = re.match(r"^\s*([IVX]+)\s*пара\s*[:\-]?\s*", clean, re.I)
            if roman:
                pair_no = {"I":1,"II":2,"III":3,"IV":4,"V":5,"VI":6}.get(roman.group(1).upper(), next_pair)
                rest = clean[roman.end():].strip()
            else:
                pair_no = next_pair
                rest = re.sub(r"^\s*(?:[-•*]|\d+[.)])\s*", "", clean).strip()

        if not rest or rest in ("—", "-", "/"):
            # Пустая строка / пропущенная пара не создаёт занятие.
            next_pair = max(next_pair, pair_no + 1)
            continue

        # Поддержка записи для разных подгрупп: «Информатика 308 / -» и «- / Информатика 308».
        subgroup = ""
        m_sub = re.match(r"^(.*?)\s+(\d{2,4}[а-яa-z]?)\s*/\s*-$", rest, re.I)
        m_sub2 = re.match(r"^-\s*/\s*(.*?)\s+(\d{2,4}[а-яa-z]?)$", rest, re.I)
        if m_sub:
            rest = m_sub.group(1).strip() + " (1-я подгруппа) — " + m_sub.group(2)
        elif m_sub2:
            rest = m_sub2.group(1).strip() + " (2-я подгруппа) — " + m_sub2.group(2)

        # Предмет и кабинет: «Математика — 208», «Информатика 308».
        room = ""
        split = re.split(r"\s+[—–-]\s+", rest, maxsplit=1)
        if len(split) == 2:
            subject, candidate = split[0].strip(), split[1].strip()
            if re.match(r"^(?:каб\.?\s*)?\d{2,4}[а-яa-z]?(?:\s*/\s*[-\dа-яa-z]+)?$", candidate, re.I):
                room = candidate.replace("каб.", "").strip()
            else:
                subject = rest
        else:
            m = re.match(r"^(.*?)(?:\s+)(\d{2,4}[а-яa-z]?(?:\s*/\s*[-\dа-яa-z]+)?)$", rest, re.I)
            if m:
                subject, room = m.group(1).strip(), m.group(2).strip()
            else:
                subject = rest.strip()

        subject = subject.strip(" —–-")
        if subject:
            st, en = bell_time(current_day, pair_no)
            items.append({"day": current_day, "pair": pair_no, "start": st, "finish": en,
                          "subject": subject[:80], "room": room[:20]})
        next_pair = max(next_pair, pair_no + 1)

    if not items:
        raise ValueError("Не удалось найти пары. Используй заголовки дней и строки с предметами.")
    return items


def week_monday(next_week=False):
    t = now().date()
    mon = t - timedelta(days=t.weekday())
    if next_week or t.weekday() >= 5:
        mon += timedelta(days=7)
    return mon


def import_schedule(gid, items, monday):
    # Удаляем старые пары выбранной недели, сохраняя связанные с ними ДЗ и отметки.
    a, b = monday.isoformat(), (monday + timedelta(days=5)).isoformat()
    for row in q("SELECT id FROM lessons WHERE gid=? AND day BETWEEN ? AND ?", (gid, a, b)):
        used = one("SELECT (SELECT COUNT(*) FROM hw WHERE lesson=?) + (SELECT COUNT(*) FROM att WHERE lesson=?) AS n",
                   (row["id"], row["id"]))["n"]
        if not used:
            q("DELETE FROM lessons WHERE id=?", (row["id"],), write=True)

    for it in items:
        day = (monday + timedelta(days=it["day"])).isoformat()
        ex = one("SELECT id FROM lessons WHERE gid=? AND day=? AND start=? AND subject=?",
                 (gid, day, it["start"], it["subject"]))
        if ex:
            q("UPDATE lessons SET finish=?, room=? WHERE id=?",
              (it["finish"], it["room"], ex["id"]), write=True)
        else:
            q("INSERT INTO lessons(gid,day,start,finish,subject,room) VALUES(?,?,?,?,?,?)",
              (gid, day, it["start"], it["finish"], it["subject"], it["room"]), write=True)


def do_schedule_text(gid, raw, next_week=False):
    items = parse_schedule_text(raw)
    monday = week_monday(next_week)
    import_schedule(gid, items, monday)
    return len(items), monday

# ---------------- API ----------------
@app.get("/")
def index():
    return send_from_directory(BASE, "index.html")


@app.get("/files/<path:name>")
def files(name):
    return send_from_directory(UPLOADS, name, max_age=86400)


@app.get("/health")
def health():
    return "ok"


@app.get("/api/me")
@api()
def me():
    if not g.m:
        return jsonify(role=None)
    grp = one("SELECT * FROM groups WHERE id=?", (g.m["gid"],))
    out = dict(role=g.m["role"], group=grp["name"], limit=LIMIT, ai=False, bot=BOT_USER, today=now().date().isoformat())
    if g.m["role"] == "owner":
        out.update(student_code=grp["student_code"], helper_code=grp["helper_code"])
    return jsonify(out)


@app.post("/api/create")
@api()
def create():
    if g.m:
        return jsonify(error="Ты уже в группе"), 400
    name = ((request.get_json(silent=True) or {}).get("name") or "").strip()[:60] or "Моя группа"
    gid = q("INSERT INTO groups(name,owner,student_code,helper_code) VALUES(?,?,?,?)",
            (name, g.u["id"], new_code(), new_code()), write=True)
    q("INSERT INTO members(uid,gid,role,name) VALUES(?,?,?,?)", (g.u["id"], gid, "owner", g.u.get("first_name", "")), write=True)
    return jsonify(ok=True)


@app.post("/api/join")
@api()
def join():
    c = ((request.get_json(silent=True) or {}).get("code") or "").strip().upper()
    grp = one("SELECT * FROM groups WHERE student_code=? OR helper_code=?", (c, c)) if c else None
    if not grp:
        return jsonify(error="Код не найден"), 404
    if g.m and g.m["role"] == "owner":
        return jsonify(error="Ты староста своей группы"), 400
    role = "helper" if grp["helper_code"] == c else "student"
    q("INSERT OR REPLACE INTO members(uid,gid,role,name) VALUES(?,?,?,?)",
      (g.u["id"], grp["id"], role, g.u.get("first_name", "")), write=True)
    return jsonify(ok=True)


@app.post("/api/regen")
@api("owner")
def regen():
    which = (request.get_json(silent=True) or {}).get("which")
    col = "helper_code" if which == "helper" else "student_code"
    q(f"UPDATE groups SET {col}=? WHERE id=?", (new_code(), g.m["gid"]), write=True)
    return jsonify(ok=True)


@app.get("/api/students")
@api("edit")
def students_list():
    return jsonify(items=q("SELECT id,name FROM students WHERE gid=? ORDER BY name", (g.m["gid"],)))


@app.post("/api/students")
@api("owner")
def students_add():
    text = (request.get_json(silent=True) or {}).get("text", "")
    have = {r["name"].lower() for r in q("SELECT name FROM students WHERE gid=?", (g.m["gid"],))}
    n = 0
    for line in text.split("\n"):
        name = re.sub(r"^\s*\d+[.)]?\s*", "", line).strip()[:80]
        if name and name.lower() not in have:
            q("INSERT INTO students(gid,name) VALUES(?,?)", (g.m["gid"], name), write=True)
            have.add(name.lower())
            n += 1
    return jsonify(added=n)


@app.delete("/api/students/<int:sid>")
@api("owner")
def students_del(sid):
    q("DELETE FROM att WHERE student=?", (sid,), write=True)
    q("DELETE FROM students WHERE id=? AND gid=?", (sid, g.m["gid"]), write=True)
    return jsonify(ok=True)


@app.get("/api/week")
@api("member")
def week():
    try:
        a = datetime.strptime(request.args.get("start", ""), "%Y-%m-%d").date()
    except ValueError:
        a = week_monday()
    b = a + timedelta(days=6)
    rows = q("""SELECT l.*, (SELECT COUNT(*) FROM hw WHERE lesson=l.id) hwc,
                (SELECT COUNT(*) FROM att WHERE lesson=l.id) marked
                FROM lessons l WHERE gid=? AND day BETWEEN ? AND ? ORDER BY day, start, id""",
             (g.m["gid"], a.isoformat(), b.isoformat()))
    return jsonify(items=rows)


@app.post("/api/lesson")
@api("owner")
def lesson_add():
    d = (request.get_json(silent=True) or {})
    try:
        day = datetime.strptime(d.get("day", ""), "%Y-%m-%d").date().isoformat()
    except ValueError:
        return jsonify(error="Укажи дату"), 400
    subj = (d.get("subject") or "").strip()[:80]
    if not subj:
        return jsonify(error="Укажи предмет"), 400
    q("INSERT INTO lessons(gid,day,start,finish,subject,room) VALUES(?,?,?,?,?,?)",
      (g.m["gid"], day, norm_t(d.get("start")), norm_t(d.get("finish")), subj, (d.get("room") or "")[:20]), write=True)
    return jsonify(ok=True)


@app.get("/api/lesson/<int:lid>")
@api("member")
def lesson_get(lid):
    gid = g.m["gid"]
    l = one("SELECT * FROM lessons WHERE id=? AND gid=?", (lid, gid))
    if not l:
        return jsonify(error="Пара не найдена"), 404
    out = dict(lesson=l, hw=q("SELECT * FROM hw WHERE lesson=? ORDER BY id", (lid,)))
    if g.m["role"] in ("owner", "helper"):
        out["students"] = q("""SELECT s.id, s.name, COALESCE(a.status,0) status FROM students s
                               LEFT JOIN att a ON a.student=s.id AND a.lesson=? WHERE s.gid=? ORDER BY s.name""", (lid, gid))
        out["marked"] = bool(one("SELECT 1 AS x FROM att WHERE lesson=?", (lid,)))
    return jsonify(out)


@app.delete("/api/lesson/<int:lid>")
@api("owner")
def lesson_del(lid):
    if not one("SELECT 1 AS x FROM lessons WHERE id=? AND gid=?", (lid, g.m["gid"])):
        return jsonify(error="Пара не найдена"), 404
    for h in q("SELECT photo FROM hw WHERE lesson=?", (lid,)):
        drop_file(h["photo"])
    q("DELETE FROM hw WHERE lesson=?", (lid,), write=True)
    q("DELETE FROM att WHERE lesson=?", (lid,), write=True)
    q("DELETE FROM lessons WHERE id=?", (lid,), write=True)
    return jsonify(ok=True)


@app.post("/api/attendance")
@api("edit")
def attendance():
    d = (request.get_json(silent=True) or {})
    gid = g.m["gid"]
    lid = int(d.get("lesson", 0))
    if not one("SELECT 1 AS x FROM lessons WHERE id=? AND gid=?", (lid, gid)):
        return jsonify(error="Пара не найдена"), 404
    marks = d.get("marks") or {}
    for s in q("SELECT id FROM students WHERE gid=?", (gid,)):
        st = int(marks.get(str(s["id"]), 0))
        q("INSERT OR REPLACE INTO att(lesson,student,status) VALUES(?,?,?)",
          (lid, s["id"], st if st in (0, 1, 2) else 0), write=True)
    return jsonify(ok=True)


@app.post("/api/hw")
@api("edit")
def hw_add():
    gid = g.m["gid"]
    lid = int(request.form.get("lesson", 0))
    l = one("SELECT * FROM lessons WHERE id=? AND gid=?", (lid, gid))
    if not l:
        return jsonify(error="Пара не найдена"), 404
    text = (request.form.get("text") or "").strip()[:2000]
    link = (request.form.get("link") or "").strip()[:500]
    if link and not re.match(r"^https?://", link, re.I):
        link = "https://" + link
    photo = save_photo(request.files.get("photo"))
    if not (text or link or photo):
        return jsonify(error="Добавь текст, ссылку или фото"), 400
    q("INSERT INTO hw(gid,lesson,text,link,photo,created,author) VALUES(?,?,?,?,?,?,?)",
      (gid, lid, text, link, photo, now().isoformat(), g.u.get("first_name", "")), write=True)
    notify(gid, f"📝 Новое ДЗ: {l['subject']} ({l['day'][8:]}.{l['day'][5:7]})\n{text[:300]}", skip=g.u["id"])
    return jsonify(ok=True)


@app.delete("/api/hw/<int:hid>")
@api("edit")
def hw_del(hid):
    h = one("SELECT * FROM hw WHERE id=? AND gid=?", (hid, g.m["gid"]))
    if h:
        drop_file(h["photo"])
        q("DELETE FROM hw WHERE id=?", (hid,), write=True)
    return jsonify(ok=True)


@app.get("/api/homework")
@api("member")
def homework():
    since = (now().date() - timedelta(days=3)).isoformat()
    rows = q("""SELECT h.*, l.day, l.subject, l.start FROM hw h JOIN lessons l ON l.id=h.lesson
                WHERE h.gid=? AND l.day>=? ORDER BY l.day, l.start, h.id""", (g.m["gid"], since))
    return jsonify(items=rows)


@app.get("/api/materials")
@api("member")
def materials():
    gid = g.m["gid"]
    items = q("SELECT * FROM materials WHERE gid=? ORDER BY id DESC", (gid,))
    subs = sorted({r["subject"] for r in q("SELECT DISTINCT subject FROM lessons WHERE gid=?", (gid,))} |
                  {i["subject"] for i in items})
    return jsonify(items=items, subjects=subs)


@app.post("/api/materials")
@api("edit")
def materials_add():
    f = request.form
    subject = (f.get("subject") or "").strip()[:80]
    kind = f.get("kind")
    title = (f.get("title") or "").strip()[:120]
    content = (f.get("content") or "").strip()[:3000]
    if not subject or kind not in ("link", "photo", "text"):
        return jsonify(error="Выбери предмет и тип"), 400
    if kind == "photo":
        content = save_photo(request.files.get("photo"))
    elif kind == "link" and not re.match(r"^https?://", content, re.I):
        content = "https://" + content
    if not content:
        return jsonify(error="Добавь содержимое"), 400
    q("INSERT INTO materials(gid,subject,kind,title,content) VALUES(?,?,?,?,?)",
      (g.m["gid"], subject, kind, title, content), write=True)
    return jsonify(ok=True)


@app.delete("/api/materials/<int:mid>")
@api("edit")
def materials_del(mid):
    m = one("SELECT * FROM materials WHERE id=? AND gid=?", (mid, g.m["gid"]))
    if m:
        if m["kind"] == "photo":
            drop_file(m["content"])
        q("DELETE FROM materials WHERE id=?", (mid,), write=True)
    return jsonify(ok=True)


@app.get("/api/stats")
@api("edit")
def stats():
    rows = q("""SELECT s.id, s.name, COALESCE(SUM(a.status=1),0) absent, COALESCE(SUM(a.status=2),0) late,
                COUNT(a.lesson) total FROM students s LEFT JOIN att a ON a.student=s.id
                WHERE s.gid=? GROUP BY s.id ORDER BY absent DESC, late DESC, s.name""", (g.m["gid"],))
    return jsonify(items=rows, limit=LIMIT)


@app.post("/api/schedule_text")
@api("owner")
def schedule_text():
    data = request.get_json(silent=True) or {}
    raw = (data.get("text") or "").strip()
    try:
        n, mon = do_schedule_text(g.m["gid"], raw, bool(data.get("next")))
    except Exception as e:
        return jsonify(error=str(e)), 400
    notify(g.m["gid"], f"📅 Расписание на неделю с {mon:%d.%m} обновлено", skip=g.u["id"])
    return jsonify(count=n, monday=mon.isoformat())


# ---------------- Telegram-бот ----------------
@bot.message_handler(commands=["start", "help"])
def on_start(m):
    bot.send_message(m.chat.id,
                     "Привет! Я Староста-бот 🎓\n\nВся группа на одном экране: расписание, отметки, домашка.\n"
                     "Нажми кнопку, чтобы открыть журнал.\n\n"
                     "Староста: открой приложение и вставь текст расписания в разделе управления расписанием.",
                     reply_markup=open_kb())


@bot.message_handler(func=lambda m: True)
def on_any(m):
    bot.send_message(m.chat.id, "Нажми кнопку ниже, чтобы открыть журнал 👇", reply_markup=open_kb())


def run_web():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), threaded=True)


if __name__ == "__main__":
    init_db()
    threading.Thread(target=run_web, daemon=True).start()
    try:
        BOT_USER = bot.get_me().username
    except Exception as e:
        print("get_me:", e)
    if WEBAPP_URL:
        try:
            bot.set_chat_menu_button(menu_button=types.MenuButtonWebApp("Журнал", types.WebAppInfo(WEBAPP_URL)))
        except Exception as e:
            print("menu button:", e)
    print("Бот запущен ✅", WEBAPP_URL or "(WEBAPP_URL не задан)")
    bot.infinity_polling(skip_pending=True)
