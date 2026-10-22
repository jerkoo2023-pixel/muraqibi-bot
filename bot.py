import nest_asyncio
nest_asyncio.apply()

import os
import sqlite3
import calendar
import requests
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from flask import Flask
import threading
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes,
)

web_app = Flask(__name__)

@web_app.route('/')
def health():
    return "Bot is alive!", 200

def run_web():
    port = int(os.environ.get("PORT", 8080))
    web_app.run(host='0.0.0.0', port=port)

TOKEN = os.environ.get("BOT_TOKEN")
DB = "monitor.db"
TZ = ZoneInfo("Asia/Riyadh")

# Conversation States
(EXP_AMOUNT, EXP_CAT, EXP_PTYPE, EXP_CREDITOR, EXP_DUE, 
 INC_AMOUNT, INC_SOURCE, 
 RATE_INPUT, LIMIT_INPUT,
 SD_ADD_NAME, SD_ADD_AMT, SD_EDIT_AMT, SD_PAY_AMT,
 LD_ADD_NAME, LD_ADD_AMT, LD_EDIT_AMT, LD_PAY_AMT,
 PAY_CREDITOR_AMT, PAY_CREDITOR_TYPE,
 ROLLOVER_CASH, ROLLOVER_DEBTS) = range(21)

SCHEDULE_NORMAL = ["09:00", "10:10", "11:20", "12:30", "13:30", "17:00", "18:15", "19:30", "20:45", "22:00", "23:15", "00:30"]
SCHEDULE_FRIDAY = ["11:00", "12:00", "13:15", "14:30", "15:45", "17:00", "18:15", "19:30", "20:45", "22:00", "23:15", "00:30"]

def now():
    return datetime.now(TZ)

def get_ts():
    return now().strftime("%Y-%m-%d %H:%M:%S")

def get_end_of_month():
    today = now().date()
    last_day = calendar.monthrange(today.year, today.month)[1]
    month_names = ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو", "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"]
    return f"{last_day} {month_names[today.month-1]}"

def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c

