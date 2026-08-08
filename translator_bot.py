import os
import threading
import time
import re
import requests
import csv
import io
import json
from datetime import datetime, timezone, timedelta
from flask import Flask, request, abort
import google.generativeai as genai
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, ImageMessage, TextSendMessage
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

app = Flask(__name__)

# ============================================================
# ⚙️ ตั้งค่า Keys (ดึงจาก Environment Variables บน Render)
# ============================================================
CHANNEL_ACCESS_TOKEN = os.environ.get("CHANNEL_ACCESS_TOKEN", "").strip()
CHANNEL_SECRET = os.environ.get("CHANNEL_SECRET", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
ALLOWED_USER_ID = os.environ.get("ALLOWED_USER_ID", "").strip()
GAS_WEBAPP_URL = os.environ.get("GAS_WEBAPP_URL", "").strip()

# Google Drive Integration Configs
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID", "").strip()
GEAR_USER_ID = os.environ.get("GEAR_USER_ID", "").strip()

CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQHZO7YI_TVcDcnUzkAOlxMrH3uI_cZSp6WDVMta71QOYFQagB3vKnWgUUJDZd3BYpJt2XiPz1XDO-M/pub?output=csv"

# ตรวจสอบค่าคอนฟิกพื้นฐาน
if CHANNEL_ACCESS_TOKEN and CHANNEL_SECRET:
    line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
    handler = WebhookHandler(CHANNEL_SECRET)
else:
    print("⚠️ Warning: LINE API keys are not set yet!")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
else:
    print("⚠️ Warning: GEMINI_API_KEY is not set yet!")

# ============================================================
# 📁 Google Drive API Initializer
# ============================================================
drive_service_cache = None
drive_service_lock = threading.Lock()

def get_drive_service():
    global drive_service_cache
    with drive_service_lock:
        if drive_service_cache:
            return drive_service_cache
        if not GOOGLE_SERVICE_ACCOUNT_JSON:
            print("⚠️ Warning: GOOGLE_SERVICE_ACCOUNT_JSON is not set!")
            return None
        try:
            json_str = GOOGLE_SERVICE_ACCOUNT_JSON
            if not json_str.startswith("{"):
                import base64
                json_str = base64.b64decode(json_str).decode("utf-8")
            info = json.loads(json_str)
            scopes = ["https://www.googleapis.com/auth/drive.file"]
            creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
            drive_service_cache = build("drive", "v3", credentials=creds)
            print("✅ Google Drive API Service Initialized")
            return drive_service_cache
        except Exception as e:
            print(f"❌ Failed to initialize Google Drive Service: {e}")
            return None

# ============================================================
# 🛡️ ระบบตรวจสอบสิทธิ์ผู้ใช้งาน (Authorization Check)
# ============================================================
def is_user_allowed(user_id: str) -> bool:
    if not ALLOWED_USER_ID or ALLOWED_USER_ID in ["ใส่_LINE_USER_ID_ตรงนี้", "*"]:
        return True
    allowed_list = [uid.strip() for uid in ALLOWED_USER_ID.split(",") if uid.strip()]
    return user_id in allowed_list

# ============================================================
# 📚 ระบบ Glossary Cache (In-Memory Caching พร้อม TTL)
# ============================================================
GLOSSARY_CACHE = {
    "text": "",
    "timestamp": 0
}
GLOSSARY_TTL_SECONDS = 300  # Cache อยู่ได้ 5 นาที
glossary_lock = threading.Lock()

def get_glossary(force_refresh: bool = False) -> str:
    current_time = time.time()
    with glossary_lock:
        if not force_refresh and GLOSSARY_CACHE["text"] and (current_time - GLOSSARY_CACHE["timestamp"] < GLOSSARY_TTL_SECONDS):
            return GLOSSARY_CACHE["text"]

    # 1. พยายามดึงแบบเรียลไทม์จาก GAS Web App (Timeout 4s)
    if GAS_WEBAPP_URL:
        try:
            response = requests.post(GAS_WEBAPP_URL, json={"action": "get_all"}, timeout=4)
            response.raise_for_status()
            res_data = response.json()
            if res_data.get("status") == "success":
                items = res_data.get("data", [])
                glossary_lines = [f"{item['thai']} = {item['chinese']}" for item in items if 'thai' in item and 'chinese' in item]
                glossary_text = "\n".join(glossary_lines)
                with glossary_lock:
                    GLOSSARY_CACHE["text"] = glossary_text
                    GLOSSARY_CACHE["timestamp"] = time.time()
                print("✅ โหลดคำศัพท์เรียลไทม์จาก GAS สำเร็จ")
                return glossary_text
        except Exception as e:
            print(f"⚠️ ดึงศัพท์จาก GAS ไม่สำเร็จ (ลอง CSV สำรอง): {e}")

    # 2. หากดึงจาก GAS ไม่สำเร็จ ให้ดึงจาก CSV URL สำรอง (Timeout 5s)
    try:
        response = requests.get(CSV_URL, timeout=5)
        response.raise_for_status()
        csv_reader = csv.reader(io.StringIO(response.text))
        glossary_lines = [f"{row[0]} = {row[1]}" for row in csv_reader if len(row) >= 2]
        glossary_text = "\n".join(glossary_lines)
        with glossary_lock:
            GLOSSARY_CACHE["text"] = glossary_text
            GLOSSARY_CACHE["timestamp"] = time.time()
        print("✅ โหลดคำศัพท์จาก CSV สำรองสำเร็จ")
        return glossary_text
    except Exception as e:
        print(f"❌ Error fetching glossary CSV: {e}")
        with glossary_lock:
            if GLOSSARY_CACHE["text"]:
                print("ℹ️ ใช้ Glossary ล่าสุดที่มีใน Cache แทน")
                return GLOSSARY_CACHE["text"]
        return "No glossary found."

def invalidate_glossary_cache():
    with glossary_lock:
        GLOSSARY_CACHE["timestamp"] = 0
    print("🔄 Invalidate Glossary Cache เรียบร้อยแล้ว")

# ============================================================
# 🧠 ความจำสนทนาและ Thread-Safe Session Management
# ============================================================
chat_sessions_translate = {}
chat_sessions_explain = {}
session_lock = threading.Lock()
SESSION_TTL_SECONDS = 7200  # ล้างความจำที่ไม่แอคทีฟเกิน 2 ชม.
MAX_HISTORY_TURNS = 20       # จำกัดประวัติไม่เกิน 20 ข้อความ

def cleanup_old_sessions():
    now = time.time()
    with session_lock:
        for user_id in list(chat_sessions_translate.keys()):
            sess = chat_sessions_translate[user_id]
            if now - sess.get("last_active", 0) > SESSION_TTL_SECONDS:
                del chat_sessions_translate[user_id]
                print(f"🧹 Evicted inactive translate session for user: {user_id}")
        for user_id in list(chat_sessions_explain.keys()):
            sess = chat_sessions_explain[user_id]
            if now - sess.get("last_active", 0) > SESSION_TTL_SECONDS:
                del chat_sessions_explain[user_id]
                print(f"🧹 Evicted inactive explain session for user: {user_id}")

def get_translate_chat(user_id: str):
    cleanup_old_sessions()
    glossary = get_glossary()
    current_glossary_ts = GLOSSARY_CACHE["timestamp"]

    prompt = f"""
You are an expert Thai-Chinese (Simplified) translator. Your user is a Thai speaker who understands Chinese (HSK 5 level).
You act as a silent translator for a LINE group chat regarding a Shrimp Farm business.

Your tasks:
1. Automatically detect the source language and translate it to the target language (Thai -> Chinese 简体, Chinese -> Thai).
2. When translating from Chinese to Thai:
   - ALWAYS translate the first-person pronoun "我" (or any self-pronoun) as "ผม" (NEVER use "ฉัน", "ดิฉัน", or "เรา").
   - ALWAYS end sentences with the polite particle "ครับ" (unless it is a question or inappropriate for the sentence structure).
   - Adjust the tone to be polite but firm/decisive, reflecting the status and authority of a professional Shrimp Farm Owner.
3. ALWAYS relate the translations to the context of a shrimp farm business and its operations.
4. If a message contains multiple lines or multiple speakers (e.g. "สมชาย: สวัสดี"), translate line by line, strictly preserving the speaker's name and original format.
5. If there are mixed languages, translate the part that is not the target language of the reader.
6. DO NOT output any conversational filler. Output ONLY the translated text.
7. Consider the conversation history for context, but only translate the LATEST message sent by the user.

Here is the Glossary of specific terms you MUST use:
<glossary>
{glossary}
</glossary>
"""
    with session_lock:
        sess = chat_sessions_translate.get(user_id)
        if not sess or sess.get("glossary_ts", 0) < current_glossary_ts:
            existing_history = []
            if sess and "chat" in sess:
                try:
                    existing_history = sess["chat"].history
                    if len(existing_history) > MAX_HISTORY_TURNS:
                        existing_history = existing_history[-MAX_HISTORY_TURNS:]
                except Exception:
                    existing_history = []
            
            model = genai.GenerativeModel("gemini-2.5-flash", system_instruction=prompt)
            chat = model.start_chat(history=existing_history)
            chat_sessions_translate[user_id] = {
                "chat": chat,
                "glossary_ts": current_glossary_ts,
                "last_active": time.time()
            }
            print(f"🔄 อัปเดต/สร้าง Chat Session (แปลภาษา) สำหรับ User: {user_id}")
        else:
            sess["last_active"] = time.time()
            chat = sess["chat"]
            
    return chat

def get_explain_chat(user_id: str):
    cleanup_old_sessions()
    glossary = get_glossary()
    current_glossary_ts = GLOSSARY_CACHE["timestamp"]

    prompt = f"""
You are an expert consultant for a Shrimp Farm business.
Your task is to explain terms, chemical substances, processes, or machinery in detail.

Your tasks:
1. You MUST ALWAYS explain and reply in Simplified Chinese (简体中文) ONLY, regardless of whether the user queries in Thai or Chinese.
2. Keep the explanation extremely short, concise, easy to understand, and straight to the point (no unnecessary conversational filler).
3. If the term is found in the Glossary, use the translated Chinese term and explain its purpose in the context of a shrimp farm.
4. Provide a very brief context on how it's used or typically applied.
5. Consider the conversation history for context.

Here is the Glossary of specific terms you can reference:
<glossary>
{glossary}
</glossary>
"""
    with session_lock:
        sess = chat_sessions_explain.get(user_id)
        if not sess or sess.get("glossary_ts", 0) < current_glossary_ts:
            existing_history = []
            if sess and "chat" in sess:
                try:
                    existing_history = sess["chat"].history
                    if len(existing_history) > MAX_HISTORY_TURNS:
                        existing_history = existing_history[-MAX_HISTORY_TURNS:]
                except Exception:
                    existing_history = []

            model = genai.GenerativeModel("gemini-2.5-flash", system_instruction=prompt)
            chat = model.start_chat(history=existing_history)
            chat_sessions_explain[user_id] = {
                "chat": chat,
                "glossary_ts": current_glossary_ts,
                "last_active": time.time()
            }
            print(f"🔄 อัปเดต/สร้าง Chat Session (อธิบายศัพท์) สำหรับ User: {user_id}")
        else:
            sess["last_active"] = time.time()
            chat = sess["chat"]

    return chat

# ============================================================
# ระบบส่งข้อความกลับไปทาง LINE (พร้อม Error Handling)
# ============================================================
def reply_to_line(reply_token: str, text: str):
    try:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=text))
    except Exception as e:
        print(f"❌ Fail to reply to LINE: {e}")

