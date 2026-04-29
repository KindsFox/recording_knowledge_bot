"""
templates_module.py
===================
Модуль шаблонов задач для Telegram-бота.

СТРУКТУРА ШАБЛОНА:
  Шаблон = набор шагов (БП → Задача → Процедура).
  Каждый шаг — одна строка в task_template_steps.
  Пользователь указывает только: объект + дату + время.
"""

import sqlite3
from datetime import datetime
from pathlib import Path

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

(
    TMPL_ADMIN_NAME,        # вводит название шаблона
    TMPL_ADMIN_DESC,        # описание (опционально)
    TMPL_ADMIN_ADD_STEP,    # добавляет шаг: выбирает БП
    TMPL_ADMIN_STEP_TASK,   # выбирает задачу
    TMPL_ADMIN_STEP_PROC,   # выбирает процедуру
    TMPL_ADMIN_CONFIRM,     # подтверждает сохранение
) = range(100, 106)

# Пользователь применяет шаблон
(
    TMPL_USER_PICK,         # выбирает шаблон из списка
    TMPL_USER_OBJECT,       # выбирает объект
    TMPL_USER_DATE,         # вводит дату
    TMPL_USER_TIME_MODE,    # единое время или по каждой задаче
    TMPL_USER_TIME_SINGLE,  # единое время начала и окончания
    TMPL_USER_TIME_EACH,    # время для каждого шага отдельно
    TMPL_USER_CONFIRM,      # подтверждение
) = range(200, 207)

DB_PATH = Path(__file__).parent / "tasks.db"


# ИНИЦИАЛИЗАЦИЯ БД
def init_templates_db(conn: sqlite3.Connection):
    """Создаёт таблицы шаблонов если их ещё нет."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS task_templates (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE,
            description TEXT DEFAULT '',
            active      INTEGER DEFAULT 1,
            created_by  TEXT DEFAULT '',
            created_at  TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS task_template_steps (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            template_id INTEGER NOT NULL REFERENCES task_templates(id) ON DELETE CASCADE,
            step_order  INTEGER NOT NULL DEFAULT 0,
            bp_id       INTEGER,
            bp_name     TEXT NOT NULL,
            task_id     INTEGER,
            task_name   TEXT NOT NULL,
            procedure_id   INTEGER,
            procedure_name TEXT DEFAULT ''
        );
    """)
    conn.commit()


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _kb(buttons: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    """Строит InlineKeyboardMarkup из списка [(text, callback_data)]."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t, callback_data=d) for t, d in row]
        for row in buttons
    ])


# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
def _get_templates(active_only=True):
    db = _db()
    q  = "SELECT * FROM task_templates"
    if active_only:
        q += " WHERE active=1"
    q += " ORDER BY name"
    rows = db.execute(q).fetchall()
    db.close()
    return rows


def _get_template_steps(template_id: int):
    db   = _db()
    rows = db.execute(
        "SELECT * FROM task_template_steps WHERE template_id=? ORDER BY step_order",
        (template_id,)
    ).fetchall()
    db.close()
    return rows


def _get_bps():
    db   = _db()
    rows = db.execute("SELECT id, name FROM bp WHERE active=1 ORDER BY name").fetchall()
    db.close()
    return rows


def _get_tasks(bp_id: int):
    db   = _db()
    rows = db.execute(
        "SELECT id, name FROM tasks_ref WHERE bp_id=? AND active=1 ORDER BY name",
        (bp_id,)
    ).fetchall()
    db.close()
    return rows


def _get_procs(task_id: int):
    db   = _db()
    rows = db.execute(
        "SELECT id, name FROM procedures WHERE task_id=? AND active=1 ORDER BY name",
        (task_id,)
    ).fetchall()
    db.close()
    return rows


def _get_objects():
    db   = _db()
    # Берём из справочника + из реальных работ
    from_ref = db.execute(
        "SELECT name FROM objects WHERE status != 'Сдан' ORDER BY name"
    ).fetchall()
    from_log = db.execute(
        "SELECT DISTINCT object_name AS name FROM work_log "
        "WHERE object_name IS NOT NULL AND object_name != '' ORDER BY name"
    ).fetchall()
    db.close()
    seen  = set()
    names = []
    for r in list(from_ref) + list(from_log):
        if r["name"] and r["name"] not in seen:
            seen.add(r["name"]); names.append(r["name"])
    return sorted(names)


def _fmt_steps(steps) -> str:
    if not steps:
        return "  (шагов нет)"
    lines = []
    for i, s in enumerate(steps, 1):
        proc = f" → {s['procedure_name']}" if s["procedure_name"] else ""
        lines.append(f"  {i}. {s['bp_name']} › {s['task_name']}{proc}")
    return "\n".join(lines)


# ЧАСТЬ 1: АДМИНИСТРАТОР — создание шаблона
async def admin_templates_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Точка входа — кнопка «Шаблоны задач» в /admin."""
    query = update.callback_query
    if query:
        await query.answer()

    templates = _get_templates(active_only=False)
    text = "📋 *Шаблоны задач*\n\n"
    if templates:
        for t in templates:
            steps = _get_template_steps(t["id"])
            status = "✅" if t["active"] else "❌"
            text += f"{status} *{t['name']}* — {len(steps)} шагов\n"
            if t["description"]:
                text += f"   _{t['description']}_\n"
    else:
        text += "_Шаблонов ещё нет_\n"

    kb = _kb([
        [("➕ Создать шаблон", "tmpl_create")],
        [("✏️ Редактировать", "tmpl_edit_list"), ("🗑 Удалить", "tmpl_del_list")],
        [("◀️ Назад", "admin_back")],
    ])

    if query:
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)


