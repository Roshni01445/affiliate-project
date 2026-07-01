import telebot
from telebot import types
from telebot import apihelper
import threading
import uuid
import time
import os
import requests
import json
import base64
import tempfile
import re
import shutil
from flask import Flask, jsonify, request, send_file
from PIL import Image
import pillow_avif
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# CONFIGURATION
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GOOGLE_SHEET_WEBHOOK_URL = os.environ.get(
    "GOOGLE_SHEET_WEBHOOK_URL",
    "https://script.google.com/macros/s/AKfycbzOpYU6DGMqNtoFBg8E_i7kq4_rhI1I6AveEqfPcpkJQcjXFtUjmObzSM8_iFwnczGm/exec",
)
N8N_TRIGGER_URL = os.environ.get("N8N_TRIGGER_URL", "https://n8n-production-3b51.up.railway.app/webhook-test/n8n-trigger")
N8N_POSTING_URL = os.environ.get("N8N_POSTING_URL", "https://n8n-production-3b51.up.railway.app/webhook-test/posting")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is required")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_PATH = os.path.join(BASE_DIR, "results.json")
IMAGE_DIR = os.path.join(BASE_DIR, "generated_images")
os.makedirs(IMAGE_DIR, exist_ok=True)

RESULTS_LOCK = threading.Lock()
PROFILE_LOCK = threading.Lock()
JOB_TIMEOUT_SECONDS = 900
DEFAULT_CHROME_PATHS = [
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

app = Flask(__name__)

bot = telebot.TeleBot(BOT_TOKEN)

# Increase timeout to handle connection lag
apihelper.CONNECT_TIMEOUT = 60
apihelper.READ_TIMEOUT = 60

user_states = {}

def get_state(chat_id):
    if chat_id not in user_states:
        user_states[chat_id] = {
            "unique_id": str(uuid.uuid4().hex)[:8].upper(),
            "meta_api": "",
            "instagram_id": "",
            "flow_type": "",
            "content_format": "",
            "media_type": "",  # Automatically set based on format
            "details_or_prompt": "",
            "price": "",
            "source": "",
            "caption": "",
            "link_mode": "",
            "generation_mode": "image",
            "generated_url": "" # Temporarily stores the Telegram URL of AI image for approval
        }
    return user_states[chat_id]

def resolve_chrome_executable_path():
    env_path = os.environ.get("CHROME_EXECUTABLE_PATH")
    if env_path and os.path.exists(env_path):
        return env_path
    for path in DEFAULT_CHROME_PATHS:
        if os.path.exists(path):
            return path
    return None

def build_image_prompt(price, custom_prompt=""):
    price_str = str(price).strip()
    if not price_str:
        price_str = "0"

    clean_digits = re.sub(r'[^\d]', '', price_str)
    try:
        base_price = int(clean_digits) if clean_digits else 0
    except ValueError:
        base_price = 0

    fake_high_mrp = round(base_price * 2)

    if custom_prompt:
        return (
            f"{custom_prompt}\n\n"
            f"Include clean overlay text across the top frame. "
            f"At the bottom frame corner, include text: 'MRP: ₹{fake_high_mrp} | Special Deal: ₹{clean_digits}'."
        )

    return (
        f"A premium 4:5 vertical fashion lookbook photograph for 'Kyra's Closet Finds'. "
        f"A stunning young Indian girl model posing warmly, looking directly at the camera. "
        f"She is wearing the exact same outfit pattern, base color, and suit design from the attached reference garment. "
        f"Style the look with elegant white trousers. The background is a clean, plain light-cream studio paper background. "
        f"Include clean overlay text across the top frame frame: 'Kyra's Closet Finds'. "
        f"At the bottom frame corner, include text: 'MRP: ₹{fake_high_mrp} | Special Deal: ₹{clean_digits}'."
    )

def build_video_prompt():
    return (
        "Animate this fashion model image with subtle, natural human motion. Keep the camera static. "
        "Maintain 1-to-1 consistency for her face, outfit details, and the background without morphing."
    )

def load_results():
    if not os.path.exists(RESULTS_PATH):
        return {}
    with open(RESULTS_PATH, "r", encoding="utf-8") as file_handle:
        return json.load(file_handle)

def save_results(data):
    with open(RESULTS_PATH, "w", encoding="utf-8") as file_handle:
        json.dump(data, file_handle)

def update_result(job_id, payload):
    with RESULTS_LOCK:
        data = load_results()
        data[job_id] = payload
        save_results(data)

def start_generation_job(chat_id, generation_mode):
    state = get_state(chat_id)
    state["generation_mode"] = generation_mode
    job_id = state["unique_id"].lower()
    threading.Thread(
        target=run_job,
        args=(job_id, state["price"], state["source"], state["details_or_prompt"], generation_mode),
        daemon=True,
    ).start()
    threading.Thread(target=monitor_and_request_approval, args=(chat_id, job_id), daemon=True).start()

def sync_and_trigger_webhooks(chat_id, target_n8n_url):
    """Fires the payload to BOTH Google Sheets and n8n via POST request."""
    state = get_state(chat_id)
    
    # We do not send the prompt to Google Sheets, only the final details
    payload = {
        "unique_id": state["unique_id"],
        "chat_id": str(chat_id),
        "flow_type": state["flow_type"],
        "meta_api": state["meta_api"],
        "instagram_id": state["instagram_id"],
        "content_format": state["content_format"],
        "media_type": state["media_type"],
        "price": state["price"],
        "source": state["source"],
        "caption": state["caption"]
    }
    
    def fire_requests():
        # 1. Log to Google Sheets
        try:
            requests.post(GOOGLE_SHEET_WEBHOOK_URL, json=payload, timeout=15)
            print(f"[{state['unique_id']}] ✅ Synced to Google Sheets.")
        except Exception as e:
            print(f"[{state['unique_id']}] ⚠️ Google Sheet sync failed: {e}")
            
        # 2. Trigger n8n Automation
        try:
            # POST request is required for n8n to catch the JSON payload
            response = requests.post(target_n8n_url, json=payload, timeout=15)
            print(f"[{state['unique_id']}] 🚀 n8n Webhook Status: {response.status_code} ({target_n8n_url.split('/')[-1]})")
        except Exception as e:
            print(f"[{state['unique_id']}] ❌ n8n webhook trigger failed: {e}")

    threading.Thread(target=fire_requests, daemon=True).start()

def _download_telegram_image(image_url, job_id):
    temp_dir = tempfile.gettempdir()
    temp_original_path = os.path.join(temp_dir, f"direct_upload_original_{job_id}.tmp")
    temp_image_path = os.path.join(temp_dir, f"direct_upload_{job_id}.png")

    response = requests.get(image_url, stream=True, timeout=20)
    if response.status_code != 200:
        raise RuntimeError(f"Failed to download image from Telegram. (HTTP {response.status_code})")

    with open(temp_original_path, "wb") as file_handle:
        for chunk in response.iter_content(1024):
            file_handle.write(chunk)

    image = Image.open(temp_original_path)
    if image.mode in ("RGBA", "P"):
        image = image.convert("RGB")
    image.save(temp_image_path, format="PNG")
    return temp_image_path

def _generate_asset_with_gemini(input_image_path, prompt_text, output_path, asset_kind):
    chrome_path = resolve_chrome_executable_path()
    prompt_selector = 'rich-textarea, div[id="utterance-input"], div[contenteditable="true"], textarea, chat-input'

    with PROFILE_LOCK, sync_playwright() as playwright:
        launch_args = {
            "user_data_dir": os.path.join(BASE_DIR, "chrome_automation_profile"),
            "headless": True,
            "accept_downloads": True,
            "ignore_default_args": ["--enable-automation"],
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-web-security",
            ],
        }
        if chrome_path:
            launch_args["executable_path"] = chrome_path

        context = playwright.chromium.launch_persistent_context(**launch_args)
        context.grant_permissions(["clipboard-read", "clipboard-write"], origin="https://gemini.google.com")

        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(180000)
            page.set_default_navigation_timeout(180000)
            page.goto("https://gemini.google.com/app", wait_until="domcontentloaded")
            page.wait_for_selector(prompt_selector, timeout=60000, state="visible")

            chat_box = page.locator(prompt_selector).first
            image_b64 = base64.b64encode(open(input_image_path, "rb").read()).decode("ascii")

            page.evaluate(
                "async ({ text }) => { await navigator.clipboard.writeText(text); }",
                {"text": prompt_text},
            )
            chat_box.click()
            chat_box.press("Control+V")
            page.wait_for_timeout(1500)

            page.evaluate(
                """async ({ imageB64, mimeType }) => {
                    const dataUrl = `data:${mimeType};base64,${imageB64}`;
                    const response = await fetch(dataUrl);
                    const blob = await response.blob();
                    await navigator.clipboard.write([new ClipboardItem({ [mimeType]: blob })]);
                }""",
                {"imageB64": image_b64, "mimeType": "image/png"},
            )
            chat_box.click()
            chat_box.press("Control+V")
            page.wait_for_timeout(3000)

            send_button = page.locator(
                'button[aria-label="Send message"], button.send-button, mat-icon:has-text("send"), button[type="submit"], button[data-testid="send-button"]'
            ).first
            send_button.click()

            if asset_kind == "video":
                page.wait_for_timeout(12000)
                try:
                    page.locator('text=Generating video, text=Creating your video, div[aria-label*="video"]').first.wait_for(state="visible", timeout=25000)
                except PlaywrightTimeoutError:
                    pass
            else:
                page.wait_for_timeout(10000)

            for _ in range(12):
                try:
                    download_button = page.locator(
                        'button[aria-label*="Download"], button:has-text("Download"), a[aria-label*="Download"], '
                        'button[aria-label*="Download video"], a[download][href*="video"], button[data-test-id="download-image-button"]'
                    ).first
                    if download_button.count() > 0 and download_button.is_visible():
                        with page.expect_download(timeout=30000) as download_info:
                            download_button.click()
                        download_info.value.save_as(output_path)
                        return
                except Exception:
                    pass

                if asset_kind == "video":
                    try:
                        video_b64 = page.evaluate("""async () => {
                            const video = document.querySelector('video');
                            if (!video || !video.src) return null;
                            const response = await fetch(video.src);
                            const blob = await response.blob();
                            return new Promise((resolve) => {
                                const reader = new FileReader();
                                reader.onloadend = () => resolve(reader.result);
                                reader.readAsDataURL(blob);
                            });
                        }""")
                        if video_b64 and "base64," in video_b64:
                            with open(output_path, "wb") as file_handle:
                                file_handle.write(base64.b64decode(video_b64.split("base64,")[1]))
                            return
                    except Exception:
                        pass
                else:
                    try:
                        image_probe = page.evaluate("""() => {
                            const candidates = Array.from(document.querySelectorAll('img')).map((img) => ({
                                src: img.getAttribute('src') || '',
                                w: img.naturalWidth || 0,
                                h: img.naturalHeight || 0,
                            }));
                            const googleImages = candidates.filter((img) => img.src.includes('googleusercontent'));
                            const filtered = googleImages.filter((img) => !img.src.includes('s64') && !img.src.includes('=s64') && img.w >= 256 && img.h >= 256);
                            const blobImages = candidates.filter((img) => img.src.startsWith('blob:'));
                            if (filtered.length > 0) {
                                filtered.sort((a, b) => (b.w * b.h) - (a.w * a.h));
                                return { final: filtered[0].src };
                            }
                            return { final: blobImages[0]?.src || null };
                        }""")
                        final_image_url = image_probe.get("final")
                        if final_image_url and final_image_url.startswith("http"):
                            fallback_response = requests.get(final_image_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
                            if fallback_response.status_code == 200:
                                with open(output_path, "wb") as file_handle:
                                    file_handle.write(fallback_response.content)
                                return
                    except Exception:
                        pass

                page.wait_for_timeout(4000)

            raise RuntimeError(f"Failed to capture Gemini {asset_kind} asset.")
        finally:
            context.close()

def run_job(job_id, price, image_url, custom_prompt="", generation_mode="both"):
    job_done = threading.Event()

    def safe_update(payload):
        if not job_done.is_set():
            update_result(job_id, payload)

    def finish_success(payload):
        safe_update(payload)
        job_done.set()

    def timeout_handler():
        if not job_done.is_set():
            update_result(job_id, {"status": "error", "message": "Job timed out"})
            job_done.set()

    timeout_timer = threading.Timer(JOB_TIMEOUT_SECONDS, timeout_handler)
    timeout_timer.daemon = True
    timeout_timer.start()

    temp_image_path = None
    downloaded_img_path = os.path.join(IMAGE_DIR, f"{job_id}.png")
    downloaded_vid_path = os.path.join(IMAGE_DIR, f"{job_id}.mp4")

    try:
        safe_update({"status": "processing", "message": "Downloading provided user image..."})
        temp_image_path = _download_telegram_image(image_url, job_id)

        if generation_mode in ("image", "both"):
            image_prompt_text = build_image_prompt(price, custom_prompt)
            safe_update({"status": "processing", "message": "Opening Gemini Session for image generation..."})
            _generate_asset_with_gemini(temp_image_path, image_prompt_text, downloaded_img_path, "image")
            if generation_mode == "image":
                finish_success({"status": "success", "asset_type": "image", "image_url": f"/image/{job_id}.png", "message": "Success!"})
                timeout_timer.cancel()
                return

        if generation_mode in ("video", "both"):
            video_prompt_text = build_video_prompt()
            source_for_video = downloaded_img_path if generation_mode == "both" and os.path.exists(downloaded_img_path) else temp_image_path
            safe_update({"status": "processing", "message": "Starting video generation..."})
            _generate_asset_with_gemini(source_for_video, video_prompt_text, downloaded_vid_path, "video")
            finish_success({"status": "success", "asset_type": "video", "video_url": f"/video/{job_id}.mp4", "message": "Success!"})
            timeout_timer.cancel()
            return

        raise RuntimeError(f"Unsupported generation mode: {generation_mode}")
    except Exception as exc:
        safe_update({"status": "error", "message": f"Automation error: {str(exc)}"})
        job_done.set()
        timeout_timer.cancel()

@app.route("/generate-collage", methods=["POST"])
def generate_collage():
    data = request.get_json(silent=True) or {}
    price = data.get("price")
    image_url = data.get("image_url")
    custom_prompt = data.get("prompt", "")
    generation_mode = data.get("generation_mode", "both")

    if not price or not image_url:
        return jsonify({"status": "error", "message": "Missing price or image_url"}), 400

    job_id = uuid.uuid4().hex
    update_result(job_id, {"status": "pending"})
    threading.Thread(target=run_job, args=(job_id, price, image_url, custom_prompt, generation_mode), daemon=True).start()

    return jsonify({"status": "accepted", "job_id": job_id}), 202

@app.route("/result/<job_id>", methods=["GET"])
def get_result(job_id):
    with RESULTS_LOCK:
        data = load_results()
    if job_id not in data:
        return jsonify({"status": "error", "message": "Unknown job_id"}), 404
    return jsonify(data[job_id])

@app.route("/image/<filename>", methods=["GET"])
def get_image(filename):
    job_id = filename.split('.')[0]
    out_path = os.path.join(IMAGE_DIR, f"{job_id}.png")
    if os.path.exists(out_path):
        return send_file(out_path, mimetype="image/png")
    if os.path.exists(IMAGE_DIR):
        for file_name in os.listdir(IMAGE_DIR):
            if file_name.startswith(job_id):
                return send_file(os.path.join(IMAGE_DIR, file_name))
    return jsonify({"status": "error", "message": "Image not found"}), 404

@app.route("/video/<filename>", methods=["GET"])
def get_video(filename):
    job_id = filename.split('.')[0]
    video_path = os.path.join(IMAGE_DIR, f"{job_id}.mp4")
    if os.path.exists(video_path):
        return send_file(video_path, mimetype="video/mp4")
    return jsonify({"status": "error", "message": "Video not found"}), 404

@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "ok"})

