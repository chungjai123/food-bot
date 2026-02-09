import telebot
import base64
import os
import sqlite3
from datetime import datetime
from huggingface_hub import InferenceClient
from dotenv import load_dotenv
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from flask import Flask, request, abort
from telebot import types

app = Flask(__name__)

pending_saves = {}
user_states = {}
load_dotenv()  # Uncomment if you use .env; otherwise set env vars directly

# Tokens (move to .env for security!)
HF_TOKEN = os.getenv("HF_TOKEN") or "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"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN") or "8049647620:AAGmBQukfcJ66h68OLnNlfEIFci40UwblMY"

if not HF_TOKEN or not TELEGRAM_TOKEN:
    raise ValueError("Missing API tokens!")

DB_FILE = "calorie_history.db"

conn = sqlite3.connect(DB_FILE)
cursor = conn.cursor()
cursor.execute("DROP TABLE IF EXISTS history")
conn.commit()
conn.close()

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # Create tables if they don't exist
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            recognized TEXT,
            calories REAL,
            protein REAL,
            carbs REAL,
            fat REAL,
            sugar REAL,
            tips TEXT,
            full_text TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            sex TEXT CHECK(sex IN ('male', 'female')),
            age INTEGER,
            height_cm REAL,
            weight_kg REAL,
            updated_at TEXT
        )
    """)

    # ── Add missing columns if they don't exist ──────────────────────────────
    cursor.execute("PRAGMA table_info(users)")
    columns = {row[1] for row in cursor.fetchall()}

    required = {
        'height_cm': 'REAL',
        'weight_kg': 'REAL',
        'updated_at': 'TEXT'
    }

    for col_name, col_type in required.items():
        if col_name not in columns:
            print(f"Adding missing column: {col_name}")
            cursor.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_type}")

    conn.commit()
    conn.close()

init_db()

#find the ai model(Qwen2.5)
client = InferenceClient(
    model="Qwen/Qwen2.5-VL-7B-Instruct:hyperbolic",
    token=HF_TOKEN
)

bot = telebot.TeleBot(TELEGRAM_TOKEN)
#find user search history, show the records if it have records
def get_user_history(user_id: int, limit: int = 10) -> str:
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # Get limited records for display
    cursor.execute("""
        SELECT timestamp, recognized, calories, protein, carbs, fat, sugar
        FROM history 
        WHERE user_id = ? 
        ORDER BY id DESC 
        LIMIT ?
    """, (user_id, limit))
    records = cursor.fetchall()
    
    # Get total calories (all time)
    cursor.execute("""
        SELECT SUM(calories) 
        FROM history 
        WHERE user_id = ?
    """, (user_id,))
    total_calories = cursor.fetchone()[0] or 0.0
    
    conn.close()

    if not records:
        return "No history yet. Send a food photo to start! 📸"

    text = f"📊 **Your calorie summary**\n"
    text += f"Total calories (all records): **{total_calories:.0f} kcal**\n\n"
    text += f"Recent {len(records)} records:\n\n"

    for row in records:
        ts, recog, cal, prot, carb, fat, sug = row
        text += (
            f"📅 {ts}\n"
            f"🍽️ {recog}\n"
            f"💪 Protein: {prot}g   🥔 Carbs: {carb}g\n"
            f"🧈 Fat: {fat}g   🍬 Sugar: {sug}g\n"
            f"🔥 {cal} kcal\n"
            "───\n"
        )
    
    return text

def get_user_profile(user_id: int) -> dict | None:
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT sex, age, height_cm, weight_kg 
        FROM users 
        WHERE user_id = ?
    """, (user_id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        return None
    
    return {
        "sex": row[0],
        "age": row[1],
        "height_cm": row[2],
        "weight_kg": row[3]
    }


def save_user_profile(user_id: int, sex: str, age: int, height: float, weight: float):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO users 
        (user_id, sex, age, height_cm, weight_kg, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (user_id, sex.lower(), age, height, weight, datetime.now().isoformat()))
    conn.commit()
    conn.close()


def delete_user_profile(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def calculate_bmr(profile: dict) -> float | None:
    if not profile:
        return None
    
    required = {"sex", "age", "height_cm", "weight_kg"}
    if not all(k in profile for k in required):
        return None
    
    sex   = profile["sex"]
    age   = profile["age"]
    kg    = profile["weight_kg"]
    cm    = profile["height_cm"]
    
    # Also guard against None values
    if any(x is None for x in [age, kg, cm]):
        return None
    
    if sex == "male":
        return 10 * kg + 6.25 * cm - 5 * age + 5
    elif sex == "female":
        return 10 * kg + 6.25 * cm - 5 * age - 161
    return None

@bot.message_handler(commands=['setprofile'])
def start_profile_setup(message):
    user_id = message.from_user.id
    user_states[user_id] = {"step": "sex", "data": {}}
    
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("Male", callback_data="profile_sex_m"),
        InlineKeyboardButton("Female", callback_data="profile_sex_f")
    )
    bot.reply_to(message, "Please select your sex:", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("profile_sex_"))
def handle_sex_selection(call):
    user_id = call.from_user.id
    if user_id not in user_states or user_states[user_id]["step"] != "sex":
        bot.answer_callback_query(call.id, "Session expired.")
        return
    
    sex = "male" if call.data.endswith("_m") else "female"
    user_states[user_id]["data"]["sex"] = sex
    user_states[user_id]["step"] = "age"
    
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"Sex set to {sex.capitalize()}.\n\nNow enter your age (years):"
    )
    bot.answer_callback_query(call.id)