def init():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS cigarettes(id INTEGER PRIMARY KEY, ts TEXT);
        CREATE TABLE IF NOT EXISTS creditors(id INTEGER PRIMARY KEY, name TEXT UNIQUE);
        CREATE TABLE IF NOT EXISTS expenses(
            id INTEGER PRIMARY KEY, ts TEXT, amount REAL, category TEXT,
            ptype TEXT, creditor_id INTEGER, due_date TEXT, paid INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS prayers(date TEXT, name TEXT, status INTEGER, PRIMARY KEY(date, name));
        CREATE TABLE IF NOT EXISTS incomes(id INTEGER PRIMARY KEY, ts TEXT, amount REAL, source TEXT);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS sudan_reqs(name TEXT PRIMARY KEY, amount_sdg REAL);
        CREATE TABLE IF NOT EXISTS sudan_pays(id INTEGER PRIMARY KEY, ts TEXT, name TEXT, paid_sdg REAL, paid_sar REAL);
        CREATE TABLE IF NOT EXISTS long_debts(id INTEGER PRIMARY KEY, name TEXT, total_sar REAL, paid_sar REAL DEFAULT 0);
        """)
        c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('exchange_rate', '140')")
        c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('daily_target', '15')")

def get_setting(key, default=None):
    with db() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return float(row["value"]) if row else default

def get_rate(): return get_setting('exchange_rate', 140.0)
def get_daily_target(): return int(get_setting('daily_target', 15.0))

def get_next_cig_time():
    sched = SCHEDULE_FRIDAY if now().weekday() == 4 else SCHEDULE_NORMAL
    current_time = now().strftime("%H:%M")
    for t in sched:
        if t > current_time: return t
    return "غداً صباحاً"

def cigs_today():
    today_str = now().strftime("%Y-%m-%d")
    with db() as c:
        rows = c.execute("SELECT ts FROM cigarettes WHERE ts LIKE ?", (f"{today_str}%",)).fetchall()
    return [datetime.strptime(r["ts"], "%Y-%m-%d %H:%M:%S") for r in rows]

def get_wallet_balance():
    with db() as c:
        inc = c.execute("SELECT COALESCE(SUM(amount),0) s FROM incomes").fetchone()["s"]
        exp = c.execute("SELECT COALESCE(SUM(amount),0) s FROM expenses WHERE ptype='cash'").fetchone()["s"]
        sudan = c.execute("SELECT COALESCE(SUM(paid_sar),0) s FROM sudan_pays").fetchone()["s"]
        long_paid = c.execute("SELECT COALESCE(SUM(paid_sar),0) s FROM long_debts").fetchone()["s"]
    return inc - exp - sudan - long_paid

def get_prayers():
    today = now().date().isoformat()
    names = ["الفجر", "الظهر", "العصر", "المغرب", "العشاء"]
    with db() as c:
        rows = c.execute("SELECT name, status FROM prayers WHERE date=?", (today,)).fetchall()
    status = {r["name"]: r["status"] for r in rows}
    return {n: status.get(n, 0) for n in names}

def get_next_prayer_al_qurayyat():
    try:
        today_str = now().strftime("%d-%m-%Y")
        url = f"http://api.aladhan.com/v1/timings/{today_str}?latitude=31.333&longitude=37.333&method=4"
        res = requests.get(url, timeout=5).json()
        timings = res['data']['timings']
        prayer_times = {"الفجر": timings["Fajr"].split(" ")[0], "الظهر": timings["Dhuhr"].split(" ")[0], "العصر": timings["Asr"].split(" ")[0], "المغرب": timings["Maghrib"].split(" ")[0], "العشاء": timings["Isha"].split(" ")[0]}
        
        current_time = now().strftime("%H:%M")
        for p_name, p_time in prayer_times.items():
            if p_time > current_time:
                fmt = "%H:%M"
                diff = datetime.strptime(p_time, fmt) - datetime.strptime(current_time, fmt)
                hours, remainder = divmod(diff.seconds, 3600)
                minutes, _ = divmod(remainder, 60)
                rem_str = f"{hours} س و " if hours > 0 else ""
                rem_str += f"{minutes} د"
                p_12 = datetime.strptime(p_time, fmt).strftime("%I:%M %p")
                return f"⏳ القادمة: {p_name} - {p_12} (بعد {rem_str})"
        return "⏳ القادمة: الفجر (غداً)"
    except: return "⏳ تعذر جلب المواقيت"

def get_expense_breakdown():
    today_str = now().strftime("%Y-%m-%d")
    with db() as c:
        rows = c.execute("SELECT category, SUM(amount) as total FROM expenses WHERE ts LIKE ? GROUP BY category", (f"{today_str}%",)).fetchall()
    if not rows: return "لا توجد مصاريف اليوم.", 0
    
    rate = get_rate()
    total_sar = sum(r["total"] for r in rows)
    lines = ["```text", "الفئة      | بالريال | بالجنيه", "-----------|---------|---------"]
    for r in sorted(rows, key=lambda x: x["total"], reverse=True):
        lines.append(f"{r['category']:<10} | {r['total']:>7.0f} | {r['total']*rate:>9.0f}")
    lines.append("-----------|---------|---------")
    lines.append(f"{'الإجمالي':<10} | {total_sar:>7.0f} | {total_sar*rate:>9.0f}")
    lines.append("```")
    return "\n".join(lines), total_sar

def get_cumulative_stats():
    rate = get_rate()
    with db() as c:
        cigs_count = c.execute("SELECT COUNT(*) c FROM cigarettes").fetchone()["c"]
        cig_manual = c.execute("SELECT SUM(amount) s FROM expenses WHERE category='دخان'").fetchone()["s"] or 0
        smoke_tot = (cigs_count * 1.0) + cig_manual
        grocery_tot = c.execute("SELECT SUM(amount) s FROM expenses WHERE category='بقالة'").fetchone()["s"] or 0
        deferred_tot = c.execute("SELECT SUM(amount) s FROM expenses WHERE ptype='credit'").fetchone()["s"] or 0
        sudan_tot = c.execute("SELECT SUM(paid_sar) s FROM sudan_pays").fetchone()["s"] or 0
        other_tot = c.execute("SELECT SUM(amount) s FROM expenses WHERE ptype='cash' AND category NOT IN ('بقالة', 'تحويل السودان', 'سداد دين طويل')").fetchone()["s"] or 0

    grand_total = smoke_tot + grocery_tot + sudan_tot + deferred_tot + other_tot
    if grand_total == 0: return "لا توجد مصاريف تراكمية.", 0

    lines = ["```text", "الفئة      | بالريال | بالجنيه  | النسبة", "-----------|---------|----------|-------"]
    def add_row(name, amt):
        lines.append(f"{name:<10} | {amt:>7.0f} | {amt*rate:>8.0f} | {int((amt/grand_total)*100):>3}%")
    add_row("دخان", smoke_tot); add_row("بقالة", grocery_tot); add_row("أخرى", other_tot); add_row("ديون آجلة", deferred_tot); add_row("السودان", sudan_tot)
    lines.append("```")
    return "\n".join(lines), grand_total

def dashboard_text():
    ts = cigs_today(); target = get_daily_target()
    smoked = len(ts); remaining = max(0, target - smoked)
    compliance_pct = (remaining / target) * 100 if target > 0 else 0
    next_cig = get_next_cig_time()
    wallet = get_wallet_balance()
    rate = get_rate()
    breakdown_text, today_total = get_expense_breakdown()
    cum_text, cum_total = get_cumulative_stats()
    
    with db() as c:
        sudan_req = c.execute("SELECT SUM(amount_sdg) s FROM sudan_reqs").fetchone()["s"] or 0
        sudan_paid = c.execute("SELECT SUM(paid_sdg) s FROM sudan_pays").fetchone()["s"] or 0
        sudan_rem_sar = (sudan_req - sudan_paid) / rate
        long_total = c.execute("SELECT SUM(total_sar) s FROM long_debts").fetchone()["s"] or 0
        long_paid = c.execute("SELECT SUM(paid_sar) s FROM long_debts").fetchone()["s"] or 0
        long_rem_sar = long_total - long_paid
        credit_tot = c.execute("SELECT SUM(amount) s FROM expenses WHERE ptype='credit' AND paid=0").fetchone()["s"] or 0

    prayers = get_prayers()
    p_icons = " ".join([f"{'✅' if v else '⬜'} {k}" for k, v in prayers.items()])
    next_prayer_text = get_next_prayer_al_qurayyat()
    due_date = get_end_of_month()

    return (
        f"📅 *{now().strftime('%A %d %B %Y | %I:%M %p')}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🚬 *رحلة الإقلاع عن التدخين:*\n"
        f"```text\n"
        f"السقف المستهدف | {target} سيجارة\n"
        f"المدخن اليوم    | {smoked}\n"
        f"المتبقي         | {remaining}\n"
        f"القادمة         | {next_cig}\n"
        f"نسبة الالتزام   | {compliance_pct:.0f}%\n"
        f"```\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 *المحفظة المالية:*\n"
        f"```text\n"
        f"الرصيد المتاح  | {wallet:,.2f} ر.س\n"
        f"سعر الصرف      | 1 ر.س = {rate} ج.س\n"
        f"```\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 *مصاريف اليوم:*\n"
        f"💵 *الإجمالي: {today_total:.0f} ر.س ({today_total*rate:,.0f} ج.س)*\n"
        f"{breakdown_text}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕌 *الصلوات:*\n{p_icons}\n"
        f"{next_prayer_text}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💼 *المصاريف المستحقة (استحقاق: {due_date}):*\n"
        f"```text\n"
        f"السودان        | {sudan_rem_sar:,.0f} ر.س ({(sudan_req-sudan_paid):,.0f} ج.س)\n"
        f"ديون طويلة     | {long_rem_sar:,.0f} ر.س ({long_rem_sar*rate:,.0f} ج.س)\n"
        f"ديون آجلة      | {credit_tot:,.0f} ر.س ({credit_tot*rate:,.0f} ج.س)\n"
        f"```\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 *التراكمي الكلي للمصاريف:*\n"
        f"💵 *الإجمالي: {cum_total:,.0f} ر.س*\n"
        f"{cum_text}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🚀 *نظامي الخاص — رحلة النجاح مستمرة | صالح محمد علي*"
    )

def main_kb():
    prayers = get_prayers()
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚬 سيجارة +1", callback_data="cig"), InlineKeyboardButton("⚙️ تعديل السقف", callback_data="edit_limit_btn")],
        [InlineKeyboardButton("💰 إضافة مصروف", callback_data="add_exp_btn"), InlineKeyboardButton("➕ إضافة إيراد", callback_data="add_inc_btn")],
        [InlineKeyboardButton("💼 إدارة الديون والمصاريف", callback_data="accrued_menu"), InlineKeyboardButton("📊 التقرير الشهري والترحيل", callback_data="monthly_report")],
        [InlineKeyboardButton("💱 تعديل سعر الصرف", callback_data="edit_rate_btn"), InlineKeyboardButton("✏️ إدارة وتصحيح", callback_data="edit_menu")],
        [InlineKeyboardButton(f"{'✅' if prayers['الفجر'] else '⬜'} الفجر", callback_data="pr|الفجر"), InlineKeyboardButton(f"{'✅' if prayers['الظهر'] else '⬜'} الظهر", callback_data="pr|الظهر"), InlineKeyboardButton(f"{'✅' if prayers['العصر'] else '⬜'} العصر", callback_data="pr|العصر")],
        [InlineKeyboardButton(f"{'✅' if prayers['المغرب'] else '⬜'} المغرب", callback_data="pr|المغرب"), InlineKeyboardButton(f"{'✅' if prayers['العشاء'] else '⬜'} العشاء", callback_data="pr|العشاء")],
        [InlineKeyboardButton("🔄 تحديث اللوحة", callback_data="refresh")],
    ])

def accrued_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📉 ديون الآجل (بقالة وأشخاص)", callback_data="menu_credit")],
        [InlineKeyboardButton("⏳ ديون طويلة الأجل (أقساط)", callback_data="menu_long")],
        [InlineKeyboardButton("🇸🇩 مستحقات السودان", callback_data="menu_sudan")],
        [InlineKeyboardButton("🔙 رجوع للوحة", callback_data="refresh")]
    ])

def monthly_report_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 ترحيل للشهر القادم", callback_data="do_rollover")],
        [InlineKeyboardButton("🔙 رجوع للوحة", callback_data="refresh")]
    ])

def credit_menu_kb():
    with db() as c:
        rows = c.execute("SELECT c.id, c.name, SUM(e.amount) as total FROM expenses e JOIN creditors c ON c.id=e.creditor_id WHERE e.ptype='credit' AND e.paid=0 GROUP BY c.id").fetchall()
    kb = []
    if not rows: kb.append([InlineKeyboardButton("لا توجد ديون آجلة", callback_data="noop")])
    for r in rows:
        kb.append([InlineKeyboardButton(f"{r['name']} - متبقي {r['total']:.0f} ر.س", callback_data=f"pay_cred|{r['id']}")])
    kb.append([InlineKeyboardButton("🔙 رجوع", callback_data="accrued_menu")])
    return InlineKeyboardMarkup(kb)

def sudan_menu_kb():
    rate = get_rate()
    with db() as c:
        sudan = c.execute("SELECT r.name, r.amount_sdg, COALESCE(p.paid_sdg,0) paid_sdg FROM sudan_reqs r LEFT JOIN (SELECT name, SUM(paid_sdg) paid_sdg FROM sudan_pays GROUP BY name) p ON r.name = p.name").fetchall()
    kb = []
    for r in sudan:
        rem_sdg = r["amount_sdg"] - r["paid_sdg"]
        kb.append([InlineKeyboardButton(f"👤 {r['name']} - {rem_sdg:,.0f} ج.س", callback_data="noop")])
        kb.append([InlineKeyboardButton("💸 تحويل دفعة", callback_data=f"sd_pay|{r['name']}"), InlineKeyboardButton("✏️ تعديل", callback_data=f"sd_edit|{r['name']}"), InlineKeyboardButton("🗑️ حذف", callback_data=f"sd_del|{r['name']}")])
    kb.append([InlineKeyboardButton("➕ إضافة مستفيد", callback_data="sd_add")])
    kb.append([InlineKeyboardButton("🔙 رجوع", callback_data="accrued_menu")])
    return InlineKeyboardMarkup(kb)

def long_menu_kb():
    rate = get_rate()
    with db() as c:
        longs = c.execute("SELECT id, name, total_sar, paid_sar FROM long_debts").fetchall()
    kb = []
    for r in longs:
        rem_sar = r["total_sar"] - r["paid_sar"]
        kb.append([InlineKeyboardButton(f"🏦 {r['name']} - {rem_sar:,.0f} ر.س ({rem_sar*rate:,.0f} ج.س)", callback_data="noop")])
        kb.append([InlineKeyboardButton("💸 سداد دفعة", callback_data=f"ld_pay|{r['id']}"), InlineKeyboardButton("✏️ تعديل", callback_data=f"ld_edit|{r['id']}"), InlineKeyboardButton("🗑️ حذف", callback_data=f"ld_del|{r['id']}")])
    kb.append([InlineKeyboardButton("➕ إضافة دين جديد", callback_data="ld_add")])
    kb.append([InlineKeyboardButton("🔙 رجوع", callback_data="accrued_menu")])
    return InlineKeyboardMarkup(kb)

def edit_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑️ حذف آخر سيجارة", callback_data="del_cig"), InlineKeyboardButton("🗑️ حذف آخر مصروف", callback_data="del_exp")],
        [InlineKeyboardButton("🧹 تصفير كافة السجلات", callback_data="reset_all_menu")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="refresh")]
    ])

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(dashboard_text(), parse_mode="Markdown", reply_markup=main_kb())

# ================== CONVERSATIONS ==================
async def add_exp_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q: await q.answer()
    msg = "💰 *إضافة مصروف*\nأرسل المبلغ بالريال:"
    if q: await q.edit_message_text(msg, parse_mode="Markdown")
    else: await update.message.reply_text(msg, parse_mode="Markdown")
    return EXP_AMOUNT

async def exp_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: ctx.user_data["amount"] = float(update.message.text.strip())
    except ValueError: return await update.message.reply_text("⚠️ أرسل رقماً فقط.")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("أكل", callback_data="cat|أكل"), InlineKeyboardButton("دخان", callback_data="cat|دخان"), InlineKeyboardButton("سناكات", callback_data="cat|سناكات")],[InlineKeyboardButton("بقالة", callback_data="cat|بقالة"), InlineKeyboardButton("مواصلات", callback_data="cat|مواصلات"), InlineKeyboardButton("أخرى", callback_data="cat|أخرى")]])
    await update.message.reply_text("📂 اختر نوع المصروف:", reply_markup=kb)
    return EXP_CAT

async def exp_category(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    ctx.user_data["category"] = q.data.split("|",1)[1]
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ نقداً", callback_data="pt|cash"), InlineKeyboardButton("📝 آجل", callback_data="pt|credit")]])
    await q.edit_message_text(f"النوع: *{ctx.user_data['category']}*\nطريقة الدفع؟", parse_mode="Markdown", reply_markup=kb)
    return EXP_PTYPE

async def exp_ptype(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    ctx.user_data["ptype"] = q.data.split("|",1)[1]
    if ctx.user_data["ptype"] == "cash":
        await q.edit_message_text("💵 نقداً. لحظة الحفظ...")
        return await save_expense(update, ctx)
    await q.edit_message_text("📝 آجل. اكتب *اسم الجهة الدائنة*:", parse_mode="Markdown")
    return EXP_CREDITOR

async def exp_creditor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["creditor"] = update.message.text.strip()
    await update.message.reply_text("📅 تاريخ السداد (YYYY-MM-DD) أو `-` لتخطيه:")
    return EXP_DUE

async def exp_due(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    ctx.user_data["due"] = None if t == "-" else t
    return await save_expense(update, ctx)

async def save_expense(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ud = ctx.user_data
    cid = None
    if ud["ptype"] == "credit":
        with db() as c:
            c.execute("INSERT OR IGNORE INTO creditors(name) VALUES(?)", (ud["creditor"],))
            cid = c.execute("SELECT id FROM creditors WHERE name=?", (ud["creditor"],)).fetchone()["id"]
    with db() as c:
        c.execute("INSERT INTO expenses(ts,amount,category,ptype,creditor_id,due_date) VALUES(?,?,?,?,?,?)", (get_ts(), ud["amount"], ud["category"], ud["ptype"], cid, ud.get("due")))
    await ctx.bot.send_message(update.effective_chat.id, dashboard_text(), parse_mode="Markdown", reply_markup=main_kb())
    return ConversationHandler.END

async def add_inc_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q: await q.answer()
    msg = "📈 *إضافة إيراد*\nأرسل المبلغ بالريال:"
    if q: await q.edit_message_text(msg, parse_mode="Markdown")
    else: await update.message.reply_text(msg, parse_mode="Markdown")
    return INC_AMOUNT

async def inc_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: ctx.user_data["inc_amount"] = float(update.message.text.strip())
    except ValueError: return await update.message.reply_text("⚠️ أرسل رقماً فقط.")
    await update.message.reply_text("📝 اكتب مصدر الإيراد:")
    return INC_SOURCE

async def inc_source(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with db() as c: c.execute("INSERT INTO incomes(ts, amount, source) VALUES(?,?,?)", (get_ts(), ctx.user_data["inc_amount"], update.message.text.strip()))
    await update.message.reply_text("✅ تم الحفظ.\n\n" + dashboard_text(), parse_mode="Markdown", reply_markup=main_kb())
    return ConversationHandler.END

async def edit_rate_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q: await q.answer()
    msg = "💱 أرسل سعر الصرف الجديد (كم جنيه يساوي 1 ريال؟):"
    if q: await q.edit_message_text(msg)
    else: await update.message.reply_text(msg)
    return RATE_INPUT

async def save_rate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: rate = float(update.message.text.strip())
    except ValueError: return await update.message.reply_text("⚠️ أرسل رقماً فقط.")
    with db() as c: c.execute("UPDATE settings SET value=? WHERE key='exchange_rate'", (str(rate),))
    await update.message.reply_text(f"✅ تم تحديث السعر.\n\n" + dashboard_text(), parse_mode="Markdown", reply_markup=main_kb())
    return ConversationHandler.END

async def edit_limit_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await q.edit_message_text("🎯 أرسل السقف اليومي الجديد للتدخين (مثال: 10):")
    return LIMIT_INPUT

async def save_limit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: limit = int(update.message.text.strip())
    except ValueError: return await update.message.reply_text("⚠️ أرسل رقماً صحيحاً فقط.")
    with db() as c: c.execute("UPDATE settings SET value=? WHERE key='daily_target'", (str(limit),))
    await update.message.reply_text(f"✅ تم تحديث السقف.\n\n" + dashboard_text(), parse_mode="Markdown", reply_markup=main_kb())
    return ConversationHandler.END

# (Sudan, Long Debt, Pay Creditor)
async def sd_add_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await q.edit_message_text("🇸🇩 *إضافة مستفيد للسودان*\nأرسل اسم الشخص:")
    return SD_ADD_NAME
async def sd_add_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    with db() as c:
        if c.execute("SELECT 1 FROM sudan_reqs WHERE name=?", (name,)).fetchone():
            return await update.message.reply_text("⚠️ الاسم موجود. أرسل اسماً آخر:"); 
    ctx.user_data["sd_name"] = name
    await update.message.reply_text("أرسل المبلغ بالجنيه السوداني (ج.س):")
    return SD_ADD_AMT
async def sd_add_amt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: amt = float(update.message.text.strip())
    except ValueError: return await update.message.reply_text("⚠️ أرسل رقماً فقط.")
    with db() as c: c.execute("INSERT INTO sudan_reqs(name, amount_sdg) VALUES(?,?)", (ctx.user_data["sd_name"], amt))
    await update.message.reply_text("✅ تم الإضافة.", reply_markup=main_kb())
    return ConversationHandler.END
async def sd_edit_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer(); ctx.user_data["sd_name"] = q.data.split("|")[1]
    await q.edit_message_text(f"✏️ تعديل مبلغ: *{ctx.user_data['sd_name']}*\nأرسل المبلغ بالجنيه (ج.س):")
    return SD_EDIT_AMT
async def sd_edit_amt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: amt = float(update.message.text.strip())
    except ValueError: return await update.message.reply_text("⚠️ أرسل رقماً فقط.")
    with db() as c: c.execute("UPDATE sudan_reqs SET amount_sdg=? WHERE name=?", (amt, ctx.user_data["sd_name"]))
    await update.message.reply_text("✅ تم التعديل.", reply_markup=main_kb())
    return ConversationHandler.END
async def sd_pay_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer(); ctx.user_data["sd_name"] = q.data.split("|")[1]
    await q.edit_message_text(f"💸 تحويل إلى: *{ctx.user_data['sd_name']}*\nأرسل المبلغ (بالجنيه):")
    return SD_PAY_AMT
async def sd_pay_amt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: amt = float(update.message.text.strip())
    except ValueError: return await update.message.reply_text("⚠️ أرسل رقماً فقط.")
    rate = get_rate(); paid_sdg = amt; paid_sar = amt / rate
    with db() as c:
        c.execute("INSERT INTO sudan_pays(ts, name, paid_sdg, paid_sar) VALUES(?,?,?,?)", (get_ts(), ctx.user_data["sd_name"], paid_sdg, paid_sar))
        c.execute("INSERT INTO expenses(ts,amount,category,ptype,creditor_id,due_date) VALUES(?,?,?,?,?,?)", (get_ts(), paid_sar, "تحويل السودان", "cash", None, None))
    await update.message.reply_text(f"✅ تم تحويل {paid_sdg:,.0f} ج.س ({paid_sar:,.2f} ر.س).", reply_markup=main_kb())
    return ConversationHandler.END
async def sd_del_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer(); name = q.data.split("|")[1]
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⚠️ نعم، احذف", callback_data=f"sd_del_confirm|{name}")], [InlineKeyboardButton("🔙 إلغاء", callback_data="menu_sudan")]])
    await q.edit_message_text(f"هل أنت متأكد من حذف *{name}*؟", reply_markup=kb)
async def sd_del_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer(); name = q.data.split("|")[1]
    with db() as c: c.execute("DELETE FROM sudan_reqs WHERE name=?", (name,)); c.execute("DELETE FROM sudan_pays WHERE name=?", (name,))
    await q.edit_message_text(f"✅ تم حذف {name}.", reply_markup=sudan_menu_kb())

async def ld_add_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await q.edit_message_text("⏳ *إضافة دين طويل الأجل*\nأرسل اسم الدين:")
    return LD_ADD_NAME
async def ld_add_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["ld_name"] = update.message.text.strip()
    await update.message.reply_text("أرسل المبلغ بالريال (ر.س):")
    return LD_ADD_AMT
async def ld_add_amt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: amt = float(update.message.text.strip())
    except ValueError: return await update.message.reply_text("⚠️ أرسل رقماً فقط.")
    with db() as c: c.execute("INSERT INTO long_debts(name, total_sar) VALUES(?,?)", (ctx.user_data["ld_name"], amt))
    await update.message.reply_text("✅ تم الإضافة.", reply_markup=main_kb())
    return ConversationHandler.END
async def ld_edit_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer(); ctx.user_data["ld_id"] = q.data.split("|")[1]
    await q.edit_message_text("✏️ أرسل المبلغ الكلي الجديد بالريال (ر.س):")
    return LD_EDIT_AMT
async def ld_edit_amt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: amt = float(update.message.text.strip())
    except ValueError: return await update.message.reply_text("⚠️ أرسل رقماً فقط.")
    with db() as c: c.execute("UPDATE long_debts SET total_sar=? WHERE id=?", (amt, ctx.user_data["ld_id"]))
    await update.message.reply_text("✅ تم التعديل.", reply_markup=main_kb())
    return ConversationHandler.END
async def ld_pay_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer(); ctx.user_data["ld_id"] = q.data.split("|")[1]
    await q.edit_message_text("💸 أرسل مبلغ السداد بالريال (ر.س):")
    return LD_PAY_AMT
async def ld_pay_amt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: amt = float(update.message.text.strip())
    except ValueError: return await update.message.reply_text("⚠️ أرسل رقماً فقط.")
    with db() as c:
        c.execute("UPDATE long_debts SET paid_sar = paid_sar + ? WHERE id=?", (amt, ctx.user_data["ld_id"]))
        c.execute("INSERT INTO expenses(ts,amount,category,ptype,creditor_id,due_date) VALUES(?,?,?,?,?,?)", (get_ts(), amt, "سداد دين طويل", "cash", None, None))
    await update.message.reply_text("✅ تم السداد.", reply_markup=main_kb())
    return ConversationHandler.END
async def ld_del_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer(); id_ = q.data.split("|")[1]
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⚠️ نعم، احذف", callback_data=f"ld_del_confirm|{id_}")], [InlineKeyboardButton("🔙 إلغاء", callback_data="menu_long")]])
    await q.edit_message_text("تأكيد حذف الدين؟", reply_markup=kb)
async def ld_del_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer(); id_ = q.data.split("|")[1]
    with db() as c: c.execute("DELETE FROM long_debts WHERE id=?", (id_,))
    await q.edit_message_text("✅ تم الحذف.", reply_markup=long_menu_kb())

# Double Entry: Pay Creditor
async def pay_cred_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    cid = q.data.split("|")[1]; ctx.user_data["cred_id"] = cid
    await q.edit_message_text("💸 كم تريد أن تسدد من هذا الدين؟ (أرسل المبلغ):")
    return PAY_CREDITOR_AMT

async def pay_cred_amt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: amt = float(update.message.text.strip())
    except ValueError: return await update.message.reply_text("⚠️ أرسل رقماً فقط.")
    ctx.user_data["pay_amt"] = amt
    await update.message.reply_text(f"هل تريد تسجيل هذا السداد كـ *نقدي* (سيخصم من الرصيد) أم *آجل* (تحويل الدين)؟", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ نقدي", callback_data="pay_cred_type|cash"),
        InlineKeyboardButton("📝 آجل", callback_data="pay_cred_type|credit")
    ]]))
    return PAY_CREDITOR_TYPE

async def pay_cred_type(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    ptype = q.data.split("|")[1]
    amt = ctx.user_data["pay_amt"]; cid = ctx.user_data["cred_id"]
    
    with db() as c:
        if ptype == "cash":
            # Deduct from specific expenses (simplified: mark as paid and record cash expense)
            remaining = amt
            rows = c.execute("SELECT id, amount FROM expenses WHERE creditor_id=? AND ptype='credit' AND paid=0 ORDER BY id ASC", (cid,)).fetchall()
            for r in rows:
                if remaining <= 0: break
                deduct = min(r["amount"], remaining)
                if deduct == r["amount"]:
                    c.execute("UPDATE expenses SET paid=1 WHERE id=?", (r["id"],))
                else:
                    c.execute("UPDATE expenses SET amount = amount - ? WHERE id=?", (deduct, r["id"]))
                remaining -= deduct
                
            c.execute("INSERT INTO expenses(ts,amount,category,ptype,creditor_id,due_date) VALUES(?,?,?,?,?,?)", (get_ts(), amt, "سداد دين آجل", "cash", None, None))
            await q.edit_message_text(f"✅ تم تسجيل سداد نقدي بقيمة {amt:.0f} ر.س وخصمه من المحفظة.", reply_markup=main_kb())
        else:
            await q.edit_message_text("⚠️ التحويل الآجل قيد التطوير (يُفضل السداد النقدي حالياً).", reply_markup=main_kb())
    return ConversationHandler.END

# Monthly Rollover
async def rollover_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    wallet = get_wallet_balance()
    await q.edit_message_text(f"🔄 *الترحيل للشهر القادم*\n\nرصيدك الحالي هو {wallet:,.2f} ر.س.\nكم تريد أن ترحل كرصيد افتتاحي للشهر القادم؟ (أرسل المبلغ):", parse_mode="Markdown")
    return ROLLOVER_CASH

async def rollover_cash(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: amt = float(update.message.text.strip())
    except ValueError: return await update.message.reply_text("⚠️ أرسل رقماً فقط.")
    
    with db() as c:
        c.execute("INSERT INTO incomes(ts, amount, source) VALUES(?,?,?)", (get_ts(), amt, "رصيد افتتاحي مرحل"))
    
    # Simplistic rollover: we just add the carried amount as a new income.
    # In a real ERP, we would close the period. For this bot, adding it as income creates the opening balance.
    await update.message.reply_text(f"✅ تم ترحيل {amt:,.2f} ر.س كرصيد افتتاحي. ستبدأ الشهر الجديد بهذا الرصيد (يمكنك تصفير المصاريف القديمة من قائمة الإدارة).", reply_markup=main_kb())
    return ConversationHandler.END

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ تم الإلغاء.", reply_markup=main_kb())
    return ConversationHandler.END

# ================== BUTTON HANDLER ==================
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q: await q.answer()
    data = q.data
    
    if data == "noop": return

    if data == "cig":
        target = get_daily_target()
        if len(cigs_today()) >= target:
            await q.answer("⚠️ وصلت السقف اليومي!", show_alert=True); return
        with db() as c: c.execute("INSERT INTO cigarettes(ts) VALUES(?)", (get_ts(),))
        await q.edit_message_text(dashboard_text(), parse_mode="Markdown", reply_markup=main_kb())
        
    elif data.startswith("pr|"):
        p_name = data.split("|")[1]
        today = now().date().isoformat()
        with db() as c:
            row = c.execute("SELECT status FROM prayers WHERE date=? AND name=?", (today, p_name)).fetchone()
            new_status = 1 if not row or row["status"] == 0 else 0
            c.execute("INSERT OR REPLACE INTO prayers(date, name, status) VALUES(?,?,?)", (today, p_name, new_status))
        await q.edit_message_text(dashboard_text(), parse_mode="Markdown", reply_markup=main_kb())
        
    elif data == "refresh":
        await q.edit_message_text(dashboard_text(), parse_mode="Markdown", reply_markup=main_kb())
        
    elif data == "accrued_menu":
        await q.edit_message_text("💼 *إدارة الديون والمصاريف*", parse_mode="Markdown", reply_markup=accrued_menu_kb())
    elif data == "monthly_report":
        await q.edit_message_text("📊 *التقارير والترحيل*\nهنا يمكنك ترحيل رصيدك للشهر القادم:", parse_mode="Markdown", reply_markup=monthly_report_kb())
    elif data == "do_rollover": await rollover_start(update, ctx)
    elif data == "menu_credit":
        await q.edit_message_text("📉 *الديون الآجلة (بقالة وأشخاص):*\nاختر جهة للسداد:", parse_mode="Markdown", reply_markup=credit_menu_kb())
    elif data == "menu_sudan":
        await q.edit_message_text("🇸🇩 *مستحقات السودان:*", parse_mode="Markdown", reply_markup=sudan_menu_kb())
    elif data == "menu_long":
        await q.edit_message_text("⏳ *الديون طويلة الأجل:*", parse_mode="Markdown", reply_markup=long_menu_kb())
        
    elif data.startswith("sd_del_confirm|"): await sd_del_confirm(update, ctx)
    elif data.startswith("ld_del_confirm|"): await ld_del_confirm(update, ctx)
    elif data == "edit_menu":
        await q.edit_message_text("✏️ *إدارة وتصحيح البيانات*", parse_mode="Markdown", reply_markup=edit_kb())
    elif data == "del_cig":
        with db() as c:
            row = c.execute("SELECT id FROM cigarettes ORDER BY id DESC LIMIT 1").fetchone()
            if row: c.execute("DELETE FROM cigarettes WHERE id=?", (row["id"],))
        await q.edit_message_text(dashboard_text(), parse_mode="Markdown", reply_markup=main_kb())
    elif data == "del_exp":
        with db() as c:
            row = c.execute("SELECT id FROM expenses ORDER BY id DESC LIMIT 1").fetchone()
            if row: c.execute("DELETE FROM expenses WHERE id=?", (row["id"],))
        await q.edit_message_text(dashboard_text(), parse_mode="Markdown", reply_markup=main_kb())
    elif data == "reset_all_menu":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⚠️ نعم، امسح كل شيء", callback_data="do_reset_all")], [InlineKeyboardButton("🔙 إلغاء", callback_data="refresh")]])
        await q.edit_message_text("⚠️ مسح كل السجلات نهائياً للبدء من جديد؟", reply_markup=kb)
    elif data == "do_reset_all":
        with db() as c:
            c.execute("DELETE FROM cigarettes"); c.execute("DELETE FROM expenses"); c.execute("DELETE FROM prayers"); c.execute("DELETE FROM incomes"); c.execute("DELETE FROM sudan_pays"); c.execute("DELETE FROM long_debts"); c.execute("DELETE FROM sudan_reqs"); c.execute("DELETE FROM creditors")
        await q.edit_message_text("🧹 *تم مسح جميع السجلات!* يمكنك البدء الآن من جديد.", parse_mode="Markdown", reply_markup=main_kb())

if __name__ == "__main__":
    init()
    threading.Thread(target=run_web, daemon=True).start()
    app = Application.builder().token(TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    
    convs = [
        ConversationHandler(entry_points=[CallbackQueryHandler(add_exp_start, pattern="^add_exp_btn$"), CommandHandler("expense", add_exp_start)], states={EXP_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, exp_amount)], EXP_CAT: [CallbackQueryHandler(exp_category, pattern="^cat\\|")], EXP_PTYPE: [CallbackQueryHandler(exp_ptype, pattern="^pt\\|")], EXP_CREDITOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, exp_creditor)], EXP_DUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, exp_due)]}, fallbacks=[CommandHandler("cancel", cancel)]),
        ConversationHandler(entry_points=[CallbackQueryHandler(add_inc_start, pattern="^add_inc_btn$")], states={INC_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, inc_amount)], INC_SOURCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, inc_source)]}, fallbacks=[CommandHandler("cancel", cancel)]),
        ConversationHandler(entry_points=[CallbackQueryHandler(edit_rate_start, pattern="^edit_rate_btn$")], states={RATE_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_rate)]}, fallbacks=[CommandHandler("cancel", cancel)]),
        ConversationHandler(entry_points=[CallbackQueryHandler(edit_limit_start, pattern="^edit_limit_btn$")], states={LIMIT_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_limit)]}, fallbacks=[CommandHandler("cancel", cancel)]),
        ConversationHandler(entry_points=[CallbackQueryHandler(sd_add_start, pattern="^sd_add$")], states={SD_ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, sd_add_name)], SD_ADD_AMT: [MessageHandler(filters.TEXT & ~filters.COMMAND, sd_add_amt)]}, fallbacks=[CommandHandler("cancel", cancel)]),
        ConversationHandler(entry_points=[CallbackQueryHandler(sd_edit_start, pattern="^sd_edit\\|")], states={SD_EDIT_AMT: [MessageHandler(filters.TEXT & ~filters.COMMAND, sd_edit_amt)]}, fallbacks=[CommandHandler("cancel", cancel)]),
        ConversationHandler(entry_points=[CallbackQueryHandler(sd_pay_start, pattern="^sd_pay\\|")], states={SD_PAY_AMT: [MessageHandler(filters.TEXT & ~filters.COMMAND, sd_pay_amt)]}, fallbacks=[CommandHandler("cancel", cancel)]),
        CallbackQueryHandler(sd_del_start, pattern="^sd_del\\|"),
        ConversationHandler(entry_points=[CallbackQueryHandler(ld_add_start, pattern="^ld_add$")], states={LD_ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ld_add_name)], LD_ADD_AMT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ld_add_amt)]}, fallbacks=[CommandHandler("cancel", cancel)]),
        ConversationHandler(entry_points=[CallbackQueryHandler(ld_edit_start, pattern="^ld_edit\\|")], states={LD_EDIT_AMT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ld_edit_amt)]}, fallbacks=[CommandHandler("cancel", cancel)]),
        ConversationHandler(entry_points=[CallbackQueryHandler(ld_pay_start, pattern="^ld_pay\\|")], states={LD_PAY_AMT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ld_pay_amt)]}, fallbacks=[CommandHandler("cancel", cancel)]),
        CallbackQueryHandler(ld_del_start, pattern="^ld_del\\|"),
        ConversationHandler(entry_points=[CallbackQueryHandler(pay_cred_start, pattern="^pay_cred\\|")], states={PAY_CREDITOR_AMT: [MessageHandler(filters.TEXT & ~filters.COMMAND, pay_cred_amt)], PAY_CREDITOR_TYPE: [CallbackQueryHandler(pay_cred_type, pattern="^pay_cred_type\\|")]}, fallbacks=[CommandHandler("cancel", cancel)]),
        ConversationHandler(entry_points=[CallbackQueryHandler(rollover_start, pattern="^do_rollover$")], states={ROLLOVER_CASH: [MessageHandler(filters.TEXT & ~filters.COMMAND, rollover_cash)]}, fallbacks=[CommandHandler("cancel", cancel)]),
    ]
    for conv in convs: app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(on_button))
    print("✅ البوت يعمل...")
    app.run_polling()
