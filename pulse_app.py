import smbus2
import time
import sys
import asyncio
import firebase_admin
from firebase_admin import credentials, db
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler, CallbackQueryHandler
)
from telegram.request import HTTPXRequest
import lcddriver
from datetime import datetime, timedelta
import statistics
import os
from dotenv import load_dotenv
load_dotenv()
# ─────────────────────────────────────────────
#  Firebase
# ─────────────────────────────────────────────
try:
    cred = credentials.Certificate("serviceAccountKey.json")
    firebase_admin.initialize_app(cred, {
        'databaseURL': 'https://iot-bpm-7e352-default-rtdb.firebaseio.com/'
    })
except Exception as e:
    print(f"Firebase Error: {e}")
    sys.exit(1)

# ─────────────────────────────────────────────
#  הגדרות טלגרם
# ─────────────────────────────────────────────
TOKEN = os.getenv('TELEGRAM_TOKEN')
request_config = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
application = ApplicationBuilder().token(TOKEN).request(request_config).build()

# ─────────────────────────────────────────────
#  חיישן ו-LCD
# ─────────────────────────────────────────────
ADDR = 0x57
bus  = smbus2.SMBus(1)
lcd  = lcddriver.LCD()
is_measuring = False

# ─────────────────────────────────────────────
#  סטטוסים לשיחת הרשמה
# ─────────────────────────────────────────────
REG_NAME, REG_AGE, REG_GENDER, REG_WEIGHT, REG_HEIGHT = range(5)

# ─────────────────────────────────────────────
#  ערכי סף לדופק ו-SpO2
# ─────────────────────────────────────────────
BPM_LOW          = 50
BPM_HIGH         = 100
BPM_PERSONAL_DEV = 15   # סטייה מהממוצע האישי (BPM)
SPO2_LOW         = 95   # % — מתחת לזה: התראה

# ─────────────────────────────────────────────
#  מקלדת ראשית
# ─────────────────────────────────────────────
main_keyboard = [
    ['❤️ מדידה', '📊 דוח שבועי'],
    ['👤 הפרופיל שלי', '⚙️ הגדרות']
]
markup = ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True)


# ══════════════════════════════════════════════
#  עזרים
# ══════════════════════════════════════════════

def get_user_profile(user_id: int) -> dict:
    """מחזיר את פרופיל המשתמש מ-Firebase, או dict ריק."""
    snap = db.reference(f'users/{user_id}/profile').get()
    return snap if snap else {}


def get_user_measurements(user_id: int, days: int = 7) -> list:
    """מחזיר רשימת מדידות מה-N ימים האחרונים."""
    all_data = db.reference(f'users/{user_id}/measurements').get()
    if not all_data:
        return []
    cutoff = datetime.now() - timedelta(days=days)
    result = []
    for val in all_data.values():
        try:
            ts = datetime.strptime(val['timestamp'], '%Y-%m-%d %H:%M:%S')
            if ts > cutoff:
                result.append(val)
        except Exception:
            pass
    return result


def classify_bpm(bpm: int) -> str:
    if bpm < 40:
        return "⚠️ ברדיקרדיה חמורה (דופק נמוך מאוד)"
    if bpm < BPM_LOW:
        return "⬇️ דופק נמוך"
    if bpm <= BPM_HIGH:
        return "✅ תקין"
    if bpm <= 120:
        return "⬆️ דופק גבוה"
    return "🚨 טכיקרדיה (דופק מהיר מאוד)"


def classify_spo2(spo2: float) -> str:
    if spo2 >= 97:
        return "✅ תקין"
    if spo2 >= SPO2_LOW:
        return "⚠️ נמוך מעט מהנורמה"
    return "🚨 נמוך מהנורמה"


def estimate_spo2(ir_vals: list, red_vals: list) -> float | None:
    """
    אומדן SpO2 גס לפי יחס AC/DC של IR ו-Red.
    ⚠️  לצורכי לימוד בלבד — אינו מחליף מכשיר רפואי.
    """
    if len(ir_vals) < 20 or len(red_vals) < 20:
        return None
    try:
        ir_ac  = max(ir_vals)  - min(ir_vals)
        red_ac = max(red_vals) - min(red_vals)
        ir_dc  = sum(ir_vals)  / len(ir_vals)
        red_dc = sum(red_vals) / len(red_vals)
        if ir_dc == 0 or red_dc == 0 or ir_ac == 0:
            return None
        r = (red_ac / red_dc) / (ir_ac / ir_dc)
        # משוואה אמפירית מקובלת: SpO2 ≈ 110 - 25·R
        spo2 = 110.0 - 25.0 * r
        return round(max(70.0, min(100.0, spo2)), 1)
    except Exception:
        return None