@bot.message_handler(func=lambda m: m.from_user.id in user_states and user_states[m.from_user.id]["step"] == "age")
def handle_age(message):
    user_id = message.from_user.id
    try:
        age = int(message.text.strip())
        if not 10 <= age <= 120:
            raise ValueError
        user_states[user_id]["data"]["age"] = age
        user_states[user_id]["step"] = "height"
        bot.reply_to(message, "Enter your height in cm (e.g. 168):")
    except:
        bot.reply_to(message, "Please enter a realistic age (10–120). Try again:")


@bot.message_handler(func=lambda m: m.from_user.id in user_states and user_states[m.from_user.id]["step"] == "height")
def handle_height(message):
    user_id = message.from_user.id
    try:
        height = float(message.text.strip())
        if not 100 <= height <= 250:
            raise ValueError
        user_states[user_id]["data"]["height_cm"] = height
        user_states[user_id]["step"] = "weight"
        bot.reply_to(message, "Enter your weight in kg (e.g. 65.5):")
    except:
        bot.reply_to(message, "Please enter height in cm (100–250). Try again:")


@bot.message_handler(func=lambda m: m.from_user.id in user_states and user_states[m.from_user.id]["step"] == "weight")
def handle_weight(message):
    user_id = message.from_user.id
    try:
        weight = float(message.text.strip())
        if not 30 <= weight <= 300:
            raise ValueError
        
        data = user_states[user_id]["data"]
        data["weight_kg"] = weight
        
        # Save to DB
        save_user_profile(
            user_id,
            data["sex"],
            data["age"],
            data["height_cm"],
            data["weight_kg"]
        )
        
        bmr = calculate_bmr(data)
        
        summary = (
            f"✅ Profile saved!\n\n"
            f"Sex: {data['sex'].capitalize()}\n"
            f"Age: {data['age']} years\n"
            f"Height: {data['height_cm']} cm\n"
            f"Weight: {data['weight_kg']} kg\n\n"
            f"Your BMR (Mifflin-St Jeor): **{bmr:.0f} kcal/day**\n"
            "(this is calories your body burns at complete rest)"
        )
        
        bot.reply_to(message, summary)
        
        # Clean up
        del user_states[user_id]
        
    except:
        bot.reply_to(message, "Please enter weight in kg (30–300). Try again:")


@bot.message_handler(commands=['clearprofile'])
def clear_profile(message):
    user_id = message.from_user.id
    delete_user_profile(user_id)
    if user_id in user_states:
        del user_states[user_id]
    bot.reply_to(message, "🗑️ Your profile and BMR data have been cleared.")


@bot.message_handler(commands=['bmr'])
def show_bmr(message):
    user_id = message.from_user.id
    profile = get_user_profile(user_id)
    
    if not profile:
        bot.reply_to(message,
            "You haven't set your profile yet.\n"
            "Use /setprofile to enter sex, age, height & weight.")
        return
    
    bmr = calculate_bmr(profile)
    if bmr is None:
        bot.reply_to(message, "Profile data incomplete. Please use /setprofile again.")
        return
    
    text = (
        f"📋 Your profile:\n"
        f"• Sex: {profile['sex'].capitalize()}\n"
        f"• Age: {profile['age']} years\n"
        f"• Height: {profile['height_cm']} cm\n"
        f"• Weight: {profile['weight_kg']} kg\n\n"
        f"🔥 **BMR: {bmr:.0f} kcal/day**\n"
        "(Basal Metabolic Rate – calories burned at rest)"
    )
    bot.reply_to(message, text)

