import nest_asyncio
nest_asyncio.apply()

import os
import sqlite3
from datetime import date, datetime
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
START_DATE = date(2026, 10, 22)
BASE_LIMIT = 12
PRICE_PER_CIG = 1.0
TZ = ZoneInfo("Asia/Riyadh")

# Conversation States
(EXP_AMOUNT, EXP_CAT, EXP_PTYPE, EXP_CREDITOR, EXP_DUE, 
 INC_AMOUNT, INC_SOURCE, 
 RATE_INPUT,
 SD_ADD_NAME, SD_ADD_AMT,
 SD_EDIT_AMT,
 LD_ADD_NAME, LD_ADD_AMT,
 LD_EDIT_AMT,
 SD_PAY_AMT,
 LD_PAY_AMT) = range(16)

SCHEDULE_NORMAL = ["09:00", "10:10", "11:20", "12:30", "13:30", "17:00", "18:15", "19:30", "20:45", "22:00", "23:15", "00:30"]
SCHEDULE_FRIDAY = ["11:00", "12:00", "13:15", "14:30", "15:45", "17:00", "18:15", "19:30", "20:45", "22:00", "23:15", "00:30"]

def now():
    return datetime.now(TZ)

def get_next_cig_time():
    sched = SCHEDULE_FRIDAY if now().weekday() == 4 else SCHEDULE_NORMAL
    current_time = now().strftime("%H:%M")
    for t in sched:
        if t > current_time:
            return t
    return "غداً صباحاً"

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

def get_rate():
    with db() as c:
        return float(c.execute("SELECT value FROM settings WHERE key='exchange_rate'").fetchone()["value"])

def today_limit():
    d = (now().date() - START_DATE).days
    return BASE_LIMIT if d < 0 else max(1, BASE_LIMIT - d // 7)

def cigs_today():
    with db() as c:
        rows = c.execute("SELECT ts FROM cigarettes WHERE date(ts)=?", (now().date().isoformat(),)).fetchall()
    return [datetime.fromisoformat(r["ts"]) for r in rows]

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

def dashboard_text():
    ts = cigs_today(); limit = today_limit()
    last = ts[-1].strftime("%I:%M %p") if ts else "—"
    
    with db() as c:
        r = c.execute("""SELECT
            COALESCE(SUM(CASE WHEN ptype='cash' THEN amount END),0) cash,
            COALESCE(SUM(CASE WHEN ptype='credit' THEN amount END),0) credit
            FROM expenses WHERE date(ts)=?""", (now().date().isoformat(),)).fetchone()
    cash, credit = r["cash"], r["credit"]
    
    wallet = get_wallet_balance()
    rate = get_rate()
    
    with db() as c:
        sudan_req = c.execute("SELECT SUM(amount_sdg) s FROM sudan_reqs").fetchone()["s"] or 0
        sudan_paid = c.execute("SELECT SUM(paid_sdg) s FROM sudan_pays").fetchone()["s"] or 0
        sudan_rem_sar = (sudan_req - sudan_paid) / rate
        
        long_total = c.execute("SELECT SUM(total_sar) s FROM long_debts").fetchone()["s"] or 0
        long_paid = c.execute("SELECT SUM(paid_sar) s FROM long_debts").fetchone()["s"] or 0
        long_rem_sar = long_total - long_paid

    prayers = get_prayers()
    p_text = " ".join([f"{'✅' if v else '⬜'} {k}" for k, v in prayers.items()])
    next_cig = get_next_cig_time()

    return (
        f"📅 *{now().strftime('%A %d %B %Y | %I:%M %p')}*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📈 *المحفظة المالية*\n"
        f"💵 *الرصيد المتاح: {wallet:.2f} ر.س*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💰 *حركة اليوم:*\n"
        f"💵 نقداً: {cash:.0f} ر.س ┃ 💳 آجلاً: {credit:.0f} ر.س\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🚬 *التدخين:* {len(ts)} / {limit}\n"
        f"⏱️ آخر: {last} ┃ ⏳ القادمة: *{next_cig}*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🕌 *الصلوات:*\n{p_text}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💼 *المصاريف المستحقة:*\n"
        f"🇸🇩 السودان: {sudan_rem_sar:.2f} ر.س\n"
        f"⏳ طويلة الأجل: {long_rem_sar:.2f} ر.س\n"
    )

def main_kb():
    prayers = get_prayers()
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚬 سيجارة +1", callback_data="cig"),
         InlineKeyboardButton("💰 إضافة مصروف", callback_data="add_exp_btn")],
        [InlineKeyboardButton("➕ إضافة إيراد", callback_data="add_inc_btn"),
         InlineKeyboardButton("✏️ إدارة وتصحيح", callback_data="edit_menu")],
        [InlineKeyboardButton("💼 المصاريف المستحقة", callback_data="accrued_menu")],
        [InlineKeyboardButton(f"{'✅' if prayers['الفجر'] else '⬜'} الفجر", callback_data="pr|الفجر"),
         InlineKeyboardButton(f"{'✅' if prayers['الظهر'] else '⬜'} الظهر", callback_data="pr|الظهر"),
         InlineKeyboardButton(f"{'✅' if prayers['العصر'] else '⬜'} العصر", callback_data="pr|العصر")],
        [InlineKeyboardButton(f"{'✅' if prayers['المغرب'] else '⬜'} المغرب", callback_data="pr|المغرب"),
         InlineKeyboardButton(f"{'✅' if prayers['العشاء'] else '⬜'} العشاء", callback_data="pr|العشاء")],
        [InlineKeyboardButton("🔄 تحديث اللوحة", callback_data="refresh")],
    ])