# ==========================================
# FLOW ENTRY
# ==========================================

@bot.message_handler(commands=['start'])
def send_welcome(message):
    chat_id = message.chat.id
    if chat_id in user_states: user_states.pop(chat_id)
    
    bot.send_message(chat_id, "👋 Welcome to Lookbook.ai Automation Master Layer!")
    msg = bot.send_message(chat_id, "🔑 Please provide your Meta Graph API Key:")
    bot.register_next_step_handler(msg, process_meta_api)

def process_meta_api(message):
    chat_id = message.chat.id
    get_state(chat_id)["meta_api"] = message.text
    msg = bot.send_message(chat_id, "📸 Now, please provide your target Instagram Business Account ID:")
    bot.register_next_step_handler(msg, process_insta_id)

def process_insta_id(message):
    chat_id = message.chat.id
    get_state(chat_id)["instagram_id"] = message.text
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("Affiliate Marketing", callback_data="main_affiliate"),
        types.InlineKeyboardButton("Normal Post / Ads", callback_data="main_normal")
    )
    bot.send_message(chat_id, "Configuration locked! Select core pipeline:", reply_markup=markup)

# ==========================================
# AFFILIATE FLOW
# ==========================================

@bot.callback_query_handler(func=lambda call: call.data == "main_affiliate")
def select_affiliate_branch(call):
    chat_id = call.message.chat.id
    get_state(chat_id)["flow_type"] = "Affiliate Marketing"
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("1 Image", callback_data="aff_1img"),
        types.InlineKeyboardButton("2 Images", callback_data="aff_2img"),
        types.InlineKeyboardButton("Carousel Pack", callback_data="aff_carousel"),
        types.InlineKeyboardButton("Reels", callback_data="aff_reel")
    )
    bot.send_message(chat_id, "Select your preferred format:", reply_markup=markup)