#command that let user find records
@bot.message_handler(commands=['history'])
def show_history(message):
    history_text = get_user_history(message.from_user.id, limit=10)
    bot.reply_to(message, history_text)

#let user choose save or not save the records
@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    user_id = call.from_user.id
    data = call.data

    if user_id not in pending_saves:
        bot.answer_callback_query(call.id, "Session expired. Please send photo again.")
        return

    saved_data = pending_saves.pop(user_id)  # remove after handling

    if data.startswith("save_yes_"):
        p = saved_data["parsed"]
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO history 
            (user_id, timestamp, recognized, calories, protein, carbs, fat, sugar, tips, full_text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id,
            saved_data["timestamp"],
            p["recognized"],
            p["calories"],
            p["protein"],
            p["carbs"],
            p["fat"],
            p["sugar"],
            p["tips"],
            saved_data["full_text"]
        ))
        conn.commit()
        conn.close()

        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text = saved_data["full_text"] + "\n\n✅ Saved!"
        )

    elif data.startswith("save_no_"):
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text = saved_data["full_text"] + "\n\nNot saved."
        )
        bot.answer_callback_query(call.id, "Not saved")

    else:
        bot.answer_callback_query(call.id, "Invalid action")

#welcome user
@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = message.from_user.id
    profile = get_user_profile(user_id)
    
    if profile:
        bmr = calculate_bmr(profile)
        if bmr is not None:
            bmr_line = f"Your BMR: ≈{bmr:.0f} kcal/day\n"
        else:
            bmr_line = "Profile data incomplete — please use /setprofile to finish or correct it\n"
    else:
        bmr_line = "Set your profile with /setprofile to see your BMR\n"
    
    welcome = (
        f"Hi! 👋 Send me a photo of food to estimate calories.\n\n"
        f"{bmr_line}\n"
        "Commands: /history • /bmr • /setprofile • /clearprofile • /clear (meals)"
    )
    bot.reply_to(message, welcome)
    
    history_text = get_user_history(user_id, limit=8)
    bot.reply_to(message, history_text)

#if user send photo, the chatbot will send the photo via API to AI and ask nutritional data.
@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    try:
        user_id = message.from_user.id
        file_id = message.photo[-1].file_id
        file_info = bot.get_file(file_id)
        downloaded_file = bot.download_file(file_info.file_path)

        base64_image = base64.b64encode(downloaded_file).decode('utf-8')

        prompt = """Analyze this food image carefully.
Describe visible food items, approximate portion sizes (small/medium/large or rough grams if possible),
cooking method if visible, and estimate total calories.
Use realistic nutritional knowledge (USDA-style averages). Break down by item if multiple foods are present.
Be conservative and realistic in your estimates.
The output style should be:
🍽️Recognized: Nasi Lemak with fried chicken, cucumber, egg & sambal
💪Protein: 38g 🥔Carbs: 92g 🧈Fat: 45g 🍬Sugar: 10g
🔥Calories: 850 kcal
and provide some tips at the end for the user."""

        response = client.chat.completions.create(
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                ]
            }],
            max_tokens=450,
            temperature=0.35,
        )

        result = response.choices[0].message.content.strip()

        if not result:
            bot.reply_to(message, "Couldn't analyze – try a clearer photo!")
            return

        # After getting result
        parsed = parse_ai_result(result)

        # Show result to user (full original text)
        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            InlineKeyboardButton("Yes, save this", callback_data=f"save_yes_{user_id}"),
            InlineKeyboardButton("No, thanks", callback_data=f"save_no_{user_id}")
        )

        bot.reply_to(
            message,
            result + "\n\nWould you like to save this record?",
            reply_markup=markup
        )

        # Store parsed + full for later
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        pending_saves[user_id] = {
            "parsed": parsed,
            "full_text": result,
            "timestamp": timestamp
        }

    except Exception as e:
        error_msg = str(e).lower()
        print("Full error:", str(e))

        if "rate limit" in error_msg or "quota" in error_msg:
            bot.reply_to(message, "Rate limit – wait 1–2 min ⏳")
        elif "unavailable" in error_msg or "bad request" in error_msg:
            bot.reply_to(message, "Model temporarily unavailable – try again soon")
        else:
            bot.reply_to(message, f"Error: {str(e)[:180]}...")


