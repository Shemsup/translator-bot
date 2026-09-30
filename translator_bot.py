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
from linebot.models import MessageEvent, TextMessage, ImageMessage, FileMessage, TextSendMessage
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

from collections import deque
import sys

app = Flask(__name__)

# ระบบจัดเก็บ Logs ในหน่วยความจำ เพื่อเช็กสถานะสดผ่าน /logs
class LiveLogBuffer:
    def __init__(self, stream):
        self.stream = stream
        self.buffer = deque(maxlen=150)
    def write(self, msg):
        if msg.strip():
            ts = time.strftime('%H:%M:%S')
            self.buffer.append(f"[{ts}] {msg.strip()}")
        self.stream.write(msg)
    def flush(self):
        self.stream.flush()

log_buffer = LiveLogBuffer(sys.stdout)
sys.stdout = log_buffer
sys.stderr = log_buffer

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
    print("⚠️ Warning: LINE API keys are not set yet! Using dummy instances for safety.")
    line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN or "dummy_channel_access_token")
    handler = WebhookHandler(CHANNEL_SECRET or "dummy_channel_secret")

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
def is_user_allowed(user_id: str, target_id: str = None) -> bool:
    if not ALLOWED_USER_ID or ALLOWED_USER_ID in ["ใส่_LINE_USER_ID_ตรงนี้", "*", "ALL"]:
        return True
    allowed_list = [uid.strip() for uid in ALLOWED_USER_ID.split(",") if uid.strip()]
    if user_id and user_id in allowed_list:
        return True
    if target_id and target_id in allowed_list:
        return True
    if not user_id and not target_id:
        return True
    return False

# ============================================================
# 📚 ระบบ Glossary Cache (In-Memory โหลดทันที ไม่บล็อก Thread)
# ============================================================
def load_local_glossary() -> str:
    glossary_path = os.path.join(os.path.dirname(__file__), "glossary.md")
    if os.path.exists(glossary_path):
        try:
            with open(glossary_path, "r", encoding="utf-8") as f:
                lines = []
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and not line.startswith("---") and not line.startswith("`") and "=" in line:
                        lines.append(line)
                return "\n".join(lines)
        except Exception as e:
            print(f"⚠️ Error reading local glossary.md: {e}")
    return ""

GLOSSARY_CACHE = {
    "text": load_local_glossary(),
    "timestamp": time.time()
}
glossary_lock = threading.Lock()

def get_glossary(force_refresh: bool = False) -> str:
    with glossary_lock:
        if not force_refresh and GLOSSARY_CACHE["text"]:
            return GLOSSARY_CACHE["text"]
    text = load_local_glossary()
    with glossary_lock:
        GLOSSARY_CACHE["text"] = text
        GLOSSARY_CACHE["timestamp"] = time.time()
    return text or "No glossary found."

def add_glossary_term(thai_word: str, chinese_word: str):
    entry = f"{thai_word} = {chinese_word}"
    with glossary_lock:
        if GLOSSARY_CACHE["text"]:
            GLOSSARY_CACHE["text"] = entry + "\n" + GLOSSARY_CACHE["text"]
        else:
            GLOSSARY_CACHE["text"] = entry
        GLOSSARY_CACHE["timestamp"] = time.time()
    print(f"✅ เพิ่มคำศัพท์เข้าหน่วยความจำ: {entry}")

def invalidate_glossary_cache():
    with glossary_lock:
        GLOSSARY_CACHE["timestamp"] = 0
    print("🔄 Invalidate Glossary Cache เรียบร้อยแล้ว")