# --- 1. HANDLING 1 IMAGE / 2 IMAGES ---
@bot.callback_query_handler(func=lambda call: call.data in ["aff_1img", "aff_2img"])
def handle_affiliate_image_selections(call):
    chat_id = call.message.chat.id
    state = get_state(chat_id)
    
    # Analyze and assign BOTH format and media_type automatically
    if call.data == "aff_1img":
        state["content_format"] = "1 Image"
        state["media_type"] = "IMAGE"
    else:
        state["content_format"] = "2 Images"
        state["media_type"] = "CAROUSEL"

    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("Generate via AI (Existing Image)", callback_data="aff_mode_ai_existing"),
        types.InlineKeyboardButton("Post directly (No AI)", callback_data="aff_mode_manual")
    )
    bot.send_message(chat_id, "Choose intake model:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("aff_mode_"))
def process_affiliate_image_intake(call):
    chat_id = call.message.chat.id
    if call.data == "aff_mode_ai_existing":
        msg = bot.send_message(chat_id, "Please enter the prompt/details for the AI to edit the image:")
        bot.register_next_step_handler(msg, process_ai_custom_prompt)
    else:
        msg = bot.send_message(chat_id, "Please upload your Image directly to post:")
        bot.register_next_step_handler(msg, handle_manual_photo)

# ---> TRACK: Edit Existing Image via AI
def process_ai_custom_prompt(message):
    chat_id = message.chat.id
    get_state(chat_id)["details_or_prompt"] = message.text
    msg = bot.send_message(chat_id, "Please upload the product image you want to edit:")
    bot.register_next_step_handler(msg, process_ai_image_upload)

def process_ai_image_upload(message):
    chat_id = message.chat.id
    if not message.photo:
        msg = bot.send_message(chat_id, "That doesn't look like an image. Please upload a Photo:")
        bot.register_next_step_handler(msg, process_ai_image_upload)
        return
        
    file_info = bot.get_file(message.photo[-1].file_id)
    # Store the direct telegram file path URL as the source
    get_state(chat_id)["source"] = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"
    
    msg = bot.send_message(chat_id, "Input Price (e.g. ₹399):")
    bot.register_next_step_handler(msg, process_ai_price)

def process_ai_price(message):
    chat_id = message.chat.id
    get_state(chat_id)["price"] = message.text
    msg = bot.send_message(chat_id, "Send Caption:")
    bot.register_next_step_handler(msg, process_ai_caption)

def process_ai_caption(message):
    chat_id = message.chat.id
    state = get_state(chat_id)
    state["caption"] = message.text
    
    bot.send_message(chat_id, "Pipeline Triggered! Generating content via AI using your uploaded image...")

    start_generation_job(chat_id, "image")

# ---> TRACK: Post Directly (No AI)
def handle_manual_photo(message):
    chat_id = message.chat.id
    if not message.photo:
        msg = bot.send_message(chat_id, "That doesn't look like an image. Please upload a Photo:")
        bot.register_next_step_handler(msg, handle_manual_photo)
        return
        
    file_info = bot.get_file(message.photo[-1].file_id)
    get_state(chat_id)["source"] = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"
    
    msg = bot.send_message(chat_id, "Image received. Send Caption:")
    bot.register_next_step_handler(msg, process_manual_caption)

def process_manual_caption(message):
    chat_id = message.chat.id
    get_state(chat_id)["caption"] = message.text
    
    msg = bot.send_message(chat_id, "Send Price:")
    bot.register_next_step_handler(msg, process_manual_price)

def process_manual_price(message):
    chat_id = message.chat.id
    get_state(chat_id)["price"] = message.text
    
    bot.send_message(chat_id, "Synced! Sending to posting pipeline...")
    # Send directly to posting since we are bypassing AI
    sync_and_trigger_webhooks(chat_id, N8N_POSTING_URL)

# --- 2. HANDLING CAROUSEL PACK ---
@bot.callback_query_handler(func=lambda call: call.data == "aff_carousel")
def handle_affiliate_carousel(call):
    chat_id = call.message.chat.id
    state = get_state(chat_id)
    state["content_format"] = "Carousel Pack"
    state["media_type"] = "CAROUSEL" # Automatically set
    
    msg = bot.send_message(chat_id, "How many images will compose your carousel display deck?")
    bot.register_next_step_handler(msg, process_carousel_count)

def process_carousel_count(message):
    chat_id = message.chat.id
    get_state(chat_id)["details_or_prompt"] = f"Count: {message.text}"
    msg = bot.send_message(chat_id, "Please pass the multiple product links or direct image endpoints sequentially:")
    bot.register_next_step_handler(msg, process_carousel_links)

def process_carousel_links(message):
    chat_id = message.chat.id
    get_state(chat_id)["source"] = message.text
    msg = bot.send_message(chat_id, "What is the Product Price? (e.g. ₹499)")
    bot.register_next_step_handler(msg, process_carousel_price)

def process_carousel_price(message):
    chat_id = message.chat.id
    get_state(chat_id)["price"] = message.text
    msg = bot.send_message(chat_id, "Send Caption:")
    bot.register_next_step_handler(msg, process_carousel_caption)

def process_carousel_caption(message):
    chat_id = message.chat.id
    get_state(chat_id)["caption"] = message.text
    bot.send_message(chat_id, "🎉 Data stored securely. Triggering pipeline...")
    # Generally Carousel links imply a need for generation
    sync_and_trigger_webhooks(chat_id, N8N_TRIGGER_URL)

# --- 3. HANDLING REELS ---
@bot.callback_query_handler(func=lambda call: call.data == "aff_reel")
def handle_affiliate_reel(call):
    chat_id = call.message.chat.id
    state = get_state(chat_id)
    state["content_format"] = "Reels Layout"
    state["media_type"] = "REELS" # Automatically set
    
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("Create with Image (AI)", callback_data="reel_src_img"),
        types.InlineKeyboardButton("I have my own Video", callback_data="reel_src_video")
    )
    bot.send_message(chat_id, "How do you want to compose this video sequence?", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("reel_src_"))