#clear records
@bot.message_handler(commands=['clear'])
def clear_history(message):
    user_id = message.from_user.id
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM history WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    bot.reply_to(message, "🗑️ Your history has been cleared!")


#extract the data from exact pattern
def parse_ai_result(text: str) -> dict:
    """
    Designed for this exact pattern:
    🍽️Recognized: [description]
    💪Protein: 38g 🥔Carbs: 92g 🧈Fat: 45g 🍬Sugar: 10g
    🔥Calories: 850 kcal
    [tips...]
    """
    result = {
        "recognized": "Unknown",
        "calories": 0.0,
        "protein": 0.0,
        "carbs": 0.0,
        "fat": 0.0,
        "sugar": 0.0,
        "tips": ""
    }

    lines = [line.strip() for line in text.split('\n') if line.strip()]

    if not lines:
        return result

    # 1. Recognized (usually first line)
    if "Recognized:" in lines[0]:
        result["recognized"] = lines[0].split("Recognized:", 1)[1].strip()

    # 2. Find macro line (contains Protein/Carbs/Fat/Sugar)
    macro_line = None
    calories_line = None

    for line in lines:
        if "Protein:" in line and ("Carbs:" in line or "Fat:" in line):
            macro_line = line
        if "Calories:" in line:
            calories_line = line

    # 3. Parse macro line (all on one line with emojis)
    if macro_line:
        # Remove emojis and zero-width spaces
        clean = macro_line
        for char in ['💪', '🥔', '🧈', '🍬', '🔥', '\u200b']:
            clean = clean.replace(char, '')

        # Now clean looks like: "Protein: 38g Carbs: 92g Fat: 45g Sugar: 10g"
        # Use regex to find "Key: numberg"
        import re
        matches = re.findall(r'(\w+):\s*([\d.]+)g?', clean)

        for key, value in matches:
            try:
                num = float(value)
                key_lower = key.lower()
                if "protein" in key_lower:
                    result["protein"] = num
                elif "carb" in key_lower:
                    result["carbs"] = num
                elif "fat" in key_lower:
                    result["fat"] = num
                elif "sugar" in key_lower:
                    result["sugar"] = num
            except ValueError:
                pass

    # 4. Parse calories line
    if calories_line:
        try:
            # Get number after "Calories:"
            after = calories_line.split("Calories:", 1)[1].strip()
            # Extract first number (handles "850 kcal" or "850")
            num_match = re.search(r'\d+\.?\d*', after)
            if num_match:
                result["calories"] = float(num_match.group())
        except:
            pass

    # 5. Collect tips (lines after calories)
    tips = []
    tips_started = False
    for line in lines:
        if "Calories:" in line:
            tips_started = True
            continue
        if tips_started:
            tips.append(line.strip())

    result["tips"] = ' '.join(tips).strip() if tips else ""

    return result

# Render gives you https://your-app-name.onrender.com
render_hostname = os.environ.get("RENDER_EXTERNAL_HOSTNAME")

if not render_hostname:
    raise RuntimeError("RENDER_EXTERNAL_HOSTNAME not found → are you running on Render?")

WEBHOOK_PATH = f"/{TELEGRAM_TOKEN}/"          # secret path = /<full-token>/
WEBHOOK_URL = f"https://{render_hostname}{WEBHOOK_PATH}"

@app.route('/', methods=['GET'])
def index():
    return "Calorie bot webhook is running 🚀", 200

@app.route(WEBHOOK_PATH, methods=['POST'])
def telegram_webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        if update:
            bot.process_new_updates([update])
        return '', 200
    else:
        abort(403)

if __name__ == '__main__':
    # For local testing you can temporarily switch back to polling:
    # bot.remove_webhook()
    # bot.infinity_polling()
    # return

    # Production (Render): set webhook
    print("Removing any old webhook...")
    bot.remove_webhook()

    print(f"Setting webhook → {WEBHOOK_URL}")
    success = bot.set_webhook(url=WEBHOOK_URL)

    if not success:
        print("Webhook set failed!")
        exit(1)

    print("Webhook set successfully.")

    # Start Flask server
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
    
#message when running the bot
if __name__ == '__main__':
    print("Food calorie bot started – /start now auto-shows history!")
    bot.infinity_polling() 