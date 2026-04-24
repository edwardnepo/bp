import smbus2
import time
import sys
import asyncio
from telegram import Bot
import lcddriver

# --- הגדרות טלגרם ---
TOKEN = '8488607418:AAF8KAXxb9-a0Iq1bs1khhRmnTF6S46QaoU'
CHAT_ID = '5423183370' 
bot = Bot(token=TOKEN)

# --- הגדרות חיישן ---
ADDR = 0x57
THRESHOLD_FINGER = 3000
TIMEOUT_LIMIT = 10.0
bus = smbus2.SMBus(1)
lcd = lcddriver.LCD()

from telegram.request import HTTPXRequest
request_config = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
bot = Bot(token=TOKEN, request=request_config)

async def send_alert(bpm):
    """פונקציה לשליחת התראה עם מנגנון הגנה מ-Timeout"""
    try:
        msg = f"⚠️ התראת דופק גבוה!\nזוהה קצב לב של {int(bpm)} BPM.\nנא לבדוק את המשתמש."
        # הוספת timeout ישירות לשליחה
        await bot.send_message(chat_id=CHAT_ID, text=msg, write_timeout=30.0)
        print("\n[TELEGRAM] Success! Message sent to your phone.")
    except Exception as e:
        print(f"\n[TELEGRAM ERROR] Failed to send: {e}")
def safe_lcd(text, line):
    try: lcd.display_string(text, line)
    except: pass

def setup_sensor():
    try:
        bus.write_byte_data(ADDR, 0x06, 0x03)
        bus.write_byte_data(ADDR, 0x09, 0x2F)
        lcd.clear()
        print("--- System Running: Peak Detection + Telegram ---")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

async def main():
    setup_sensor()

    # --- Peak Detection Variables ---
    ir_buffer = []          
    BUFFER_SIZE = 5         
    beat_intervals = []
    last_beat_time = time.time()
    last_beat_time_valid = False
    
    # סף דינמי
    dynamic_threshold = 0
    THRESHOLD_ALPHA = 0.1   

    # ניהול התראות וזמנים
    last_alert_time = 0      # מונע הצפה של הודעות
    no_finger_start_time = None
    last_lcd_update = 0
    bpm = 0

    try:
        while True:
            ir_raw = 0
            try:
                data = bus.read_i2c_block_data(ADDR, 0x05, 4)
                ir_raw = (data[0] << 8) | data[1]
            except: pass

            curr_time = time.time()

            if ir_raw > THRESHOLD_FINGER:
                no_finger_start_time = None

                ir_buffer.append(ir_raw)
                if len(ir_buffer) > BUFFER_SIZE:
                    ir_buffer.pop(0)

                is_peak = False
                if len(ir_buffer) == BUFFER_SIZE:
                    mid = BUFFER_SIZE // 2
                    if ir_buffer[mid] == max(ir_buffer) and ir_buffer[mid] > dynamic_threshold:
                        is_peak = True

                dynamic_threshold = (1 - THRESHOLD_ALPHA) * dynamic_threshold + THRESHOLD_ALPHA * ir_raw

                if is_peak:
                    if last_beat_time_valid:
                        interval = curr_time - last_beat_time
                        if 0.33 < interval < 1.5:
                            beat_intervals.append(interval)
                            if len(beat_intervals) > 8: beat_intervals.pop(0)
                            bpm = 60 / (sum(beat_intervals) / len(beat_intervals))

                            sys.stdout.write("♥")
                            sys.stdout.flush()

                            # --- לוגיקת טלגרם ---
                            # אם דופק גבוה מ-110 ולא שלחנו הודעה ב-2 הדקות האחרונות
                            if bpm > 110 and (curr_time - last_alert_time > 120):
                                asyncio.create_task(send_alert(bpm))
                                last_alert_time = curr_time

                    last_beat_time = curr_time
                    last_beat_time_valid = True

                if curr_time - last_lcd_update > 0.6:
                    display_bpm = int(bpm) if bpm > 0 else "--"
                    safe_lcd(f"Pulse: {display_bpm} BPM  ", 1)
                    status = "HIGH!" if bpm > 110 else "Normal"
                    safe_lcd(f"Status: {status}  ", 2)
                    last_lcd_update = curr_time
                    sys.stdout.write(f"\r[DATA] IR: {ir_raw} | BPM: {int(bpm)}   ")
                    sys.stdout.flush()

            else:
                if no_finger_start_time is None:
                    no_finger_start_time = curr_time
                elapsed = curr_time - no_finger_start_time

                if elapsed >= TIMEOUT_LIMIT:
                    lcd.clear()
                    print("\nProgram Ended.")
                    sys.exit(0)

                if curr_time - last_lcd_update > 0.5:
                    safe_lcd("Place Finger    ", 1)
                    safe_lcd(f"Exit in: {int(TIMEOUT_LIMIT - elapsed)}s  ", 2)
                    last_lcd_update = curr_time

                bpm = 0
                beat_intervals = []
                ir_buffer = []
                last_beat_time_valid = False
                dynamic_threshold = 0

            # שימוש ב-sleep אסינכרוני כדי לא לתקוע את הבוט
            await asyncio.sleep(0.04)

    except KeyboardInterrupt:
        lcd.clear()
        sys.exit(0)

if __name__ == "__main__":
    # הפעלת הלולאה האסינכרונית
    asyncio.run(main())