def process_reel_source_choice(call):
    chat_id = call.message.chat.id
    
    if call.data == "reel_src_img":
        get_state(chat_id)["generation_mode"] = "video"
        msg = bot.send_message(chat_id, "Upload the reference image or paste its direct image URL for video generation:")
        bot.register_next_step_handler(msg, process_reel_image_source)
    else:
        # Since Telegram videos can be heavy, we usually ask for a link or prompt to upload
        get_state(chat_id)["source"] = "User Provided Raw Video Asset"
        msg = bot.send_message(chat_id, "Provide the Video URL link to upload:")
        bot.register_next_step_handler(msg, process_reel_details)

def process_reel_image_source(message):
    chat_id = message.chat.id
    state = get_state(chat_id)

    if message.photo:
        file_info = bot.get_file(message.photo[-1].file_id)
        state["source"] = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"
    else:
        state["source"] = message.text

    msg = bot.send_message(chat_id, "Provide the text prompt or direction for the video generation:")
    bot.register_next_step_handler(msg, process_reel_details)

def process_reel_details(message):
    chat_id = message.chat.id
    get_state(chat_id)["details_or_prompt"] = message.text
    
    msg = bot.send_message(chat_id, "What is the Product Price? (e.g. ₹499)")
    bot.register_next_step_handler(msg, process_reel_price)

