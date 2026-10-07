import datetime
import logging
import os
import re
import sqlite3

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
) 
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("pishino")

BOT_TOKEN = os.getenv("BOT_TOKEN")

CHANNEL_USERNAME = "@pishino100official"
CHANNEL_LINK = "https://t.me/pishino100official"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "pishino.db")

DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

# ====================================================================
# DATABASE
# ====================================================================

conn = sqlite3.connect(DB_NAME, check_same_thread=False)
conn.row_factory = sqlite3.Row

conn.executescript(
    """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT,
    first TEXT,
    last TEXT,
    dob TEXT,
    lang TEXT,
    mode TEXT,
    country TEXT,
    grade TEXT,
    competition INTEGER DEFAULT 0,
    public INTEGER DEFAULT 0,
    rest INTEGER DEFAULT 0,
    goal TEXT,
    reward TEXT,
    created TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    name TEXT,
    weight REAL DEFAULT 1,
    proof INTEGER DEFAULT 0,
    active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS done (
    user_id INTEGER,
    task_id INTEGER,
    day TEXT,
    photo TEXT,
    PRIMARY KEY (user_id, task_id, day)
);

CREATE TABLE IF NOT EXISTS bugs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    text TEXT,
    created TEXT
);

CREATE TABLE IF NOT EXISTS restdays (
    user_id INTEGER,
    day TEXT,
    PRIMARY KEY (user_id, day)
);
"""
)


def ensure_column(table, column, decl):
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        conn.commit()


# ستون‌های جدید (برای دیتابیس‌های قدیمی هم کار می‌کند)
ensure_column("users", "edu", "TEXT")
ensure_column("users", "goal_month", "TEXT")


# ====================================================================
# HELPERS
# ====================================================================


def now():
    return datetime.datetime.now()


def today():
    return now().date().isoformat()


def this_month():
    return now().strftime("%Y-%m")


def get_user(user_id):
    return conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def touch_rest(user_id):
    """اگر حالت استراحت روشن است، امروز را روز استراحت ثبت می‌کند."""
    row = get_user(user_id)
    if row and row["rest"]:
        conn.execute(
            "INSERT OR IGNORE INTO restdays (user_id, day) VALUES (?, ?)",
            (user_id, today()),
        )
        conn.commit()


def main_menu():
    return ReplyKeyboardMarkup(
        [
            ["📋 برنامه من", "🌙 گزارش امروز"],
            ["🏆 رتبه‌بندی", "📊 آمار من"],
            ["🎖 دستاوردها", "🏅 لیگ"],
            ["🎁 هدف و پاداش", "⚙️ تنظیمات"],
            ["🛠 گزارش مشکل"],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def home_button():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🏠 خانه", callback_data="home")]]
    )


def join_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📢 عضویت در کانال رسمی", url=CHANNEL_LINK)],
            [InlineKeyboardButton("✅ بررسی عضویت", callback_data="check")],
        ]
    )


async def safe_edit(query, text, markup=None):
    try:
        await query.edit_message_text(text, reply_markup=markup)
    except BadRequest as error:
        if "not modified" not in str(error).lower():
            raise


async def check_member(context, user_id):
    try:
        member = await context.bot.get_chat_member(
            chat_id=CHANNEL_USERNAME, user_id=user_id
        )
        return member.status not in ("left", "kicked")
    except Exception as error:
        logger.warning("Membership check failed: %s", error)
        return False


# ====================================================================
# STREAK / POINTS / LEAGUE / ACHIEVEMENTS
# ====================================================================


def active_day_set(user_id):
    rows = conn.execute(
        "SELECT DISTINCT day FROM done WHERE user_id=?", (user_id,)
    ).fetchall()
    days = {r["day"] for r in rows}
    rest_rows = conn.execute(
        "SELECT day FROM restdays WHERE user_id=?", (user_id,)
    ).fetchall()
    days |= {r["day"] for r in rest_rows}
    return days


def current_streak(user_id):
    days = active_day_set(user_id)
    d = now().date()
    if d.isoformat() not in days:
        d -= datetime.timedelta(days=1)
    streak = 0
    while d.isoformat() in days:
        streak += 1
        d -= datetime.timedelta(days=1)
    return streak