def accrued_menu_kb():
    rate = get_rate()
    kb = [
        [InlineKeyboardButton("🇸🇩 مستحقات السودان (بالجنيه)", callback_data="menu_sudan")],
        [InlineKeyboardButton("⏳ ديون طويلة الأجل (بالريال)", callback_data="menu_long")],
        [InlineKeyboardButton(f"💱 تعديل سعر الصرف (حالياً {rate})", callback_data="edit_rate_btn")],
        [InlineKeyboardButton("🔙 رجوع للوحة", callback_data="refresh")]
    ]
    return InlineKeyboardMarkup(kb)

def sudan_menu_kb():
    rate = get_rate()
    with db() as c:
        sudan = c.execute("""
            SELECT r.name, r.amount_sdg, COALESCE(p.paid_sdg,0) paid_sdg 
            FROM sudan_reqs r
            LEFT JOIN (SELECT name, SUM(paid_sdg) paid_sdg FROM sudan_pays GROUP BY name) p ON r.name = p.name
        """).fetchall()
    
    kb = []
    for r in sudan:
        rem_sdg = r["amount_sdg"] - r["paid_sdg"]
        rem_sar = rem_sdg / rate
        # Display name and amount
        kb.append([InlineKeyboardButton(f"👤 {r['name']} - {rem_sdg:,.0f} ج.س ({rem_sar:,.0f} ر.س)", callback_data="noop")])
        kb.append([
            InlineKeyboardButton("💸 دفع", callback_data=f"sd_pay|{r['name']}"),
            InlineKeyboardButton("✏️ تعديل", callback_data=f"sd_edit|{r['name']}"),
            InlineKeyboardButton("🗑️ حذف", callback_data=f"sd_del|{r['name']}")
        ])
    
    kb.append([InlineKeyboardButton("➕ إضافة مستفيد جديد", callback_data="sd_add")])
    kb.append([InlineKeyboardButton("🔙 رجوع", callback_data="accrued_menu")])
    return InlineKeyboardMarkup(kb)

def long_menu_kb():
    rate = get_rate()
    with db() as c:
        longs = c.execute("SELECT id, name, total_sar, paid_sar FROM long_debts").fetchall()
    
    kb = []
    for r in longs:
        rem_sar = r["total_sar"] - r["paid_sar"]
        rem_sdg = rem_sar * rate
        kb.append([InlineKeyboardButton(f"🏦 {r['name']} - {rem_sar:,.0f} ر.س ({rem_sdg:,.0f} ج.س)", callback_data="noop")])
        kb.append([
            InlineKeyboardButton("💸 سداد", callback_data=f"ld_pay|{r['id']}"),
            InlineKeyboardButton("✏️ تعديل", callback_data=f"ld_edit|{r['id']}"),
            InlineKeyboardButton("🗑️ حذف", callback_data=f"ld_del|{r['id']}")
        ])
    
    kb.append([InlineKeyboardButton("➕ إضافة دين جديد", callback_data="ld_add")])
    kb.append([InlineKeyboardButton("🔙 رجوع", callback_data="accrued_menu")])
    return InlineKeyboardMarkup(kb)