async def send_alert(user_id: int, message: str, context: ContextTypes.DEFAULT_TYPE):
    """שולח התראה למשתמש."""
    try:
        await context.bot.send_message(chat_id=user_id, text=f"🚨 *התראה!*\n{message}",
                                       parse_mode='Markdown')
    except Exception as e:
        print(f"Alert error: {e}")


# ══════════════════════════════════════════════
#  הרשמה — ConversationHandler
# ══════════════════════════════════════════════

async def register_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = get_user_profile(update.effective_user.id)
    if profile.get('registered'):
        await update.message.reply_text(
            "כבר רשום/ה! רוצה לעדכן פרטים?\nשלח/י /register שוב לאיפוס הפרופיל.",
            reply_markup=markup
        )
        return ConversationHandler.END

    await update.message.reply_text("מה שמך המלא?")
    return REG_NAME


async def reg_get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['reg_name'] = update.message.text.strip()
    await update.message.reply_text("מה גילך? (מספר שנים)")
    return REG_AGE


async def reg_get_age(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        age = int(update.message.text.strip())
        if not (1 <= age <= 120):
            raise ValueError
        context.user_data['reg_age'] = age
    except ValueError:
        await update.message.reply_text("נא להזין מספר תקין בין 1 ל-120.")
        return REG_AGE

    kb = ReplyKeyboardMarkup([['זכר', 'נקבה', 'אחר']], resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text("מה המין שלך?", reply_markup=kb)
    return REG_GENDER


async def reg_get_gender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['reg_gender'] = update.message.text.strip()
    await update.message.reply_text('מה משקלך? (ק"ג, לדוגמה: 70)')
    return REG_WEIGHT


async def reg_get_weight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        w = float(update.message.text.strip())
        if not (20 <= w <= 300):
            raise ValueError
        context.user_data['reg_weight'] = w
    except ValueError:
        await update.message.reply_text("נא להזין משקל תקין.")
        return REG_WEIGHT
    await update.message.reply_text('מה גובהך? (ס"מ, לדוגמה: 175)')
    return REG_HEIGHT


async def reg_get_height(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        h = float(update.message.text.strip())
        if not (50 <= h <= 250):
            raise ValueError
        context.user_data['reg_height'] = h
    except ValueError:
        await update.message.reply_text("נא להזין גובה תקין.")
        return REG_HEIGHT

    uid  = update.effective_user.id
    data = context.user_data

    # חישוב BMI
    bmi = round(data['reg_weight'] / ((data['reg_height'] / 100) ** 2), 1)

    db.reference(f'users/{uid}/profile').set({
        'first_name':  update.effective_user.first_name,
        'full_name':   data['reg_name'],
        'age':         data['reg_age'],
        'gender':      data['reg_gender'],
        'weight':      data['reg_weight'],
        'height':      data['reg_height'],
        'bmi':         bmi,
        'registered':  True,
        'joined':      time.strftime('%Y-%m-%d %H:%M:%S'),
        'last_login':  time.strftime('%Y-%m-%d %H:%M:%S'),
        'alerts_enabled': True
    })

    await update.message.reply_text(
        f"✅ *ההרשמה הושלמה!*\n\n"
        f"👤 שם: {data['reg_name']}\n"
        f"🎂 גיל: {data['reg_age']}\n"
        f"⚖️  BMI: {bmi}\n\n"
        "מעכשיו תקבל/י התראות מותאמות אישית על פי ההיסטוריה שלך.",
        parse_mode='Markdown',
        reply_markup=markup
    )
    return ConversationHandler.END


async def reg_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("ההרשמה בוטלה.", reply_markup=markup)
    return ConversationHandler.END


# ══════════════════════════════════════════════
#  /start
# ══════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    profile = get_user_profile(user.id)

    db.reference(f'users/{user.id}/profile').update({
        'first_name': user.first_name,
        'last_login': time.strftime('%Y-%m-%d %H:%M:%S')
    })

    if not profile.get('registered'):
        # משתמש חדש — הודעת פתיחה ומיד מתחיל הרשמה
        await update.message.reply_text(
            f"👋 שלום {user.first_name}, ברוך הבא/ברוכה הבאה ל-*HeartWatch!* ❤️\n\n"
            "🩺 אני עוזר לך לעקוב אחרי *הדופק* ורמת *החמצן בדם* שלך,\n"
            "ולשלוח לך התראות חכמות על פי ההיסטוריה האישית שלך.\n\n"
            "כדי להתחיל נעשה הרשמה קצרה — רק 5 שאלות פשוטות 🙂",
            parse_mode='Markdown'
        )
        # מיד מפעיל את תהליך ההרשמה
        await update.message.reply_text("מה שמך המלא?")
        return REG_NAME
    else:
        # משתמש חוזר — ברוך הבא עם כפתור Start
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🚀 התחל", callback_data='goto_main')
        ]])
        await update.message.reply_text(
            f"ברוך הבא/ברוכה הבאה, *{profile.get('full_name', user.first_name)}!* ❤️\n"
            "שמחים לראות אותך שוב!",
            parse_mode='Markdown',
            reply_markup=kb
        )