def best_streak(user_id):
    days = sorted(active_day_set(user_id))
    best = 0
    run = 0
    prev = None
    for text in days:
        try:
            d = datetime.date.fromisoformat(text)
        except ValueError:
            continue
        if prev is not None and (d - prev).days == 1:
            run += 1
        else:
            run = 1
        best = max(best, run)
        prev = d
    return best


def month_points(user_id):
    row = conn.execute(
        """
        SELECT COALESCE(SUM(t.weight), 0) AS pts
        FROM done d
        JOIN tasks t ON t.id = d.task_id
        WHERE d.user_id=? AND d.day LIKE ?
        """,
        (user_id, this_month() + "%"),
    ).fetchone()
    return row["pts"] or 0


def league_for(points):
    if points < 100:
        return "🥉 برنز"
    if points < 300:
        return "🥈 نقره"
    if points < 700:
        return "🥇 طلا"
    return "💎 الماس"


def daily_numbers(user_id, day=None):
    day = day or today()
    rows = conn.execute(
        """
        SELECT t.id, t.name, t.weight, d.task_id AS completed
        FROM tasks t
        LEFT JOIN done d
          ON d.task_id = t.id AND d.user_id = ? AND d.day = ?
        WHERE t.user_id = ? AND t.active = 1
        ORDER BY t.id
        """,
        (user_id, day, user_id),
    ).fetchall()
    total = sum(r["weight"] for r in rows)
    completed = sum(r["weight"] for r in rows if r["completed"] is not None)
    score = round(completed / total * 100) if total > 0 else 0
    return rows, total, completed, score


