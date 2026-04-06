import os, re, asyncio, logging, tempfile, json, uuid
from datetime import datetime, timedelta, timezone, date as dt_date
from dotenv import load_dotenv
from groq import Groq
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    CallbackQueryHandler, filters, ContextTypes
)

load_dotenv()
logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])

MAX_VOICE_DURATION = 200
MAX_REMINDERS = 5
N_EMOJI = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]

# ═══════════════════════════════════════════════════════
#  JSON ХРАНИЛИЩЕ
# ═══════════════════════════════════════════════════════
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")

def load_data() -> dict:
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_data(data: dict):
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка сохранения данных: {e}")

def get_user_data_json(user_id: int) -> dict:
    data = load_data()
    uid = str(user_id)
    if uid not in data:
        data[uid] = {}
    return data[uid]

def set_user_data_json(user_id: int, user_data: dict):
    data = load_data()
    data[str(user_id)] = user_data
    save_data(data)

def sync_to_json(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    ud = context.user_data
    stored = {
        "tasks": ud.get("tasks", []),
        "today_steps": ud.get("today_steps", []),
        "today_priority": ud.get("today_priority", ""),
        "today_date": ud.get("today_date", ""),
        "tomorrow_steps": ud.get("tomorrow_steps", []),
        "tomorrow_priority": ud.get("tomorrow_priority", ""),
        "reminders": ud.get("reminders", []),
    }
    set_user_data_json(user_id, stored)

def load_from_json(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    stored = get_user_data_json(user_id)
    if stored:
        context.user_data.setdefault("tasks", stored.get("tasks", []))
        context.user_data.setdefault("today_steps", stored.get("today_steps", []))
        context.user_data.setdefault("today_priority", stored.get("today_priority", ""))
        context.user_data.setdefault("today_date", stored.get("today_date", ""))
        context.user_data.setdefault("tomorrow_steps", stored.get("tomorrow_steps", []))
        context.user_data.setdefault("tomorrow_priority", stored.get("tomorrow_priority", ""))
        context.user_data.setdefault("reminders", stored.get("reminders", []))

# ═══════════════════════════════════════════════════════
#  ГЛОБАЛЬНАЯ ССЫЛКА НА APP
# ═══════════════════════════════════════════════════════
_app_ref = None

# ═══════════════════════════════════════════════════════
#  ПРОМПТЫ
# ═══════════════════════════════════════════════════════
TASKS_PROMPT = """Ты — ассистент для создания задач из голосовых сообщений и текста.

Выдели от 1 до 5 конкретных задач. Для каждой:
- Короткий заголовок (3-7 слов)
- Краткое описание (1-2 предложения)

Формат СТРОГО:
ЗАДАЧА 1
📌 [заголовок]
📝 [описание]

ЗАДАЧА 2
📌 [заголовок]
📝 [описание]

Только русский язык. Никаких вступлений."""

PLAN_PROMPT = """Ты — эксперт по тайм-менеджменту. Составь оптимальный план дня.

Учитывай: логику порядка, энергозатратность, зависимости задач, здравый смысл.

Формат СТРОГО (каждый блок на отдельных строках):
🗓 ПЛАН НА ДЕНЬ

ШАГ 1 — [время, например "08:00" или "Утро"]
☑️ [что делать]
💡 [почему именно сейчас — 1 предложение]

ШАГ 2 — [время]
☑️ [что делать]
💡 [почему именно сейчас — 1 предложение]

(3–7 шагов)

⚡️ Главный приоритет: [самая важная задача]

Только русский язык."""

REMINDER_PROMPT = """Ты — ассистент для создания напоминаний.

Из текста пользователя извлеки:
1. Короткое название напоминания (3-6 слов)
2. Описание — только если текст достаточно подробный и есть что уточнить (1 предложение, максимум 80 символов). Если текст короткий или простой — пиши "нет".
3. Время — если упомянуто (например "в 18:00", "в 9 утра" → 09:00, "в полдень" → 12:00, "вечером" → 19:00)
4. Дату — если упомянуто
5. Относительное время — если сказано "через X минут/часов"

Для ВРЕМЕНИ: HH:MM или "не указано"
Для ДАТЫ используй один из форматов:
- сегодня / завтра / послезавтра
- через N дней (например: через 3 дня)
- день недели (например: в пятницу, в понедельник)
- DD.MM.YYYY (конкретная дата)
- не указано

Для ОТНОСИТЕЛЬНОГО: "через N минут" или "через N часов"
Если сказано "через X минут/часов" — пиши в ОТНОСИТЕЛЬНОЕ, остальные поля оставь "не указано".

Формат СТРОГО:
НАЗВАНИЕ: [название]
ОПИСАНИЕ: [1 предложение или "нет"]
ВРЕМЯ: [HH:MM или "не указано"]
ДАТА: [дата в одном из форматов выше]
ОТНОСИТЕЛЬНОЕ: [через N минут / через N часов / не указано]

Только русский язык. Никаких вступлений."""

# ═══════════════════════════════════════════════════════
#  НИЖНЕЕ МЕНЮ
# ═══════════════════════════════════════════════════════
def get_bottom_kb(context: ContextTypes.DEFAULT_TYPE) -> ReplyKeyboardMarkup:
    has_today = bool(context.user_data.get("today_steps"))
    has_tomorrow = bool(context.user_data.get("tomorrow_steps"))
    # Проверяем активные напоминания (не просроченные)
    now_utc = datetime.now(timezone.utc)
    active_rems = [
        r for r in context.user_data.get("reminders", [])
        if datetime.fromisoformat(r["fire_at"]) > now_utc
    ]
    has_reminders = bool(active_rems)

    row1 = [KeyboardButton("📎 Задача"), KeyboardButton("📁 План"), KeyboardButton("✉️ Напоминание")]
    row2 = [KeyboardButton("📋 Мои задачи")]
    if has_today:
        row2.append(KeyboardButton("🗓 Сегодня"))
    if has_tomorrow:
        row2.append(KeyboardButton("🌙 Завтра"))
    if has_reminders:
        row2.append(KeyboardButton("🔔 Напоминания"))
    return ReplyKeyboardMarkup([row1, row2], resize_keyboard=True)

# ═══════════════════════════════════════════════════════
#  ЭМОДЗИ ДЛЯ ЗАДАЧ/НАПОМИНАНИЙ
# ═══════════════════════════════════════════════════════
def task_emoji(index: int) -> str:
    if index < 10:
        return N_EMOJI[index]
    return f"*️⃣{index + 1}"

# ═══════════════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ═══════════════════════════════════════════════════════
def get_tasks(ctx) -> list:
    if "tasks" not in ctx.user_data:
        ctx.user_data["tasks"] = []
    return ctx.user_data["tasks"]

def get_reminders(ctx) -> list:
    if "reminders" not in ctx.user_data:
        ctx.user_data["reminders"] = []
    return ctx.user_data["reminders"]

def parse_tasks(text: str) -> list:
    blocks = re.split(r"ЗАДАЧА\s+\d+", text)
    return [b.strip() for b in blocks if b.strip()]

def parse_plan(text: str) -> tuple:
    steps = []
    for marker in ("☑️", "✅"):
        pattern = re.compile(
            rf"ШАГ\s+\d+\s*[—\-]\s*([^\n]+)\n{re.escape(marker)}\s*([^\n]+)\n💡\s*([^\n]+)",
            re.MULTILINE
        )
        for m in pattern.finditer(text):
            steps.append({
                "time": m.group(1).strip(),
                "action": m.group(2).strip(),
                "reason": m.group(3).strip()
            })
        if steps:
            break
    priority = ""
    pm = re.search(r"⚡️\s*Главный приоритет:\s*(.+)", text)
    if pm:
        priority = pm.group(1).strip()
    return steps, priority

# ─── Работа с датами ────────────────────────────────────

def now_msk() -> datetime:
    return datetime.now(timezone(timedelta(hours=3)))

def resolve_date_str(date_str: str) -> str | None:
    """
    Преобразует строку даты от LLM в YYYY-MM-DD.
    Возвращает None если не удалось распознать.
    """
    if not date_str:
        return None
    s = date_str.strip().lower()
    if "не указано" in s or s == "":
        return None

    today = now_msk().date()

    if s == "сегодня":
        return today.strftime("%Y-%m-%d")
    if s == "завтра":
        return (today + timedelta(days=1)).strftime("%Y-%m-%d")
    if s == "послезавтра":
        return (today + timedelta(days=2)).strftime("%Y-%m-%d")

    # через N дней
    m = re.match(r"через\s+(\d+)\s+д", s)
    if m:
        return (today + timedelta(days=int(m.group(1)))).strftime("%Y-%m-%d")

    # день недели
    days_ru = {
        "понедельник": 0, "вторник": 1,
        "среда": 2, "среды": 2, "среду": 2,
        "четверг": 3,
        "пятница": 4, "пятницу": 4, "пятницы": 4,
        "суббота": 5, "субботу": 5, "субботы": 5,
        "воскресенье": 6, "воскресенья": 6,
    }
    for name, num in days_ru.items():
        if name in s:
            days_ahead = (num - today.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7  # следующая неделя
            return (today + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

    # DD.MM.YYYY или DD.MM
    m = re.match(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?", date_str.strip())
    if m:
        day = int(m.group(1))
        month = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        try:
            d = dt_date(year, month, day)
            if d < today:
                d = dt_date(year + 1, month, day)
            return d.strftime("%Y-%m-%d")
        except Exception:
            pass

    return None

def parse_date_input(text: str) -> str | None:
    """
    Парсит ввод пользователя вида DD.MM или DD.MM.YYYY.
    Возвращает YYYY-MM-DD или None.
    """
    text = text.strip()
    today = now_msk().date()
    m = re.match(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?$", text)
    if m:
        day = int(m.group(1))
        month = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        try:
            d = dt_date(year, month, day)
            if d < today:
                d = dt_date(year + 1, month, day)
            return d.strftime("%Y-%m-%d")
        except Exception:
            return None
    return None

def parse_reminder_llm(text: str) -> dict:
    result = {"name": "", "description": None, "time": None, "fire_date": None, "day": "today", "relative_minutes": None}

    name_m = re.search(r"НАЗВАНИЕ:\s*(.+)", text)
    if name_m:
        result["name"] = name_m.group(1).strip()

    desc_m = re.search(r"ОПИСАНИЕ:\s*(.+)", text)
    if desc_m:
        d = desc_m.group(1).strip()
        if d.lower() != "нет" and len(d) > 3:
            result["description"] = d

    time_m = re.search(r"ВРЕМЯ:\s*(.+)", text)
    if time_m:
        t = time_m.group(1).strip()
        if "не указано" not in t.lower():
            tm = re.search(r"(\d{1,2}):(\d{2})", t)
            if tm:
                result["time"] = f"{int(tm.group(1)):02d}:{tm.group(2)}"

    # Относительное время — приоритет над датой/временем
    rel_m = re.search(r"ОТНОСИТЕЛЬНОЕ:\s*(.+)", text)
    if rel_m:
        rel = rel_m.group(1).strip().lower()
        if "не указано" not in rel:
            # через N минут
            m = re.search(r"через\s+(\d+)\s*мин", rel)
            if m:
                result["relative_minutes"] = int(m.group(1))
            # через N часов
            m = re.search(r"через\s+(\d+)\s*час", rel)
            if m:
                result["relative_minutes"] = int(m.group(1)) * 60

    if not result["relative_minutes"]:
        date_m = re.search(r"ДАТА:\s*(.+)", text)
        if date_m:
            d_str = date_m.group(1).strip()
            fire_date = resolve_date_str(d_str)
            if fire_date:
                result["fire_date"] = fire_date
                today = now_msk().date()
                resolved = dt_date.fromisoformat(fire_date)
                if resolved == today:
                    result["day"] = "today"
                elif resolved == today + timedelta(days=1):
                    result["day"] = "tomorrow"
                else:
                    result["day"] = "other"

    return result

def parse_time(text: str):
    text = text.strip()
    m = re.match(r"^(\d{1,2}):(\d{2})$", text)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.match(r"^(\d{1,2})$", text)
    if m:
        return int(m.group(1)), 0
    return None

def render_plan(steps: list, priority: str, title="ПЛАН НА ДЕНЬ") -> str:
    out = f"🗓 {title}\n\n"
    for i, s in enumerate(steps):
        out += f"ШАГ {i+1} — {s['time']}\n☑️ {s['action']}\n💡 {s['reason']}\n\n"
    if priority:
        out += f"⚡️ Главный приоритет: {priority}"
    return out.strip()

def plan_step_keyboard(steps: list, plan_type: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        f"⏰ {s['time']}", callback_data=f"step:{plan_type}:{i}"
    )] for i, s in enumerate(steps)]
    rows.append([InlineKeyboardButton("🗑 Удалить план", callback_data=f"delete_plan:{plan_type}")])
    return InlineKeyboardMarkup(rows)

def format_reminder_label(reminder: dict) -> str:
    """
    Возвращает красивую строку даты и времени для напоминания.
    Использует fire_at (UTC) для точного отображения.
    """
    try:
        fire_dt = datetime.fromisoformat(reminder["fire_at"]).astimezone(
            timezone(timedelta(hours=3))
        )
        today = now_msk().date()
        fire_date = fire_dt.date()
        time_s = fire_dt.strftime("%H:%M")

        if fire_date == today:
            return f"сегодня в {time_s}"
        elif fire_date == today + timedelta(days=1):
            return f"завтра в {time_s}"
        else:
            return f"{fire_date.day:02d}.{fire_date.month:02d} в {time_s}"
    except Exception:
        return reminder.get("time_str", "?")

def build_fire_dt(time_str: str, fire_date: str | None = None, day: str = "today") -> datetime:
    """
    Строит datetime срабатывания в UTC.
    fire_date — YYYY-MM-DD, приоритет над day.
    """
    msk_tz = timezone(timedelta(hours=3))
    h, m = map(int, time_str.split(":"))

    if fire_date:
        d = dt_date.fromisoformat(fire_date)
        fire_msk = datetime(d.year, d.month, d.day, h, m, 0, tzinfo=msk_tz)
    else:
        fire_msk = now_msk().replace(hour=h, minute=m, second=0, microsecond=0)
        if day == "tomorrow":
            fire_msk += timedelta(days=1)
        elif fire_msk <= now_msk():
            fire_msk += timedelta(days=1)

    return fire_msk.astimezone(timezone.utc)

async def transcribe_voice(voice, context) -> str:
    vf = await context.bot.get_file(voice.file_id)
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        await vf.download_to_drive(tmp.name)
        path = tmp.name
    with open(path, "rb") as f:
        result = groq_client.audio.transcriptions.create(
            file=("voice.ogg", f),
            model="whisper-large-v3-turbo",
            language="ru",
            response_format="text"
        )
    os.unlink(path)
    return result.strip()

async def call_llm(system: str, user: str, max_tokens=900) -> str:
    r = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role":"system","content":system},{"role":"user","content":user}],
        temperature=0.3, max_tokens=max_tokens
    )
    return r.choices[0].message.content.strip()

# ═══════════════════════════════════════════════════════
#  АВТООЧИСТКА ПЛАНА В 3:00 НОЧИ (МСК = UTC+3)
# ═══════════════════════════════════════════════════════
async def nightly_plan_cleanup(context: ContextTypes.DEFAULT_TYPE):
    logger.info("🌙 Ночная очистка плана на сегодня...")
    data = load_data()
    changed = False
    for uid in data:
        if data[uid].get("today_steps"):
            data[uid]["today_steps"] = []
            data[uid]["today_priority"] = ""
            data[uid]["today_date"] = ""
            changed = True
    if changed:
        save_data(data)
    logger.info("☑️ Планы очищены.")

def schedule_nightly_cleanup(app):
    now_utc = datetime.now(timezone.utc)
    target = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    if target <= now_utc:
        target += timedelta(days=1)
    delay = (target - now_utc).total_seconds()
    app.job_queue.run_repeating(
        nightly_plan_cleanup,
        interval=86400,
        first=delay,
        name="nightly_cleanup"
    )
    logger.info(f"🕐 Ночная очистка запланирована через {int(delay//3600)}ч {int((delay%3600)//60)}мин")

# ═══════════════════════════════════════════════════════
#  НАПОМИНАНИЯ — CORE
# ═══════════════════════════════════════════════════════
async def fire_reminder(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    rid = job.data["rid"]
    name = job.data["name"]
    chat_id = job.chat_id

    desc = job.data.get("description")
    desc_line = ("\n" + desc) if desc else ""
    await context.bot.send_message(
        chat_id=chat_id,
        text="🔔 <b>Напоминание!</b>\n\n" + name + desc_line,
        parse_mode="HTML"
    )
    data = load_data()
    for uid in data:
        rems = data[uid].get("reminders", [])
        data[uid]["reminders"] = [r for r in rems if r.get("id") != rid]
    save_data(data)

def schedule_reminder_job(chat_id: int, reminder: dict, job_queue=None):
    """
    Планирует job для напоминания.
    job_queue — явно переданная очередь (из context.job_queue или app.job_queue).
    """
    if job_queue is None:
        global _app_ref
        if _app_ref and _app_ref.job_queue:
            job_queue = _app_ref.job_queue
        else:
            logger.error("❌ job_queue недоступен")
            return
    fire_at_str = reminder.get("fire_at")
    if not fire_at_str:
        logger.error(f"❌ Нет fire_at у напоминания {reminder.get('id')}")
        return
    try:
        fire_at = datetime.fromisoformat(fire_at_str)
        now = datetime.now(timezone.utc)
        if fire_at <= now:
            logger.warning(f"⚠️ Напоминание {reminder['id']} просрочено: {fire_at} <= {now}")
            return
        job = job_queue.run_once(
            fire_reminder,
            when=fire_at,
            chat_id=chat_id,
            data={"rid": reminder["id"], "name": reminder["name"], "description": reminder.get("description")},
            name=f"rem_{reminder['id']}"
        )
        logger.info(f"✅ Напоминание '{reminder['name']}' -> {fire_at} (chat={chat_id}, job={job})")
    except Exception as e:
        logger.error(f"❌ Ошибка планирования: {e}", exc_info=True)

async def finalize_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохраняет черновик напоминания, ставит job, отвечает пользователю."""
    pending = context.user_data.get("pending_reminder", {})
    name = pending.get("name", "Напоминание")
    time_str = pending.get("time_str", "09:00")
    day = pending.get("day", "today")
    fire_date = pending.get("fire_date")  # YYYY-MM-DD или None

    fire_utc = build_fire_dt(time_str, fire_date, day)
    rid = str(uuid.uuid4())[:8]

    reminder = {
        "id": rid,
        "name": name,
        "description": pending.get("description"),
        "time_str": time_str,
        "day": day,
        "fire_date": fire_date,
        "fire_at": fire_utc.isoformat(),
    }

    reminders = get_reminders(context)
    reminders.append(reminder)

    user_id = update.effective_user.id
    sync_to_json(user_id, context)
    schedule_reminder_job(update.effective_chat.id, reminder, job_queue=context.job_queue)

    label = format_reminder_label(reminder)
    desc = reminder.get("description")
    desc_line = ("\n" + desc) if desc else ""
    context.user_data["mode"] = "idle"
    context.user_data.pop("pending_reminder", None)

    msg = update.message or update.callback_query.message
    await msg.reply_text(
        "☑️ Напоминание добавлено!\n\n🔔 <b>" + name + "</b>" + desc_line + "\n⏰ " + label,
        parse_mode="HTML",
        reply_markup=get_bottom_kb(context)
    )

# ═══════════════════════════════════════════════════════
#  НАПОМИНАНИЯ — UI
# ═══════════════════════════════════════════════════════
async def enter_reminder_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reminders = get_reminders(context)
    now_utc = datetime.now(timezone.utc)
    context.user_data["reminders"] = [
        r for r in reminders
        if datetime.fromisoformat(r["fire_at"]) > now_utc
    ]
    if len(context.user_data["reminders"]) >= MAX_REMINDERS:
        await update.message.reply_text(
            f"⚠️ У тебя уже {MAX_REMINDERS} активных напоминаний.\n"
            f"Удали одно чтобы добавить новое.\n\nНажми <b>🔔 Напоминания</b> для просмотра.",
            parse_mode="HTML",
            reply_markup=get_bottom_kb(context)
        )
        await show_reminders(update, context)
        return

    context.user_data["mode"] = "awaiting_reminder_input"
    await update.message.reply_text(
        "✉️ <b>Новое напоминание</b>\n\n"
        "<blockquote>Отправь голосовое или текст — опиши что и когда напомнить.\n\n"
        "Примеры:\n"
        "• <i>«Напомни позвонить маме в 18:00»</i>\n"
        "• <i>«В пятницу в 9 утра встреча с командой»</i>\n"
        "• <i>«15 марта в 12:00 день рождения»</i>\n"
        "• <i>«Через 3 дня сдать отчёт»</i></blockquote>",
        parse_mode="HTML",
        reply_markup=get_bottom_kb(context)
    )

async def process_reminder_input(
    text: str, transcript: str, message,
    context: ContextTypes.DEFAULT_TYPE, processing_msg
):
    raw = await call_llm(REMINDER_PROMPT, f"Текст:\n\n{text}")
    parsed = parse_reminder_llm(raw)

    name = parsed.get("name") or text[:50]
    description = parsed.get("description")           # str или None
    time_str = parsed.get("time")                     # HH:MM или None
    fire_date = parsed.get("fire_date")               # YYYY-MM-DD или None
    day = parsed.get("day", "today")                  # today/tomorrow/other
    relative_minutes = parsed.get("relative_minutes") # int или None

    await processing_msg.delete()

    if transcript:
        await message.reply_text(
            f"<blockquote expandable>🗣 {transcript}</blockquote>",
            parse_mode="HTML"
        )

    # ── Относительное время ("через 10 минут") ───────────
    if relative_minutes:
        fire_utc = datetime.now(timezone.utc) + timedelta(minutes=relative_minutes)
        rid = str(uuid.uuid4())[:8]
        reminder = {
            "id": rid,
            "name": name,
            "description": description,
            "time_str": fire_utc.astimezone(timezone(timedelta(hours=3))).strftime("%H:%M"),
            "day": "today",
            "fire_date": None,
            "fire_at": fire_utc.isoformat(),
        }
        reminders = get_reminders(context)
        reminders.append(reminder)
        user_id = message.chat_id
        sync_to_json(user_id, context)
        schedule_reminder_job(message.chat_id, reminder, job_queue=context.job_queue)

        if relative_minutes < 60:
            time_label = f"через {relative_minutes} мин"
        else:
            h = relative_minutes // 60
            m = relative_minutes % 60
            time_label = f"через {h} ч" + (f" {m} мин" if m else "")

        context.user_data["mode"] = "idle"
        desc_line = ("\n" + description) if description else ""
        text_out = "☑️ Напомню " + time_label + "!\n\n🔔 <b>" + name + "</b>" + desc_line
        await message.reply_text(
            text_out,
            parse_mode="HTML",
            reply_markup=get_bottom_kb(context)
        )
        return

    context.user_data["pending_reminder"] = {
        "name": name,
        "description": description,
        "time_str": time_str,
        "fire_date": fire_date,
        "day": day,
    }
    context.user_data["mode"] = "idle"

    has_time = bool(time_str)
    has_date = bool(fire_date)

    desc_line = ("\n" + description) if description else ""

    if has_time and has_date:
        label = format_reminder_label({
            "fire_at": build_fire_dt(time_str, fire_date, day).isoformat(),
            "time_str": time_str,
        })
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("☑️ Подтвердить", callback_data="rem_confirm"),
            InlineKeyboardButton("✏️ Изменить", callback_data="rem_change_time"),
        ]])
        await message.reply_text(
            "🔔 <b>" + name + "</b>" + desc_line + "\n⏰ " + label + "\n\nВсё верно?",
            parse_mode="HTML",
            reply_markup=kb
        )

    elif has_time and not has_date:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("☀️ Сегодня", callback_data="rem_day:today"),
            InlineKeyboardButton("🌙 Завтра", callback_data="rem_day:tomorrow"),
            InlineKeyboardButton("📅 Другая дата", callback_data="rem_day:other"),
        ]])
        await message.reply_text(
            "🔔 <b>" + name + "</b>" + desc_line + " в " + (time_str or "") + "\n\nНа какой день?",
            parse_mode="HTML",
            reply_markup=kb
        )

    elif has_date and not has_time:
        context.user_data["mode"] = "awaiting_reminder_time_input"
        d = dt_date.fromisoformat(fire_date)
        await message.reply_text(
            "🔔 <b>" + name + "</b>" + desc_line + " — " + f"{d.day:02d}.{d.month:02d}" +
            "\n\n⏰ Во сколько напомнить? Напиши время: <b>18:00</b>",
            parse_mode="HTML",
            reply_markup=get_bottom_kb(context)
        )

    else:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("☀️ Сегодня", callback_data="rem_day:today"),
            InlineKeyboardButton("🌙 Завтра", callback_data="rem_day:tomorrow"),
            InlineKeyboardButton("📅 Другая дата", callback_data="rem_day:other"),
        ]])
        await message.reply_text(
            "🔔 <b>" + name + "</b>" + desc_line + "\n\nНа какой день?",
            parse_mode="HTML",
            reply_markup=kb
        )

async def show_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE, from_callback=False):
    reminders = get_reminders(context)
    now_utc = datetime.now(timezone.utc)
    reminders = [r for r in reminders if datetime.fromisoformat(r["fire_at"]) > now_utc]
    context.user_data["reminders"] = reminders

    reply = update.callback_query.message if from_callback else update.message

    if not reminders:
        await reply.reply_text(
            "📭 Активных напоминаний нет.\nНажми <b>✉️ Напоминание</b> чтобы добавить.",
            parse_mode="HTML",
            reply_markup=get_bottom_kb(context)
        )
        return

    lines = "🔔 <b>Твои напоминания:</b>\n\n"
    for i, r in enumerate(reminders):
        em = task_emoji(i)
        label = format_reminder_label(r)
        desc = r.get("description")
        desc_part = ("\n    " + desc) if desc else ""
        lines += em + " <b>" + r['name'] + "</b>" + desc_part + "\n    ⏰ " + label + "\n\n"

    lines += "<blockquote>Нажми 🗑 рядом с напоминанием чтобы удалить его.</blockquote>"

    # Кнопки удаления — по одной на строку для читаемости
    del_rows = []
    for i, r in enumerate(reminders):
        em = task_emoji(i)
        label_short = format_reminder_label(r)
        del_rows.append([InlineKeyboardButton(
            f"🗑 {em} {label_short}",
            callback_data=f"rem_delete:{r['id']}"
        )])

    kb = InlineKeyboardMarkup(del_rows)
    await reply.reply_text(lines, parse_mode="HTML", reply_markup=kb)

# ═══════════════════════════════════════════════════════
#  /start
# ═══════════════════════════════════════════════════════
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    load_from_json(user_id, context)
    context.user_data["mode"] = "idle"
    await update.message.reply_text(
        "👋 Привет! Я твой личный планировщик.\n\n"
        "Отправляй <b>голосовые</b> или <b>текстовые</b> сообщения — я:\n"
        "📎 Выделю задачи и занесу их в список\n"
        "📁 Составлю оптимальный план дня или на завтра\n"
        "✉️ Установлю напоминания в нужное время\n\n"
        "⏱ <b>Макс. длина голосового: 2:30 мин</b>\n\n"
        "Используй кнопки меню снизу 👇",
        parse_mode="HTML",
        reply_markup=get_bottom_kb(context)
    )

# ═══════════════════════════════════════════════════════
#  ЗАДАЧИ
# ═══════════════════════════════════════════════════════
async def enter_task_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mode"] = "awaiting_task"
    await update.message.reply_text(
        "📎 <b>Режим: Записать задачу</b>\n\n"
        "<blockquote>Отправь голосовое или текст — я выделю задачи и добавлю их в твой список.</blockquote>",
        parse_mode="HTML",
        reply_markup=get_bottom_kb(context)
    )

async def process_task_input(text: str, transcript: str, message, context: ContextTypes.DEFAULT_TYPE, processing_msg):
    raw = await call_llm(TASKS_PROMPT, f"Текст:\n\n{text}")
    new_tasks = parse_tasks(raw)
    tasks = get_tasks(context)
    start = len(tasks)
    tasks.extend(new_tasks)

    body = ""
    for i, task in enumerate(new_tasks):
        gn = start + i + 1
        em = task_emoji(start + i)
        body += f"{em} <b>Задача {gn}</b>\n{task}\n\n"

    buttons = []
    if start > 0:
        buttons.append([InlineKeyboardButton("📋 Открыть все задачи", callback_data="show_all_tasks")])
    kb = InlineKeyboardMarkup(buttons) if buttons else None

    await processing_msg.delete()
    if transcript:
        await message.reply_text(
            f"<blockquote expandable>🗣 {transcript}</blockquote>",
            parse_mode="HTML"
        )
    await message.reply_text(body.strip(), parse_mode="HTML", reply_markup=kb)
    context.user_data["mode"] = "idle"
    sync_to_json(message.chat_id, context)

async def show_all_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE, from_callback=False):
    tasks = get_tasks(context)
    if not tasks:
        text = "📭 У тебя пока нет задач.\nОтправь голосовое или нажми <b>📎 Задача</b>."
        if from_callback:
            await update.callback_query.message.reply_text(text, parse_mode="HTML")
        else:
            await update.message.reply_text(text, parse_mode="HTML", reply_markup=get_bottom_kb(context))
        return

    lines = "📋 <b>Все твои задачи:</b>\n\n"
    for i, task in enumerate(tasks):
        em = task_emoji(i)
        lines += f"{em} <b>Задача {i+1}</b>\n{task}\n\n"

    del_buttons = [
        InlineKeyboardButton(f"{task_emoji(i)} Del", callback_data=f"deltask:{i}")
        for i in range(len(tasks))
    ]
    rows = [del_buttons[i:i+3] for i in range(0, len(del_buttons), 3)]
    rows.append([InlineKeyboardButton("🗑 Очистить все", callback_data="clear_tasks")])
    lines += "<blockquote>Нажми на кнопку с номером — удалишь конкретную задачу.\nИли очисти все сразу.</blockquote>"

    kb = InlineKeyboardMarkup(rows)
    if from_callback:
        await update.callback_query.message.reply_text(lines, parse_mode="HTML", reply_markup=kb)
    else:
        await update.message.reply_text(lines, parse_mode="HTML", reply_markup=kb)

# ═══════════════════════════════════════════════════════
#  ПЛАН
# ═══════════════════════════════════════════════════════
async def enter_plan_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mode"] = "idle"
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("☀️ На сегодня", callback_data="plan_type:today"),
        InlineKeyboardButton("🌙 На завтра", callback_data="plan_type:tomorrow"),
    ]])
    await update.message.reply_text(
        "📁 <b>Составить план</b>\n\nВыбери для какого дня:",
        parse_mode="HTML",
        reply_markup=kb
    )

async def process_plan_input(text: str, transcript: str, message, context: ContextTypes.DEFAULT_TYPE, processing_msg, plan_type: str):
    raw = await call_llm(PLAN_PROMPT, f"Текст:\n\n{text}", max_tokens=1200)
    steps, priority = parse_plan(raw)

    title = "ПЛАН НА ДЕНЬ" if plan_type == "today" else "ПЛАН НА ЗАВТРА"
    plan_text = render_plan(steps, priority, title)

    if plan_type == "today":
        context.user_data["today_steps"] = steps
        context.user_data["today_priority"] = priority
        context.user_data["today_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    else:
        context.user_data["tomorrow_steps"] = steps
        context.user_data["tomorrow_priority"] = priority

    kb = plan_step_keyboard(steps, plan_type)
    hint = "<blockquote>Нажми на кнопку с временем шага — сможешь изменить или удалить его.</blockquote>"

    await processing_msg.delete()
    if transcript:
        await message.reply_text(
            f"<blockquote expandable>🗣 {transcript}</blockquote>",
            parse_mode="HTML"
        )
    await message.reply_text(plan_text + "\n\n" + hint, parse_mode="HTML", reply_markup=kb)
    sync_to_json(message.chat_id, context)

    if plan_type == "tomorrow":
        context.user_data["mode"] = "awaiting_plan_reminder_time"
        context.user_data["reminder_plan"] = plan_text
        await message.reply_text(
            "⏰ <b>Напоминание</b>\n\n"
            "Во сколько прислать этот план завтра утром?\n"
            "Напиши время в формате <b>8:00</b> или <b>09:30</b>\n"
            "Или напиши <b>пропустить</b>.",
            parse_mode="HTML",
            reply_markup=get_bottom_kb(context)
        )
    else:
        context.user_data["mode"] = "idle"
        await message.reply_text("☑️ План готов!", reply_markup=get_bottom_kb(context))

async def show_plan(update: Update, context: ContextTypes.DEFAULT_TYPE, plan_type: str, from_callback=False):
    steps = context.user_data.get(f"{plan_type}_steps")
    priority = context.user_data.get(f"{plan_type}_priority", "")
    title = "ПЛАН НА ДЕНЬ" if plan_type == "today" else "ПЛАН НА ЗАВТРА"
    reply = update.callback_query.message if from_callback else update.message

    if not steps:
        txt = f"📭 Плана {'на сегодня' if plan_type == 'today' else 'на завтра'} ещё нет.\nНажми <b>📁 План</b> чтобы создать."
        await reply.reply_text(txt, parse_mode="HTML", reply_markup=get_bottom_kb(context))
        return

    plan_text = render_plan(steps, priority, title)
    kb = plan_step_keyboard(steps, plan_type)
    hint = "<blockquote>Нажми на кнопку с временем шага — сможешь изменить или удалить его.</blockquote>"
    await reply.reply_text(plan_text + "\n\n" + hint, parse_mode="HTML", reply_markup=kb)

# ═══════════════════════════════════════════════════════
#  НАПОМИНАНИЕ ДЛЯ ПЛАНА НА ЗАВТРА
# ═══════════════════════════════════════════════════════
async def send_plan_reminder(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    await context.bot.send_message(
        chat_id=job.chat_id,
        text=f"🌅 Доброе утро! Вот твой план на сегодня:\n\n{job.data['plan']}"
    )

# ═══════════════════════════════════════════════════════
#  ГОЛОСОВОЙ ХЭНДЛЕР
# ═══════════════════════════════════════════════════════
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    load_from_json(user_id, context)

    message = update.message
    mode = context.user_data.get("mode", "idle")
    duration = message.voice.duration

    valid_modes = ("awaiting_task", "awaiting_plan_today", "awaiting_plan_tomorrow", "awaiting_reminder_input")
    if mode not in valid_modes:
        await message.reply_text("Выбери что хочешь сделать 👇", reply_markup=get_bottom_kb(context))
        return

    if duration > MAX_VOICE_DURATION:
        m, s = MAX_VOICE_DURATION // 60, MAX_VOICE_DURATION % 60
        await message.reply_text(
            f"⚠️ Голосовое слишком длинное ({duration} сек).\nМаксимум — <b>{m}:{s:02d} мин</b>.",
            parse_mode="HTML"
        )
        return

    pm = await message.reply_text("🎙 Расшифровываю речь...")
    try:
        transcript = await transcribe_voice(message.voice, context)
        if not transcript:
            await pm.edit_text("❌ Не удалось распознать речь.")
            return
        await pm.edit_text("🧠 Обрабатываю...")

        if mode == "awaiting_task":
            await process_task_input(transcript, transcript, message, context, pm)
        elif mode == "awaiting_plan_today":
            await process_plan_input(transcript, transcript, message, context, pm, "today")
        elif mode == "awaiting_plan_tomorrow":
            await process_plan_input(transcript, transcript, message, context, pm, "tomorrow")
        elif mode == "awaiting_reminder_input":
            await process_reminder_input(transcript, transcript, message, context, pm)
    except Exception as e:
        logger.error(e)
        await pm.edit_text("❌ Ошибка. Попробуй ещё раз.")

# ═══════════════════════════════════════════════════════
#  ТЕКСТОВЫЙ ХЭНДЛЕР
# ═══════════════════════════════════════════════════════
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    load_from_json(user_id, context)

    text = update.message.text
    mode = context.user_data.get("mode", "idle")

    # ── Нижнее меню ───────────────────────────────────
    if text == "📎 Задача":
        await enter_task_mode(update, context); return
    if text == "📁 План":
        await enter_plan_mode(update, context); return
    if text == "📋 Мои задачи":
        await show_all_tasks(update, context); return
    if text == "🗓 Сегодня":
        await show_plan(update, context, "today"); return
    if text == "🌙 Завтра":
        await show_plan(update, context, "tomorrow"); return
    if text == "✉️ Напоминание":
        await enter_reminder_mode(update, context); return
    if text == "🔔 Напоминания":
        await show_reminders(update, context); return

    # ── Ожидание времени напоминания для плана ────────
    if mode == "awaiting_plan_reminder_time":
        if text.lower() in ("пропустить", "пропуск", "нет", "skip"):
            context.user_data["mode"] = "idle"
            await update.message.reply_text("☑️ Хорошо, без напоминания.", reply_markup=get_bottom_kb(context))
            return
        parsed = parse_time(text)
        if not parsed:
            await update.message.reply_text(
                "Не понял формат. Напиши как <b>8:00</b> или <b>09:30</b>, или <b>пропустить</b>.",
                parse_mode="HTML"
            )
            return
        hour, minute = parsed
        now = datetime.now(timezone.utc)
        remind_dt = (now + timedelta(days=1)).replace(hour=max(0,hour-3)%24, minute=minute, second=0, microsecond=0)
        if remind_dt < now:
            remind_dt += timedelta(days=1)
        plan_text = context.user_data.get("reminder_plan", "")
        context.job_queue.run_once(
            send_plan_reminder,
            when=remind_dt,
            chat_id=update.effective_chat.id,
            data={"plan": plan_text},
            name=f"plan_remind_{update.effective_user.id}"
        )
        context.user_data["mode"] = "idle"
        await update.message.reply_text(
            f"☑️ Напомню завтра в <b>{hour}:{minute:02d}</b> (МСК).",
            parse_mode="HTML",
            reply_markup=get_bottom_kb(context)
        )
        return

    # ── Ввод времени напоминания ──────────────────────
    if mode == "awaiting_reminder_time_input":
        parsed = parse_time(text)
        if not parsed:
            await update.message.reply_text(
                "Не понял формат. Напиши как <b>18:00</b> или <b>9:30</b>",
                parse_mode="HTML"
            )
            return
        hour, minute = parsed
        pending = context.user_data.get("pending_reminder", {})
        pending["time_str"] = f"{hour:02d}:{minute:02d}"
        context.user_data["pending_reminder"] = pending
        await finalize_reminder(update, context)
        return

    # ── Ввод конкретной даты (DD.MM или DD.MM.YYYY) ───
    if mode == "awaiting_reminder_date_input":
        fire_date = parse_date_input(text)
        if not fire_date:
            await update.message.reply_text(
                "Не понял формат. Введи дату как <b>15.03</b> или <b>25.12.2025</b>",
                parse_mode="HTML"
            )
            return
        pending = context.user_data.get("pending_reminder", {})
        pending["fire_date"] = fire_date
        pending["day"] = "other"
        context.user_data["pending_reminder"] = pending
        if pending.get("time_str"):
            await finalize_reminder(update, context)
        else:
            context.user_data["mode"] = "awaiting_reminder_time_input"
            d = dt_date.fromisoformat(fire_date)
            await update.message.reply_text(
                f"📅 {d.day:02d}.{d.month:02d} — отлично!\n\n"
                f"⏰ Во сколько напомнить? Напиши время: <b>18:00</b>",
                parse_mode="HTML",
                reply_markup=get_bottom_kb(context)
            )
        return

    # ── Редактирование времени существующего напоминания ──
    if mode == "awaiting_reminder_edit_time":
        parsed = parse_time(text)
        if not parsed:
            await update.message.reply_text(
                "Не понял формат. Напиши как <b>18:00</b> или <b>9:30</b>",
                parse_mode="HTML"
            )
            return
        hour, minute = parsed
        new_time_str = f"{hour:02d}:{minute:02d}"
        rid = context.user_data.get("editing_reminder_id")
        reminders = get_reminders(context)
        rem = next((r for r in reminders if r["id"] == rid), None)
        if not rem:
            await update.message.reply_text("Напоминание не найдено.")
            context.user_data["mode"] = "idle"
            return
        jq = context.job_queue
        if jq:
            for j in jq.get_jobs_by_name(f"rem_{rid}"):
                j.schedule_removal()
        rem["time_str"] = new_time_str
        fire_utc = build_fire_dt(new_time_str, rem.get("fire_date"), rem.get("day", "today"))
        rem["fire_at"] = fire_utc.isoformat()
        sync_to_json(user_id, context)
        schedule_reminder_job(update.effective_chat.id, rem, job_queue=context.job_queue)
        label = format_reminder_label(rem)
        context.user_data["mode"] = "idle"
        context.user_data.pop("editing_reminder_id", None)
        await update.message.reply_text(
            f"☑️ Время изменено!\n\n🔔 <b>{rem['name']}</b>\n⏰ {label}",
            parse_mode="HTML",
            reply_markup=get_bottom_kb(context)
        )
        return

    # ── Ожидание нового текста шага ──────────────────
    if mode.startswith("awaiting_step_edit:"):
        _, plan_type, idx_str = mode.split(":")
        idx = int(idx_str)
        steps = context.user_data.get(f"{plan_type}_steps", [])
        if idx < len(steps):
            steps[idx]["action"] = text
            priority = context.user_data.get(f"{plan_type}_priority", "")
            title = "ПЛАН НА ДЕНЬ" if plan_type == "today" else "ПЛАН НА ЗАВТРА"
            new_plan = render_plan(steps, priority, title)
            kb = plan_step_keyboard(steps, plan_type)
            hint = "<blockquote>Нажми на кнопку с временем шага — сможешь изменить или удалить его.</blockquote>"
            await update.message.reply_text(new_plan + "\n\n" + hint, parse_mode="HTML", reply_markup=kb)
            sync_to_json(user_id, context)
        context.user_data["mode"] = "idle"
        return

    # ── Ввод для задачи или плана ────────────────────
    if mode == "awaiting_task":
        pm = await update.message.reply_text("🧠 Обрабатываю...")
        try:
            await process_task_input(text, "", update.message, context, pm)
        except Exception as e:
            logger.error(e)
            await pm.edit_text("❌ Ошибка. Попробуй снова.")
        return

    if mode == "awaiting_plan_today":
        pm = await update.message.reply_text("🧠 Составляю план...")
        try:
            await process_plan_input(text, "", update.message, context, pm, "today")
        except Exception as e:
            logger.error(e)
            await pm.edit_text("❌ Ошибка. Попробуй снова.")
        return

    if mode == "awaiting_plan_tomorrow":
        pm = await update.message.reply_text("🧠 Составляю план...")
        try:
            await process_plan_input(text, "", update.message, context, pm, "tomorrow")
        except Exception as e:
            logger.error(e)
            await pm.edit_text("❌ Ошибка. Попробуй снова.")
        return

    if mode == "awaiting_reminder_input":
        pm = await update.message.reply_text("🧠 Обрабатываю...")
        try:
            await process_reminder_input(text, "", update.message, context, pm)
        except Exception as e:
            logger.error(e)
            await pm.edit_text("❌ Ошибка. Попробуй снова.")
        return

    # ── По умолчанию ─────────────────────────────────
    await update.message.reply_text("Выбери что хочешь сделать 👇", reply_markup=get_bottom_kb(context))

# ═══════════════════════════════════════════════════════
#  CALLBACK КНОПКИ
# ═══════════════════════════════════════════════════════
async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id
    load_from_json(user_id, context)

    # Выбор типа плана
    if data.startswith("plan_type:"):
        plan_type = data.split(":")[1]
        context.user_data["mode"] = "awaiting_plan_today" if plan_type == "today" else "awaiting_plan_tomorrow"
        label = "сегодня" if plan_type == "today" else "завтра"
        await query.message.reply_text(
            f"📁 <b>Режим: план на {label}</b>\n\n"
            f"<blockquote>Отправь голосовое или текст — опиши что планируешь.</blockquote>",
            parse_mode="HTML",
            reply_markup=get_bottom_kb(context)
        )
        return

    if data == "show_all_tasks":
        await show_all_tasks(update, context, from_callback=True); return

    if data.startswith("deltask:"):
        idx = int(data.split(":")[1])
        tasks = get_tasks(context)
        if idx < len(tasks):
            tasks.pop(idx)
        sync_to_json(user_id, context)
        await query.message.reply_text(f"☑️ Задача удалена. Осталось: {len(tasks)}", reply_markup=get_bottom_kb(context))
        return

    if data == "clear_tasks":
        context.user_data["tasks"] = []
        sync_to_json(user_id, context)
        await query.message.edit_text("☑️ Все задачи очищены!")
        return

    if data.startswith("delete_plan:"):
        plan_type = data.split(":")[1]
        context.user_data[f"{plan_type}_steps"] = []
        context.user_data[f"{plan_type}_priority"] = ""
        sync_to_json(user_id, context)
        label = "на сегодня" if plan_type == "today" else "на завтра"
        await query.message.reply_text(f"☑️ План {label} удалён.", reply_markup=get_bottom_kb(context))
        return

    if data.startswith("step:"):
        _, plan_type, idx_str = data.split(":")
        idx = int(idx_str)
        steps = context.user_data.get(f"{plan_type}_steps", [])
        if idx < len(steps):
            step = steps[idx]
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✏️ Изменить", callback_data=f"step_edit:{plan_type}:{idx}"),
                InlineKeyboardButton("🗑 Удалить", callback_data=f"step_del:{plan_type}:{idx}"),
            ]])
            await query.message.reply_text(
                f"<b>Шаг {idx+1} — {step['time']}</b>\n{step['action']}",
                parse_mode="HTML", reply_markup=kb
            )
        return

    if data.startswith("step_edit:"):
        _, plan_type, idx_str = data.split(":")
        context.user_data["mode"] = f"awaiting_step_edit:{plan_type}:{idx_str}"
        await query.message.reply_text("✏️ Напиши новый текст для этого шага:", reply_markup=get_bottom_kb(context))
        return

    if data.startswith("step_del:"):
        _, plan_type, idx_str = data.split(":")
        idx = int(idx_str)
        steps = context.user_data.get(f"{plan_type}_steps", [])
        if idx < len(steps):
            steps.pop(idx)
        priority = context.user_data.get(f"{plan_type}_priority", "")
        title = "ПЛАН НА ДЕНЬ" if plan_type == "today" else "ПЛАН НА ЗАВТРА"
        sync_to_json(user_id, context)
        if steps:
            kb = plan_step_keyboard(steps, plan_type)
            hint = "<blockquote>Нажми на кнопку с временем шага — сможешь изменить или удалить его.</blockquote>"
            await query.message.reply_text(render_plan(steps, priority, title) + "\n\n" + hint, parse_mode="HTML", reply_markup=kb)
        else:
            await query.message.reply_text("☑️ Шаг удалён. В плане больше нет шагов.")
        return

    # ── НАПОМИНАНИЯ ──────────────────────────────────

    if data == "rem_confirm":
        await finalize_reminder(update, context); return

    if data == "rem_change_time":
        context.user_data["mode"] = "awaiting_reminder_time_input"
        await query.message.reply_text(
            "⏰ Введи время в формате <b>18:00</b>",
            parse_mode="HTML",
            reply_markup=get_bottom_kb(context)
        )
        return

    if data.startswith("rem_day:"):
        day = data.split(":")[1]
        pending = context.user_data.get("pending_reminder", {})

        if day == "other":
            # Просим ввести конкретную дату
            context.user_data["mode"] = "awaiting_reminder_date_input"
            await query.message.reply_text(
                "📅 Введи дату в формате <b>ДД.ММ</b> или <b>ДД.ММ.ГГГГ</b>\n"
                "Например: <b>15.03</b> или <b>25.12.2025</b>",
                parse_mode="HTML",
                reply_markup=get_bottom_kb(context)
            )
            return

        pending["day"] = day
        pending["fire_date"] = None  # сбрасываем конкретную дату
        context.user_data["pending_reminder"] = pending

        if pending.get("time_str"):
            await finalize_reminder(update, context)
        else:
            context.user_data["mode"] = "awaiting_reminder_time_input"
            await query.message.reply_text(
                "⏰ Во сколько напомнить? Напиши время в формате <b>18:00</b>",
                parse_mode="HTML",
                reply_markup=get_bottom_kb(context)
            )
        return

    if data.startswith("rem_view:"):
        rid = data.split(":")[1]
        reminders = get_reminders(context)
        rem = next((r for r in reminders if r["id"] == rid), None)
        if not rem:
            await query.message.reply_text("Напоминание не найдено.")
            return
        label = format_reminder_label(rem)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✏️ Изменить время", callback_data=f"rem_edit_time:{rid}"),
            InlineKeyboardButton("🗑 Удалить", callback_data=f"rem_delete:{rid}"),
        ]])
        await query.message.reply_text(
            f"🔔 <b>{rem['name']}</b>\n⏰ {label}",
            parse_mode="HTML", reply_markup=kb
        )
        return

    if data.startswith("rem_edit_time:"):
        rid = data.split(":")[1]
        context.user_data["editing_reminder_id"] = rid
        context.user_data["mode"] = "awaiting_reminder_edit_time"
        await query.message.reply_text(
            "⏰ Введи новое время в формате <b>18:00</b>",
            parse_mode="HTML",
            reply_markup=get_bottom_kb(context)
        )
        return

    if data.startswith("rem_delete:"):
        rid = data.split(":")[1]
        reminders = get_reminders(context)
        context.user_data["reminders"] = [r for r in reminders if r["id"] != rid]
        jq = context.job_queue
        if jq:
            for j in jq.get_jobs_by_name(f"rem_{rid}"):
                j.schedule_removal()
        sync_to_json(user_id, context)
        await query.message.reply_text("☑️ Напоминание удалено.", reply_markup=get_bottom_kb(context))
        return

# ═══════════════════════════════════════════════════════
#  ЗАПУСК
# ═══════════════════════════════════════════════════════
async def cmd_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    load_from_json(user_id, context)
    await show_reminders(update, context)

async def reschedule_all_reminders(app):
    """При старте бота восстанавливает все jobs для напоминаний из JSON."""
    data = load_data()
    now_utc = datetime.now(timezone.utc)
    total = 0
    jq = app.job_queue
    for uid, udata in data.items():
        reminders = udata.get("reminders", [])
        chat_id = int(uid)
        valid = []
        for r in reminders:
            try:
                fire_at = datetime.fromisoformat(r["fire_at"])
                if fire_at > now_utc:
                    schedule_reminder_job(chat_id, r, job_queue=jq)
                    valid.append(r)
                    total += 1
            except Exception as e:
                logger.error(f"Ошибка при восстановлении напоминания {r}: {e}")
        data[uid]["reminders"] = valid
    save_data(data)
    logger.info(f"✅ Восстановлено {total} напоминаний после перезапуска")

async def post_init(app):
    global _app_ref
    _app_ref = app
    from telegram import BotCommand, MenuButtonCommands
    await app.bot.set_my_commands([
        BotCommand("start", "👋 Запустить / перезапустить бота"),
        BotCommand("reminders", "🔔 Мои напоминания"),
    ])
    await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    schedule_nightly_cleanup(app)
    await reschedule_all_reminders(app)

async def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = ApplicationBuilder().token(token).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reminders", cmd_reminders))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_buttons))

    logger.info("🤖 Бот запущен!")
    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        await asyncio.sleep(float("inf"))

if __name__ == "__main__":
    asyncio.run(main())