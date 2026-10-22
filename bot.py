import nest_asyncio
nest_asyncio.apply()

import os
import sqlite3
import io
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
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
 SUDAN_NAME, SUDAN_AMT,
 SUDAN_PAY_CUR, SUDAN_PAY_AMT,
 LONG_NAME, LONG_AMT, LONG_PAY_AMT) = range(15)

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
        # Insert default rate if not exists
        c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('exchange_rate', '140')")
        # Insert default Sudan members if empty
        if c.execute("SELECT COUNT(*) n FROM sudan_reqs").fetchone()["n"] == 0:
            c.executemany("INSERT INTO sudan_reqs(name, amount_sdg) VALUES(?,?)", 
                          [("الزوجة", 500000), ("الأب", 500000), ("الأخت", 400000), ("أخرى", 600000)])

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

def money_today():
    with db() as c:
        r = c.execute("""SELECT
            COALESCE(SUM(CASE WHEN ptype='cash' THEN amount END),0) cash,
            COALESCE(SUM(CASE WHEN ptype='credit' THEN amount END),0) credit
            FROM expenses WHERE date(ts)=?""", (now().date().isoformat(),)).fetchone()
    return r["cash"], r["credit"]

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

AR_DAYS = {"Monday":"الاثنين","Tuesday":"الثلاثاء","Wednesday":"الأربعاء","Thursday":"الخميس","Friday":"الجمعة","Saturday":"السبت","Sunday":"الأحد"}
AR_MONTHS = {"January":"يناير","February":"فبراير","March":"مارس","April":"أبريل","May":"مايو","June":"يونيو","July":"يوليو","August":"أغسطس","September":"سبتمبر","October":"أكتوبر","November":"نوفمبر","December":"ديسمبر"}

def fmt_date(d):
    en = d.strftime("%A %d %B %Y").split()
    return f"{AR_DAYS[en[0]]} {en[1]} {AR_MONTHS[en[2]]} {en[3]}"

def dashboard_text():
    ts = cigs_today(); limit = today_limit()
    last = ts[-1].strftime("%I:%M %p") if ts else "—"
    cash, credit = money_today()
    wallet = get_wallet_balance()
    rate = get_rate()
    
    with db() as c:
        sudan_req = c.execute("SELECT SUM(amount_sdg) s FROM sudan_reqs").fetchone()["s"] or 0
        sudan_paid = c.execute("SELECT SUM(paid_sdg) s FROM sudan_pays").fetchone()["s"] or 0
        sudan_rem_sdg = sudan_req - sudan_paid
        sudan_rem_sar = sudan_rem_sdg / rate
        
        long_total = c.execute("SELECT SUM(total_sar) s FROM long_debts").fetchone()["s"] or 0
        long_paid = c.execute("SELECT SUM(paid_sar) s FROM long_debts").fetchone()["s"] or 0
        long_rem_sar = long_total - long_paid
        long_rem_sdg = long_rem_sar * rate

    prayers = get_prayers()
    p_text = " ".join([f"{'✅' if v else '⬜'} {k}" for k, v in prayers.items()])
    next_cig = get_next_cig_time()

    return (
        f"📅 *{fmt_date(now().date())} | {now().strftime('%I:%M %p')}*\n"
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
        f"💼 *الالتزامات الخارجية:*\n"
        f"🇸🇩 السودان: {sudan_rem_sar:.2f} ر.س\n"
        f"⏳ طويلة الأجل: {long_rem_sar:.2f} ر.س\n"
    )

def main_kb():
    prayers = get_prayers()
    p_btns = [
        InlineKeyboardButton(f"{'✅' if prayers['الفجر'] else '⬜'} الفجر", callback_data="pr|الفجر"),
        InlineKeyboardButton(f"{'✅' if prayers['الظهر'] else '⬜'} الظهر", callback_data="pr|الظهر"),
        InlineKeyboardButton(f"{'✅' if prayers['العصر'] else '⬜'} العصر", callback_data="pr|العصر")
    ]
    p_btns2 = [
        InlineKeyboardButton(f"{'✅' if prayers['المغرب'] else '⬜'} المغرب", callback_data="pr|المغرب"),
        InlineKeyboardButton(f"{'✅' if prayers['العشاء'] else '⬜'} العشاء", callback_data="pr|العشاء")
    ]

    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚬 سيجارة +1", callback_data="cig"),
         InlineKeyboardButton("💰 إضافة مصروف", callback_data="add_exp")],
        [InlineKeyboardButton("➕ إضافة إيراد", callback_data="add_inc"),
         InlineKeyboardButton("✏️ إدارة وتصحيح", callback_data="edit_menu")],
        [InlineKeyboardButton("🔍 التفاصيل المالية", callback_data="details_menu")],
        p_btns,
        p_btns2,
        [InlineKeyboardButton("🔄 تحديث اللوحة", callback_data="refresh")],
    ])