# ====================================================================
# /start  /menu
# ====================================================================


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("state", None)

    if get_user(update.effective_user.id):
        await update.message.reply_text(
            "🏠 به پیشینو خوش برگشتی!", reply_markup=main_menu()
        )
        return

    keyboard = [
        [
            InlineKeyboardButton("🇮🇷 فارسی", callback_data="lang_fa"),
            InlineKeyboardButton("🇬🇧 English", callback_data="lang_en"),
        ],
        [
            InlineKeyboardButton("🇸🇦 العربية", callback_data="lang_ar"),
            InlineKeyboardButton("🇹🇷 Türkçe", callback_data="lang_tr"),
        ],
        [
            InlineKeyboardButton("🇪🇸 Español", callback_data="lang_es"),
            InlineKeyboardButton("🇫🇷 Français", callback_data="lang_fr"),
        ],
    ]

    await update.message.reply_text(
        "🌍 زبان خود را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("state", None)

    if not get_user(update.effective_user.id):
        await update.message.reply_text("ابتدا /start را بزن.")
        return

    await update.message.reply_text("🏠 پیشینو", reply_markup=main_menu())
# ====================================================================
# CALLBACKS
# ====================================================================


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data

    # ---------------- MEMBERSHIP CHECK ----------------
    if data == "check":
        if not await check_member(context, user_id):
            await query.answer("❌ هنوز عضو کانال نیستی!", show_alert=True)
            return

        await query.answer()

        if get_user(user_id):
            await safe_edit(query, "🏠 پیشینو", main_menu())
            return

        context.user_data["state"] = "first"
        await safe_edit(
            query, "🎉 عضویت تأیید شد!\n\nحالا اسم کوچکت رو بفرست:"
        )
        return

    await query.answer()

    # ---------------- LANGUAGE ----------------
    if data.startswith("lang_"):
        if get_user(user_id):
            await safe_edit(query, "🏠 پیشینو", main_menu())
            return

        context.user_data["lang"] = data[5:]
        await safe_edit(
            query,
            "✅ زبان انتخاب شد.\n\n"
            "برای استفاده از پیشینو ابتدا عضو کانال رسمی شوید:",
            join_keyboard(),
        )
        return

    # ---------------- MODE (ثبت‌نام) ----------------
    if data in ("mode_study", "mode_normal"):
        if context.user_data.get("state") != "mode":
            await safe_edit(query, "ابتدا /start را بزن.")
            return

        if data == "mode_study":
            context.user_data["mode"] = "study"
            context.user_data["state"] = "country"
            await safe_edit(
                query, "📚 حالت مطالعه انتخاب شد.\n\nکشورت رو بنویس:"
            )
            return

        context.user_data["mode"] = "normal"
        await save_user(query.from_user, context)
        return

    # از اینجا به بعد کاربر باید ثبت‌نام کرده باشد
    if not get_user(user_id):
        await safe_edit(query, "ابتدا /start را بزن.")
        return

    # عضویت اجباری
    if not await check_member(context, user_id):
        await safe_edit(
            query,
            "📢 برای استفاده از پیشینو باید عضو کانال رسمی باشی:",
            join_keyboard(),
        )
        return

    touch_rest(user_id)

    # ---------------- HOME ----------------
    if data == "home":
        context.user_data.pop("state", None)
        await safe_edit(query, "🏠 پیشینو", main_menu())
        return

    # ---------------- PLAN ----------------
    if data == "plan":
        await show_plan(query, context)
        return

    if data == "addtask":
        context.user_data["state"] = "task"
        await query.message.reply_text(
            "➕ کار جدید را این‌طوری بفرست:\n\n"
            "نام کار | امتیاز\n\n"
            "مثال:\n"
            "مطالعه ۲ ساعت | 30"
        )
        return

    if data == "manage":
        await show_manage(query, context)
        return

    if data.startswith("del:"):
        task_id = int(data.split(":")[1])
        conn.execute(
            "UPDATE tasks SET active=0 WHERE id=? AND user_id=?",
            (task_id, user_id),
        )
        conn.commit()
        await show_manage(query, context)
        return

    if data.startswith("done:"):
        task_id = int(data.split(":")[1])

        task = conn.execute(
            "SELECT id FROM tasks WHERE id=? AND user_id=? AND active=1",
            (task_id, user_id),
        ).fetchone()

        if task:
            existing = conn.execute(
                "SELECT 1 FROM done WHERE user_id=? AND task_id=? AND day=?",
                (user_id, task_id, today()),
            ).fetchone()

            if existing:
                conn.execute(
                    "DELETE FROM done WHERE user_id=? AND task_id=? AND day=?",
                    (user_id, task_id, today()),
                )
            else:
                conn.execute(
                    "INSERT OR IGNORE INTO done (user_id, task_id, day, photo) "
                    "VALUES (?, ?, ?, ?)",
                    (user_id, task_id, today(), ""),
                )
            conn.commit()

        await show_plan(query, context)
        return

    # ---------------- REPORT / RANK / STATS ----------------
    if data == "report":
        await show_report(query, context)
        return

    if data == "rank":
        await show_rank(query, context)
        return

    if data == "stats":
        await show_stats(query, context)
        return

    if data == "ach":
        await show_achievements(query, context)
        return

    if data == "league":
        await show_league(query, context)
        return

    # ---------------- GOAL ----------------
    if data == "goal":
        await show_goal(query, context)
        return

    if data == "goal_edit":
        context.user_data["state"] = "goal"
        await query.message.reply_text("🎯 هدف ماهانه‌ات رو بنویس:")
        return

    # ---------------- SETTINGS ----------------
    if data == "settings":
        await show_settings(query)
        return

    if data in ("toggle_comp", "toggle_public", "toggle_rest"):
        column = {
            "toggle_comp": "competition",
            "toggle_public": "public",
            "toggle_rest": "rest",
        }[data]

        conn.execute(
            f"UPDATE users SET {column} = 1 - COALESCE({column}, 0) WHERE id=?",
            (user_id,),
        )
        conn.commit()

        if data == "toggle_rest":
            row = get_user(user_id)
            if row["rest"]:
                conn.execute(
                    "INSERT OR IGNORE INTO restdays (user_id, day) VALUES (?, ?)",
                    (user_id, today()),
                )
            else:
                conn.execute(
                    "DELETE FROM restdays WHERE user_id=? AND day=?",
                    (user_id, today()),
                )
            conn.commit()

        await show_settings(query)
        return

    # ---------------- BUG ----------------
    if data == "bug":
        context.user_data["state"] = "bug"
        await query.message.reply_text("🛠 مشکل را کامل توضیح بده:")
        return


# ====================================================================
# TEXT MESSAGES
# ====================================================================


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id
    state = context.user_data.get("state")
    text = update.message.text.strip()
    button_data = {
        "📋 برنامه من": "plan",
        "🌙 گزارش امروز": "report",
        "🏆 رتبه‌بندی": "rank",
        "📊 آمار من": "stats",
        "🎖 دستاوردها": "ach",
        "🏅 لیگ": "league",
        "🎁 هدف و پاداش": "goal",
        "⚙️ تنظیمات": "settings",
        "🛠 گزارش مشکل": "bug",
    }

    if text in button_data and not state:
        class FakeQuery:
            def __init__(self, update, data):
                self._update = update
                self.data = data
                self.message = update.message
                self.from_user = update.effective_user

            async def answer(self, *args, **kwargs):
                pass

            async def edit_message_text(self, text, *args, **kwargs):
                return await self.message.reply_text(
                    text,
                    **kwargs
                )

        class FakeUpdate:
            def __init__(self, update, data):
                self.callback_query = FakeQuery(update, data)
                self.effective_user = update.effective_user
                self.message = update.message

        await callback_handler(
            FakeUpdate(update, button_data[text]),
            context
        )
        return

    # ---------------- REGISTRATION ----------------
    if state == "first":
        context.user_data["first"] = text[:40]
        context.user_data["state"] = "last"
        await update.message.reply_text("👤 نام خانوادگی:")
        return

    if state == "last":
        context.user_data["last"] = text[:40]
        context.user_data["state"] = "dob"
        await update.message.reply_text(
            "🎂 تاریخ تولدت رو بنویس:\n\nمثال: 1385/04/12 یا 2006-07-03"
        )
        return

    if state == "dob":
        value = text.translate(DIGITS)

        if not re.fullmatch(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}", value):
            await update.message.reply_text(
                "❌ فرمت تاریخ درست نیست.\n\nمثال: 1385/04/12"
            )
            return

        context.user_data["dob"] = value
        context.user_data["state"] = "mode"

        keyboard = [
            [InlineKeyboardButton("📚 حالت مطالعه", callback_data="mode_study")],
            [InlineKeyboardButton("⚡ حالت عادی", callback_data="mode_normal")],
        ]
        await update.message.reply_text(
            "نوع استفاده‌ات رو انتخاب کن:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if state == "mode":
        await update.message.reply_text(
            "لطفاً یکی از دکمه‌های بالا را انتخاب کن 👆"
        )
        return

    if state == "country":
        context.user_data["country"] = text[:60]
        context.user_data["state"] = "grade"
        await update.message.reply_text(
            "🎓 پایه و سیستم آموزشی‌ات رو بنویس:\n\n"
            "مثال: دهم، ایران، رشته ریاضی"
        )
        return

    if state == "grade":
        context.user_data["grade"] = text[:80]
        context.user_data["edu"] = text[:80]
        await save_user(update.effective_user, context)
        return

    # از اینجا به بعد کاربر باید ثبت‌نام کرده باشد
    if not get_user(user_id):
        await update.message.reply_text("ابتدا /start را بزن.")
        return

    # ---------------- TASK ----------------
    if state == "task":
        try:
            name, weight_text = text.split("|", 1)
            name = name.strip()
            weight = float(
                weight_text.translate(DIGITS).replace("٫", ".").strip()
            )
            if not name or weight <= 0 or weight > 1000:
                raise ValueError
        except Exception:
            await update.message.reply_text(
                "❌ فرمت اشتباهه.\n\n"
                "مثال درست:\n"
                "مطالعه ۲ ساعت | 30"
            )
            return

        conn.execute(
            "INSERT INTO tasks (user_id, name, weight) VALUES (?, ?, ?)",
            (user_id, name[:60], weight),
        )
        conn.commit()

        context.user_data["state"] = None
        await update.message.reply_text(
            "✅ کار با موفقیت اضافه شد!", reply_markup=main_menu()
        )
        return

    # ---------------- GOAL ----------------
    if state == "goal":
        context.user_data["goal"] = text[:200]
        context.user_data["state"] = "reward"
        await update.message.reply_text(
            "🎁 اگر به این هدف رسیدی چه پاداشی می‌خوای؟"
        )
        return

    if state == "reward":
        goal = context.user_data.get("goal", "")

        conn.execute(
            "UPDATE users SET goal=?, reward=?, goal_month=? WHERE id=?",
            (goal, text[:200], this_month(), user_id),
        )
        conn.commit()

        context.user_data["state"] = None
        await update.message.reply_text(
            "🎁 هدف و پاداشت ذخیره شد!", reply_markup=main_menu()
        )
        return

    # ---------------- BUG ----------------
    if state == "bug":
        conn.execute(
            "INSERT INTO bugs (user_id, text, created) VALUES (?, ?, ?)",
            (user_id, text[:1000], now().isoformat()),
        )
        conn.commit()

        context.user_data["state"] = None
        await update.message.reply_text(
            "🛠 گزارش مشکل ثبت شد.\n"
            "ممنون که پیشینو رو بهتر می‌کنی!",
            reply_markup=main_menu(),
        )
        return

    # هیچ حالتی فعال نبود
    await update.message.reply_text("🏠 پیشینو", reply_markup=main_menu())


# ====================================================================
# SAVE USER
# ====================================================================


async def save_user(telegram_user, context):
    user_id = telegram_user.id

    conn.execute(
        """
        INSERT OR REPLACE INTO users
        (id, username, first, last, dob, lang, mode, country, grade,
         competition, public, rest, goal, reward, created, edu, goal_month)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            telegram_user.username or "",
            context.user_data.get("first", ""),
            context.user_data.get("last", ""),
            context.user_data.get("dob", ""),
            context.user_data.get("lang", "fa"),
            context.user_data.get("mode", "normal"),
            context.user_data.get("country", ""),
            context.user_data.get("grade", ""),
            0,
            0,
            0,
            "",
            "",
            now().isoformat(),
            context.user_data.get("edu", ""),
            "",
        ),
    )
    conn.commit()

    context.user_data.clear()

    await context.bot.send_message(
        chat_id=user_id,
        text="🎉 پروفایلت ساخته شد!\n\nاز اینجا می‌تونی برنامه‌ات رو بسازی.",
        reply_markup=main_menu(),
    )
# ====================================================================
# SCREENS
# ====================================================================


async def show_plan(query, context):
    user_id = query.from_user.id
    rows, total, completed, score = daily_numbers(user_id)

    keyboard = []
    for row in rows:
        icon = "✅" if row["completed"] is not None else "⬜"
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"{icon} {row['name']} ({row['weight']:g})",
                    callback_data=f"done:{row['id']}",
                )
            ]
        )

    keyboard.append(
        [
            InlineKeyboardButton("➕ افزودن کار", callback_data="addtask"),
            InlineKeyboardButton("🗂 مدیریت", callback_data="manage"),
        ]
    )
    keyboard.append([InlineKeyboardButton("🏠 خانه", callback_data="home")])

    if rows:
        text = (
            "📋 برنامه من\n\n"
            "روی هر کار بزن تا برای امروز ثبت شود "
            "(یک بار دیگر بزنی برداشته می‌شود).\n\n"
            f"🎯 امتیاز امروز: {score}/100"
        )
    else:
        text = "📋 برنامه من\n\nهنوز کاری اضافه نکردی."

    await safe_edit(query, text, InlineKeyboardMarkup(keyboard))


async def show_manage(query, context):
    user_id = query.from_user.id
    rows = conn.execute(
        "SELECT id, name, weight FROM tasks WHERE user_id=? AND active=1 ORDER BY id",
        (user_id,),
    ).fetchall()

    keyboard = []
    for row in rows:
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"🗑 {row['name']} ({row['weight']:g})",
                    callback_data=f"del:{row['id']}",
                )
            ]
        )

    keyboard.append([InlineKeyboardButton("➕ افزودن کار", callback_data="addtask")])
    keyboard.append([InlineKeyboardButton("📋 برنامه من", callback_data="plan")])
    keyboard.append([InlineKeyboardButton("🏠 خانه", callback_data="home")])

    if rows:
        text = "🗂 مدیریت کارها\n\nبرای حذف هر کار روی آن بزن."
    else:
        text = "🗂 مدیریت کارها\n\nهیچ کاری برای حذف وجود ندارد."

    await safe_edit(query, text, InlineKeyboardMarkup(keyboard))


async def show_report(query, context):
    user_id = query.from_user.id
    rows, total, completed, score = daily_numbers(user_id)
    streak = current_streak(user_id)
    user = get_user(user_id)

    lines = [
        "🌙 گزارش امروز\n",
        f"🎯 امتیاز: {score}/100",
        f"✅ انجام‌شده: {completed:g}",
        f"📋 کل برنامه: {total:g}",
        f"🔥 Streak: {streak} روز",
    ]

    if user["rest"]:
        lines.append("😴 حالت استراحت روشن است.")

    if rows:
        lines.append("")
        for row in rows:
            icon = "✅" if row["completed"] is not None else "⬜"
            lines.append(f"{icon} {row['name']}")

    await safe_edit(query, "\n".join(lines), main_menu())


async def show_rank(query, context):
    user_id = query.from_user.id

    rows = conn.execute(
        """
        SELECT u.id, u.username, u.first, u.public,
               COALESCE(SUM(t.weight), 0) AS pts
        FROM users u
        LEFT JOIN done d
          ON d.user_id = u.id AND d.day LIKE ?
        LEFT JOIN tasks t
          ON t.id = d.task_id
        WHERE u.competition = 1
        GROUP BY u.id
        ORDER BY pts DESC, u.id ASC
        """,
        (this_month() + "%",),
    ).fetchall()

    me = get_user(user_id)

    if not rows:
        text = "🏆 رتبه‌بندی ماه\n\nهنوز کسی وارد رقابت نشده است."
    else:
        lines = ["🏆 رتبه‌بندی ماه\n"]

        for index, row in enumerate(rows[:10], start=1):
            if row["id"] == user_id:
                name = "شما"
            elif row["public"]:
                name = ("@" + row["username"]) if row["username"] else (
                    row["first"] or "کاربر"
                )
            else:
                name = "کاربر ناشناس"

            lines.append(f"{index}. {name} — {row['pts']:g} امتیاز")

        position = next(
            (i for i, r in enumerate(rows, start=1) if r["id"] == user_id), None
        )

        if position and position > 10:
            mine = next(r for r in rows if r["id"] == user_id)
            lines.append(f"\n📍 رتبه شما: {position} — {mine['pts']:g} امتیاز")

        text = "\n".join(lines)

    if not me["competition"]:
        text += (
            "\n\nℹ️ تو الان در رقابت نیستی. از تنظیمات می‌تونی وارد رقابت بشی."
        )

    await safe_edit(query, text, main_menu())


async def show_stats(query, context):
    user_id = query.from_user.id

    active_days = conn.execute(
        "SELECT COUNT(DISTINCT day) AS n FROM done WHERE user_id=?", (user_id,)
    ).fetchone()["n"]

    total_done = conn.execute(
        "SELECT COUNT(*) AS n FROM done WHERE user_id=?", (user_id,)
    ).fetchone()["n"]

    total_tasks = conn.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE user_id=? AND active=1", (user_id,)
    ).fetchone()["n"]

    points = month_points(user_id)

    await safe_edit(
        query,
        "📊 آمار من\n\n"
        f"📅 روزهای فعال: {active_days}\n"
        f"✅ کارهای انجام‌شده: {total_done}\n"
        f"📋 تعداد کارهای برنامه: {total_tasks}\n"
        f"🔥 Streak فعلی: {current_streak(user_id)} روز\n"
        f"🏅 بهترین Streak: {best_streak(user_id)} روز\n"
        f"⭐ امتیاز این ماه: {points:g}\n"
        f"🏆 لیگ: {league_for(points)}",
        main_menu(),
    )


async def show_achievements(query, context):
    user_id = query.from_user.id
    user = get_user(user_id)

    total_done = conn.execute(
        "SELECT COUNT(*) AS n FROM done WHERE user_id=?", (user_id,)
    ).fetchone()["n"]

    best = best_streak(user_id)

    items = [
        ("🌱 اولین کار انجام‌شده", total_done >= 1),
        ("🔟 ۱۰ کار انجام‌شده", total_done >= 10),
        ("💯 ۱۰۰ کار انجام‌شده", total_done >= 100),
        ("🔥 Streak سه‌روزه", best >= 3),
        ("🔥 Streak هفت‌روزه", best >= 7),
        ("🔥 Streak سی‌روزه", best >= 30),
        ("🎯 تعیین هدف ماهانه", bool(user["goal"])),
        ("🏆 ورود به رقابت", bool(user["competition"])),
    ]

    lines = ["🎖 دستاوردها\n"]
    for title, unlocked in items:
        lines.append(("✅ " if unlocked else "🔒 ") + title)

    await safe_edit(query, "\n".join(lines), main_menu())


async def show_league(query, context):
    user_id = query.from_user.id
    points = month_points(user_id)

    await safe_edit(
        query,
        "🏅 لیگ ماهانه\n\n"
        f"⭐ امتیاز این ماه: {points:g}\n"
        f"🏆 لیگ فعلی: {league_for(points)}\n\n"
        "🥉 برنز: کمتر از ۱۰۰\n"
        "🥈 نقره: از ۱۰۰\n"
        "🥇 طلا: از ۳۰۰\n"
        "💎 الماس: از ۷۰۰",
        main_menu(),
    )


async def show_goal(query, context):
    user_id = query.from_user.id
    user = get_user(user_id)

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✏️ ثبت / تغییر هدف", callback_data="goal_edit")],
            [InlineKeyboardButton("🏠 خانه", callback_data="home")],
        ]
    )

    if user["goal"] and user["goal_month"] == this_month():
        text = (
            "🎁 هدف و پاداش این ماه\n\n"
            f"🎯 هدف: {user['goal']}\n"
            f"🎁 پاداش: {user['reward']}\n"
            f"⭐ امتیاز این ماه: {month_points(user_id):g}"
        )
    else:
        text = (
            "🎁 هدف و پاداش\n\n"
            "برای این ماه هنوز هدفی ثبت نکردی."
        )

    await safe_edit(query, text, keyboard)


async def show_settings(query):
    row = get_user(query.from_user.id)

    if not row:
        await safe_edit(query, "ابتدا /start را بزن.")
        return

    competition = "روشن 🟢" if row["competition"] else "خاموش 🔴"
    public = "روشن 🟢" if row["public"] else "خاموش 🔴"
    rest = "روشن 🟢" if row["rest"] else "خاموش 🔴"

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🏆 رقابت", callback_data="toggle_comp"),
                InlineKeyboardButton("🌍 عمومی", callback_data="toggle_public"),
            ],
            [InlineKeyboardButton("😴 حالت استراحت", callback_data="toggle_rest")],
            [InlineKeyboardButton("🏠 خانه", callback_data="home")],
        ]
    )

    await safe_edit(
        query,
        "⚙️ تنظیمات\n\n"
        f"🏆 رقابت: {competition}\n"
        f"🌍 نمایش عمومی: {public}\n"
        f"😴 استراحت: {rest}",
        keyboard,
    )


# ====================================================================
# ERRORS + MAIN
# ====================================================================


async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled error:", exc_info=context.error)


def main():
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN تنظیم نشده است.")
        print('اول این دستور را بزن:  export BOT_TOKEN="توکن_ربات"')
        return

    app = (
    ApplicationBuilder()
    .token(BOT_TOKEN)
    .connect_timeout(30)
    .read_timeout(30)
    .write_timeout(30)
    .pool_timeout(30)
    .build()
)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler)
    )
    app.add_error_handler(error_handler)

    print("✅ Pishino bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
