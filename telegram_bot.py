import telebot
from telebot import types
from telebot import apihelper
import threading
import uuid
import time
import os
import requests
import json
import socket
import urllib3.util.connection as urllib3_cn
from telebot.apihelper import ApiTelegramException

# --- IPv4 NETWORK FIX ---
urllib3_cn.allowed_gai_family = lambda: socket.AF_INET

# Import your existing generation logic
from gemini_robot import run_job, load_results

# CONFIGURATION
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GOOGLE_SHEET_WEBHOOK_URL = os.environ.get("GOOGLE_SHEET_WEBHOOK_URL")
N8N_TRIGGER_URL = os.environ.get("N8N_TRIGGER_URL")
N8N_POSTING_URL = os.environ.get("N8N_POSTING_URL")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is required")

if not GOOGLE_SHEET_WEBHOOK_URL:
    raise RuntimeError("GOOGLE_SHEET_WEBHOOK_URL environment variable is required")

if not N8N_TRIGGER_URL:
    raise RuntimeError("N8N_TRIGGER_URL environment variable is required")

if not N8N_POSTING_URL:
    raise RuntimeError("N8N_POSTING_URL environment variable is required")

# TIMEOUTS & INITIALIZATION
apihelper.proxy = None
apihelper.API_URL = "https://api.telegram.org/bot{0}/{1}"
apihelper.CONNECT_TIMEOUT = 60
apihelper.READ_TIMEOUT = 60

bot = telebot.TeleBot(BOT_TOKEN)

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
            "generated_url": "" # Temporarily stores the Telegram URL of AI image for approval
        }
    return user_states[chat_id]

def sync_and_trigger_webhooks(chat_id, target_n8n_url):
    """Fires the payload to BOTH Google Sheets and n8n via POST request."""
    state = get_state(chat_id)
    
    # CORRECTED PAYLOAD: Matches your Google Sheet column sequence step-by-step
    payload = {
        "unique_id": state["unique_id"],
        "chat_id": str(chat_id),
        "flow_type": state["flow_type"],
        "meta_api": state["meta_api"],
        "instagram_id": state["instagram_id"],
        "content_format": state["content_format"],
        "details_or_prompt": state["details_or_prompt"], # Added back to fill Prompt column
        "price": state["price"],
        "source": state["source"],
        "caption": state["caption"],
        "media_type": state["media_type"]                # Appended cleanly to the end
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
            response = requests.post(target_n8n_url, json=payload, timeout=15)
            print(f"[{state['unique_id']}] 🚀 n8n Webhook Status: {response.status_code} ({target_n8n_url.split('/')[-1]})")
        except Exception as e:
            print(f"[{state['unique_id']}] ❌ n8n webhook trigger failed: {e}")

    threading.Thread(target=fire_requests, daemon=True).start()

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

@bot.callback_query_handler(func=lambda call: call.data in ["aff_1img", "aff_2img"])
def handle_affiliate_image_selections(call):
    chat_id = call.message.chat.id
    state = get_state(chat_id)
    
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
    
    job_id = state["unique_id"].lower()
    threading.Thread(target=run_job, args=(job_id, state["price"], state["source"], state["details_or_prompt"]), daemon=True).start()
    threading.Thread(target=monitor_and_request_approval, args=(chat_id, job_id), daemon=True).start()

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
    sync_and_trigger_webhooks(chat_id, N8N_POSTING_URL)

@bot.callback_query_handler(func=lambda call: call.data == "aff_carousel")
def handle_affiliate_carousel(call):
    chat_id = call.message.chat.id
    state = get_state(chat_id)
    state["content_format"] = "Carousel Pack"
    state["media_type"] = "CAROUSEL" 
    
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
    sync_and_trigger_webhooks(chat_id, N8N_TRIGGER_URL)

@bot.callback_query_handler(func=lambda call: call.data == "aff_reel")
def handle_affiliate_reel(call):
    chat_id = call.message.chat.id
    state = get_state(chat_id)
    state["content_format"] = "Reels Layout"
    state["media_type"] = "REELS" 
    
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
        get_state(chat_id)["source"] = "Create with AI Image Engine"
        msg = bot.send_message(chat_id, "Provide the Image Link/Upload for generation:")
        bot.register_next_step_handler(msg, process_reel_details)
    else:
        get_state(chat_id)["source"] = "User Provided Raw Video Asset"
        msg = bot.send_message(chat_id, "Provide the Video URL link to upload:")
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
        sync_and_trigger_webhooks(chat_id, N8N_TRIGGER_URL)
    else:
        sync_and_trigger_webhooks(chat_id, N8N_POSTING_URL)

# ==========================================
# NORMAL POSTS / ADS
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
    state["media_type"] = "IMAGE" 
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
            bot.send_message(chat_id, "🌟 AI Generation Complete! Please review the generated asset:")
            base_dir = os.path.dirname(os.path.abspath(__file__))
            img_path = os.path.join(base_dir, "generated_images", f"{job_id}.png")
            
            if os.path.exists(img_path):
                with open(img_path, 'rb') as f:
                    markup = types.InlineKeyboardMarkup()
                    markup.add(types.InlineKeyboardButton("✅ Approve & Post", callback_data="approve_ai_post"))
                    msg = bot.send_photo(chat_id, f, reply_markup=markup)
                    
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
    
    state = get_state(chat_id)
    new_job_id = str(uuid.uuid4().hex)[:8].lower()
    
    threading.Thread(
        target=run_job, 
        args=(new_job_id, state["price"], state["source"], state["details_or_prompt"]), 
        daemon=True
    ).start()
    
    threading.Thread(target=monitor_and_request_approval, args=(chat_id, new_job_id), daemon=True).start()

@bot.callback_query_handler(func=lambda call: call.data == "approve_ai_post")
def handle_approval(call):
    chat_id = call.message.chat.id
    state = get_state(chat_id)
    
    if state.get("generated_url"):
        state["source"] = state["generated_url"]
    
    bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
    bot.send_message(chat_id, "✅ Approved! Sending to posting pipeline...")
    
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

if __name__ == '__main__':
    print("Bot execution loop initialized. Polling for triggers...")
    bot.set_my_commands([
        types.BotCommand("/start", "Restart the bot and start over")
    ])

    while True:
        try:
            bot.infinity_polling(skip_pending=True, timeout=20, long_polling_timeout=20)
        except ApiTelegramException as exc:
            if getattr(exc, "error_code", None) == 409:
                print("Another bot instance is already polling this token. Retrying after a delay...")
                time.sleep(15)
                continue
            raise
        except Exception as exc:
            print(f"Polling stopped unexpectedly: {exc}")
            time.sleep(15)