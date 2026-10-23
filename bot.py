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
(EXP_AMOUNT, EXP_CAT, EXP_PTYPE, EXP_VENDOR, EXP_DUE, 
 INC_AMOUNT, INC_SOURCE, 
 RATE_INPUT, LIMIT_INPUT,
 CIG_TYPE, CIG_VENDOR,
 VENDOR_ADD_NAME,
 LD_ADD_NAME, LD_ADD_AMT, LD_ADD_INSTALLMENT,
 PAY_DEBT_AMT, 
 ROLLOVER_CASH) = range(17)

def now():
    return datetime.now(TZ)

def get_ts():
    return now().strftime("%Y-%m-%d %H:%M:%S")

def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c

def init():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS cigarettes(id INTEGER PRIMARY KEY, ts TEXT, cig_type TEXT, cost REAL, vendor_id INTEGER);
        CREATE TABLE IF NOT EXISTS vendors(id INTEGER PRIMARY KEY, name TEXT UNIQUE);
        CREATE TABLE IF NOT EXISTS expenses(
            id INTEGER PRIMARY KEY, ts TEXT, amount REAL, category TEXT,
            ptype TEXT, vendor_id INTEGER, due_date TEXT, paid INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS prayers(date TEXT, name TEXT, status INTEGER, PRIMARY KEY(date, name));
        CREATE TABLE IF NOT EXISTS incomes(id INTEGER PRIMARY KEY, ts TEXT, amount REAL, source TEXT);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS long_debts(id INTEGER PRIMARY KEY, name TEXT, total_sar REAL, installment_sar REAL, paid_sar REAL DEFAULT 0);
        """)
        c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('exchange_rate', '140')")
        c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('daily_target', '15')")
        c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('cig_price', '1.0')")

def get_setting(key, default=None):
    with db() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return float(row["value"]) if row else default

def get_rate(): return get_setting('exchange_rate', 140.0)
def get_daily_target(): return int(get_setting('daily_target', 15.0))
def get_cig_price(): return get_setting('cig_price', 1.0)

def get_next_cig_time():
    t = now()
    target = get_daily_target()
    today_str = t.strftime("%Y-%m-%d")
    
    with db() as c:
        smoked = c.execute("SELECT COUNT(*) c FROM cigarettes WHERE ts LIKE ?", (f"{today_str}%",)).fetchone()["c"]
    
    remaining = max(0, target - smoked)
    if remaining <= 1: return "انتهى السقف اليومي"

    wake_1 = t.replace(hour=8, minute=30, second=0, microsecond=0)
    sleep_1 = t.replace(hour=14, minute=0, second=0, microsecond=0)
    wake_2 = t.replace(hour=17, minute=0, second=0, microsecond=0)
    sleep_2 = t.replace(hour=2, minute=0, second=0, microsecond=0) + timedelta(days=1)
    first_cig_time = t.replace(hour=9, minute=0, second=0, microsecond=0)
    
    current = t

    if current < wake_1: return "09:00 ص"
    if current < first_cig_time and smoked == 0: return "09:00 ص"
    
    if wake_1 <= current < sleep_1:
        rem_mins_1 = (sleep_1 - current).total_seconds() / 60
        rem_mins_2 = (sleep_2 - wake_2).total_seconds() / 60
        total_awake_mins = rem_mins_1 + rem_mins_2
    elif wake_2 <= current < sleep_2:
        total_awake_mins = (sleep_2 - current).total_seconds() / 60
    else:
        return "05:00 م"

    gap = total_awake_mins / remaining
    next_time = current + timedelta(minutes=gap)
    
    if sleep_1 <= next_time <= wake_2: next_time = wake_2
    if next_time > sleep_2: return "غداً 09:00 ص"

    return next_time.strftime("%I:%M %p").replace("AM", "ص").replace("PM", "م")

def cigs_today():
    today_str = now().strftime("%Y-%m-%d")
    with db() as c:
        rows = c.execute("SELECT ts FROM cigarettes WHERE ts LIKE ?", (f"{today_str}%",)).fetchall()
    return [datetime.strptime(r["ts"], "%Y-%m-%d %H:%M:%S") for r in rows]

def get_wallet_balance():
    with db() as c:
        inc = c.execute("SELECT COALESCE(SUM(amount),0) s FROM incomes").fetchone()["s"]
        exp = c.execute("SELECT COALESCE(SUM(amount),0) s FROM expenses WHERE ptype='cash'").fetchone()["s"]
        cigs_cash = c.execute("SELECT COALESCE(SUM(cost),0) s FROM cigarettes WHERE cig_type='cash'").fetchone()["s"]
        ld_paid = c.execute("SELECT COALESCE(SUM(paid_sar),0) s FROM long_debts").fetchone()["s"]
    return inc - exp - cigs_cash - ld_paid

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
                p_12 = datetime.strptime(p_time, fmt).strftime("%I:%M %p").replace("AM", "ص").replace("PM", "م")
                return f"⏳ القادمة: {p_name} - {p_12} (بعد {rem_str})"
        return "⏳ القادمة: الفجر (غداً)"
    except: return "⏳ تعذر جلب المواقيت"

def get_debt_summary():
    with db() as c:
        short_term = c.execute("SELECT COALESCE(SUM(amount),0) s FROM expenses WHERE ptype='credit' AND paid=0").fetchone()["s"]
        long_term_installments = c.execute("SELECT COALESCE(SUM(installment_sar),0) s FROM long_debts WHERE total_sar > paid_sar").fetchone()["s"]
        cig_debts = c.execute("SELECT COALESCE(SUM(cost),0) s FROM cigarettes WHERE cig_type='credit'").fetchone()["s"]
        
    return short_term + long_term_installments + cig_debts, short_term + cig_debts, long_term_installments

def get_detailed_debt_report():
    with db() as c:
        # ديون المصاريف الآجلة
        exp_debts = c.execute("SELECT v.name, SUM(e.amount) as amt FROM expenses e JOIN vendors v ON e.vendor_id=v.id WHERE e.ptype='credit' AND e.paid=0 GROUP BY v.id").fetchall()
        # ديون السجائر الآجلة
        cig_debts = c.execute("SELECT v.name, SUM(cig.cost) as amt FROM cigarettes cig JOIN vendors v ON cig.vendor_id=v.id WHERE cig.cig_type='credit' GROUP BY v.id").fetchall()
        # الديون طويلة الأجل
        long_debts = c.execute("SELECT name, total_sar, installment_sar, paid_sar FROM long_debts WHERE total_sar > paid_sar").fetchall()
        
    short_dict = {}
    for r in exp_debts: short_dict[r["name"]] = short_dict.get(r["name"], 0) + r["amt"]
    for r in cig_debts: short_dict[r["name"]] = short_dict.get(r["name"], 0) + r["amt"]
        
    text = "📉 *تقرير الديون المفصل*\n"
    text += "━━━━━━━━━━━━━━━━━━━━━\n"
    text += "🔴 *الديون قصيرة الأجل:*\n"
    if not short_dict: text += "لا توجد ديون قصيرة الأجل.\n"
    else:
        for name, amt in short_dict.items(): text += f"• {name}: {amt:,.0f} ر.س\n"
            
    text += "\n🟡 *الديون طويلة الأجل (أقساط):*\n"
    if not long_debts: text += "لا توجد ديون طويلة الأجل.\n"
    else:
        for r in long_debts:
            rem = r["total_sar"] - r["paid_sar"]
            text += f"• {r['name']}: المتبقي {rem:,.0f} ر.س | القسط {r['installment_sar']:,.0f} ر.س\n"
    return text

def get_expense_breakdown(period="month"):
    with db() as c:
        if period == "month":
            month_str = now().strftime("%Y-%m")
            query = "SELECT category, SUM(amount) as total FROM expenses WHERE ts LIKE ? GROUP BY category"
            rows = c.execute(query, (f"{month_str}%",)).fetchall()
            cig_cash = c.execute("SELECT SUM(cost) s FROM cigarettes WHERE ts LIKE ? AND cig_type='cash'", (f"{month_str}%",)).fetchone()["s"] or 0
        else:
            rows = c.execute("SELECT category, SUM(amount) as total FROM expenses GROUP BY category").fetchall()
            cig_cash = c.execute("SELECT SUM(cost) s FROM cigarettes WHERE cig_type='cash'").fetchone()["s"] or 0
            
    if rows: rows = [dict(r) for r in rows]
    else: rows = []
        
    if cig_cash > 0:
        found = False
        for r in rows:
            if r["category"] == "دخان":
                r["total"] += cig_cash; found = True; break
        if not found: rows.append({"category": "دخان", "total": cig_cash})
            
    total_sar = sum(r["total"] for r in rows)
    if total_sar == 0: return "لا توجد بيانات.", 0
    
    lines = []
    for r in sorted(rows, key=lambda x: x["total"], reverse=True):
        pct = (r["total"] / total_sar) * 100
        lines.append(f"• {r['category']}: {r['total']:.0f} ر.س | {pct:.0f}%")
    return "\n".join(lines), total_sar

def dashboard_text():
    ts = cigs_today(); target = get_daily_target()
    smoked = len(ts); remaining = max(0, target - smoked)
    compliance_pct = (remaining / target) * 100 if target > 0 else 0
    next_cig = get_next_cig_time()
    wallet = get_wallet_balance()
    rate = get_rate()
    total_debt, short_debt, long_installments = get_debt_summary()
    prayers = get_prayers()
    p_icons = " ".join([f"{'✅' if v else '⬜'} {k}" for k, v in prayers.items()])
    next_prayer_text = get_next_prayer_al_qurayyat()

    return (
        f"📅 *{now().strftime('%A %d %B %Y | %I:%M %p').replace('AM', 'ص').replace('PM', 'م')}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 *المحفظة:* {wallet:,.2f} ر.س\n"
        f"💱 *سعر الصرف:* 1 ر.س = {rate} ج.س\n"
        f"📉 *إجمالي الالتزامات المستحقة:* {total_debt:,.0f} ر.س\n"
        f"   _(قصيرة: {short_debt:,.0f} | قسط طويل: {long_installments:,.0f})_\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🚬 *التدخين اليوم:* {smoked} من {target}\n"
        f"⏰ *القادمة:* {next_cig}\n"
        f"🛡️ *الالتزام:* {compliance_pct:.0f}%\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕌 *الصلوات:*\n"
        f"{p_icons}\n"
        f"{next_prayer_text}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🚀 رحلة النجاح مستمرة | *صالح محمد علي*"
    )

def main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚬 سيجارة +1", callback_data="cig")],
        [InlineKeyboardButton("💰 مصروف", callback_data="add_exp_btn"), InlineKeyboardButton("💵 إيداع راتب", callback_data="add_inc_btn")],
        [InlineKeyboardButton("💼 إدارة الديون", callback_data="debt_menu"), InlineKeyboardButton("📊 التقارير", callback_data="report_menu")],
        [InlineKeyboardButton("⚙️ الإعدادات", callback_data="settings_menu"), InlineKeyboardButton("🔄 تحديث", callback_data="refresh")],
    ])

def debt_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📉 عرض الديون الحالية", callback_data="show_debts")],
        [InlineKeyboardButton("💸 تسديد دين", callback_data="pay_debt_menu")],
        [InlineKeyboardButton("➕ إضافة دين طويل الأجل", callback_data="add_long_debt")],
        [InlineKeyboardButton("🏪 إدارة الجهات (بقالة/مطاعم)", callback_data="vendor_menu")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="refresh")]
    ])

def report_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧾 بيان المصروفات (هذا الشهر)", callback_data="rep_expenses_month")],
        [InlineKeyboardButton("📈 التقرير الشهري الشامل", callback_data="rep_month")],
        [InlineKeyboardButton("📉 تقرير الديون", callback_data="show_debts")],
        [InlineKeyboardButton("🚬 تقرير التدخين", callback_data="rep_cigs")],
        [InlineKeyboardButton("🔄 ترحيل الرصيد للشهر القادم", callback_data="do_rollover")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="refresh")]
    ])

def settings_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💱 تعديل سعر الصرف", callback_data="edit_rate_btn")],
        [InlineKeyboardButton("🎯 تعديل سقف التدخين", callback_data="edit_limit_btn")],
        [InlineKeyboardButton("✏️ تصحيح البيانات (حذف آخر)", callback_data="edit_menu")],
        [InlineKeyboardButton("🧹 تصفير كافة السجلات", callback_data="reset_all_menu")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="refresh")]
    ])

def vendor_menu_kb():
    with db() as c:
        rows = c.execute("SELECT id, name FROM vendors").fetchall()
    kb = []
    for r in rows: kb.append([InlineKeyboardButton(f"🏪 {r['name']}", callback_data=f"vendor_view|{r['id']}")])
    kb.append([InlineKeyboardButton("➕ إضافة جهة جديدة", callback_data="add_vendor")])
    kb.append([InlineKeyboardButton("🔙 رجوع", callback_data="debt_menu")])
    return InlineKeyboardMarkup(kb)

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(dashboard_text(), parse_mode="Markdown", reply_markup=main_kb())

# ================== EXPENSE ==================
async def add_exp_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q: await q.answer()
    msg = "💰 *إضافة مصروف*\nأرسل المبلغ بالريال:"
    if q: await q.edit_message_text(msg, parse_mode="Markdown")
    else: await update.message.reply_text(msg, parse_mode="Markdown")
    return EXP_AMOUNT

async def exp_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: 
        amt = float(update.message.text.strip())
        if amt <= 0: raise ValueError
        ctx.user_data["amount"] = amt
    except ValueError: return await update.message.reply_text("⚠️ أرسل رقماً صحيحاً أكبر من صفر.")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("أكل", callback_data="cat|أكل"), InlineKeyboardButton("بقالة", callback_data="cat|بقالة"), InlineKeyboardButton("منظفات", callback_data="cat|منظفات")],
        [InlineKeyboardButton("مواصلات", callback_data="cat|مواصلات"), InlineKeyboardButton("دخان", callback_data="cat|دخان"), InlineKeyboardButton("أخرى", callback_data="cat|أخرى")]
    ])
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
    ptype = q.data.split("|",1)[1]
    if ptype == "cash":
        with db() as c: c.execute("INSERT INTO expenses(ts,amount,category,ptype,paid) VALUES(?,?,?,?,1)", (get_ts(), ctx.user_data["amount"], ctx.user_data["category"], "cash"))
        await q.edit_message_text("✅ تم تسجيل المصروف النقدي.", reply_markup=main_kb())
        return ConversationHandler.END
    
    ctx.user_data["ptype"] = "credit"
    with db() as c: rows = c.execute("SELECT id, name FROM vendors").fetchall()
    if not rows:
        await q.edit_message_text("⚠️ لا توجد جهات مسجلة. يرجى إضافتها من إدارة الديون أولاً.", reply_markup=main_kb())
        return ConversationHandler.END
        
    kb = [[InlineKeyboardButton(r["name"], callback_data=f"ven|{r['id']}")] for r in rows]
    await q.edit_message_text("🏪 اختر الجهة (الدائن):", reply_markup=InlineKeyboardMarkup(kb))
    return EXP_VENDOR

async def exp_vendor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    vid = q.data.split("|")[1]
    with db() as c: c.execute("INSERT INTO expenses(ts,amount,category,ptype,vendor_id,paid) VALUES(?,?,?,?,?,0)", (get_ts(), ctx.user_data["amount"], ctx.user_data["category"], "credit", vid))
    await q.edit_message_text("✅ تم تسجيل المصروف الآجل.", reply_markup=main_kb())
    return ConversationHandler.END

# ================== INCOME ==================
async def add_inc_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q: await q.answer()
    msg = "💵 *إضافة إيراد*\nأرسل المبلغ بالريال:"
    if q: await q.edit_message_text(msg, parse_mode="Markdown")
    else: await update.message.reply_text(msg, parse_mode="Markdown")
    return INC_AMOUNT

async def inc_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: 
        amt = float(update.message.text.strip())
        if amt <= 0: raise ValueError
        ctx.user_data["inc_amount"] = amt
    except ValueError: return await update.message.reply_text("⚠️ أرسل رقماً صحيحاً أكبر من صفر.")
    await update.message.reply_text("📝 اكتب مصدر الإيراد (مثال: راتب):")
    return INC_SOURCE

async def inc_source(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with db() as c: c.execute("INSERT INTO incomes(ts, amount, source) VALUES(?,?,?)", (get_ts(), ctx.user_data["inc_amount"], update.message.text.strip()))
    await update.message.reply_text("✅ تم الإيداع.\n\n" + dashboard_text(), parse_mode="Markdown", reply_markup=main_kb())
    return ConversationHandler.END

# ================== CIGARETTE ==================
async def cig_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    target = get_daily_target()
    if len(cigs_today()) >= target:
        await q.answer("⚠️ وصلت السقف اليومي!", show_alert=True); return ConversationHandler.END
        
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💵 نقدي (من جيبي)", callback_data="cig_type|cash")],
        [InlineKeyboardButton("📝 آجل (على الحساب)", callback_data="cig_type|credit")],
        [InlineKeyboardButton("🎁 مجاناً", callback_data="cig_type|free")]
    ])
    await q.edit_message_text("🚬 كيف حصلت على هذه السيجارة؟", reply_markup=kb)
    return CIG_TYPE

async def cig_type(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    ctype = q.data.split("|")[1]
    cost = get_cig_price()
    
    if ctype == "cash":
        with db() as c: c.execute("INSERT INTO cigarettes(ts, cig_type, cost) VALUES(?,?,?)", (get_ts(), "cash", cost))
        await q.edit_message_text("✅ تم الخصم من المحفظة.\n\n" + dashboard_text(), parse_mode="Markdown", reply_markup=main_kb())
        return ConversationHandler.END
    elif ctype == "free":
        with db() as c: c.execute("INSERT INTO cigarettes(ts, cig_type, cost) VALUES(?,?,?)", (get_ts(), "free", 0))
        await q.edit_message_text("✅ تم التسجيل مجاناً.\n\n" + dashboard_text(), parse_mode="Markdown", reply_markup=main_kb())
        return ConversationHandler.END
        
    with db() as c: rows = c.execute("SELECT id, name FROM vendors").fetchall()
    if not rows:
        await q.edit_message_text("⚠️ لا توجد جهات مسجلة.", reply_markup=main_kb())
        return ConversationHandler.END
    kb = [[InlineKeyboardButton(r["name"], callback_data=f"cigven|{r['id']}")] for r in rows]
    await q.edit_message_text("🏪 اختر الجهة (البقالة):", reply_markup=InlineKeyboardMarkup(kb))
    return CIG_VENDOR

async def cig_vendor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    vid = q.data.split("|")[1]
    with db() as c: c.execute("INSERT INTO cigarettes(ts, cig_type, cost, vendor_id) VALUES(?,?,?,?)", (get_ts(), "credit", get_cig_price(), vid))
    await q.edit_message_text("✅ تم إضافتها لديون الجهة.\n\n" + dashboard_text(), parse_mode="Markdown", reply_markup=main_kb())
    return ConversationHandler.END

# ================== VENDORS ==================
async def add_vendor_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await q.edit_message_text("🏪 أرسل اسم الجهة الجديدة (مثال: بقالة الأمانة):")
    return VENDOR_ADD_NAME

async def vendor_add_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    with db() as c: c.execute("INSERT OR IGNORE INTO vendors(name) VALUES(?)", (name,))
    await update.message.reply_text(f"✅ تم إضافة {name}.", reply_markup=debt_menu_kb())
    return ConversationHandler.END

# ================== LONG DEBT ==================
async def ld_add_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await q.edit_message_text("⏳ *إضافة دين طويل الأجل*\nأرسل اسم الدين (مثال: قرض):")
    return LD_ADD_NAME

async def ld_add_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["ld_name"] = update.message.text.strip()
    await update.message.reply_text("أرسل المبلغ الكلي للدين بالريال:")
    return LD_ADD_AMT

async def ld_add_amt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: 
        amt = float(update.message.text.strip())
        if amt <= 0: raise ValueError
        ctx.user_data["ld_amt"] = amt
    except ValueError: return await update.message.reply_text("⚠️ أرسل رقماً صحيحاً أكبر من صفر.")
    await update.message.reply_text("أرسل مبلغ القسط المستحق حالياً (بالريال):")
    return LD_ADD_INSTALLMENT

async def ld_add_installment(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: 
        inst = float(update.message.text.strip())
        if inst <= 0: raise ValueError
    except ValueError: return await update.message.reply_text("⚠️ أرسل رقماً صحيحاً أكبر من صفر.")
    with db() as c: c.execute("INSERT INTO long_debts(name, total_sar, installment_sar) VALUES(?,?,?)", (ctx.user_data["ld_name"], ctx.user_data["ld_amt"], inst))
    await update.message.reply_text("✅ تم الإضافة.", reply_markup=debt_menu_kb())
    return ConversationHandler.END

# ================== PAY DEBT ==================
async def pay_debt_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    with db() as c:
        vendors_debts = c.execute("""
            SELECT v.id, v.name, COALESCE(SUM(e.amount),0) + COALESCE((SELECT SUM(cost) FROM cigarettes WHERE vendor_id=v.id AND cig_type='credit'),0) as tot
            FROM vendors v 
            LEFT JOIN expenses e ON e.vendor_id = v.id AND e.ptype='credit' AND e.paid=0
            GROUP BY v.id HAVING tot > 0
        """).fetchall()
        long_debts = c.execute("SELECT id, name, installment_sar FROM long_debts WHERE total_sar > paid_sar").fetchall()
        
    kb = []
    for r in vendors_debts: kb.append([InlineKeyboardButton(f"🏪 {r['name']} - {r['tot']:.0f} ر.س", callback_data=f"p_debt_v|{r['id']}")])
    for r in long_debts: kb.append([InlineKeyboardButton(f"⏳ {r['name']} - قسط {r['installment_sar']:.0f} ر.س", callback_data=f"p_debt_l|{r['id']}")])
        
    kb.append([InlineKeyboardButton("🔙 رجوع", callback_data="debt_menu")])
    await q.edit_message_text("💸 اختر الدين الذي تريد سداده:", reply_markup=InlineKeyboardMarkup(kb))
    return ConversationHandler.END

async def pay_debt_amt_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    data = q.data.split("|")
    dtype, did = data[0], data[1]
    
    if dtype == "p_debt_v":
        ctx.user_data["pay_type"] = "vendor"; ctx.user_data["pay_id"] = did
        await q.edit_message_text("💸 كم تريد أن تسدد لهذه الجهة؟ (أرسل المبلغ):")
    else:
        ctx.user_data["pay_type"] = "long"; ctx.user_data["pay_id"] = did
        await q.edit_message_text("💸 كم تريد أن تسدد من القسط؟ (أرسل المبلغ):")
    return PAY_DEBT_AMT

async def pay_debt_amt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: 
        amt = float(update.message.text.strip())
        if amt <= 0: raise ValueError
    except ValueError: return await update.message.reply_text("⚠️ أرسل رقماً صحيحاً أكبر من صفر.")
    
    ptype = ctx.user_data["pay_type"]; pid = ctx.user_data["pay_id"]
    
    with db() as c:
        if ptype == "vendor":
            remaining = amt
            rows = c.execute("SELECT id, amount FROM expenses WHERE vendor_id=? AND ptype='credit' AND paid=0 ORDER BY id ASC", (pid,)).fetchall()
            for r in rows:
                if remaining <= 0: break
                deduct = min(r["amount"], remaining)
                if deduct == r["amount"]: c.execute("UPDATE expenses SET paid=1 WHERE id=?", (r["id"],))
                else: c.execute("UPDATE expenses SET amount = amount - ? WHERE id=?", (deduct, r["id"]))
                remaining -= deduct
            
            cigs = c.execute("SELECT id, cost FROM cigarettes WHERE vendor_id=? AND cig_type='credit' ORDER BY id ASC", (pid,)).fetchall()
            for r in cigs:
                if remaining <= 0: break
                deduct = min(r["cost"], remaining)
                if deduct == r["cost"]: c.execute("DELETE FROM cigarettes WHERE id=?", (r["id"],))
                else: c.execute("UPDATE cigarettes SET cost = cost - ? WHERE id=?", (deduct, r["id"]))
                remaining -= deduct
        else:
            c.execute("UPDATE long_debts SET paid_sar = paid_sar + ? WHERE id=?", (amt, pid))
            
        c.execute("INSERT INTO expenses(ts, amount, category, ptype, paid) VALUES(?,?,?,?,1)", (get_ts(), amt, "سداد ديون", "cash"))

    await update.message.reply_text("✅ تم تسجيل السداد وخصمه من المحفظة.", reply_markup=main_kb())
    return ConversationHandler.END

# ================== SETTINGS ==================
async def edit_rate_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await q.edit_message_text("💱 أرسل سعر الصرف الجديد (كم جنيه يساوي 1 ريال؟):")
    return RATE_INPUT

async def save_rate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: 
        rate = float(update.message.text.strip())
        if rate <= 0: raise ValueError
    except ValueError: return await update.message.reply_text("⚠️ أرسل رقماً صحيحاً أكبر من صفر.")
    with db() as c: c.execute("UPDATE settings SET value=? WHERE key='exchange_rate'", (str(rate),))
    await update.message.reply_text("✅ تم تحديث السعر.", reply_markup=main_kb())
    return ConversationHandler.END

async def edit_limit_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await q.edit_message_text("🎯 أرسل السقف اليومي الجديد للتدخين:")
    return LIMIT_INPUT

async def save_limit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: 
        limit = int(update.message.text.strip())
        if limit <= 0: raise ValueError
    except ValueError: return await update.message.reply_text("⚠️ أرسل رقماً صحيحاً أكبر من صفر.")
    with db() as c: c.execute("UPDATE settings SET value=? WHERE key='daily_target'", (str(limit),))
    await update.message.reply_text("✅ تم تحديث السقف.", reply_markup=main_kb())
    return ConversationHandler.END

# ================== REPORTS ==================
async def rep_expenses(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    text, total = get_expense_breakdown("month")
    rate = get_rate()
    msg = f"🧾 *بيان مصروفات الشهر:*\n💵 الإجمالي: {total:,.0f} ر.س ({total*rate:,.0f} ج.س)\n\n{text}"
    await q.edit_message_text(msg, parse_mode="Markdown", reply_markup=report_menu_kb())

# ================== ROLLOVER ==================
async def rollover_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    wallet = get_wallet_balance()
    if wallet <= 0:
        await q.edit_message_text("⚠️ رصيدك الحالي صفر أو سالب، لا يوجد رصيد لترحيله.", reply_markup=main_kb())
        return ConversationHandler.END
    
    await q.edit_message_text(f"🔄 *الترحيل للشهر القادم*\nرصيدك الحالي هو {wallet:,.2f} ر.س.\nكم تريد أن ترحل كرصيد افتتاحي؟ (أرسل المبلغ):", parse_mode="Markdown")
    return ROLLOVER_CASH

async def rollover_cash(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: 
        amt = float(update.message.text.strip())
        if amt <= 0: raise ValueError
    except ValueError: return await update.message.reply_text("⚠️ أرسل رقماً صحيحاً أكبر من صفر (أو اضغط إلغاء).")
    
    with db() as c: c.execute("INSERT INTO incomes(ts, amount, source) VALUES(?,?,?)", (get_ts(), amt, "رصيد افتتاحي مرحل"))
    await update.message.reply_text(f"✅ تم ترحيل {amt:,.2f} ر.س كإيراد جديد للشهر الجديد.", reply_markup=main_kb())
    return ConversationHandler.END

# ================== MAIN HANDLER ==================
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q: await q.answer()
    data = q.data
    
    if data == "noop": return

    if data == "refresh":
        await q.edit_message_text(dashboard_text(), parse_mode="Markdown", reply_markup=main_kb())
    elif data == "debt_menu":
        await q.edit_message_text("💼 *إدارة الديون*", parse_mode="Markdown", reply_markup=debt_menu_kb())
    elif data == "report_menu":
        await q.edit_message_text("📊 *التقارير*", parse_mode="Markdown", reply_markup=report_menu_kb())
    elif data == "settings_menu":
        await q.edit_message_text("⚙️ *الإعدادات*", parse_mode="Markdown", reply_markup=settings_menu_kb())
    elif data == "vendor_menu":
        await q.edit_message_text("🏪 *إدارة الجهات*", parse_mode="Markdown", reply_markup=vendor_menu_kb())
    
    elif data == "show_debts":
        report = get_detailed_debt_report()
        await q.edit_message_text(report, parse_mode="Markdown", reply_markup=debt_menu_kb())
        
    elif data.startswith("pr|"):
        p_name = data.split("|")[1]
        today = now().date().isoformat()
        with db() as c:
            row = c.execute("SELECT status FROM prayers WHERE date=? AND name=?", (today, p_name)).fetchone()
            new_status = 1 if not row or row["status"] == 0 else 0
            c.execute("INSERT OR REPLACE INTO prayers(date, name, status) VALUES(?,?,?)", (today, p_name, new_status))
        await q.edit_message_text(dashboard_text(), parse_mode="Markdown", reply_markup=main_kb())

    elif data == "edit_menu":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑️ حذف آخر مصروف", callback_data="del_exp"), InlineKeyboardButton("🗑️ حذف آخر سيجارة", callback_data="del_cig")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="settings_menu")]
        ])
        await q.edit_message_text("✏️ *التصحيح*", reply_markup=kb)
    elif data == "del_exp":
        with db() as c:
            row = c.execute("SELECT id FROM expenses ORDER BY id DESC LIMIT 1").fetchone()
            if row: c.execute("DELETE FROM expenses WHERE id=?", (row["id"],))
        await q.edit_message_text("✅ تم حذف آخر مصروف.", reply_markup=settings_menu_kb())
    elif data == "del_cig":
        with db() as c:
            row = c.execute("SELECT id FROM cigarettes ORDER BY id DESC LIMIT 1").fetchone()
            if row: c.execute("DELETE FROM cigarettes WHERE id=?", (row["id"],))
        await q.edit_message_text("✅ تم حذف آخر سيجارة.", reply_markup=settings_menu_kb())
        
    elif data == "reset_all_menu":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⚠️ نعم، امسح كل شيء", callback_data="do_reset_all")], [InlineKeyboardButton("🔙 إلغاء", callback_data="refresh")]])
        await q.edit_message_text("⚠️ مسح كل السجلات نهائياً؟", reply_markup=kb)
    elif data == "do_reset_all":
        with db() as c:
            c.execute("DELETE FROM cigarettes"); c.execute("DELETE FROM expenses"); c.execute("DELETE FROM prayers"); c.execute("DELETE FROM incomes"); c.execute("DELETE FROM long_debts"); c.execute("DELETE FROM vendors")
        await q.edit_message_text("🧹 *تم مسح كل شيء!*", reply_markup=main_kb())

    elif data == "rep_expenses_month": await rep_expenses(update, ctx)
    elif data.startswith("p_debt_"): await pay_debt_amt_start(update, ctx)

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ تم الإلغاء.", reply_markup=main_kb())
    return ConversationHandler.END

if __name__ == "__main__":
    init()
    threading.Thread(target=run_web, daemon=True).start()
    app = Application.builder().token(TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    
    convs = [
        ConversationHandler(entry_points=[CallbackQueryHandler(add_exp_start, pattern="^add_exp_btn$"), CommandHandler("expense", add_exp_start)], states={EXP_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, exp_amount)], EXP_CAT: [CallbackQueryHandler(exp_category, pattern="^cat\\|")], EXP_PTYPE: [CallbackQueryHandler(exp_ptype, pattern="^pt\\|")], EXP_VENDOR: [CallbackQueryHandler(exp_vendor, pattern="^ven\\|")]}, fallbacks=[CommandHandler("cancel", cancel)]),
        ConversationHandler(entry_points=[CallbackQueryHandler(add_inc_start, pattern="^add_inc_btn$")], states={INC_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, inc_amount)], INC_SOURCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, inc_source)]}, fallbacks=[CommandHandler("cancel", cancel)]),
        ConversationHandler(entry_points=[CallbackQueryHandler(cig_start, pattern="^cig$")], states={CIG_TYPE: [CallbackQueryHandler(cig_type, pattern="^cig_type\\|")], CIG_VENDOR: [CallbackQueryHandler(cig_vendor, pattern="^cigven\\|")]}, fallbacks=[CommandHandler("cancel", cancel)]),
        ConversationHandler(entry_points=[CallbackQueryHandler(add_vendor_start, pattern="^add_vendor$")], states={VENDOR_ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, vendor_add_name)]}, fallbacks=[CommandHandler("cancel", cancel)]),
        ConversationHandler(entry_points=[CallbackQueryHandler(ld_add_start, pattern="^add_long_debt$")], states={LD_ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ld_add_name)], LD_ADD_AMT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ld_add_amt)], LD_ADD_INSTALLMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ld_add_installment)]}, fallbacks=[CommandHandler("cancel", cancel)]),
        ConversationHandler(entry_points=[CallbackQueryHandler(pay_debt_start, pattern="^pay_debt_menu$"), CallbackQueryHandler(pay_debt_amt_start, pattern="^p_debt_")], states={PAY_DEBT_AMT: [MessageHandler(filters.TEXT & ~filters.COMMAND, pay_debt_amt)]}, fallbacks=[CommandHandler("cancel", cancel)]),
        ConversationHandler(entry_points=[CallbackQueryHandler(edit_rate_start, pattern="^edit_rate_btn$")], states={RATE_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_rate)]}, fallbacks=[CommandHandler("cancel", cancel)]),
        ConversationHandler(entry_points=[CallbackQueryHandler(edit_limit_start, pattern="^edit_limit_btn$")], states={LIMIT_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_limit)]}, fallbacks=[CommandHandler("cancel", cancel)]),
        ConversationHandler(entry_points=[CallbackQueryHandler(rollover_start, pattern="^do_rollover$")], states={ROLLOVER_CASH: [MessageHandler(filters.TEXT & ~filters.COMMAND, rollover_cash)]}, fallbacks=[CommandHandler("cancel", cancel)]),
    ]
    for conv in convs: app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(on_button))
    print("✅ النظام المتكامل يعمل...")
    app.run_polling()