def process_reel_price(message):
    chat_id = message.chat.id
    get_state(chat_id)["price"] = message.text
    
    msg = bot.send_message(chat_id, "Send Caption:")
    bot.register_next_step_handler(msg, process_reel_caption)
    
def process_reel_caption(message):
    chat_id = message.chat.id
    get_state(chat_id)["caption"] = message.text
    
    state = get_state(chat_id)
    bot.send_message(chat_id, "Core metrics saved. Triggering pipeline...")
    
    if state["source"] == "Create with AI Image Engine":
        start_generation_job(chat_id, "video")
    else:
        sync_and_trigger_webhooks(chat_id, N8N_POSTING_URL)

# ==========================================
# BRANCH B: NORMAL POSTS / ADS
# ==========================================
@bot.callback_query_handler(func=lambda call: call.data == "main_normal")
def select_normal_branch(call):
    chat_id = call.message.chat.id
    get_state(chat_id)["flow_type"] = "Normal Post / Ads"
    
    markup = types.InlineKeyboardMarkup(row_width=3)
    markup.add(
        types.InlineKeyboardButton("Image Post", callback_data="norm_img"),
        types.InlineKeyboardButton("Video Post", callback_data="norm_vid"),
        types.InlineKeyboardButton("Ads Suite", callback_data="norm_ads")
    )
    bot.send_message(chat_id, "Choose asset grouping structure type:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "norm_img")
def handle_normal_image(call):
    chat_id = call.message.chat.id
    state = get_state(chat_id)
    state["content_format"] = "Normal Image"
    state["media_type"] = "IMAGE"
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("Providing Asset", callback_data="img_src_provide"),
        types.InlineKeyboardButton("Generate from Prompt", callback_data="img_src_prompt")
    )
    bot.send_message(chat_id, "Will you be providing the raw asset or generating via structural context tokens?", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("img_src_"))