# ============================================================
# ระบบทำความสะอาดข้อความ ลบเครื่องหมายแท็กคนอื่นออก (@Name)
# ============================================================
def clean_mentions(event_message) -> str:
    text = getattr(event_message, 'text', "") or ""
    if not text:
        return ""
        
    if hasattr(event_message, 'mention') and event_message.mention:
        try:
            mentees = sorted(event_message.mention.mentees, key=lambda m: m.index, reverse=True)
            for mentee in mentees:
                start = mentee.index
                end = start + mentee.length
                text = text[:start] + text[end:]
        except Exception as e:
            print(f"⚠️ Error cleaning LINE mention metadata: {e}")
            
    text = re.sub(r'(?<=^|\s)@[^\s]+', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

# ============================================================
# ตัวหลักประมวลผลข้อความแปลภาษา (Text Mode)
# ============================================================
def process_text_message(reply_token: str, user_id: str, text: str):
    if not is_user_allowed(user_id):
        print(f"❌ Blocked message from unauthorized User ID: {user_id}")
        return

    text_strip = text.strip()

    # 1. 🔄 โหมดรีเซ็ตความจำ (++reset)
    if text_strip.lower() == "++reset":
        with session_lock:
            chat_sessions_translate.pop(user_id, None)
            chat_sessions_explain.pop(user_id, None)
        print(f"🧹 รีเซ็ตความจำสำหรับ User ID: {user_id}")
        reply_to_line(reply_token, "🔄 รีเซ็ตความจำเรียบร้อยแล้วครับ เริ่มนับหนึ่งใหม่!")
        return

    # 2. 📋 โหมดดูคำศัพท์ล่าสุด (++)
    if text_strip == "++":
        if not GAS_WEBAPP_URL:
            reply_to_line(reply_token, "❌ ไม่พบการตั้งค่า GAS_WEBAPP_URL บนระบบคลาวด์ ไม่สามารถดึงคลังคำศัพท์ได้")
            return
        
        try:
            response = requests.post(GAS_WEBAPP_URL, json={"action": "list"}, timeout=8)
            response.raise_for_status()
            res_data = response.json()
            
            if res_data.get("status") == "success":
                items = res_data.get("data", [])
                if not items:
                    reply_to_line(reply_token, "📋 ยังไม่มีคำศัพท์ถูกบันทึกไว้ในคลังคลาสครับ")
                else:
                    reply_lines = ["📋 คำศัพท์ 5 รายการล่าสุดในคลัง:"]
                    for idx, item in enumerate(items, 1):
                        reply_lines.append(f"{idx}. {item['thai']} = {item['chinese']}")
                    reply_to_line(reply_token, "\n".join(reply_lines))
            else:
                reply_to_line(reply_token, f"❌ ดึงข้อมูลจาก Sheets ล้มเหลว: {res_data.get('message')}")
        except Exception as e:
            reply_to_line(reply_token, f"❌ เกิดข้อผิดพลาดในการเชื่อมต่อ Google Sheets: {e}")
        return

    # 3. ✍️ โหมดบันทึกคำศัพท์ (+)
    if text_strip.startswith("+"):
        if not GAS_WEBAPP_URL:
            reply_to_line(reply_token, "❌ ไม่พบการตั้งค่า GAS_WEBAPP_URL บนระบบคลาวด์ ไม่สามารถบันทึกคำศัพท์ได้")
            return
            
        content = text_strip[1:].strip()
        if "=" not in content:
            reply_to_line(reply_token, "⚠️ รูปแบบคำสั่งบันทึกไม่ถูกต้อง\nกรุณาใช้: +[คำไทย] = [คำจีน]\nตัวอย่างเช่น: +สีกันสนิม = 防锈漆")
            return
            
        parts = content.split("=", 1)
        thai_word = parts[0].strip()
        chinese_word = parts[1].strip()
        
        if not thai_word or not chinese_word:
            reply_to_line(reply_token, "⚠️ กรุณากรอกทั้งคำไทยและคำจีนให้ครบถ้วน\nตัวอย่างเช่น: +สีกันสนิม = 防锈漆")
            return
            
        try:
            payload = {
                "action": "add",
                "thai": thai_word,
                "chinese": chinese_word
            }
            response = requests.post(GAS_WEBAPP_URL, json=payload, timeout=8)
            response.raise_for_status()
            res_data = response.json()
            
            if res_data.get("status") == "success":
                invalidate_glossary_cache()
                get_glossary(force_refresh=True)
                reply_to_line(reply_token, f"✍️ บันทึก \"{thai_word} = {chinese_word}\" ลง Google Sheets เรียบร้อยแล้วครับ!")
            else:
                reply_to_line(reply_token, f"❌ บันทึกข้อมูลล้มเหลว: {res_data.get('message')}")
        except Exception as e:
            reply_to_line(reply_token, f"❌ เกิดข้อผิดพลาดในการบันทึกข้อมูลไป Google Sheets: {e}")
        return

    # 4. 💬 โหมดขอคำอธิบาย (?) หรือแชทปกติ
    is_explain_mode = False
    query_text = text_strip
    if text_strip.startswith("?") or text_strip.startswith("？"):
        is_explain_mode = True
        query_text = text_strip[1:].strip()

    try:
        if is_explain_mode:
            chat = get_explain_chat(user_id)
        else:
            chat = get_translate_chat(user_id)
            
        response = chat.send_message(query_text)
        translated_text = response.text.strip()
        reply_to_line(reply_token, translated_text)
        print("✅ ประมวลผลและตอบกลับผ่าน LINE สำเร็จ")
    except Exception as e:
        print(f"❌ Error during Gemini processing: {e}")
        reply_to_line(reply_token, f"❌ เกิดข้อผิดพลาดในการประมวลผล AI: {e}")

# ============================================================
# 📁 ตัวประมวลผลรูปภาพ อัปโหลดเข้า Google Drive (Image Mode)
# ============================================================
def process_image_message(reply_token: str, user_id: str, message_id: str):
    print(f"📸 ได้รับรูปภาพจาก LINE User ID: {user_id}")
    
    # 1. เช็กสิทธิ์ GEAR_USER_ID หากมีการกำหนดไว้เฉพาะเจาะจง
    if GEAR_USER_ID and user_id != GEAR_USER_ID:
        print(f"ℹ️ ข้ามรูปจาก User ID {user_id} เนื่องจากมีการจำกัดสิทธิ์เฉพาะ GEAR_USER_ID")
        return

    # 2. เช็กสิทธิ์ ALLOWED_USER_ID ทั่วไป
    if not is_user_allowed(user_id):
        print(f"❌ Blocked image from unauthorized User ID: {user_id}")
        return

    # 3. เช็กการตั้งค่าโฟลเดอร์ Google Drive
    if not DRIVE_FOLDER_ID:
        print("❌ Error: DRIVE_FOLDER_ID is not set in Environment Variables")
        reply_to_line(reply_token, "❌ ระบบยังไม่ได้ตั้งค่า DRIVE_FOLDER_ID ในระบบคลาวด์ ไม่สามารถอัปโหลดรูปภาพได้ครับ")
        return

    # 4. เรียกใช้ Google Drive Service
    drive_service = get_drive_service()
    if not drive_service:
        reply_to_line(reply_token, "❌ ไม่สามารถเชื่อมต่อ Google Drive API ได้ กรุณาตรวจสอบการตั้งค่า Service Account")
        return

    try:
        # a. ดาวน์โหลดไฟล์รูปภาพจาก LINE Content API
        url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
        headers = {"Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}"}
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()
        image_bytes = response.content

        # b. ใช้ชื่อไฟล์จาก LINE Message ID ตรงๆ ({message_id}.jpg) เพื่อให้ระบบอื่นตรวจไฟล์ซ้ำได้
        ext = "jpg"
        content_type = response.headers.get("Content-Type", "").lower()
        if "png" in content_type:
            ext = "png"
        filename = f"{message_id}.{ext}"

        # c. อัปโหลดไฟล์เข้า Google Drive API v3 (เปิด supportsAllDrives=True)
        file_metadata = {
            "name": filename,
            "parents": [DRIVE_FOLDER_ID]
        }
        
        try:
            media = MediaIoBaseUpload(io.BytesIO(image_bytes), mimetype="image/jpeg", resumable=True)
            uploaded_file = drive_service.files().create(
                body=file_metadata,
                media_body=media,
                fields="id",
                supportsAllDrives=True
            ).execute()
            print(f"✅ อัปโหลดไฟล์ {filename} เข้า Google Drive สำเร็จ! (File ID: {uploaded_file.get('id')})")
            reply_to_line(reply_token, "ได้รับรูปแล้ว บันทึกเข้าระบบฟาร์มกุ้งเรียบร้อยครับ 📁")
            return
        except Exception as drive_err:
            print(f"⚠️ Drive API Upload failed ({drive_err}), trying GAS WebApp fallback...")
            if GAS_WEBAPP_URL:
                import base64
                base64_str = base64.b64encode(image_bytes).decode("utf-8")
                payload = {
                    "action": "upload_image",
                    "folder_id": DRIVE_FOLDER_ID,
                    "filename": filename,
                    "base64_data": base64_str
                }
                gas_res = requests.post(GAS_WEBAPP_URL, json=payload, timeout=20)
                gas_res.raise_for_status()
                gas_data = gas_res.json()
                if gas_data.get("status") == "success":
                    print(f"✅ อัปโหลดไฟล์ {filename} ผ่าน GAS WebApp สำเร็จ!")
                    reply_to_line(reply_token, "ได้รับรูปแล้ว บันทึกเข้าระบบฟาร์มกุ้งเรียบร้อยครับ 📁")
                    return
                else:
                    raise Exception(gas_data.get("message", "GAS Upload Failed"))
            else:
                raise drive_err

    except Exception as e:
        print(f"❌ เกิดข้อผิดพลาดในการอัปโหลดรูปภาพเข้า Google Drive: {e}")
        reply_to_line(reply_token, f"❌ เกิดข้อผิดพลาดในการบันทึกรูปภาพเข้าระบบ Google Drive: {e}")

# ============================================================
# Webhook Route & Health Check
# ============================================================
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    
    return "OK"

@app.route("/health", methods=["GET"])
def health():
    return "OK", 200

# ============================================================
# LINE Event Listeners
# ============================================================
@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    user_id = event.source.user_id
    reply_token = event.reply_token
    
    text = clean_mentions(event.message)
    
    print(f"📩 ได้รับข้อความจาก User ID: {user_id}")
    print(f"🧹 ข้อความที่ทำความสะอาดแล้ว: {text}")
    
    if not text:
        print("ℹ️ ข้อความว่างเปล่าหลังเคลียร์ @mention จึงข้ามการแปล")
        return
        
    thread = threading.Thread(target=process_text_message, args=(reply_token, user_id, text))
    thread.start()

@handler.add(MessageEvent, message=ImageMessage)
def handle_image_message(event):
    user_id = event.source.user_id
    message_id = event.message.id
    reply_token = event.reply_token
    
    print(f"📸 ได้รับข้อความภาพจาก User ID: {user_id}")
    
    thread = threading.Thread(target=process_image_message, args=(reply_token, user_id, message_id))
    thread.start()

if __name__ == "__main__":
    print("==================================================")
    print("🦐 น้องกุ้งนักแปล (Translator Bot v2.2) กำลังทำงาน...")
    print("🔗 Webhook URL พร้อมรับข้อมูลที่พอร์ต 5051")
    print("==================================================")
    app.run(host="0.0.0.0", port=5051)
