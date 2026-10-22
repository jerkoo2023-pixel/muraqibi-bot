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

# إعداد خادم الويب (لإرضاء Render المجاني)
web_app = Flask(__name__)

@web_app.route('/')
def health():
    return "Bot is alive!", 200

def run_web():
    port = int(os.environ.get("PORT", 8080))
    web_app.run(host='0.0.0.0', port=port)

# إعدادات البوت
TOKEN = os.environ.get("BOT_TOKEN")
DB = "monitor.db"
START_DATE = date(2026, 10, 22)
BASE_LIMIT = 12
PRICE_PER_CIG = 1.0
TZ = ZoneInfo("Asia/Riyadh") # توقيت السعودية

(AMOUNT, CATEGORY, PTYPE, CREDITOR, DUE) = range(5)

def now():
    return datetime.now(TZ)

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
        CREATE TABLE IF NOT EXISTS prayers(
            date TEXT, name TEXT, status INTEGER, PRIMARY KEY(date, name));
        """)

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

def debts():
    with db() as c:
        return c.execute("""
            SELECT COALESCE(cr.name,'بدون اسم') name, SUM(e.amount) total, COUNT(*) n
            FROM expenses e LEFT JOIN creditors cr ON cr.id=e.creditor_id
            WHERE e.ptype='credit' AND e.paid=0
            GROUP BY cr.name ORDER BY total DESC""").fetchall()

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
    remaining = max(0, limit - len(ts))
    last = ts[-1].strftime("%I:%M %p") if ts else "—"
    gap = f"{int((now()-ts[-1]).total_seconds()//60)} دقيقة" if ts else "—"
    cash, credit = money_today()
    d = debts(); total = sum(x["total"] for x in d)
    lines = "\n".join(f"   • {x['name']}: {x['total']:.0f} ر.س ({x['n']} بند)" for x in d) or "   • لا يوجد"
    
    prayers = get_prayers()
    p_text = " | ".join([f"{'✅' if v else '⬜'} {k}" for k, v in prayers.items()])

    return (
        "🎛️ *لوحة التحكم*\n"
        f"📅 {fmt_date(now().date())}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🕌 *الصلوات:*\n   {p_text}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🚬 *السجائر اليوم:* {len(ts)} / {limit}\n"
        f"   المتبقي: {remaining} سيجارة\n"
        f"   آخر سيجارة: {last}\n"
        f"   الفارق الزمني: {gap}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💰 *مصاريف اليوم*\n"
        f"   نقداً: {cash:.0f} ر.س\n"
        f"   آجلاً: {credit:.0f} ر.س\n"
        f"   الإجمالي: {cash+credit:.0f} ر.س\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📉 *إجمالي المديونية:* {total:.0f} ر.س\n{lines}"
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
        p_btns,
        p_btns2,
        [InlineKeyboardButton("🚬 سيجارة +1", callback_data="cig"),
         InlineKeyboardButton("💰 إضافة مصروف", callback_data="add_exp")],
        [InlineKeyboardButton("📉 عرض الديون", callback_data="debt"),
         InlineKeyboardButton("📊 تقرير الشهر", callback_data="report")],
        [InlineKeyboardButton("🔄 تحديث اللوحة", callback_data="refresh")],
    ])

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(dashboard_text(), parse_mode="Markdown", reply_markup=main_kb())

async def cig(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ts = cigs_today()
    if len(ts) >= today_limit():
        await update.message.reply_text("⚠️ وصلت السقف اليومي. حاول تأجيل السيجارة القادمة.")
        return
    with db() as c:
        c.execute("INSERT INTO cigarettes(ts) VALUES(?)", (now().isoformat(timespec="seconds"),))
    await update.message.reply_text("✅ سُجّلت سيجارة.\n\n" + dashboard_text(), parse_mode="Markdown", reply_markup=main_kb())

async def debt_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with db() as c:
        rows = c.execute("""SELECT e.id, e.amount, e.category, e.due_date, COALESCE(cr.name,'—') name
                            FROM expenses e LEFT JOIN creditors cr ON cr.id=e.creditor_id
                            WHERE e.ptype='credit' AND e.paid=0 ORDER BY e.id""").fetchall()
    if not rows:
        await update.message.reply_text("✅ لا توجد ديون قائمة حالياً."); return
    lines = [f"#{r['id']} | {r['amount']:.0f} ر.س | {r['category']} | {r['name']} | استحقاق: {r['due_date'] or '—'}" for r in rows]
    await update.message.reply_text("📉 *الديون القائمة:*\n" + "\n".join(lines), parse_mode="Markdown")

async def pay(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: eid = int(ctx.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("الصيغة: `/pay 3`"); return
    with db() as c:
        c.execute("UPDATE expenses SET paid=1 WHERE id=?", (eid,))
    await update.message.reply_text(f"✅ تم تسجيل سداد الدين #{eid}.\n\n" + dashboard_text(), parse_mode="Markdown", reply_markup=main_kb())

async def report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with db() as c:
        cigs = c.execute("SELECT COUNT(*) n FROM cigarettes WHERE date(ts)>=date('now','-30 day')").fetchone()["n"]
        cash = c.execute("SELECT COALESCE(SUM(amount),0) s FROM expenses WHERE ptype='cash' AND date(ts)>=date('now','-30 day')").fetchone()["s"]
        credit = c.execute("SELECT COALESCE(SUM(amount),0) s FROM expenses WHERE ptype='credit' AND date(ts)>=date('now','-30 day')").fetchone()["s"]
    cost = cigs * PRICE_PER_CIG
    saved = (BASE_LIMIT*30 - cigs) * PRICE_PER_CIG
    await update.message.reply_text(
        "📊 *تقرير آخر 30 يوماً*\n"
        f"🚬 السجائر: {cigs} | التكلفة: {cost:.0f} ر.س\n"
        f"💡 الوفر مقابل سقف 12: {saved:.0f} ر.س\n"
        f"💰 المصاريف: نقداً {cash:.0f} | آجلاً {credit:.0f} | الإجمالي {cash+credit:.0f} ر.س",
        parse_mode="Markdown")

async def add_exp_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("💰 *إضافة مصروف*\nأرسل المبلغ بالريال (مثال: 25)", parse_mode="Markdown")
    return AMOUNT

async def exp_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        ctx.user_data["amount"] = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("⚠️ أرسل رقماً فقط، مثال: 25"); return AMOUNT
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
    return CATEGORY

async def exp_category(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    ctx.user_data["category"] = q.data.split("|",1)[1]
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ نقداً", callback_data="pt|cash"),
        InlineKeyboardButton("📝 آجل", callback_data="pt|credit"),
    ]])
    await q.edit_message_text(f"النوع: *{ctx.user_data['category']}*\nطريقة الدفع؟", parse_mode="Markdown", reply_markup=kb)
    return PTYPE

async def exp_ptype(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    ctx.user_data["ptype"] = q.data.split("|",1)[1]
    if ctx.user_data["ptype"] == "cash":
        await q.edit_message_text("💵 نقداً. لحظة الحفظ...")
        return await save_expense(update, ctx)
    await q.edit_message_text("📝 آجل. اكتب *اسم الجهة الدائنة* (مثال: بقالة الأمانة):", parse_mode="Markdown")
    return CREDITOR

async def exp_creditor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["creditor"] = update.message.text.strip()
    await update.message.reply_text("📅 تاريخ السداد المتوقع؟ (YYYY-MM-DD)\nأو أرسل `-` لتخطيه.", parse_mode="Markdown")
    return DUE

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
    text = f"✅ سُجّل المصروف: *{ud['amount']:.0f} ر.س* — {ud['category']} — " + \
           ("نقداً" if ud["ptype"]=="cash" else f"آجل لـ {ud['creditor']}")
    await ctx.bot.send_message(update.effective_chat.id, text + "\n\n" + dashboard_text(), parse_mode="Markdown", reply_markup=main_kb())
    return ConversationHandler.END

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ تم الإلغاء.")
    return ConversationHandler.END

async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    data = q.data
    
    if data == "cig":
        ts = cigs_today()
        if len(ts) >= today_limit():
            await q.answer("⚠️ وصلت السقف اليومي", show_alert=True); return
        with db() as c:
            c.execute("INSERT INTO cigarettes(ts) VALUES(?)", (now().isoformat(timespec="seconds"),))
        await q.edit_message_text("✅ سُجّلت سيجارة.\n\n" + dashboard_text(), parse_mode="Markdown", reply_markup=main_kb())
        
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
        
    elif data == "debt":
        with db() as c:
            rows = c.execute("""SELECT e.id, e.amount, e.category, e.due_date, COALESCE(cr.name,'—') name
                                FROM expenses e LEFT JOIN creditors cr ON cr.id=e.creditor_id
                                WHERE e.ptype='credit' AND e.paid=0 ORDER BY e.id""").fetchall()
        if not rows:
            await q.answer("لا توجد ديون", show_alert=True); return
        lines = [f"#{r['id']} | {r['amount']:.0f} ر.س | {r['category']} | {r['name']} | {r['due_date'] or '—'}" for r in rows]
        await q.message.reply_text("📉 *الديون:*\n" + "\n".join(lines), parse_mode="Markdown")
        
    elif data == "report":
        await report(update, ctx)

async def add_exp_start_from_btn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await q.message.reply_text("💰 *إضافة مصروف*\nأرسل المبلغ بالريال (مثال: 25)", parse_mode="Markdown")
    return AMOUNT

if __name__ == "__main__":
    init()
    threading.Thread(target=run_web, daemon=True).start()
    app = Application.builder().token(TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("debt", debt_cmd))
    app.add_handler(CommandHandler("pay", pay))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(CommandHandler("expense", add_exp_start))
    
    app.add_handler(MessageHandler(filters.Regex(r"^(بداية|لوحة|اللوحة)$"), start))
    app.add_handler(MessageHandler(filters.Regex(r"^(ديون|الديون)$"), debt_cmd))
    app.add_handler(MessageHandler(filters.Regex(r"^(تقرير|التقرير)$"), report))
    app.add_handler(MessageHandler(filters.Regex(r"^(مصروف|اضافة مصروف)$"), add_exp_start))
    
    app.add_handler(MessageHandler(filters.Regex(r"^(1\+|سيجارة|دخنت)$"), cig))
    
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("expense", add_exp_start),
            CallbackQueryHandler(add_exp_start_from_btn, pattern="^add_exp$"),
        ],
        states={
            AMOUNT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, exp_amount)],
            CATEGORY: [CallbackQueryHandler(exp_category, pattern="^cat\\|")],
            PTYPE:    [CallbackQueryHandler(exp_ptype, pattern="^pt\\|")],
            CREDITOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, exp_creditor)],
            DUE:      [MessageHandler(filters.TEXT & ~filters.COMMAND, exp_due)],
        },
        fallbacks=[CommandHandler("cancel", cancel), MessageHandler(filters.Regex(r"^(إلغاء)$"), cancel)],
    )
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(on_button, pattern="^(cig|refresh|debt|report|pr\\|)"))
    
    print("✅ البوت يعمل...")
    app.run_polling()