def process_normal_image_choice(call):
    chat_id = call.message.chat.id
    state = get_state(chat_id)
    state["source"] = "User Uploaded" if "provide" in call.data else "AI Core Prompt Matrix"
    msg = bot.send_message(chat_id, "Enter the textual details/prompt string or description pattern:")
    bot.register_next_step_handler(msg, process_normal_text_payload)

def process_normal_text_payload(message):
    chat_id = message.chat.id
    get_state(chat_id)["details_or_prompt"] = message.text
    msg = bot.send_message(chat_id, "Finally, append target caption copy text layout configuration string:")
    bot.register_next_step_handler(msg, complete_normal_image_flow)

def complete_normal_image_flow(message):
    chat_id = message.chat.id
    state = get_state(chat_id)
    state["caption"] = message.text
    bot.send_message(chat_id, "🎯 Structural specifications updated and mapped.")
    if state["source"] == "User Uploaded":
        sync_and_trigger_webhooks(chat_id, N8N_POSTING_URL)
    else:
        sync_and_trigger_webhooks(chat_id, N8N_TRIGGER_URL)

@bot.callback_query_handler(func=lambda call: call.data == "norm_vid")
def handle_normal_video(call):
    chat_id = call.message.chat.id
    state = get_state(chat_id)
    state["content_format"] = "Normal Video Layout"
    state["media_type"] = "REELS"
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("Providing Video Link", callback_data="vid_src_raw"),
        types.InlineKeyboardButton("Animate from Base Image", callback_data="vid_src_ani"),
        types.InlineKeyboardButton("Pure Text-To-Video Generation", callback_data="vid_src_gen")
    )
    bot.send_message(chat_id, "Identify target compilation methodology layer:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("vid_src_"))
def process_normal_video_choice(call):
    chat_id = call.message.chat.id
    mapping = {"raw": "Direct Link Asset", "ani": "Image Animation Layer", "gen": "Text-To-Video Generation Prompt"}
    choice_key = call.data.split('_')[2]
    get_state(chat_id)["source"] = mapping[choice_key]
    
    msg = bot.send_message(chat_id, "Input reference data target string or functional operational tokens directive:")
    bot.register_next_step_handler(msg, complete_normal_video_flow)

def complete_normal_video_flow(message):
    chat_id = message.chat.id
    state = get_state(chat_id)
    state["details_or_prompt"] = message.text
    bot.send_message(chat_id, "🚀 Queued. Data package synced to operational analytics matrix sheets.")
    if state["source"] == "Direct Link Asset":
        sync_and_trigger_webhooks(chat_id, N8N_POSTING_URL)
    else:
        sync_and_trigger_webhooks(chat_id, N8N_TRIGGER_URL)

@bot.callback_query_handler(func=lambda call: call.data == "norm_ads")
def handle_normal_ads(call):
    chat_id = call.message.chat.id
    state = get_state(chat_id)
    state["content_format"] = "Paid Ad Campaign Set"
    state["media_type"] = "IMAGE" # Assuming ads default to image structure
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("Yes, has link node", callback_data="ads_link_yes"),
        types.InlineKeyboardButton("No, pure structural visual", callback_data="ads_link_no")
    )
    bot.send_message(chat_id, "Does this marketing campaign unit include an external click destination routing URL node parameter?", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("ads_link_"))