# ══════════════════════════════════════════════
#  פרופיל
# ══════════════════════════════════════════════

async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid     = update.effective_user.id
    profile = get_user_profile(uid)

    if not profile.get('registered'):
        await update.message.reply_text(
            "טרם נרשמת. שלח/י /register להרשמה.", reply_markup=markup
        )
        return

    measurements = get_user_measurements(uid, days=30)
    bpms  = [m['bpm']  for m in measurements if 'bpm'  in m]
    spo2s = [m['spo2'] for m in measurements if 'spo2' in m]

    avg_bpm  = int(sum(bpms)  / len(bpms))  if bpms  else "—"
    avg_spo2 = round(sum(spo2s) / len(spo2s), 1) if spo2s else "—"

    text = (
        f"👤 *הפרופיל שלי*\n\n"
        f"🆔 שם: {profile.get('full_name', '—')}\n"
        f"🎂 גיל: {profile.get('age', '—')}\n"
        f'⚖️  משקל: {profile.get("weight", "—")} ק"ג\n'
        f'📏 גובה: {profile.get("height", "—")} ס"מ\n'
        f"🧮 BMI: {profile.get('bmi', '—')}\n\n"
        f"📊 *סטטיסטיקת 30 ימים*\n"
        f"❤️  ממוצע דופק: {avg_bpm} BPM\n"
        f"🩸 ממוצע SpO2: {avg_spo2}%\n"
        f'🔢 סך הכל מדידות: {len(measurements)}'
    )
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=markup)


# ══════════════════════════════════════════════
#  הגדרות
# ══════════════════════════════════════════════

async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid     = update.effective_user.id
    profile = get_user_profile(uid)
    alerts  = profile.get('alerts_enabled', True)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"🔔 התראות: {'פעיל ✅' if alerts else 'כבוי ❌'}",
            callback_data='toggle_alerts'
        )],
        [InlineKeyboardButton("✏️ עדכון פרופיל", callback_data='update_profile')]
    ])
    await update.message.reply_text("⚙️ *הגדרות*", parse_mode='Markdown', reply_markup=kb)


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    if query.data == 'goto_main':
        await query.edit_message_text("מעולה! בוא נתחיל 💪")
        await context.bot.send_message(
            chat_id=uid,
            text="במה אוכל לעזור היום?",
            reply_markup=markup
        )

    elif query.data == 'toggle_alerts':
        ref     = db.reference(f'users/{uid}/profile')
        profile = ref.get() or {}
        new_val = not profile.get('alerts_enabled', True)
        ref.update({'alerts_enabled': new_val})
        status = "פעיל ✅" if new_val else "כבוי ❌"
        await query.edit_message_text(f"🔔 ההתראות עודכנו: {status}")

    elif query.data == 'update_profile':
        await query.edit_message_text(
            "לעדכון הפרופיל שלח/י /register מחדש.\n"
            "(הנתונים הקיימים יוחלפו)"
        )


# ══════════════════════════════════════════════
#  דוח שבועי
# ══════════════════════════════════════════════

