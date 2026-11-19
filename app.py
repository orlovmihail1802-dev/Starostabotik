import os, sqlite3, random, string
from datetime import date
import telebot
from telebot import types

TOKEN = os.environ.get("TOKEN", "ВСТАВЬ_ТОКЕН_СЮДА")  # токен от @BotFather
LIMIT = 3                     # сколько пропусков = "на грани"

bot = telebot.TeleBot(TOKEN, threaded=False)
db = sqlite3.connect("starosta.db", check_same_thread=False)
db.executescript("""
CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY, owner INTEGER, code TEXT);
CREATE TABLE IF NOT EXISTS members(user INTEGER PRIMARY KEY, gid INTEGER);
CREATE TABLE IF NOT EXISTS students(id INTEGER PRIMARY KEY, gid INTEGER, name TEXT);
CREATE TABLE IF NOT EXISTS subjects(id INTEGER PRIMARY KEY, gid INTEGER, name TEXT);
CREATE TABLE IF NOT EXISTS att(id INTEGER PRIMARY KEY, sid INTEGER, subj INTEGER, day TEXT, status INTEGER);
CREATE TABLE IF NOT EXISTS materials(id INTEGER PRIMARY KEY, subj INTEGER, kind TEXT, content TEXT, caption TEXT);
CREATE TABLE IF NOT EXISTS hw(id INTEGER PRIMARY KEY, subj INTEGER, text TEXT, due TEXT);
""")


def q(sql, a=()):
    cur = db.execute(sql, a)
    db.commit()
    return cur.fetchall()


def ctx(uid):
    """Возвращает (id группы, староста ли)"""
    r = q("SELECT id FROM groups WHERE owner=?", (uid,))
    if r:
        return r[0][0], True
    r = q("SELECT gid FROM members WHERE user=?", (uid,))
    if r:
        return r[0][0], False
    return None, False


ACTIONS = {"📋 Отметить пару": "att", "📎 Добавить материал": "addm",
           "📝 Добавить ДЗ": "addhw", "📚 Материалы": "mats"}
BTN = set(ACTIONS) | {"📊 Статистика", "🏠 Домашка", "👥 Студенты", "📖 Предметы"}


def menu(owner):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    if owner:
        kb.row("📋 Отметить пару", "📊 Статистика")
        kb.row("📝 Добавить ДЗ", "📎 Добавить материал")
        kb.row("📚 Материалы", "🏠 Домашка")
        kb.row("👥 Студенты", "📖 Предметы")
    else:
        kb.row("📚 Материалы", "🏠 Домашка")
    return kb


def guard(m):
    """Отмена, если вместо ответа нажали кнопку/команду"""
    if not m.text or m.text.startswith("/") or m.text in BTN:
        bot.send_message(m.chat.id, "Отменено")
        return True
    return False


# ---------- старт и группы ----------
@bot.message_handler(commands=["start"])
def start(m):
    gid, owner = ctx(m.from_user.id)
    if gid:
        bot.send_message(m.chat.id, "Привет! Выбери действие 👇", reply_markup=menu(owner))
    else:
        bot.send_message(m.chat.id, "Привет! Я Староста-бот 🎓\n\n"
                         "Ты староста? Жми /newgroup\n"
                         "Ты студент? Возьми у старосты код и напиши /join КОД")


@bot.message_handler(commands=["newgroup"])
def newgroup(m):
    gid, _ = ctx(m.from_user.id)
    if gid:
        return bot.send_message(m.chat.id, "У тебя уже есть группа. Жми /start")
    code = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    q("INSERT INTO groups(owner,code) VALUES(?,?)", (m.from_user.id, code))
    bot.send_message(m.chat.id, f"Группа создана ✅\n\nКод для студентов: {code}\n"
                     f"Пусть напишут мне: /join {code}\n\n"
                     "Теперь добавь 👥 Студенты и 📖 Предметы.", reply_markup=menu(True))


@bot.message_handler(commands=["join"])
def join(m):
    p = m.text.split()
    if len(p) < 2:
        return bot.send_message(m.chat.id, "Напиши так: /join КОД")
    r = q("SELECT id FROM groups WHERE code=?", (p[1].upper(),))
    if not r:
        return bot.send_message(m.chat.id, "Код не найден 🤔")
    q("INSERT OR REPLACE INTO members VALUES(?,?)", (m.from_user.id, r[0][0]))
    bot.send_message(m.chat.id, "Ты в группе ✅", reply_markup=menu(False))