def process_ads_link_choice(call):
    chat_id = call.message.chat.id
    has_link = "Yes" in call.data
    if has_link:
        msg = bot.send_message(chat_id, "Provide target click destination hyperlink:")
        bot.register_next_step_handler(msg, process_ads_link_details)
    else:
        get_state(chat_id)["details_or_prompt"] = "No external URL link target layer configured."
        display_ads_media_routing_matrix(chat_id)

def process_ads_link_details(message):
    chat_id = message.chat.id
    get_state(chat_id)["details_or_prompt"] = f"Ad Link Target: {message.text}"
    display_ads_media_routing_matrix(chat_id)

def display_ads_media_routing_matrix(chat_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("Provide Custom Image", callback_data="ads_media_img_prov"),
        types.InlineKeyboardButton("AI Prompt Image Gen", callback_data="ads_media_img_gen"),
        types.InlineKeyboardButton("Provide Video Path", callback_data="ads_media_vid_prov"),
        types.InlineKeyboardButton("Animate Video Matrix", callback_data="ads_media_vid_gen")
    )
    bot.send_message(chat_id, "Identify execution parameters for media payload allocation profiling:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("ads_media_"))
def process_ads_final_media_step(call):
    chat_id = call.message.chat.id
    get_state(chat_id)["source"] = f"Ad Mode Context: {call.data.replace('ads_media_', '')}"
    msg = bot.send_message(chat_id, "Append context prompt payload configuration or text descriptions:")
    bot.register_next_step_handler(msg, complete_ads_pipeline)

def complete_ads_pipeline(message):
    chat_id = message.chat.id
    state = get_state(chat_id)
    state["caption"] = message.text
    bot.send_message(chat_id, "⚙️ Ad profile completely parsed. Synchronizing lead matrices dynamically.")
    # Assuming custom provided assets go to POSTING, generated go to TRIGGER
    if "prov" in state["source"]:
        sync_and_trigger_webhooks(chat_id, N8N_POSTING_URL)
    else:
        sync_and_trigger_webhooks(chat_id, N8N_TRIGGER_URL)

# ==========================================
# MONITORING & APPROVAL
# ==========================================

def monitor_and_request_approval(chat_id, job_id):
    """Monitors local results.json for Gemini script output and requests approval."""
    for _ in range(45):
        time.sleep(10)
        data = load_results()
        
        if job_id in data and data[job_id].get("status") == "success":
            result = data[job_id]
            asset_type = result.get("asset_type", "image")
            bot.send_message(chat_id, "🌟 AI Generation Complete! Please review the generated asset:")
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("✅ Approve & Post", callback_data="approve_ai_post"))

            if asset_type == "video":
                video_path = os.path.join(BASE_DIR, "generated_images", f"{job_id}.mp4")
                if os.path.exists(video_path):
                    with open(video_path, "rb") as file_handle:
                        msg = bot.send_video(chat_id, file_handle, reply_markup=markup)
                        file_info = bot.get_file(msg.video.file_id)
                        get_state(chat_id)["generated_url"] = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"
            else:
                img_path = os.path.join(BASE_DIR, "generated_images", f"{job_id}.png")
                if os.path.exists(img_path):
                    with open(img_path, 'rb') as file_handle:
                        msg = bot.send_photo(chat_id, file_handle, reply_markup=markup)
                        file_info = bot.get_file(msg.photo[-1].file_id)
                        get_state(chat_id)["generated_url"] = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"
            return
            
        elif job_id in data and data[job_id].get("status") == "error":
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔄 Retry Generation", callback_data=f"retry_{job_id}"))
            bot.send_message(chat_id, f"⚠️ Pipeline failed: {data[job_id].get('message')}", reply_markup=markup)
            return
            
    bot.send_message(chat_id, "⏱️ Generation operation timed out.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("retry_"))
def handle_retry(call):
    chat_id = call.message.chat.id
    
    bot.send_message(chat_id, "🔄 Restarting generation pipeline...")
    
    # Re-trigger the same generation logic
    state = get_state(chat_id)
    new_job_id = str(uuid.uuid4().hex)[:8].lower()
    state["unique_id"] = new_job_id.upper()
    threading.Thread(
        target=run_job,
        args=(new_job_id, state["price"], state["source"], state["details_or_prompt"], state.get("generation_mode", "image")),
        daemon=True,
    ).start()
    threading.Thread(target=monitor_and_request_approval, args=(chat_id, new_job_id), daemon=True).start()

@bot.callback_query_handler(func=lambda call: call.data == "approve_ai_post")
def handle_approval(call):
    chat_id = call.message.chat.id
    state = get_state(chat_id)
    
    # Swap the stored original link source to the newly approved AI Image URL
    if state.get("generated_url"):
        state["source"] = state["generated_url"]
    
    # Remove the approval button from the message
    bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
    bot.send_message(chat_id, "✅ Approved! Sending to posting pipeline...")
    
    # Post it!
    sync_and_trigger_webhooks(chat_id, N8N_POSTING_URL)

def monitor_background_job(chat_id, job_id):
    """Standard background monitor for non-approval flows."""
    for _ in range(45):
        time.sleep(10)
        data = load_results()
        if job_id in data and data[job_id].get("status") == "success":
            bot.send_message(chat_id, "🌟 Success!")
            return
    bot.send_message(chat_id, "⏱️ Timeout.")

def _start_flask_server():
    port = int(os.environ.get("PORT", "7860"))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)

if __name__ == "__main__":
    print("Bot execution loop initialized. Polling for triggers...")
    threading.Thread(target=_start_flask_server, daemon=True).start()
    bot.infinity_polling()