async def tmpl_create_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Начало создания шаблона — запрашиваем название."""
    await update.callback_query.answer()
    ctx.user_data["new_tmpl"] = {"name": "", "desc": "", "steps": []}
    await update.callback_query.edit_message_text(
        "📋 *Создание шаблона*\n\n"
        "Введи название шаблона:\n"
        "_Например: «Монтаж котла под ключ» или «Плановое ТО котельной»_",
        parse_mode="Markdown",
    )
    return TMPL_ADMIN_NAME


async def tmpl_admin_got_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("Название не может быть пустым. Попробуй снова:")
        return TMPL_ADMIN_NAME

    # Проверяем уникальность
    db  = _db()
    dup = db.execute("SELECT 1 FROM task_templates WHERE name=?", (name,)).fetchone()
    db.close()
    if dup:
        await update.message.reply_text(
            f"Шаблон «{name}» уже существует. Введи другое название:"
        )
        return TMPL_ADMIN_NAME

    ctx.user_data["new_tmpl"]["name"] = name
    await update.message.reply_text(
        f"✅ Название: *{name}*\n\nДобавь описание (или /skip чтобы пропустить):",
        parse_mode="Markdown",
    )
    return TMPL_ADMIN_DESC


async def tmpl_admin_got_desc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    desc = update.message.text.strip()
    if desc != "/skip":
        ctx.user_data["new_tmpl"]["desc"] = desc
    return await _tmpl_admin_show_add_step(update, ctx)


async def tmpl_admin_skip_desc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    return await _tmpl_admin_show_add_step(update, ctx)


async def _tmpl_admin_show_add_step(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Показываем текущие шаги и предлагаем добавить ещё."""
    tmpl  = ctx.user_data["new_tmpl"]
    steps = tmpl["steps"]
    n     = len(steps)

    text  = f"📋 *{tmpl['name']}*\n"
    if steps:
        text += f"\nШаги ({n}):\n" + _fmt_steps(steps) + "\n"
    else:
        text += "\n_Шагов ещё нет_\n"

    bps = _get_bps()
    if not bps:
        await (update.message or update.callback_query.message).reply_text(
            "⚠️ Нет бизнес-процессов в справочнике. Добавь их через /admin → БП."
        )
        return ConversationHandler.END

    kb_rows = [[( f"➕ Добавить шаг {n+1}", "tmpl_add_step")]]
    if steps:
        kb_rows.append([("✅ Сохранить шаблон", "tmpl_save")])
    kb_rows.append([("❌ Отмена", "tmpl_cancel")])

    msg = update.message or update.callback_query.message
    await msg.reply_text(text, parse_mode="Markdown", reply_markup=_kb(kb_rows))
    return TMPL_ADMIN_CONFIRM