# ---------- списки студентов и предметов ----------
def edit_list(m, table, title):
    gid, owner = ctx(m.from_user.id)
    if not owner:
        return
    names = [r[0] for r in q(f"SELECT name FROM {table} WHERE gid=? ORDER BY name", (gid,))]
    txt = f"{title}:\n" + ("\n".join(names) or "пока пусто")
    txt += "\n\nПришли новые, каждый с новой строки.\nЧтобы удалить, поставь минус: -Иванов"
    msg = bot.send_message(m.chat.id, txt)
    bot.register_next_step_handler(msg, save_list, table, gid)


def save_list(m, table, gid):
    if guard(m):
        return
    for line in m.text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("-"):
            q(f"DELETE FROM {table} WHERE gid=? AND name=?", (gid, line[1:].strip()))
        else:
            q(f"INSERT INTO {table}(gid,name) VALUES(?,?)", (gid, line))
    bot.send_message(m.chat.id, "Готово ✅")


# ---------- выбор предмета ----------
def pick(m, action):
    gid, owner = ctx(m.from_user.id)
    if not gid:
        return bot.send_message(m.chat.id, "Сначала нажми /start")
    if action != "mats" and not owner:
        return
    kb = types.InlineKeyboardMarkup()
    for sid, name in q("SELECT id,name FROM subjects WHERE gid=? ORDER BY name", (gid,)):
        kb.add(types.InlineKeyboardButton(name, callback_data=f"s:{action}:{sid}"))
    if not kb.keyboard:
        return bot.send_message(m.chat.id, "Предметов нет. Добавь их в 📖 Предметы")
    bot.send_message(m.chat.id, "Выбери предмет:", reply_markup=kb)


@bot.message_handler(func=lambda m: m.text in BTN)
def buttons(m):
    t = m.text
    if t in ACTIONS:
        pick(m, ACTIONS[t])
    elif t == "📊 Статистика":
        stats(m)
    elif t == "🏠 Домашка":
        show_hw(m)
    elif t == "👥 Студенты":
        edit_list(m, "students", "Студенты")
    elif t == "📖 Предметы":
        edit_list(m, "subjects", "Предметы")


# ---------- посещаемость ----------
ATT = {}
ICON = ["✅", "❌", "⏰"]


def att_kb(uid):
    st = ATT[uid]
    kb = types.InlineKeyboardMarkup(row_width=1)
    for sid, name in st["names"]:
        kb.add(types.InlineKeyboardButton(f"{ICON[st['marks'][sid]]} {name}", callback_data=f"t:{sid}"))
    kb.add(types.InlineKeyboardButton("💾 Сохранить", callback_data="save"))
    return kb


def start_att(chat, uid, gid, subj):
    sts = q("SELECT id,name FROM students WHERE gid=? ORDER BY name", (gid,))
    if not sts:
        return bot.send_message(chat, "Сначала добавь 👥 Студенты")
    ATT[uid] = {"subj": subj, "names": sts, "marks": {s: 0 for s, _ in sts}}
    bot.send_message(chat, "Нажимай на имя:\n✅ на паре → ❌ нет → ⏰ опоздал",
                     reply_markup=att_kb(uid))


def toggle(c, uid, sid):
    if uid not in ATT:
        return
    ATT[uid]["marks"][sid] = (ATT[uid]["marks"][sid] + 1) % 3
    bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=att_kb(uid))


def save_att(c, uid):
    st = ATT.pop(uid, None)
    chat = c.message.chat.id
    if not st:
        return bot.send_message(chat, "Сессия устарела, начни отметку заново")
    day = date.today().isoformat()
    q("DELETE FROM att WHERE subj=? AND day=?", (st["subj"], day))
    for sid, mk in st["marks"].items():
        q("INSERT INTO att(sid,subj,day,status) VALUES(?,?,?,?)", (sid, st["subj"], day, mk))
    absent = [n for s, n in st["names"] if st["marks"][s] == 1]
    late = [n for s, n in st["names"] if st["marks"][s] == 2]
    txt = "Сохранено ✅\n"
    txt += "❌ Нет: " + (", ".join(absent) or "никого") + "\n"
    txt += "⏰ Опоздали: " + (", ".join(late) or "никого")
    bot.edit_message_text(txt, chat, c.message.message_id)


def stats(m):
    gid, owner = ctx(m.from_user.id)
    if not owner:
        return
    rows = q("""SELECT s.name, SUM(a.status=1), SUM(a.status=2) FROM students s
                LEFT JOIN att a ON a.sid=s.id WHERE s.gid=? GROUP BY s.id
                ORDER BY 2 DESC""", (gid,))
    if not rows:
        return bot.send_message(m.chat.id, "Студентов пока нет")
    out = []
    for n, ab, lt in rows:
        ab, lt = ab or 0, lt or 0
        flag = "🚨" if ab >= LIMIT else ("⚠️" if ab == LIMIT - 1 else "✅")
        out.append(f"{flag} {n}: пропусков {ab}, опозданий {lt}")
    bot.send_message(m.chat.id, "📊 Посещаемость\n🚨 на грани | ⚠️ почти\n\n" + "\n".join(out))