def edit_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑️ حذف آخر سيجارة", callback_data="del_cig"),
         InlineKeyboardButton("🗑️ حذف آخر مصروف", callback_data="del_exp")],
        [InlineKeyboardButton("🧹 تصفير كافة السجلات", callback_data="reset_all_menu")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="refresh")]
    ])

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(dashboard_text(), parse_mode="Markdown", reply_markup=main_kb())

# ================== EXPENSE CONV ==================
async def add_exp_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q: await q.answer()
    msg = "💰 *إضافة مصروف*\nأرسل المبلغ بالريال:"
    if q: await q.edit_message_text(msg, parse_mode="Markdown")
    else: await update.message.reply_text(msg, parse_mode="Markdown")
    return EXP_AMOUNT

async def exp_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: ctx.user_data["amount"] = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("⚠️ أرسل رقماً فقط."); return EXP_AMOUNT
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("أكل", callback_data="cat|أكل"),
        InlineKeyboardButton("دخان", callback_data="cat|دخان"),
        InlineKeyboardButton("سناكات", callback_data="cat|سناكات"),
    ],[
        InlineKeyboardButton("بقالة", callback_data="cat|بقالة"),
        InlineKeyboardButton("مواصلات", callback_data="cat|مواصلات"),
        InlineKeyboardButton("أخرى", callback_data="cat|أخرى"),
    ]])
    await update.message.reply_text("📂 اختر نوع المصروف:", reply_markup=kb)
    return EXP_CAT

async def exp_category(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    ctx.user_data["category"] = q.data.split("|",1)[1]
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ نقداً", callback_data="pt|cash"),
        InlineKeyboardButton("📝 آجل", callback_data="pt|credit"),
    ]])
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
    await update.message.reply_text("📅 تاريخ السداد (YYYY-MM-DD) أو `-` لتخطيه:", parse_mode="Markdown")
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
        c.execute("INSERT INTO expenses(ts,amount,category,ptype,creditor_id,due_date) VALUES(?,?,?,?,?,?)",
                  (now().isoformat(timespec="seconds"), ud["amount"], ud["category"], ud["ptype"], cid, ud.get("due")))
    await ctx.bot.send_message(update.effective_chat.id, dashboard_text(), parse_mode="Markdown", reply_markup=main_kb())
    return ConversationHandler.END

# ================== INCOME CONV ==================
async def add_inc_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q: await q.answer()
    msg = "📈 *إضافة إيراد*\nأرسل المبلغ بالريال:"
    if q: await q.edit_message_text(msg, parse_mode="Markdown")
    else: await update.message.reply_text(msg, parse_mode="Markdown")
    return INC_AMOUNT

async def inc_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: ctx.user_data["inc_amount"] = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("⚠️ أرسل رقماً فقط."); return INC_AMOUNT
    await update.message.reply_text("📝 اكتب مصدر الإيراد:")
    return INC_SOURCE