def sync_remote_glossary():
    if not CSV_URL:
        return
    try:
        res = requests.get(CSV_URL, timeout=10)
        if res.status_code == 200 and res.text:
            lines = []
            reader = csv.reader(io.StringIO(res.text))
            header = True
            for row in reader:
                if header:
                    header = False
                    continue
                if len(row) >= 2:
                    th = row[0].strip()
                    zh = row[1].strip()
                    if th and zh:
                        lines.append(f"{th} = {zh}")
            if lines:
                new_text = "\n".join(lines)
                with glossary_lock:
                    local_text = load_local_glossary()
                    combined = (local_text + "\n" + new_text) if local_text else new_text
                    seen = set()
                    unique_lines = []
                    for l in combined.split("\n"):
                        clean_l = l.strip()
                        if clean_l and clean_l not in seen:
                            seen.add(clean_l)
                            unique_lines.append(clean_l)
                    GLOSSARY_CACHE["text"] = "\n".join(unique_lines)
                    GLOSSARY_CACHE["timestamp"] = time.time()
                print(f"✅ ซิงค์คลังคำศัพท์จาก Google Sheets สำเร็จ ({len(lines)} รายการ)")
    except Exception as e:
        print(f"⚠️ ซิงค์คลังคำศัพท์ล้มเหลว (ใช้คลังท้องถิ่นแทน): {e}")