def details_text():
    rate = get_rate()
    with db() as c:
        sudan = c.execute("""
            SELECT r.name, r.amount_sdg, COALESCE(p.paid_sdg,0) paid_sdg 
            FROM sudan_reqs r
            LEFT JOIN (SELECT name, SUM(paid_sdg) paid_sdg FROM sudan_pays GROUP BY name) p ON r.name = p.name
        """).fetchall()
        
        longs = c.execute("SELECT id, name, total_sar, paid_sar FROM long_debts").fetchall()

    text = f"💱 *سعر الصرف:* 1 ر.س = {rate} ج.س\n\n"
    
    text += "🇸🇩 *جدول السودان (الأساس بالجنيه):*\n```text\n"
    text += f"{'البند':<10} | {'بالجنيه':<9} | {'بالريال':<7}\n"
    text += "-"*32 + "\n"
    
    total_req_sdg = 0; total_req_sar = 0
    for r in sudan:
        rem_sdg = r["amount_sdg"] - r["paid_sdg"]
        rem_sar = rem_sdg / rate
        total_req_sdg += rem_sdg; total_req_sar += rem_sar
        text += f"{r['name']:<10} | {rem_sdg:,.0f} | {rem_sar:,.0f}\n"
    
    text += "-"*32 + "\n"
    text += f"{'الإجمالي':<10} | {total_req_sdg:,.0f} | {total_req_sar:,.0f}\n```\n"
    
    text += "⏳ *جدول الديون طويلة الأجل (الأساس بالريال):*\n```text\n"
    text += f"{'البند':<10} | {'بالريال':<9} | {'بالجنيه':<7}\n"
    text += "-"*32 + "\n"
    
    total_long_sar = 0; total_long_sdg = 0
    for r in longs:
        rem_sar = r["total_sar"] - r["paid_sar"]
        rem_sdg = rem_sar * rate
        total_long_sar += rem_sar; total_long_sdg += rem_sdg
        text += f"{r['name']:<10} | {rem_sar:,.0f} | {rem_sdg:,.0f}\n"
    
    text += "-"*32 + "\n"
    text += f"{'الإجمالي':<10} | {total_long_sar:,.0f} | {total_long_sdg:,.0f}\n```\n"
    
    return text

def details_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💱 تحديث سعر الصرف", callback_data="edit_rate"),
         InlineKeyboardButton("✏️ تعديل مبالغ السودان", callback_data="edit_sudan")],
        [InlineKeyboardButton("💸 تحويل للسودان", callback_data="pay_sudan"),
         InlineKeyboardButton("➕ إضافة دين طويل", callback_data="add_long")],
        [InlineKeyboardButton("💸 سداد دين طويل", callback_data="pay_long")],
        [InlineKeyboardButton("🔙 رجوع للوحة", callback_data="refresh")]
    ])

def edit_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑️ حذف آخر سيجارة", callback_data="del_cig")],
        [InlineKeyboardButton("🗑️ حذف آخر مصروف", callback_data="del_exp")],
        [InlineKeyboardButton("💸 ترحيل المديونية للشهر القادم", callback_data="rollover")],
        [InlineKeyboardButton("🧹 تصفير الديون فقط", callback_data="reset_debts")],
        [InlineKeyboardButton("⚠️ تصفير كافة السجلات", callback_data="reset_all_menu")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="refresh")]
    ])

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = dashboard_text()
    if update.callback_query:
        await update.callback_query.edit_message_text(msg, parse_mode="Markdown", reply_markup=main_kb())
    else:
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=main_kb())

# ================== ADD EXPENSE ==================
async def add_exp_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("💰 *إضافة مصروف*\nأرسل المبلغ بالريال:", parse_mode="Markdown")
    return EXP_AMOUNT