async def inc_source(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with db() as c:
        c.execute("INSERT INTO incomes(ts, amount, source) VALUES(?,?,?)", (now().isoformat(timespec="seconds"), ctx.user_data["inc_amount"], update.message.text.strip()))
    await update.message.reply_text("✅ تم الحفظ.\n\n" + dashboard_text(), parse_mode="Markdown", reply_markup=main_kb())
    return ConversationHandler.END

# ================== RATE CONV ==================
async def edit_rate_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q: await q.answer()
    msg = "💱 أرسل سعر الصرف الجديد (كم جنيه يساوي 1 ريال؟):"
    if q: await q.edit_message_text(msg)
    else: await update.message.reply_text(msg)
    return RATE_INPUT

async def save_rate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: rate = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("⚠️ أرسل رقماً فقط."); return RATE_INPUT
    with db() as c:
        c.execute("UPDATE settings SET value=? WHERE key='exchange_rate'", (str(rate),))
    await update.message.reply_text(f"✅ تم تحديث السعر إلى {rate}.\n\n" + dashboard_text(), parse_mode="Markdown", reply_markup=main_kb())
    return ConversationHandler.END

# ================== SUDAN ADD CONV ==================
async def sd_add_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await q.edit_message_text("🇸🇩 *إضافة مستفيد للسودان*\nأرسل اسم الشخص (مثال: الزوجة):", parse_mode="Markdown")
    return SD_ADD_NAME

async def sd_add_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    with db() as c:
        if c.execute("SELECT 1 FROM sudan_reqs WHERE name=?", (name,)).fetchone():
            await update.message.reply_text("⚠️ هذا الاسم موجود مسبقاً. أرسل اسماً آخر:"); return SD_ADD_NAME
    ctx.user_data["sd_name"] = name
    await update.message.reply_text("أرسل المبلغ بالجنيه السوداني (ج.س):")
    return SD_ADD_AMT

async def sd_add_amt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: amt = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("⚠️ أرسل رقماً فقط."); return SD_ADD_AMT
    with db() as c:
        c.execute("INSERT INTO sudan_reqs(name, amount_sdg) VALUES(?,?)", (ctx.user_data["sd_name"], amt))
    await update.message.reply_text("✅ تم الإضافة بنجاح.", reply_markup=main_kb())
    return ConversationHandler.END

# ================== SUDAN EDIT CONV ==================
async def sd_edit_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    ctx.user_data["sd_name"] = q.data.split("|")[1]
    await q.edit_message_text(f"✏️ تعديل مبلغ: *{ctx.user_data['sd_name']}*\nأرسل المبلغ الجديد بالجنيه السوداني (ج.س):", parse_mode="Markdown")
    return SD_EDIT_AMT

async def sd_edit_amt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: amt = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("⚠️ أرسل رقماً فقط."); return SD_EDIT_AMT
    with db() as c:
        c.execute("UPDATE sudan_reqs SET amount_sdg=? WHERE name=?", (amt, ctx.user_data["sd_name"]))
    await update.message.reply_text("✅ تم التعديل بنجاح.", reply_markup=main_kb())
    return ConversationHandler.END

# ================== SUDAN PAY CONV ==================
async def sd_pay_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    ctx.user_data["sd_name"] = q.data.split("|")[1]
    await q.edit_message_text(f"💸 تحويل إلى: *{ctx.user_data['sd_name']}*\nأرسل المبلغ الذي سترسله (بالجنيه السوداني):", parse_mode="Markdown")
    return SD_PAY_AMT

async def sd_pay_amt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: amt = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("⚠️ أرسل رقماً فقط."); return SD_PAY_AMT
    
    rate = get_rate()
    paid_sdg = amt; paid_sar = amt / rate
    with db() as c:
        c.execute("INSERT INTO sudan_pays(ts, name, paid_sdg, paid_sar) VALUES(?,?,?,?)", (now().isoformat(timespec="seconds"), ctx.user_data["sd_name"], paid_sdg, paid_sar))
        c.execute("INSERT INTO expenses(ts,amount,category,ptype,creditor_id,due_date) VALUES(?,?,?,?,?,?)", 
                  (now().isoformat(timespec="seconds"), paid_sar, "تحويل السودان", "cash", None, None))
    await update.message.reply_text(f"✅ تم تحويل {paid_sdg:,.0f} ج.س ({paid_sar:,.2f} ر.س).", reply_markup=main_kb())
    return ConversationHandler.END

# ================== SUDAN DELETE CONV ==================
async def sd_del_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    name = q.data.split("|")[1]
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⚠️ نعم، احذف", callback_data=f"sd_del_confirm|{name}")], [InlineKeyboardButton("🔙 إلغاء", callback_data="menu_sudan")]])
    await q.edit_message_text(f"هل أنت متأكد من حذف *{name}* بالكامل؟\n(سيتم حذف سجل الدفعات أيضاً)", parse_mode="Markdown", reply_markup=kb)
    return ConversationHandler.END # Using callback directly

async def sd_del_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    name = q.data.split("|")[1]
    with db() as c:
        c.execute("DELETE FROM sudan_reqs WHERE name=?", (name,))
        c.execute("DELETE FROM sudan_pays WHERE name=?", (name,))
    await q.edit_message_text(f"✅ تم حذف {name}.", reply_markup=sudan_menu_kb())

# ================== LONG ADD CONV ==================
async def ld_add_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await q.edit_message_text("⏳ *إضافة دين طويل الأجل*\nأرسل اسم الدين (مثال: قرض بنك):", parse_mode="Markdown")
    return LD_ADD_NAME

async def ld_add_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["ld_name"] = update.message.text.strip()
    await update.message.reply_text("أرسل المبلغ بالريال (ر.س):")
    return LD_ADD_AMT

async def ld_add_amt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: amt = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("⚠️ أرسل رقماً فقط."); return LD_ADD_AMT
    with db() as c:
        c.execute("INSERT INTO long_debts(name, total_sar) VALUES(?,?)", (ctx.user_data["ld_name"], amt))
    await update.message.reply_text("✅ تم الإضافة بنجاح.", reply_markup=main_kb())
    return ConversationHandler.END

# ================== LONG EDIT CONV ==================
async def ld_edit_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    ctx.user_data["ld_id"] = q.data.split("|")[1]
    await q.edit_message_text("✏️ أرسل المبلغ الكلي الجديد بالريال (ر.س):")
    return LD_EDIT_AMT

async def ld_edit_amt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: amt = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("⚠️ أرسل رقماً فقط."); return LD_EDIT_AMT
    with db() as c:
        c.execute("UPDATE long_debts SET total_sar=? WHERE id=?", (amt, ctx.user_data["ld_id"]))
    await update.message.reply_text("✅ تم التعديل بنجاح.", reply_markup=main_kb())
    return ConversationHandler.END

# ================== LONG PAY CONV ==================
async def ld_pay_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    ctx.user_data["ld_id"] = q.data.split("|")[1]
    await q.edit_message_text("💸 أرسل مبلغ السداد بالريال (ر.س):")
    return LD_PAY_AMT

async def ld_pay_amt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: amt = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("⚠️ أرسل رقماً فقط."); return LD_PAY_AMT
    with db() as c:
        c.execute("UPDATE long_debts SET paid_sar = paid_sar + ? WHERE id=?", (amt, ctx.user_data["ld_id"]))
        c.execute("INSERT INTO expenses(ts,amount,category,ptype,creditor_id,due_date) VALUES(?,?,?,?,?,?)", 
                  (now().isoformat(timespec="seconds"), amt, "سداد دين طويل", "cash", None, None))
    await update.message.reply_text("✅ تم تسجيل السداد بنجاح.", reply_markup=main_kb())
    return ConversationHandler.END

# ================== LONG DELETE CONV ==================
async def ld_del_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    id_ = q.data.split("|")[1]
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⚠️ نعم، احذف", callback_data=f"ld_del_confirm|{id_}")], [InlineKeyboardButton("🔙 إلغاء", callback_data="menu_long")]])
    await q.edit_message_text("هل أنت متأكد من حذف هذا الدين بالكامل؟", reply_markup=kb)
    return ConversationHandler.END

async def ld_del_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    id_ = q.data.split("|")[1]
    with db() as c:
        c.execute("DELETE FROM long_debts WHERE id=?", (id_,))
    await q.edit_message_text("✅ تم حذف الدين.", reply_markup=long_menu_kb())

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
        ts = cigs_today()
        if len(ts) >= today_limit():
            await q.answer("⚠️ وصلت السقف اليومي", show_alert=True); return
        with db() as c: c.execute("INSERT INTO cigarettes(ts) VALUES(?)", (now().isoformat(timespec="seconds"),))
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
        await q.edit_message_text("💼 *إدارة المصاريف المستحقة*\nاختر القسم:", parse_mode="Markdown", reply_markup=accrued_menu_kb())
        
    elif data == "menu_sudan":
        await q.edit_message_text("🇸🇩 *مستحقات السودان:*", parse_mode="Markdown", reply_markup=sudan_menu_kb())
        
    elif data == "menu_long":
        await q.edit_message_text("⏳ *الديون طويلة الأجل:*", parse_mode="Markdown", reply_markup=long_menu_kb())
        
    elif data.startswith("sd_del_confirm|"):
        await sd_del_confirm(update, ctx)
        
    elif data.startswith("ld_del_confirm|"):
        await ld_del_confirm(update, ctx)
        
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
        await q.edit_message_text("⚠️ مسح كل السجلات نهائياً؟", reply_markup=kb)
        
    elif data == "do_reset_all":
        with db() as c:
            c.execute("DELETE FROM cigarettes"); c.execute("DELETE FROM expenses")
            c.execute("DELETE FROM prayers"); c.execute("DELETE FROM incomes")
            c.execute("DELETE FROM sudan_pays"); c.execute("DELETE FROM long_debts")
            c.execute("DELETE FROM sudan_reqs")
        await q.edit_message_text("🧹 *تم مسح جميع السجلات!*", parse_mode="Markdown", reply_markup=main_kb())

if __name__ == "__main__":
    init()
    threading.Thread(target=run_web, daemon=True).start()
    app = Application.builder().token(TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    
    # Defining all Conversations
    convs = [
        ConversationHandler(entry_points=[CallbackQueryHandler(add_exp_start, pattern="^add_exp_btn$"), CommandHandler("expense", add_exp_start)],
                            states={EXP_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, exp_amount)],
                                    EXP_CAT: [CallbackQueryHandler(exp_category, pattern="^cat\\|")],
                                    EXP_PTYPE: [CallbackQueryHandler(exp_ptype, pattern="^pt\\|")],
                                    EXP_CREDITOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, exp_creditor)],
                                    EXP_DUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, exp_due)]},
                            fallbacks=[CommandHandler("cancel", cancel)]),
        
        ConversationHandler(entry_points=[CallbackQueryHandler(add_inc_start, pattern="^add_inc_btn$"), CommandHandler("income", add_inc_start)],
                            states={INC_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, inc_amount)],
                                    INC_SOURCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, inc_source)]},
                            fallbacks=[CommandHandler("cancel", cancel)]),
        
        ConversationHandler(entry_points=[CallbackQueryHandler(edit_rate_start, pattern="^edit_rate_btn$")],
                            states={RATE_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_rate)]},
                            fallbacks=[CommandHandler("cancel", cancel)]),
                            
        # Sudan Add
        ConversationHandler(entry_points=[CallbackQueryHandler(sd_add_start, pattern="^sd_add$")],
                            states={SD_ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, sd_add_name)],
                                    SD_ADD_AMT: [MessageHandler(filters.TEXT & ~filters.COMMAND, sd_add_amt)]},
                            fallbacks=[CommandHandler("cancel", cancel)]),
                            
        # Sudan Edit
        ConversationHandler(entry_points=[CallbackQueryHandler(sd_edit_start, pattern="^sd_edit\\|")],
                            states={SD_EDIT_AMT: [MessageHandler(filters.TEXT & ~filters.COMMAND, sd_edit_amt)]},
                            fallbacks=[CommandHandler("cancel", cancel)]),
                            
        # Sudan Pay
        ConversationHandler(entry_points=[CallbackQueryHandler(sd_pay_start, pattern="^sd_pay\\|")],
                            states={SD_PAY_AMT: [MessageHandler(filters.TEXT & ~filters.COMMAND, sd_pay_amt)]},
                            fallbacks=[CommandHandler("cancel", cancel)]),
                            
        # Sudan Delete Confirm Handler (Inline)
        CallbackQueryHandler(sd_del_start, pattern="^sd_del\\|"),
        
        # Long Add
        ConversationHandler(entry_points=[CallbackQueryHandler(ld_add_start, pattern="^ld_add$")],
                            states={LD_ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ld_add_name)],
                                    LD_ADD_AMT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ld_add_amt)]},
                            fallbacks=[CommandHandler("cancel", cancel)]),
                            
        # Long Edit
        ConversationHandler(entry_points=[CallbackQueryHandler(ld_edit_start, pattern="^ld_edit\\|")],
                            states={LD_EDIT_AMT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ld_edit_amt)]},
                            fallbacks=[CommandHandler("cancel", cancel)]),
                            
        # Long Pay
        ConversationHandler(entry_points=[CallbackQueryHandler(ld_pay_start, pattern="^ld_pay\\|")],
                            states={LD_PAY_AMT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ld_pay_amt)]},
                            fallbacks=[CommandHandler("cancel", cancel)]),
                            
        # Long Delete Confirm Handler (Inline)
        CallbackQueryHandler(ld_del_start, pattern="^ld_del\\|"),
    ]

    for conv in convs:
        app.add_handler(conv)
        
    app.add_handler(CallbackQueryHandler(on_button))
    
    print("✅ البوت يعمل...")
    app.run_polling()