async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text("⏳ מפיק דוח שבועי...")

    measurements = get_user_measurements(uid, days=7)
    if not measurements:
        await update.message.reply_text("לא נמצאו מדידות מהשבוע האחרון.", reply_markup=markup)
        return

    bpms  = [m['bpm']  for m in measurements if 'bpm'  in m]
    spo2s = [m['spo2'] for m in measurements if 'spo2' in m and m['spo2'] is not None]

    # BPM stats
    avg_bpm = int(sum(bpms) / len(bpms))
    trend   = ""
    if len(bpms) >= 4:
        first_half  = bpms[:len(bpms)//2]
        second_half = bpms[len(bpms)//2:]
        diff = sum(second_half)/len(second_half) - sum(first_half)/len(first_half)
        trend = f"\n📈 מגמה: {'עולה ⬆️' if diff > 3 else 'יורדת ⬇️' if diff < -3 else 'יציבה ➡️'}"

    # SpO2 stats
    spo2_line = ""
    if spo2s:
        avg_spo2 = round(sum(spo2s)/len(spo2s), 1)
        spo2_line = f"\n🩸 SpO2 ממוצע: {avg_spo2}% {classify_spo2(avg_spo2)}"

    # התראות שהופעלו
    alerts_count = sum(
        1 for m in measurements
        if m.get('alert_sent')
    )

    text = (
        f"📊 *דוח דופק שבועי*\n"
        f"────────────────\n"
        f"❤️  ממוצע: {avg_bpm} BPM {classify_bpm(avg_bpm)}\n"
        f"📈 מקסימום: {max(bpms)} BPM\n"
        f"📉 מינימום: {min(bpms)} BPM\n"
        f"{trend}"
        f"{spo2_line}\n"
        f"────────────────\n"
        f'🔢 סך הכל מדידות: {len(measurements)}\n'
        f"⚠️  התראות שנשלחו: {alerts_count}\n\n"
        f"⚠️ *יש לפנות לרופא/ת קופת החולים לקבלת פרשנות רפואית*"
    )
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=markup)


# ══════════════════════════════════════════════
#  מדידה
# ══════════════════════════════════════════════

async def measure(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global is_measuring
    if is_measuring:
        await update.message.reply_text("⏳ מדידה כבר בתהליך...")
        return
    is_measuring = True
    await update.message.reply_text(
        "🖐️ הנח/י את האצבע על החיישן בעדינות.\n"
        "המדידה תימשך *20 שניות*.\n\n"
        "_יש לשמור על האצבע יציבה ולא לזוז_",
        parse_mode='Markdown'
    )
    asyncio.create_task(run_measurement_session(update, context))


async def run_measurement_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global is_measuring
    uid = update.effective_user.id

    ir_buffer, red_buffer = [], []
    BUFFER_SIZE = 5
    beat_intervals = []
    last_beat_time, last_beat_valid = time.time(), False
    dynamic_threshold = 0
    bpm = 0
    all_session_bpms = []
    all_ir_vals, all_red_vals = [], []
    start_time = time.time()
    DURATION   = 20

    lcd.clear()
    lcd.display_string("Measuring...", 1)

    try:
        while time.time() - start_time < DURATION:
            try:
                data    = bus.read_i2c_block_data(ADDR, 0x05, 6)
                ir_raw  = (data[0] << 8) | data[1]
                red_raw = (data[2] << 8) | data[3]
            except Exception:
                ir_raw = red_raw = 0

            if ir_raw > 3000:
                ir_buffer.append(ir_raw)
                red_buffer.append(red_raw)
                all_ir_vals.append(ir_raw)
                all_red_vals.append(red_raw)

                if len(ir_buffer) > BUFFER_SIZE:
                    ir_buffer.pop(0)
                    red_buffer.pop(0)

                is_peak = False
                if len(ir_buffer) == BUFFER_SIZE:
                    mid = BUFFER_SIZE // 2
                    if ir_buffer[mid] == max(ir_buffer) and ir_buffer[mid] > dynamic_threshold:
                        is_peak = True

                dynamic_threshold = 0.9 * dynamic_threshold + 0.1 * ir_raw

                if is_peak:
                    curr = time.time()
                    if last_beat_valid:
                        interval = curr - last_beat_time
                        if 0.33 < interval < 1.5:
                            bpm_inst = 60.0 / interval
                            all_session_bpms.append(bpm_inst)
                            lcd.display_string(f"BPM: {int(bpm_inst)}", 2)
                    last_beat_time, last_beat_valid = curr, True

            await asyncio.sleep(0.04)

        # ── חישוב תוצאות ──────────────────────────────
        if len(all_session_bpms) > 5:
            final_bpm = int(sum(all_session_bpms) / len(all_session_bpms))
            spo2      = estimate_spo2(all_ir_vals, all_red_vals)
            spo2_disp = f"{spo2}%" if spo2 else "—"

            # בדיקת התראות
            alert_messages = []
            profile = get_user_profile(uid)

            # סף קבוע
            if final_bpm < BPM_LOW:
                alert_messages.append(f"דופק נמוך מהנורמה: {final_bpm} BPM (טווח תקין: {BPM_LOW}–{BPM_HIGH})")
            elif final_bpm > BPM_HIGH:
                alert_messages.append(f"דופק גבוה מהנורמה: {final_bpm} BPM (טווח תקין: {BPM_LOW}–{BPM_HIGH})")

            # סף אישי (לפי ממוצע היסטורי)
            past = get_user_measurements(uid, days=30)
            past_bpms = [m['bpm'] for m in past if 'bpm' in m]
            if len(past_bpms) >= 5:
                personal_avg = sum(past_bpms) / len(past_bpms)
                if abs(final_bpm - personal_avg) > BPM_PERSONAL_DEV:
                    alert_messages.append(
                        f"סטייה מהממוצע האישי שלך ({int(personal_avg)} BPM) "
                        f"של {abs(int(final_bpm - personal_avg))} פעימות!"
                    )

            # SpO2
            if spo2 and spo2 < SPO2_LOW:
                alert_messages.append(
                    f"רמת חמצן בדם נמוכה: {spo2}% (ערך תקין: {SPO2_LOW}% ומעלה)"
                )

            alert_sent = len(alert_messages) > 0
            if alert_sent and profile.get('alerts_enabled', True):
                for msg in alert_messages:
                    await send_alert(uid, msg, context)

            # שמירה ב-Firebase
            db.reference(f'users/{uid}/measurements').push({
                'bpm':        final_bpm,
                'spo2':       spo2,
                'timestamp':  time.strftime('%Y-%m-%d %H:%M:%S'),
                'alert_sent': alert_sent
            })

            result_text = (
                f"✅ *המדידה הושלמה!*\n\n"
                f"❤️  דופק: *{final_bpm} BPM* {classify_bpm(final_bpm)}\n"
                f"🩸 SpO2: *{spo2_disp}* {classify_spo2(spo2) if spo2 else ''}\n\n"
                f"⚠️ _ערך SpO2 הוא אומדן לצורכי לימוד בלבד — אינו מחליף מכשיר רפואי מוסמך._"
            )
            await update.message.reply_text(result_text, parse_mode='Markdown', reply_markup=markup)

        else:
            await update.message.reply_text(
                "❌ לא זוהה דופק ברור.\n"
                "יש לוודא שהאצבע מונחת על החיישן בלחץ קל ולנסות שוב.",
                reply_markup=markup
            )

    except Exception as e:
        print(f"Measurement error: {e}")
        await update.message.reply_text("❌ אירעה שגיאה במדידה. נסה/י שוב.", reply_markup=markup)
    finally:
        is_measuring = False
        lcd.clear()
        lcd.display_string("System Ready", 1)


# ══════════════════════════════════════════════
#  ניתוב הודעות
# ══════════════════════════════════════════════

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == '❤️ מדידה':
        await measure(update, context)
    elif text == '📊 דוח שבועי':
        await report(update, context)
    elif text == '👤 הפרופיל שלי':
        await show_profile(update, context)
    elif text == '⚙️ הגדרות':
        await settings(update, context)
    else:
        await update.message.reply_text(
            "לא זיהיתי פקודה. השתמש/י בכפתורים למטה.", reply_markup=markup
        )


# ══════════════════════════════════════════════
#  הרצה
# ══════════════════════════════════════════════

if __name__ == '__main__':
    # אתחול חיישן
    try:
        bus.write_byte_data(ADDR, 0x06, 0x03)
        bus.write_byte_data(ADDR, 0x09, 0x2F)
    except Exception as e:
        print(f"Sensor init error: {e}")

    # אתחול LCD
    try:
        lcd.clear()
        lcd.display_string("System Ready", 1)
        lcd.display_string("Waiting for App", 2)
    except Exception as e:
        print(f"LCD init error: {e}")

    # ConversationHandler להרשמה
    # start כ-entry point כדי שמשתמש חדש שנכנס דרך קישור יתחיל הרשמה ישירות
    reg_conv = ConversationHandler(
        entry_points=[
            CommandHandler('start', start),
            CommandHandler('register', register_start),
        ],
        states={
            REG_NAME:   [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_get_name)],
            REG_AGE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_get_age)],
            REG_GENDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_get_gender)],
            REG_WEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_get_weight)],
            REG_HEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_get_height)],
        },
        fallbacks=[CommandHandler('cancel', reg_cancel)]
    )

    application.add_handler(reg_conv)
    application.add_handler(CallbackQueryHandler(settings_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("✅ Bot running with full features.")
    application.run_polling()