async def exp_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        ctx.user_data["amount"] = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("⚠️ أرسل رقماً فقط، مثال: 25"); return EXP_AMOUNT
    
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
    await update.message.reply_text("📅 تاريخ السداد المتوقع (YYYY-MM-DD) أو `-` لتخطيه:", parse_mode="Markdown")
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
        c.execute("""INSERT INTO expenses(ts,amount,category,ptype,creditor_id,due_date)
                     VALUES(?,?,?,?,?,?)""",
                  (now().isoformat(timespec="seconds"), ud["amount"], ud["category"], ud["ptype"], cid, ud.get("due")))
    
    await ctx.bot.send_message(update.effective_chat.id, dashboard_text(), parse_mode="Markdown", reply_markup=main_kb())
    return ConversationHandler.END

# ================== ADD INCOME ==================
async def add_inc_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📈 *إضافة إيراد*\nأرسل المبلغ بالريال:", parse_mode="Markdown")
    return INC_AMOUNT

async def inc_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        ctx.user_data["inc_amount"] = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("⚠️ أرسل رقماً فقط."); return INC_AMOUNT
    await update.message.reply_text("📝 اكتب مصدر الإيراد (مثال: راتب، مكافأة):")
    return INC_SOURCE

async def inc_source(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    source = update.message.text.strip()
    with db() as c:
        c.execute("INSERT INTO incomes(ts, amount, source) VALUES(?,?,?)", (now().isoformat(timespec="seconds"), ctx.user_data["inc_amount"], source))
    await update.message.reply_text(f"✅ تم إضافة إيراد بقيمة {ctx.user_data['inc_amount']:.0f} ر.س.\n\n" + dashboard_text(), parse_mode="Markdown", reply_markup=main_kb())
    return ConversationHandler.END

# ================== EDIT RATE ==================
async def edit_rate_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("💱 أرسل سعر الصرف الجديد (كم جنيه يساوي 1 ريال؟):")
    return RATE_INPUT

async def save_rate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        rate = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("⚠️ أرسل رقماً فقط."); return RATE_INPUT
    with db() as c:
        c.execute("UPDATE settings SET value=? WHERE key='exchange_rate'", (str(rate),))
    await update.message.reply_text(f"✅ تم تحديث سعر الصرف إلى {rate}.\n\n" + dashboard_text(), parse_mode="Markdown", reply_markup=main_kb())
    return ConversationHandler.END

# ================== PAY SUDAN ==================
async def pay_sudan_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with db() as c:
        rows = c.execute("SELECT name FROM sudan_reqs").fetchall()
    if not rows:
        await update.message.reply_text("لا يوجد مستفيدون."); return ConversationHandler.END
    kb = [[InlineKeyboardButton(r["name"], callback_data=f"psn|{r['name']}")] for r in rows]
    await update.message.reply_text("👤 لمن تريد التحويل؟", reply_markup=InlineKeyboardMarkup(kb))
    return SUDAN_NAME

async def pay_sudan_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    ctx.user_data["s_name"] = q.data.split("|")[1]
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("بالريال (ر.س)", callback_data="psc|sar"),
        InlineKeyboardButton("بالجنيه (ج.س)", callback_data="psc|sdg")
    ]])
    await q.edit_message_text(f"تحويل لـ {ctx.user_data['s_name']}. بأي عملة سترسل؟", reply_markup=kb)
    return SUDAN_PAY_CUR