# ---------- материалы ----------
def save_mat(m, subj):
    if m.text and (m.text.startswith("/") or m.text in BTN):
        return bot.send_message(m.chat.id, "Отменено")
    if m.document:
        kind, content = "doc", m.document.file_id
    elif m.photo:
        kind, content = "photo", m.photo[-1].file_id
    elif m.text:
        kind, content = "text", m.text
    else:
        return bot.send_message(m.chat.id, "Нужен файл, фото или текст")
    q("INSERT INTO materials(subj,kind,content,caption) VALUES(?,?,?,?)",
      (subj, kind, content, m.caption or ""))
    bot.send_message(m.chat.id, "Материал сохранён ✅")


def send_mats(chat, subj):
    rows = q("SELECT kind,content,caption FROM materials WHERE subj=?", (subj,))
    if not rows:
        return bot.send_message(chat, "Тут пока пусто")
    for k, c, cap in rows:
        if k == "doc":
            bot.send_document(chat, c, caption=cap)
        elif k == "photo":
            bot.send_photo(chat, c, caption=cap)
        else:
            bot.send_message(chat, c)


# ---------- домашка ----------
def hw_text(m, subj):
    if guard(m):
        return
    msg = bot.send_message(m.chat.id, "До какого числа? (например 25.11, или напиши «-»)")
    bot.register_next_step_handler(msg, hw_due, subj, m.text)


def hw_due(m, subj, text):
    if guard(m):
        return
    due = "" if m.text.strip() == "-" else m.text.strip()
    q("INSERT INTO hw(subj,text,due) VALUES(?,?,?)", (subj, text, due))
    name, gid = q("SELECT name,gid FROM subjects WHERE id=?", (subj,))[0]
    bot.send_message(m.chat.id, "ДЗ добавлено ✅ Студентам отправлено")
    for (u,) in q("SELECT user FROM members WHERE gid=?", (gid,)):
        try:
            bot.send_message(u, f"📝 Новое ДЗ: {name}\n{text}" + (f"\n⏰ До: {due}" if due else ""))
        except Exception:
            pass


def show_hw(m):
    gid, owner = ctx(m.from_user.id)
    if not gid:
        return
    rows = q("""SELECT h.id,s.name,h.text,h.due FROM hw h JOIN subjects s ON s.id=h.subj
                WHERE s.gid=? ORDER BY h.id DESC LIMIT 15""", (gid,))
    if not rows:
        return bot.send_message(m.chat.id, "Домашки нет 🎉")
    for i, n, t, d in rows:
        kb = None
        if owner:
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("🗑 Удалить", callback_data=f"dhw:{i}"))
        bot.send_message(m.chat.id, f"📌 {n}\n{t}" + (f"\n⏰ До: {d}" if d else ""), reply_markup=kb)


# ---------- нажатия на inline-кнопки ----------
@bot.callback_query_handler(func=lambda c: True)
def cb(c):
    uid, chat = c.from_user.id, c.message.chat.id
    gid, owner = ctx(uid)
    bot.answer_callback_query(c.id)
    if not gid:
        return
    d = c.data.split(":")
    if d[0] == "s":
        act, sid = d[1], int(d[2])
        if not q("SELECT 1 FROM subjects WHERE id=? AND gid=?", (sid, gid)):
            return
        if act == "att" and owner:
            start_att(chat, uid, gid, sid)
        elif act == "addm" and owner:
            msg = bot.send_message(chat, "Пришли файл, фото или текст 📎")
            bot.register_next_step_handler(msg, save_mat, sid)
        elif act == "addhw" and owner:
            msg = bot.send_message(chat, "Что задано? Напиши текст ✍️")
            bot.register_next_step_handler(msg, hw_text, sid)
        elif act == "mats":
            send_mats(chat, sid)
    elif d[0] == "t" and owner:
        toggle(c, uid, int(d[1]))
    elif d[0] == "save" and owner:
        save_att(c, uid)
    elif d[0] == "dhw" and owner:
        q("DELETE FROM hw WHERE id=?", (int(d[1]),))
        bot.delete_message(chat, c.message.message_id)


print("Бот запущен ✅")
bot.infinity_polling()