threading.Thread(target=sync_remote_glossary, daemon=True).start()

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
6. STRICTLY NO CONVERSATIONAL FILLER, NO EXPLANATIONS, NO PINYIN, NO VOCABULARY BREAKDOWNS. Output ONLY the translated text.
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
            
            generation_config = {
                "temperature": 0.2,
                "max_output_tokens": 3000,
            }
            model = genai.GenerativeModel("gemini-2.5-flash", system_instruction=prompt, generation_config=generation_config)
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
def reply_to_line(reply_token: str, text: str, target_id: str = None, user_id: str = None) -> bool:
    target = target_id or user_id
    if not text:
        return False

    # LINE TextSendMessage จำกัดไม่เกิน 5,000 ตัวอักษรต่อ 1 บับเบิ้ล
    # ถ้าข้อความยาวเกิน 4,500 ตัวอักษร ให้แบ่งเป็นหลายบับเบิ้ลต่อเนื่องกัน
    max_chunk = 4500
    if len(text) <= max_chunk:
        messages = [TextSendMessage(text=text)]
    else:
        chunks = [text[i:i + max_chunk] for i in range(0, len(text), max_chunk)]
        # LINE reply_message อนุญาตให้ส่ง Message objects ได้สูงสุด 5 รายการในครั้งเดียว
        messages = [TextSendMessage(text=c) for c in chunks[:5]]

    try:
        line_bot_api.reply_message(reply_token, messages)
        print("✅ ตอบกลับผ่าน LINE reply_token สำเร็จ")
        return True
    except Exception as e:
        print(f"❌ Fail to reply to LINE via reply_token: {e}")
        if target:
            try:
                print(f"🔄 กำลังลองส่งข้อความผ่าน push_message ไปยัง Target: {target}...")
                line_bot_api.push_message(target, messages)
                print("✅ ส่งผ่าน push_message สำเร็จ!")
                return True
            except Exception as push_err:
                print(f"❌ Fail to push to LINE: {push_err}")
    return False

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
            
    text = re.sub(r'(?:^|\s)@[^\s]+', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

# ============================================================
# ตัวหลักประมวลผลข้อความแปลภาษา (Text Mode)
# ============================================================
def process_text_message(reply_token: str, user_id: str, text: str, target_id: str = None):
    target = target_id or user_id
    if not is_user_allowed(user_id, target_id):
        print(f"❌ Blocked message from unauthorized User ID: {user_id} (Target: {target_id})")
        return

    text_strip = text.strip()

    # 1. 🔄 โหมดรีเซ็ตความจำ (++reset)
    if text_strip.lower() == "++reset":
        session_key = target or user_id
        with session_lock:
            chat_sessions_translate.pop(session_key, None)
            chat_sessions_explain.pop(session_key, None)
        print(f"🧹 รีเซ็ตความจำสำหรับ Session: {session_key}")
        reply_to_line(reply_token, "🔄 รีเซ็ตความจำเรียบร้อยแล้วครับ เริ่มนับหนึ่งใหม่!", target_id=target)
        return

    # 2. 📋 โหมดดูคำศัพท์ล่าสุด (++)
    if text_strip == "++":
        if not GAS_WEBAPP_URL:
            reply_to_line(reply_token, "❌ ไม่พบการตั้งค่า GAS_WEBAPP_URL บนระบบคลาวด์ ไม่สามารถดึงคลังคำศัพท์ได้", target_id=target)
            return
        
        try:
            response = requests.post(GAS_WEBAPP_URL, json={"action": "list"}, timeout=8)
            response.raise_for_status()
            res_data = response.json()
            
            if res_data.get("status") == "success":
                items = res_data.get("data", [])
                if not items:
                    reply_to_line(reply_token, "📋 ยังไม่มีคำศัพท์ถูกบันทึกไว้ในคลังคำศัพท์ครับ", target_id=target)
                else:
                    reply_lines = ["📋 คำศัพท์ 5 รายการล่าสุดในคลัง:"]
                    for idx, item in enumerate(items, 1):
                        reply_lines.append(f"{idx}. {item['thai']} = {item['chinese']}")
                    reply_to_line(reply_token, "\n".join(reply_lines), target_id=target)
            else:
                reply_to_line(reply_token, f"❌ ดึงข้อมูลจาก Sheets ล้มเหลว: {res_data.get('message')}", target_id=target)
        except Exception as e:
            reply_to_line(reply_token, f"❌ เกิดข้อผิดพลาดในการเชื่อมต่อ Google Sheets: {e}", target_id=target)
        return

    # 3. ✍️ โหมดบันทึกคำศัพท์ (+)
    if text_strip.startswith("+"):
        content = text_strip[1:].strip()
        if "=" not in content:
            reply_to_line(reply_token, "⚠️ รูปแบบคำสั่งบันทึกไม่ถูกต้อง\nกรุณาใช้: +[คำไทย] = [คำจีน]\nตัวอย่างเช่น: +สีกันสนิม = 防锈漆", target_id=target)
            return
            
        parts = content.split("=", 1)
        thai_word = parts[0].strip()
        chinese_word = parts[1].strip()
        
        if not thai_word or not chinese_word:
            reply_to_line(reply_token, "⚠️ กรุณากรอกทั้งคำไทยและคำจีนให้ครบถ้วน\nตัวอย่างเช่น: +สีกันสนิม = 防锈漆", target_id=target)
            return
            
        # เพิ่มเข้าหน่วยความจำทันทีเพื่อให้ AI นำไปใช้ได้ในแชทถัดไป
        add_glossary_term(thai_word, chinese_word)
        
        if not GAS_WEBAPP_URL:
            reply_to_line(reply_token, f"✍️ บันทึก \"{thai_word} = {chinese_word}\" เข้าหน่วยความจำเรียบร้อยแล้วครับ!", target_id=target)
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
                reply_to_line(reply_token, f"✍️ บันทึก \"{thai_word} = {chinese_word}\" ลง Google Sheets เรียบร้อยแล้วครับ!", target_id=target)
            else:
                reply_to_line(reply_token, f"✍️ บันทึกในบอทแล้ว แต่บันทึกลง Sheets ไม่สำเร็จ: {res_data.get('message')}", target_id=target)
        except Exception as e:
            reply_to_line(reply_token, f"✍️ บันทึกในบอทแล้ว แต่ส่งไป Sheets ล้มเหลว: {e}", target_id=target)
        return

    # 4. 💬 โหมดขอคำอธิบาย (?) หรือแชทปกติ
    is_explain_mode = False
    query_text = text_strip
    if text_strip.startswith("?") or text_strip.startswith("？"):
        is_explain_mode = True
        query_text = text_strip[1:].strip()

    # ใช้ target_id (group_id หรือ room_id) เป็น session key เพื่อให้ทุกคนในกลุ่มแชร์บริบทการสนทนาร่วมกัน
    session_key = target or user_id
    try:
        if is_explain_mode:
            chat = get_explain_chat(session_key)
        else:
            chat = get_translate_chat(session_key)
            
        response = chat.send_message(query_text)
        try:
            if response.candidates and response.candidates[0].finish_reason:
                fr_name = getattr(response.candidates[0].finish_reason, 'name', str(response.candidates[0].finish_reason))
                if "MAX_TOKENS" in fr_name:
                    print(f"⚠️ Warning: ข้อความถูกตัดจบเนื่องจากชนเพดาน Token! (finish_reason: {fr_name})")
        except Exception:
            pass
        translated_text = response.text.strip()
        delivered = reply_to_line(reply_token, translated_text, target_id=target)
        if delivered:
            print("✅ ประมวลผลและส่งข้อความผ่าน LINE สำเร็จ")
        else:
            print("⚠️ ประมวลผลสำเร็จ แต่ส่งข้อความผ่าน LINE ล้มเหลว")
            
        # บันทึก Chat Log ลงคิวเพื่อนำไปสกัดความรู้ประจำวัน
        enqueue_chat_log(user_id, target, query_text, translated_text, mode="explain" if is_explain_mode else "translate")
    except Exception as e:
        print(f"❌ Error during Gemini processing: {e}")
        reply_to_line(reply_token, f"❌ เกิดข้อผิดพลาดในการประมวลผล AI: {e}", target_id=target)

# ============================================================
# 📝 ระบบสะสมและบันทึก Chat Log ลง Google Sheet (Async Batched Queue)
# ============================================================
chat_log_queue = deque()
chat_log_lock = threading.Lock()
CHAT_LOG_BATCH_SIZE = 5
CHAT_LOG_FLUSH_INTERVAL = 30.0  # ตรวจสอบและส่งทุก 30 วินาที

def enqueue_chat_log(user_id: str, target_id: str, raw_text: str, translated_text: str, mode: str = "translate"):
    if not raw_text or not translated_text:
        return
    th_now = datetime.now(timezone(timedelta(hours=7))).strftime("%Y-%m-%d %H:%M:%S")
    entry = {
        "timestamp": th_now,
        "user_id": user_id or "unknown",
        "target_id": target_id or "direct",
        "mode": mode,
        "raw_text": raw_text,
        "translated_text": translated_text
    }
    with chat_log_lock:
        chat_log_queue.append(entry)
        q_len = len(chat_log_queue)
    print(f"📝 บันทึก Chat Log เข้าคิว (สะสม: {q_len} รายการ)")
    if q_len >= CHAT_LOG_BATCH_SIZE:
        threading.Thread(target=flush_chat_logs, daemon=True).start()

def flush_chat_logs():
    with chat_log_lock:
        if not chat_log_queue:
            return
        entries_to_flush = list(chat_log_queue)
        chat_log_queue.clear()
        
    if not GAS_WEBAPP_URL:
        print("⚠️ ไม่พบการตั้งค่า GAS_WEBAPP_URL ข้ามการส่ง Chat Log ไป Google Sheets")
        return

    payload = {
        "action": "log_chat",
        "entries": entries_to_flush
    }
    try:
        res = requests.post(GAS_WEBAPP_URL, json=payload, timeout=15)
        res.raise_for_status()
        res_data = res.json()
        if res_data.get("status") == "success":
            print(f"✅ บันทึก Chat Log {len(entries_to_flush)} รายการลง Google Sheet สำเร็จ!")
        else:
            print(f"⚠️ GAS log_chat warning: {res_data.get('message')}")
    except Exception as e:
        print(f"⚠️ ส่ง Chat Log ไปยัง GAS ล้มเหลว (เก็บกลับเข้าคิว): {e}")
        with chat_log_lock:
            for item in reversed(entries_to_flush):
                chat_log_queue.appendleft(item)

def chat_log_flusher_daemon():
    """ตรวจดูและส่ง Log เข้า Google Sheet เป็นระยะแบบอัตโนมัติ"""
    time.sleep(20)
    while True:
        try:
            with chat_log_lock:
                has_items = len(chat_log_queue) > 0
            if has_items:
                flush_chat_logs()
        except Exception as e:
            print(f"⚠️ Error in chat_log_flusher_daemon: {e}")
        time.sleep(CHAT_LOG_FLUSH_INTERVAL)

threading.Thread(target=chat_log_flusher_daemon, daemon=True).start()

# ============================================================
# 📁 ระบบคิวและอัปโหลดไฟล์เข้า Google Drive (GAS WebApp With Retry)
# ============================================================
# Lock เพื่อรับประกันการอัปโหลดเข้า GAS เป็นแบบ Sequential (ทีละ 1 ไฟล์)
# ป้องกันปัญหา Concurrency Limit และ Read Timeout ของ Google Apps Script ได้ 100%
gas_upload_lock = threading.Lock()

def upload_to_gas_with_retry(filename: str, file_bytes: bytes, max_retries: int = 3) -> dict:
    if not GAS_WEBAPP_URL:
        raise Exception("ไม่ได้ตั้งค่า GAS_WEBAPP_URL บนระบบคลาวด์")
    if not DRIVE_FOLDER_ID:
        raise Exception("ไม่ได้ตั้งค่า DRIVE_FOLDER_ID บนระบบคลาวด์")

    import base64
    base64_str = base64.b64encode(file_bytes).decode("utf-8")
    payload = {
        "action": "upload_image",
        "folder_id": DRIVE_FOLDER_ID,
        "filename": filename,
        "base64_data": base64_str
    }

    with gas_upload_lock:
        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                # ตั้ง timeout 45 วินาที เพื่อรองรับรูปความละเอียดสูง
                res = requests.post(GAS_WEBAPP_URL, json=payload, timeout=45)
                res.raise_for_status()
                data = res.json()
                if data.get("status") == "success":
                    return data
                else:
                    err_msg = data.get("message", "GAS returned failure")
                    last_error = Exception(f"GAS API Error: {err_msg}")
                    print(f"⚠️ GAS attempt {attempt}/{max_retries} error for {filename}: {err_msg}")
            except Exception as e:
                last_error = e
                print(f"⚠️ GAS attempt {attempt}/{max_retries} failed for {filename}: {e}")

            if attempt < max_retries:
                sleep_time = 2 * attempt
                print(f"⏳ รอรอบถัดไป {sleep_time} วินาทีก่อนลองใหม่ ({filename})...")
                time.sleep(sleep_time)

        raise last_error

# ============================================================
# 📦 ระบบ Batch Debounce Collector สำหรับรวบรวมรูปภาพที่ส่งพร้อมกัน
# ============================================================
image_batch_lock = threading.Lock()
active_image_batches = {}

def enqueue_image_message(reply_token: str, user_id: str, message_id: str, target_id: str):
    target = target_id or user_id
    if not is_user_allowed(user_id, target_id):
        print(f"❌ Blocked image from unauthorized User ID: {user_id}")
        return

    with image_batch_lock:
        if target not in active_image_batches:
            active_image_batches[target] = {
                "items": [],
                "timer": None
            }

        batch = active_image_batches[target]
        batch["items"].append({
            "message_id": message_id,
            "reply_token": reply_token,
            "user_id": user_id,
            "target_id": target,
            "timestamp": time.time()
        })

        # รีเซ็ต Timer หน่วงเวลา 2.0 วินาที เพื่อรอรวบรวมรูปที่ส่งมาในชุดเดียวกัน
        if batch["timer"]:
            batch["timer"].cancel()

        timer = threading.Timer(2.0, process_image_batch, args=(target,))
        batch["timer"] = timer
        timer.start()

def process_image_batch(target_id: str):
    with image_batch_lock:
        batch = active_image_batches.pop(target_id, None)

    if not batch or not batch["items"]:
        return

    items = batch["items"]
    total_count = len(items)
    print(f"📦 เริ่มประมวลผลชุดรูปภาพ {total_count} รูป สำหรับ Target: {target_id}...")

    # ใช้ reply_token ล่าสุดในชุด
    latest_reply_token = items[-1]["reply_token"]
    user_id = items[-1]["user_id"]

    success_count = 0
    failed_items = []

    for idx, item in enumerate(items, 1):
        msg_id = item["message_id"]
        try:
            # 1. ดาวน์โหลดไฟล์รูปภาพจาก LINE Content API
            url = f"https://api-data.line.me/v2/bot/message/{msg_id}/content"
            headers = {"Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}"}
            response = requests.get(url, headers=headers, timeout=25)
            response.raise_for_status()
            image_bytes = response.content

            # 2. นามสกุลไฟล์
            ext = "png" if "png" in response.headers.get("Content-Type", "").lower() else "jpg"
            filename = f"{msg_id}.{ext}"

            # 3. อัปโหลดเข้า Google Drive ผ่าน GAS WebApp แบบมีคิวและ Retry
            res_data = upload_to_gas_with_retry(filename, image_bytes)
            print(f"✅ ({idx}/{total_count}) อัปโหลด {filename} ผ่าน GAS WebApp สำเร็จ! (File ID: {res_data.get('id')})")
            success_count += 1
        except Exception as e:
            print(f"❌ ({idx}/{total_count}) อัปโหลดรูป {msg_id} ล้มเหลว: {e}")
            failed_items.append(msg_id)

    # 4. ส่งข้อความสรุปผลกลับไปยัง LINE เพียง 1 ข้อความ (ไม่รกแชท และประหยัดโควตา Push Message)
    if total_count == 1:
        if success_count == 1:
            reply_to_line(latest_reply_token, "ได้รับรูปแล้ว บันทึกเข้าระบบฟาร์มกุ้งเรียบร้อยครับ 📁", target_id=target_id, user_id=user_id)
        else:
            reply_to_line(latest_reply_token, "❌ ไม่สามารถบันทึกรูปภาพได้ กรุณาลองใหม่อีกครั้งครับ", target_id=target_id, user_id=user_id)
    else:
        if success_count == total_count:
            reply_to_line(latest_reply_token, f"✅ ได้รับรูปภาพครบทั้ง {total_count} รูป บันทึกเข้าระบบฟาร์มกุ้งเรียบร้อยครับ 📁", target_id=target_id, user_id=user_id)
        elif success_count > 0:
            reply_to_line(latest_reply_token, f"⚠️ บันทึกรูปภาพสำเร็จ {success_count}/{total_count} รูป (มี {len(failed_items)} รูปขัดข้อง) เข้าระบบฟาร์มกุ้งแล้วครับ 📁", target_id=target_id, user_id=user_id)
        else:
            reply_to_line(latest_reply_token, f"❌ เกิดข้อผิดพลาด ไม่สามารถบันทึกรูปภาพทั้ง {total_count} รูปได้ กรุณาลองใหม่อีกครั้งครับ", target_id=target_id, user_id=user_id)

# ============================================================
# 📄 ตัวประมวลผลไฟล์เอกสาร เช่น บิล PDF จากซัพพลายเออร์ (File Mode)
# ============================================================
def process_file_message(reply_token: str, user_id: str, message_id: str, original_filename: str, target_id: str = None):
    target = target_id or user_id
    print(f"📄 ได้รับไฟล์เอกสารจาก LINE: {original_filename} (User ID: {user_id}, Target: {target})")

    if not is_user_allowed(user_id, target_id):
        print(f"❌ Blocked file from unauthorized User ID: {user_id}")
        return

    if not DRIVE_FOLDER_ID:
        reply_to_line(reply_token, "❌ ระบบยังไม่ได้ตั้งค่า DRIVE_FOLDER_ID ในระบบคลาวด์ ไม่สามารถอัปโหลดไฟล์ได้ครับ", target_id=target)
        return

    try:
        url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
        headers = {"Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}"}
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        file_bytes = response.content

        safe_filename = original_filename or f"{message_id}.pdf"
        res_data = upload_to_gas_with_retry(safe_filename, file_bytes)
        print(f"✅ อัปโหลดไฟล์เอกสาร {safe_filename} ผ่าน GAS WebApp สำเร็จ! (File ID: {res_data.get('id')})")
        reply_to_line(reply_token, f"ได้รับเอกสาร \"{safe_filename}\" บันทึกเข้าระบบฟาร์มกุ้งเรียบร้อยครับ 📁", target_id=target)
    except Exception as e:
        print(f"❌ เกิดข้อผิดพลาดในการอัปโหลดไฟล์: {e}")
        reply_to_line(reply_token, f"❌ เกิดข้อผิดพลาดในการบันทึกเอกสาร: {e}", target_id=target)

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
    ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    print(f"💓 Health Ping received from {ip}")
    return "OK", 200

@app.route("/debug-config", methods=["GET"])
def debug_config():
    return {
        "ALLOWED_USER_ID": ALLOWED_USER_ID,
        "GEAR_USER_ID": GEAR_USER_ID,
        "DRIVE_FOLDER_ID": DRIVE_FOLDER_ID,
        "HAS_GAS": bool(GAS_WEBAPP_URL),
        "HAS_SERVICE_ACCOUNT": bool(GOOGLE_SERVICE_ACCOUNT_JSON)
    }, 200

@app.route("/logs", methods=["GET"])
def get_logs():
    return "<pre style='font-family:monospace; background:#111; color:#0f0; padding:15px; border-radius:8px; line-height:1.4;'>" + "\n".join(list(log_buffer.buffer)) + "</pre>", 200

# ============================================================
# LINE Event Listeners
# ============================================================
@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    target_id = getattr(event.source, 'group_id', None) or getattr(event.source, 'room_id', None) or getattr(event.source, 'user_id', None)
    user_id = getattr(event.source, 'user_id', None)
    reply_token = event.reply_token
    
    text = clean_mentions(event.message)
    
    print(f"📩 ได้รับข้อความจาก User ID: {user_id} (Target: {target_id})")
    print(f"🧹 ข้อความที่ทำความสะอาดแล้ว: {text}")
    
    if not text:
        print("ℹ️ ข้อความว่างเปล่าหลังเคลียร์ @mention จึงข้ามการแปล")
        return
        
    thread = threading.Thread(target=process_text_message, args=(reply_token, user_id, text, target_id))
    thread.start()

@handler.add(MessageEvent, message=ImageMessage)
def handle_image_message(event):
    target_id = getattr(event.source, 'group_id', None) or getattr(event.source, 'room_id', None) or getattr(event.source, 'user_id', None)
    user_id = getattr(event.source, 'user_id', None)
    message_id = event.message.id
    reply_token = event.reply_token
    
    print(f"📸 ได้รับข้อความภาพ ID: {message_id} จาก User: {user_id} (Target: {target_id})")
    enqueue_image_message(reply_token, user_id, message_id, target_id)

@handler.add(MessageEvent, message=FileMessage)
def handle_file_message(event):
    target_id = getattr(event.source, 'group_id', None) or getattr(event.source, 'room_id', None) or getattr(event.source, 'user_id', None)
    user_id = getattr(event.source, 'user_id', None)
    message_id = event.message.id
    original_filename = getattr(event.message, 'file_name', f"{message_id}.pdf")
    reply_token = event.reply_token

    print(f"📄 ได้รับไฟล์เอกสาร {original_filename} จาก User: {user_id} (Target: {target_id})")
    thread = threading.Thread(target=process_file_message, args=(reply_token, user_id, message_id, original_filename, target_id))
    thread.start()

# ============================================================
# 💓 Keep-Alive Daemon Worker (ป้องกัน Render Free Tier หลับ)
# ============================================================
def keep_alive_worker():
    time.sleep(30)
    print("💓 เริ่มต้นระบบ Keep-Alive daemon ป้องกันเซิร์ฟเวอร์หลับ...")
    while True:
        try:
            r = requests.get("https://translator-bot-le71.onrender.com/health", timeout=15)
            print(f"💓 Keep-alive ping สำเร็จ (Status: {r.status_code})")
        except Exception as e:
            print(f"⚠️ Keep-alive ping failed: {e}")
        time.sleep(8 * 60) # ping ทุก 8 นาที ก่อน Render จะหลับที่นาทีที่ 15

threading.Thread(target=keep_alive_worker, daemon=True).start()

if __name__ == "__main__":
    print("==================================================")
    print("🦐 น้องกุ้งนักแปล (Translator Bot v2.2) กำลังทำงาน...")
    print("🔗 Webhook URL พร้อมรับข้อมูลที่พอร์ต 5051")
    print("==================================================")
    app.run(host="0.0.0.0", port=5051)