async def pay_sudan_cur(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    ctx.user_data["s_cur"] = q.data.split("|")[1]
    await q.edit_message_text("أرسل المبلغ:")
    return SUDAN_PAY_AMT

async def save_sudan_pay(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        amt = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("⚠️ أرسل رقماً فقط."); return SUDAN_PAY_AMT
    
    rate = get_rate()
    name = ctx.user_data["s_name"]
    if ctx.user_data["s_cur"] == "sar":
        paid_sar = amt; paid_sdg = amt * rate
    else:
        paid_sdg = amt; paid_sar = amt / rate
    
    with db() as c:
        c.execute("INSERT INTO sudan_pays(ts, name, paid_sdg, paid_sar) VALUES(?,?,?,?)", 
                  (now().isoformat(timespec="seconds"), name, paid_sdg, paid_sar))
        # Register as expense too so it deducts from wallet
        c.execute("""INSERT INTO expenses(ts,amount,category,ptype,creditor_id,due_date) 
                     VALUES(?,?,?,?,?,?)""", 
                  (now().isoformat(timespec="seconds"), paid_sar, "تحويل السودان", "cash", None, None))
        
    await update.message.reply_text(f"✅ تم تحويل {paid_sdg:,.0f} ج.س ({paid_sar:,.0f} ر.س) لـ {name}.\n\n" + dashboard_text(), parse_mode="Markdown", reply_markup=main_kb())
    return ConversationHandler.END

# ================== ADD LONG DEBT ==================
async def add_long_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ *إضافة دين طويل الأجل*\nأرسل اسم الدين (مثال: قرض بنك):", parse_mode="Markdown")
    return LONG_NAME

async def long_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["l_name"] = update.message.text.strip()
    await update.message.reply_text("أرسل المبلغ بالريال (ر.س):")
    return LONG_AMT

async def save_long(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        amt = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("⚠️ أرسل رقماً فقط."); return LONG_AMT
    with db() as c:
        c.execute("INSERT INTO long_debts(name, total_sar) VALUES(?,?)", (ctx.user_data["l_name"], amt))
    await update.message.reply_text(f"✅ تم إضافة دين {ctx.user_data['l_name']} بقيمة {amt} ر.س.\n\n" + dashboard_text(), parse_mode="Markdown", reply_markup=main_kb())
    return ConversationHandler.END

# ================== PAY LONG DEBT ==================
async def pay_long_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with db() as c:
        rows = c.execute("SELECT id, name, total_sar - paid_sar as rem FROM long_debts WHERE total_sar > paid_sar").fetchall()
    if not rows:
        await update.message.reply_text("لا توجد ديون متبقية."); return ConversationHandler.END
    kb = [[InlineKeyboardButton(f"{r['name']} (متبقي {r['rem']:.0f} ر.س)", callback_data=f"pln|{r['id']}")] for r in rows]
    await update.message.reply_text("أي دين تريد سداده؟", reply_markup=InlineKeyboardMarkup(kb))
    return LONG_PAY_AMT # Reuse state for amount input

async def pay_long_amt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        q = update.callback_query; await q.answer()
        ctx.user_data["l_id"] = q.data.split("|")[1]
        await q.edit_message_text("أرسل مبلغ السداد بالريال:")
        return LONG_PAY_AMT
    
    # This is the text message amount
    try:
        amt = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("⚠️ أرسل رقماً فقط."); return LONG_PAY_AMT
    
    with db() as c:
        c.execute("UPDATE long_debts SET paid_sar = paid_sar + ? WHERE id=?", (amt, ctx.user_data["l_id"]))
        # Register as expense
        c.execute("""INSERT INTO expenses(ts,amount,category,ptype,creditor_id,due_date) 
                     VALUES(?,?,?,?,?,?)""", 
                  (now().isoformat(timespec="seconds"), amt, "سداد دين طويل", "cash", None, None))
    await update.message.reply_text("✅ تم تسجيل السداد.\n\n" + dashboard_text(), parse_mode="Markdown", reply_markup=main_kb())
    return ConversationHandler.END

# ================== MAIN BUTTON HANDLER ==================
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    data = q.data
    
    if data == "cig":
        ts = cigs_today()
        if len(ts) >= today_limit():
            await q.answer("⚠️ وصلت السقف اليومي", show_alert=True); return
        with db() as c:
            c.execute("INSERT INTO cigarettes(ts) VALUES(?)", (now().isoformat(timespec="seconds"),))
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
        
    elif data == "details_menu":
        await q.edit_message_text(details_text(), parse_mode="Markdown", reply_markup=details_kb())
        
    elif data == "edit_menu":
        await q.edit_message_text("✏️ *إدارة وتصحيح البيانات*\nماذا تريد أن تفعل؟", parse_mode="Markdown", reply_markup=edit_kb())
        
    elif data == "del_cig":
        with db() as c:
            row = c.execute("SELECT id FROM cigarettes ORDER BY id DESC LIMIT 1").fetchone()
            if row:
                c.execute("DELETE FROM cigarettes WHERE id=?", (row["id"],))
                await q.answer("✅ تم حذف آخر سيجارة")
            else:
                await q.answer("لا توجد سجائر لحذفها", show_alert=True)
        await q.edit_message_text(dashboard_text(), parse_mode="Markdown", reply_markup=main_kb())
        
    elif data == "del_exp":
        with db() as c:
            row = c.execute("SELECT id FROM expenses ORDER BY id DESC LIMIT 1").fetchone()
            if row:
                c.execute("DELETE FROM expenses WHERE id=?", (row["id"],))
                await q.answer("✅ تم حذف آخر مصروف")
            else:
                await q.answer("لا توجد مصاريف لحذفها", show_alert=True)
        await q.edit_message_text(dashboard_text(), parse_mode="Markdown", reply_markup=main_kb())
        
    elif data == "reset_all_menu":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⚠️ نعم، امسح كل شيء", callback_data="do_reset_all")], [InlineKeyboardButton("🔙 إلغاء", callback_data="refresh")]])
        await q.edit_message_text("⚠️ هل أنت متأكد من مسح كل السجلات؟", parse_mode="Markdown", reply_markup=kb)
        
    elif data == "do_reset_all":
        with db() as c:
            c.execute("DELETE FROM cigarettes")
            c.execute("DELETE FROM expenses")
            c.execute("DELETE FROM creditors")
            c.execute("DELETE FROM prayers")
            c.execute("DELETE FROM incomes")
            c.execute("DELETE FROM sudan_pays")
            c.execute("DELETE FROM long_debts")
        await q.edit_message_text("🧹 *تم مسح جميع السجلات!*", parse_mode="Markdown", reply_markup=main_kb())
        
    # Trigger conversation starters from buttons
    elif data == "add_exp": await add_exp_start(update, ctx)
    elif data == "add_inc": await add_inc_start(update, ctx)
    elif data == "edit_rate": await edit_rate_start(update, ctx)
    elif data == "pay_sudan": await pay_sudan_start(update, ctx)
    elif data == "add_long": await add_long_start(update, ctx)
    elif data == "pay_long": await pay_long_start(update, ctx)

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ تم الإلغاء.")
    return ConversationHandler.END

if __name__ == "__main__":
    init()
    threading.Thread(target=run_web, daemon=True).start()
    app = Application.builder().token(TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    
    conv_handlers = [
        ConversationHandler(
            entry_points=[
                CommandHandler("expense", add_exp_start),
                CallbackQueryHandler(on_button, pattern="^add_exp$")
            ],
            states={
                EXP_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, exp_amount)],
                EXP_CAT: [CallbackQueryHandler(exp_category, pattern="^cat\\|")],
                EXP_PTYPE: [CallbackQueryHandler(exp_ptype, pattern="^pt\\|")],
                EXP_CREDITOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, exp_creditor)],
                EXP_DUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, exp_due)],
            },
            fallbacks=[CommandHandler("cancel", cancel)]
        ),
        ConversationHandler(
            entry_points=[
                CommandHandler("income", add_inc_start),
                CallbackQueryHandler(on_button, pattern="^add_inc$")
            ],
            states={
                INC_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, inc_amount)],
                INC_SOURCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, inc_source)],
            },
            fallbacks=[CommandHandler("cancel", cancel)]
        ),
        ConversationHandler(
            entry_points=[CallbackQueryHandler(on_button, pattern="^edit_rate$")],
            states={ RATE_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_rate)] },
            fallbacks=[CommandHandler("cancel", cancel)]
        ),
        ConversationHandler(
            entry_points=[CallbackQueryHandler(on_button, pattern="^pay_sudan$")],
            states={
                SUDAN_NAME: [CallbackQueryHandler(pay_sudan_name, pattern="^psn\\|")],
                SUDAN_PAY_CUR: [CallbackQueryHandler(pay_sudan_cur, pattern="^psc\\|")],
                SUDAN_PAY_AMT: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_sudan_pay)],
            },
            fallbacks=[CommandHandler("cancel", cancel)]
        ),
        ConversationHandler(
            entry_points=[
                CommandHandler("long", add_long_start),
                CallbackQueryHandler(on_button, pattern="^add_long$")
            ],
            states={
                LONG_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, long_name)],
                LONG_AMT: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_long)],
            },
            fallbacks=[CommandHandler("cancel", cancel)]
        ),
        ConversationHandler(
            entry_points=[CallbackQueryHandler(on_button, pattern="^pay_long$")],
            states={
                LONG_PAY_AMT: [CallbackQueryHandler(pay_long_amt, pattern="^pln\\|"), MessageHandler(filters.TEXT & ~filters.COMMAND, pay_long_amt)],
            },
            fallbacks=[CommandHandler("cancel", cancel)]
        )
    ]
    
    # Add Conversation Handlers first, so they take priority over generic button clicks
    for ch in conv_handlers:
        app.add_handler(ch)
        
    app.add_handler(CallbackQueryHandler(on_button))
    
    print("✅ البوت يعمل...")
    app.run_polling()