async def tmpl_admin_add_step(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Выбор БП для нового шага."""
    await update.callback_query.answer()
    bps    = _get_bps()
    kb_rows = [[( b["name"], f"tbp_{b['id']}_{b['name']}")] for b in bps]
    kb_rows.append([("❌ Отмена", "tmpl_cancel")])
    await update.callback_query.edit_message_text(
        "Выбери бизнес-процесс для шага:",
        reply_markup=_kb(kb_rows),
    )
    return TMPL_ADMIN_ADD_STEP


async def tmpl_admin_step_bp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Получили БП — показываем задачи."""
    await update.callback_query.answer()
    data   = update.callback_query.data  # tbp_{id}_{name}
    parts  = data.split("_", 2)
    bp_id  = int(parts[1])
    bp_name = parts[2]
    ctx.user_data["step_bp"] = {"id": bp_id, "name": bp_name}

    tasks = _get_tasks(bp_id)
    if not tasks:
        await update.callback_query.edit_message_text(
            f"⚠️ У БП «{bp_name}» нет задач в справочнике."
        )
        return await _tmpl_admin_show_add_step(update, ctx)

    kb_rows = [[(t["name"], f"ttask_{t['id']}_{t['name']}")] for t in tasks]
    kb_rows.append([("◀️ Назад", "tmpl_add_step")])
    await update.callback_query.edit_message_text(
        f"БП: *{bp_name}*\nВыбери задачу:",
        parse_mode="Markdown",
        reply_markup=_kb(kb_rows),
    )
    return TMPL_ADMIN_STEP_TASK


async def tmpl_admin_step_task(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Получили задачу — показываем процедуры."""
    await update.callback_query.answer()
    data    = update.callback_query.data  # ttask_{id}_{name}
    parts   = data.split("_", 2)
    task_id = int(parts[1])
    task_name = parts[2]
    ctx.user_data["step_task"] = {"id": task_id, "name": task_name}

    procs = _get_procs(task_id)
    kb_rows = []
    if procs:
        kb_rows = [[(p["name"], f"tproc_{p['id']}_{p['name']}")] for p in procs]
    kb_rows.append([("— Без процедуры", "tproc_0_")])
    kb_rows.append([("◀️ Назад", f"tbp_{ctx.user_data['step_bp']['id']}_{ctx.user_data['step_bp']['name']}")])

    await update.callback_query.edit_message_text(
        f"Задача: *{task_name}*\nВыбери процедуру (опционально):",
        parse_mode="Markdown",
        reply_markup=_kb(kb_rows),
    )
    return TMPL_ADMIN_STEP_PROC


async def tmpl_admin_step_proc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Получили процедуру — добавляем шаг и возвращаемся."""
    await update.callback_query.answer()
    data  = update.callback_query.data  # tproc_{id}_{name}
    parts = data.split("_", 2)
    proc_id   = int(parts[1])
    proc_name = parts[2] if len(parts) > 2 else ""

    bp   = ctx.user_data["step_bp"]
    task = ctx.user_data["step_task"]

    ctx.user_data["new_tmpl"]["steps"].append({
        "bp_id":        bp["id"],
        "bp_name":      bp["name"],
        "task_id":      task["id"],
        "task_name":    task["name"],
        "procedure_id": proc_id,
        "procedure_name": proc_name,
    })

    return await _tmpl_admin_show_add_step(update, ctx)


async def tmpl_admin_save(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Сохраняем шаблон в БД."""
    await update.callback_query.answer()
    tmpl  = ctx.user_data.get("new_tmpl", {})
    steps = tmpl.get("steps", [])

    if not steps:
        await update.callback_query.edit_message_text("Нельзя сохранить пустой шаблон.")
        return ConversationHandler.END

    db  = _db()
    cur = db.execute(
        "INSERT INTO task_templates (name, description, created_by, created_at) VALUES (?,?,?,?)",
        (tmpl["name"], tmpl.get("desc",""),
         update.effective_user.full_name,
         datetime.now().strftime("%d.%m.%Y %H:%M"))
    )
    tmpl_id = cur.lastrowid
    for i, s in enumerate(steps):
        db.execute(
            "INSERT INTO task_template_steps "
            "(template_id, step_order, bp_id, bp_name, task_id, task_name, procedure_id, procedure_name) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (tmpl_id, i, s["bp_id"], s["bp_name"],
             s["task_id"], s["task_name"],
             s["procedure_id"], s["procedure_name"])
        )
    db.commit()
    db.close()

    text = (
        f"✅ *Шаблон «{tmpl['name']}» сохранён!*\n\n"
        f"Шагов: {len(steps)}\n"
        f"{_fmt_steps(steps)}\n\n"
        f"Сотрудники могут применять его командой /template"
    )
    await update.callback_query.edit_message_text(text, parse_mode="Markdown")
    ctx.user_data.pop("new_tmpl", None)
    return ConversationHandler.END


async def tmpl_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("new_tmpl", None)
    ctx.user_data.pop("step_bp", None)
    ctx.user_data.pop("step_task", None)
    msg = update.callback_query or update.message
    if hasattr(msg, "edit_message_text"):
        await msg.answer()
        await msg.edit_message_text("Отменено.")
    else:
        await msg.reply_text("Отменено.")
    return ConversationHandler.END


# УДАЛЕНИЕ / ДЕАКТИВАЦИЯ ШАБЛОНА
async def tmpl_del_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    templates = _get_templates(active_only=False)
    if not templates:
        await update.callback_query.edit_message_text("Шаблонов нет.")
        return

    kb_rows = [[(f"{'✅' if t['active'] else '❌'} {t['name']}",
                 f"tmpl_toggle_{t['id']}")]  for t in templates]
    kb_rows.append([("◀️ Назад", "admin_templates")])
    await update.callback_query.edit_message_text(
        "Нажми на шаблон чтобы включить/выключить:",
        reply_markup=_kb(kb_rows),
    )


async def tmpl_toggle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    tmpl_id = int(update.callback_query.data.split("_")[-1])
    db = _db()
    current = db.execute("SELECT active FROM task_templates WHERE id=?", (tmpl_id,)).fetchone()
    if current:
        new_val = 0 if current["active"] else 1
        db.execute("UPDATE task_templates SET active=? WHERE id=?", (new_val, tmpl_id))
        db.commit()
    db.close()
    await update.callback_query.edit_message_text(
        "✅ Статус шаблона обновлён.", reply_markup=_kb([[("◀️ Назад", "admin_templates")]])
    )


# ЧАСТЬ 2: ПОЛЬЗОВАТЕЛЬ — применение шаблона (/template)
async def cmd_template(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/template — показываем список шаблонов."""
    templates = _get_templates(active_only=True)
    if not templates:
        await update.message.reply_text(
            "📋 Шаблонов задач пока нет.\n"
            "Попроси администратора создать их через /admin → Шаблоны."
        )
        return ConversationHandler.END

    text = "📋 *Шаблоны задач*\nВыбери шаблон:"
    kb_rows = []
    for t in templates:
        steps = _get_template_steps(t["id"])
        label = f"{t['name']} ({len(steps)} шагов)"
        kb_rows.append([(label, f"utmpl_{t['id']}")])
    kb_rows.append([("❌ Отмена", "utmpl_cancel")])

    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=_kb(kb_rows))
    return TMPL_USER_PICK


async def tmpl_user_picked(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Пользователь выбрал шаблон — показываем детали, запрашиваем объект."""
    await update.callback_query.answer()
    tmpl_id = int(update.callback_query.data.split("_")[-1])

    db   = _db()
    tmpl = db.execute("SELECT * FROM task_templates WHERE id=?", (tmpl_id,)).fetchone()
    db.close()
    if not tmpl:
        await update.callback_query.edit_message_text("Шаблон не найден.")
        return ConversationHandler.END

    steps = _get_template_steps(tmpl_id)
    ctx.user_data["apply_tmpl"] = {
        "id":    tmpl_id,
        "name":  tmpl["name"],
        "steps": [dict(s) for s in steps],
        "times": [],   # время для каждого шага (если по шагам)
    }

    text  = f"📋 *{tmpl['name']}*\n\n"
    if tmpl["description"]:
        text += f"_{tmpl['description']}_\n\n"
    text += f"Шаги:\n{_fmt_steps(steps)}\n\nВыбери объект:"

    objects = _get_objects()
    if not objects:
        await update.callback_query.edit_message_text(
            "Нет объектов. Добавь через /admin → Объекты."
        )
        return ConversationHandler.END

    kb_rows = [[(o, f"uobj_{o}")] for o in objects[:20]]  # первые 20
    kb_rows.append([("❌ Отмена", "utmpl_cancel")])
    await update.callback_query.edit_message_text(
        text, parse_mode="Markdown", reply_markup=_kb(kb_rows)
    )
    return TMPL_USER_OBJECT


async def tmpl_user_object(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Пользователь выбрал объект — запрашиваем дату."""
    await update.callback_query.answer()
    obj = update.callback_query.data[5:]  # uobj_{name}
    ctx.user_data["apply_tmpl"]["object"] = obj
    today = datetime.now().strftime("%d.%m.%Y")
    await update.callback_query.edit_message_text(
        f"Объект: *{obj}*\n\nВведи дату выполнения:\n"
        f"_Формат: ДД.ММ.ГГГГ, например {today}_",
        parse_mode="Markdown",
    )
    return TMPL_USER_DATE


async def tmpl_user_date(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Получили дату — предлагаем выбрать режим времени."""
    raw = update.message.text.strip()
    try:
        datetime.strptime(raw, "%d.%m.%Y")
    except ValueError:
        await update.message.reply_text(
            "Неверный формат даты. Введи в формате ДД.ММ.ГГГГ:"
        )
        return TMPL_USER_DATE

    ctx.user_data["apply_tmpl"]["date"] = raw
    n = len(ctx.user_data["apply_tmpl"]["steps"])

    kb = _kb([
        [("🕐 Одно время для всего", "utime_single")],
        [("🕑 Своё время для каждого шага", "utime_each")],
        [("❌ Отмена", "utmpl_cancel")],
    ])
    await update.message.reply_text(
        f"Дата: *{raw}*\n\nКак указать время?\n"
        f"_(шагов в шаблоне: {n})_",
        parse_mode="Markdown",
        reply_markup=kb,
    )
    return TMPL_USER_TIME_MODE


async def tmpl_user_time_single_ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Единое время — запрашиваем start и end."""
    await update.callback_query.answer()
    ctx.user_data["apply_tmpl"]["time_mode"] = "single"
    await update.callback_query.edit_message_text(
        "Введи время начала и окончания через дефис:\n"
        "_Например: 09:00-17:00_",
        parse_mode="Markdown",
    )
    return TMPL_USER_TIME_SINGLE


async def tmpl_user_time_single(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Парсим единое время."""
    raw = update.message.text.strip()
    try:
        ts, te = raw.split("-")
        datetime.strptime(ts.strip(), "%H:%M")
        datetime.strptime(te.strip(), "%H:%M")
    except Exception:
        await update.message.reply_text(
            "Формат: HH:MM-HH:MM, например 09:00-17:00. Попробуй снова:"
        )
        return TMPL_USER_TIME_SINGLE

    ctx.user_data["apply_tmpl"]["time_start"] = ts.strip()
    ctx.user_data["apply_tmpl"]["time_end"]   = te.strip()
    return await _tmpl_user_show_confirm(update, ctx)


async def tmpl_user_time_each_ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Время по шагам — начинаем с первого."""
    await update.callback_query.answer()
    ctx.user_data["apply_tmpl"]["time_mode"] = "each"
    ctx.user_data["apply_tmpl"]["times"]     = []
    ctx.user_data["apply_tmpl"]["step_idx"]  = 0
    return await _ask_step_time(update.callback_query.message, ctx)


async def _ask_step_time(msg, ctx):
    """Запрашиваем время для очередного шага."""
    tmpl = ctx.user_data["apply_tmpl"]
    idx  = tmpl["step_idx"]
    steps = tmpl["steps"]
    if idx >= len(steps):
        return await _tmpl_user_show_confirm(None, ctx, msg=msg)
    s = steps[idx]
    proc = f" → {s['procedure_name']}" if s["procedure_name"] else ""
    await msg.reply_text(
        f"Шаг {idx+1}/{len(steps)}: *{s['bp_name']} › {s['task_name']}{proc}*\n\n"
        "Введи время начала и окончания:\n_Например: 09:00-10:30_",
        parse_mode="Markdown",
    )
    return TMPL_USER_TIME_EACH


async def tmpl_user_time_each(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Получаем время очередного шага."""
    raw = update.message.text.strip()
    try:
        ts, te = raw.split("-")
        datetime.strptime(ts.strip(), "%H:%M")
        datetime.strptime(te.strip(), "%H:%M")
    except Exception:
        await update.message.reply_text("Формат: HH:MM-HH:MM. Попробуй снова:")
        return TMPL_USER_TIME_EACH

    ctx.user_data["apply_tmpl"]["times"].append({
        "start": ts.strip(), "end": te.strip()
    })
    ctx.user_data["apply_tmpl"]["step_idx"] += 1
    return await _ask_step_time(update.message, ctx)


async def _tmpl_user_show_confirm(update, ctx, msg=None):
    """Показываем итоговый план для подтверждения."""
    tmpl  = ctx.user_data["apply_tmpl"]
    steps = tmpl["steps"]
    mode  = tmpl.get("time_mode", "single")
    date  = tmpl["date"]
    obj   = tmpl["object"]

    text = (
        f"📋 *{tmpl['name']}*\n"
        f"📅 {date}  📍 {obj}\n\n"
        "Задачи которые будут добавлены в расписание:\n"
    )
    for i, s in enumerate(steps):
        if mode == "each" and i < len(tmpl["times"]):
            t = tmpl["times"][i]
            tstr = f" {t['start']}–{t['end']}"
        else:
            tstr = f" {tmpl.get('time_start','')}–{tmpl.get('time_end','')}"
        proc = f" › {s['procedure_name']}" if s["procedure_name"] else ""
        text += f"  {i+1}. {s['bp_name']} › {s['task_name']}{proc}{tstr}\n"

    kb = _kb([
        [("✅ Подтвердить", "utmpl_confirm")],
        [("❌ Отмена", "utmpl_cancel")],
    ])

    target = msg or (update.message if update and update.message else None)
    if target:
        await target.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    return TMPL_USER_CONFIRM


async def tmpl_user_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Сохраняем все задачи из шаблона в planned_tasks."""
    await update.callback_query.answer()
    tmpl  = ctx.user_data.get("apply_tmpl", {})
    steps = tmpl.get("steps", [])
    if not steps:
        await update.callback_query.edit_message_text("Ошибка: шаблон пустой.")
        return ConversationHandler.END

    user      = update.effective_user
    mode      = tmpl.get("time_mode", "single")
    date      = tmpl["date"]
    obj       = tmpl["object"]
    assignee  = user.full_name
    tg_id     = str(user.id)
    now_str   = datetime.now().strftime("%d.%m.%Y %H:%M")

    db  = _db()
    ids = []
    for i, s in enumerate(steps):
        if mode == "each" and i < len(tmpl.get("times", [])):
            ts = tmpl["times"][i]["start"]
            te = tmpl["times"][i]["end"]
        else:
            ts = tmpl.get("time_start", "")
            te = tmpl.get("time_end", "")

        title = f"{s['task_name']}"
        if s["procedure_name"]:
            title += f" ({s['procedure_name']})"

        cur = db.execute(
            "INSERT INTO planned_tasks "
            "(title, object_name, assignee_name, assignee_tg_id, "
            " planned_date, planned_time, planned_time_end, "
            " bp_name, task_ref_name, status, created_by, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                title, obj, assignee, tg_id,
                date, ts, te,
                s["bp_name"], s["task_name"],
                "Запланирована",
                assignee, now_str,
            )
        )
        ids.append(cur.lastrowid)

    db.commit()
    db.close()

    text = (
        f"✅ *Добавлено {len(ids)} задач в расписание!*\n\n"
        f"📅 {date}  📍 {obj}\n"
        f"Шаблон: *{tmpl['name']}*\n\n"
        "Просмотреть через /tasks"
    )
    await update.callback_query.edit_message_text(text, parse_mode="Markdown")
    ctx.user_data.pop("apply_tmpl", None)
    return ConversationHandler.END


async def tmpl_user_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("apply_tmpl", None)
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("Отменено.")
    return ConversationHandler.END


# РЕГИСТРАЦИЯ ХЭНДЛЕРОВ
def get_admin_template_handlers():
    """
    Хэндлеры для АДМИНИСТРАТОРА — создание шаблонов.
    Добавить в application через:
        for h in get_admin_template_handlers():
            application.add_handler(h)
    """
    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(tmpl_create_start, pattern="^tmpl_create$"),
        ],
        states={
            TMPL_ADMIN_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, tmpl_admin_got_name),
            ],
            TMPL_ADMIN_DESC: [
                CommandHandler("skip", tmpl_admin_skip_desc),
                MessageHandler(filters.TEXT & ~filters.COMMAND, tmpl_admin_got_desc),
            ],
            TMPL_ADMIN_ADD_STEP: [
                CallbackQueryHandler(tmpl_admin_step_bp, pattern=r"^tbp_\d+_"),
                CallbackQueryHandler(tmpl_admin_add_step, pattern="^tmpl_add_step$"),
            ],
            TMPL_ADMIN_STEP_TASK: [
                CallbackQueryHandler(tmpl_admin_step_task, pattern=r"^ttask_\d+_"),
                CallbackQueryHandler(tmpl_admin_add_step, pattern="^tmpl_add_step$"),
            ],
            TMPL_ADMIN_STEP_PROC: [
                CallbackQueryHandler(tmpl_admin_step_proc, pattern=r"^tproc_"),
            ],
            TMPL_ADMIN_CONFIRM: [
                CallbackQueryHandler(tmpl_admin_add_step, pattern="^tmpl_add_step$"),
                CallbackQueryHandler(tmpl_admin_save,     pattern="^tmpl_save$"),
                CallbackQueryHandler(tmpl_cancel,         pattern="^tmpl_cancel$"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(tmpl_cancel, pattern="^tmpl_cancel$"),
        ],
        per_user=True,
        per_chat=True,
    )

    return [
        conv,
        CallbackQueryHandler(admin_templates_menu, pattern="^admin_templates$"),
        CallbackQueryHandler(tmpl_del_list,        pattern="^tmpl_del_list$"),
        CallbackQueryHandler(tmpl_toggle,          pattern=r"^tmpl_toggle_\d+$"),
    ]


def get_template_handlers():
    """
    Хэндлеры для ПОЛЬЗОВАТЕЛЯ — команда /template.
    """
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("template", cmd_template),
        ],
        states={
            TMPL_USER_PICK: [
                CallbackQueryHandler(tmpl_user_picked, pattern=r"^utmpl_\d+$"),
            ],
            TMPL_USER_OBJECT: [
                CallbackQueryHandler(tmpl_user_object, pattern=r"^uobj_"),
            ],
            TMPL_USER_DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, tmpl_user_date),
            ],
            TMPL_USER_TIME_MODE: [
                CallbackQueryHandler(tmpl_user_time_single_ask, pattern="^utime_single$"),
                CallbackQueryHandler(tmpl_user_time_each_ask,   pattern="^utime_each$"),
            ],
            TMPL_USER_TIME_SINGLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, tmpl_user_time_single),
            ],
            TMPL_USER_TIME_EACH: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, tmpl_user_time_each),
            ],
            TMPL_USER_CONFIRM: [
                CallbackQueryHandler(tmpl_user_confirm, pattern="^utmpl_confirm$"),
                CallbackQueryHandler(tmpl_user_cancel,  pattern="^utmpl_cancel$"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(tmpl_user_cancel, pattern="^utmpl_cancel$"),
        ],
        per_user=True,
        per_chat=True,
    )
    return [conv]
