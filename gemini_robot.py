import base64
import json
import os
import tempfile
import threading
import time
import uuid
import requests
import re
from flask import send_file, Flask, jsonify, request
from PIL import Image
import pillow_avif
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
import shutil

app = Flask(__name__)

# ==========================================
# PATH & GLOBAL CONFIGURATION (UPDATED FOR CLOUD)
# ==========================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_PATH = os.path.join(BASE_DIR, "results.json")

IMAGE_DIR = os.path.join(BASE_DIR, "generated_images")
os.makedirs(IMAGE_DIR, exist_ok=True) 

RESULTS_LOCK = threading.Lock()
PROFILE_LOCK = threading.Lock()
JOB_TIMEOUT_SECONDS = 900  
PORT = int(os.environ.get("PORT", "7860"))
HEADLESS = os.environ.get("HEADLESS", "true").lower() in {"1", "true", "yes", "on"}
CHROME_PROFILE_DIR = os.environ.get("CHROME_PROFILE_DIR", os.path.join(BASE_DIR, "chrome_automation_profile"))

DEFAULT_CHROME_PATHS = [
    "/usr/bin/chromium", 
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

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

def detect_gemini_login_required(page):
    current_url = (page.url or "").lower()
    if "accounts.google.com" in current_url:
        return True

    page_text = ""
    try:
        page_text = page.locator("body").inner_text(timeout=3000).lower()
    except Exception:
        page_text = ""

    login_markers = [
        "sign in",
        "sign in to continue",
        "choose an account",
        "use your google account",
        "verify it's you",
    ]
    return any(marker in page_text for marker in login_markers)

def extract_generated_image_url(page):
    image_candidates = page.evaluate("""() => {
        const selectors = [
            'main img',
            'article img',
            '[role="main"] img',
            '[data-testid*="response"] img',
            '[data-testid*="result"] img',
            '[class*="response"] img',
            '[class*="result"] img'
        ];

        const seen = new Set();
        const results = [];

        for (const selector of selectors) {
            for (const img of document.querySelectorAll(selector)) {
                const src = img.getAttribute('src') || '';
                if (!src || seen.has(src)) continue;
                seen.add(src);

                const alt = (img.getAttribute('alt') || '').toLowerCase();
                const title = (img.getAttribute('title') || '').toLowerCase();
                const className = (img.className || '').toString().toLowerCase();
                const width = img.naturalWidth || img.clientWidth || 0;
                const height = img.naturalHeight || img.clientHeight || 0;

                results.push({ src, alt, title, className, width, height });
            }
        }

        return results;
    }"")

    filtered_candidates = []
    for candidate in image_candidates:
        src = candidate.get("src") or ""
        alt = (candidate.get("alt") or "").lower()
        title = (candidate.get("title") or "").lower()
        class_name = (candidate.get("className") or "").lower()
        width = int(candidate.get("width") or 0)
        height = int(candidate.get("height") or 0)

        if not src.startswith(("http://", "https://", "blob:")):
            continue
        if width and height and (width < 256 or height < 256):
            continue
        if any(token in alt or token in title or token in class_name for token in ["profile", "avatar", "user-icon", "icon", "logo"]):
            continue

        filtered_candidates.append(candidate)

    return filtered_candidates[0]["src"] if filtered_candidates else None

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

# ADDED job_type PARAMETER (Defaults to "image" to prevent accidental video generation)
def run_job(job_id, price, image_url, custom_prompt="", job_type="image"):
    job_done = threading.Event()

    def safe_update(payload):
        if job_done.is_set():
            return
        update_result(job_id, payload)

    def timeout_handler():
        if job_done.is_set():
            return
        update_result(job_id, {"status": "error", "message": "Job timed out"})
        job_done.set()

    timeout_timer = threading.Timer(JOB_TIMEOUT_SECONDS, timeout_handler)
    timeout_timer.daemon = True
    timeout_timer.start()

    safe_update({"status": "processing", "message": "Downloading provided user image..."})
    
    chrome_path = resolve_chrome_executable_path()

    temp_dir = tempfile.gettempdir()
    temp_original_path = os.path.join(temp_dir, f"direct_upload_original_{job_id}.tmp")
    temp_image_path = os.path.join(temp_dir, f"direct_upload_{job_id}.png")

    try:
        response = requests.get(image_url, stream=True, timeout=20)
        if response.status_code == 200:
            with open(temp_original_path, "wb") as f:
                for chunk in response.iter_content(1024):
                    f.write(chunk)
        else:
            safe_update({"status": "error", "message": f"Failed to download image from Telegram. (HTTP {response.status_code})"})
            job_done.set()
            timeout_timer.cancel()
            return
    except Exception as e:
        safe_update({"status": "error", "message": f"Download connection error: {str(e)}"})
        job_done.set()
        timeout_timer.cancel()
        return

    try:
        image = Image.open(temp_original_path)
        if image.mode in ("RGBA", "P"):
            image = image.convert("RGB")
        image.save(temp_image_path, format="PNG")
    except Exception as e:
        safe_update({"status": "error", "message": f"Image processing error: {str(e)}"})
        job_done.set()
        timeout_timer.cancel()
        return

    image_prompt_text = build_image_prompt(price, custom_prompt)
    video_prompt_text = build_video_prompt()
    
    try:
        with open(temp_image_path, "rb") as image_file:
            image_b64 = base64.b64encode(image_file.read()).decode("ascii")
    except Exception as e:
        safe_update({"status": "error", "message": f"Failed to prepare image clipboard data: {str(e)}"})
        job_done.set()
        timeout_timer.cancel()
        return

    downloaded_img_path = os.path.join(IMAGE_DIR, f"{job_id}.png")
    downloaded_vid_path = os.path.join(IMAGE_DIR, f"{job_id}.mp4")

    # --- IN-SESSION DUAL PHASE PIPELINE ---
    try:
        with PROFILE_LOCK, sync_playwright() as p:
            launch_args = {
                "user_data_dir": CHROME_PROFILE_DIR,
                "headless": HEADLESS,
                "accept_downloads": True,
                "ignore_default_args": ["--enable-automation"],
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-web-security",
                ]
            }
            if chrome_path:
                launch_args["executable_path"] = chrome_path

            context = p.chromium.launch_persistent_context(**launch_args)
            context.grant_permissions(["clipboard-read", "clipboard-write"], origin="https://gemini.google.com")

            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(180000)
            page.set_default_navigation_timeout(180000)
            
            prompt_selector = 'rich-textarea, div[id="utterance-input"], div[contenteditable="true"], textarea, chat-input'

            # --- PHASE 1: GENERATE LOOKBOOK IMAGE ---
            safe_update({"status": "processing", "message": "Opening Gemini Session..."})
            page.goto("https://gemini.google.com/app", wait_until="domcontentloaded")

            if detect_gemini_login_required(page):
                raise RuntimeError(
                    "Gemini login is required in the browser profile. Use a persistent Railway volume for CHROME_PROFILE_DIR and sign in once."
                )
            
            page.wait_for_selector(prompt_selector, timeout=60000, state="visible")
            page.wait_for_timeout(2000)

            chat_box = page.locator(prompt_selector).first

            for attempt in range(3):
                try:
                    chat_box.click(force=True)
                    page.wait_for_timeout(300)
                    chat_box.press("Control+A")
                    chat_box.press("Backspace")
                    page.evaluate(
                        "async ({ text }) => { await navigator.clipboard.writeText(text); }",
                        {"text": image_prompt_text},
                    )
                    chat_box.press("Control+V")
                    page.wait_for_timeout(2000)
                    
                    pasted_text = page.evaluate(
                        """(selector) => {
                            const el = document.querySelector(selector);
                            if (!el) return '';
                            if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') return el.value || '';
                            return el.innerText || el.textContent || '';
                        }""", prompt_selector
                    )
                    if pasted_text.strip()[:10] in image_prompt_text:
                        break
                except Exception:
                    if attempt == 2: raise
                page.wait_for_timeout(1500)

            page.evaluate(
                """async ({ imageB64, mimeType, selector }) => {
                    const target = document.querySelector(selector);
                    if (!target) throw new Error("Prompt element not found.");
                    const dataUrl = `data:${mimeType};base64,${imageB64}`;
                    const response = await fetch(dataUrl);
                    const blob = await response.blob();
                    const item = new ClipboardItem({ [mimeType]: blob });
                    await navigator.clipboard.write([item]);
                }""",
                {"imageB64": image_b64, "mimeType": "image/png", "selector": prompt_selector},
            )
            chat_box.click(force=True)
            chat_box.press("Control+V")
            page.wait_for_timeout(4000)

            send_btn = page.locator('button[aria-label="Send message"], button.send-button, mat-icon:has-text("send"), button[type="submit"], button[data-testid="send-button"]').first
            
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(500)
            except:
                pass

            send_btn.click(force=True)

            page.wait_for_timeout(8000)  

            try:
                page.locator('text=Creating your image, div[aria-label*="Generating"]').first.wait_for(state="visible", timeout=20000)
            except PlaywrightTimeoutError: 
                pass 

            try:
                page.locator('text=Creating your image, div[aria-label*="Generating"]').first.wait_for(state="hidden", timeout=60000)
            except PlaywrightTimeoutError:
                page.reload(wait_until="domcontentloaded")
                page.wait_for_timeout(5000)
            
            page.wait_for_timeout(10000)

            img_download_success = False
            for attempt in range(10): 
                if os.path.exists(downloaded_img_path):
                    img_download_success = True
                    break
                    
                page.wait_for_timeout(4000)
                
                try:
                    download_button = page.locator(
                        'button[aria-label*="Download"], button:has-text("Download"), a[aria-label*="Download"], '
                        'button:has(svg[aria-label*="Download"]), button[data-test-id="download-image-button"]'
                    ).first
                    if download_button.count() > 0 and download_button.is_visible():
                        with page.expect_download(timeout=30000) as download_info:
                            download_button.click(force=True)
                        download = download_info.value
                        download.save_as(downloaded_img_path)
                        img_download_success = True
                        break
                except Exception:
                    pass

                final_image_url = extract_generated_image_url(page)
                
                if final_image_url and final_image_url.startswith("http"):
                    try:
                        fallback_response = requests.get(final_image_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=30)
                        if fallback_response.status_code == 200:
                            content_type = (fallback_response.headers.get("Content-Type") or "").lower()
                            ext = "jpg" if "image/jpeg" in content_type else "png"
                            temp_dl_path = os.path.join(IMAGE_DIR, f"{job_id}.{ext}")
                            
                            with open(temp_dl_path, "wb") as file_handle:
                                file_handle.write(fallback_response.content)
                                
                            if temp_dl_path != downloaded_img_path:
                                shutil.copy(temp_dl_path, downloaded_img_path)
                                
                            img_download_success = True
                            break
                    except Exception as e:
                        pass

            if not img_download_success or not os.path.exists(downloaded_img_path):
                current_url = page.url
                raise RuntimeError(f"Failed to extract lookbook image layout. Current page: {current_url}")

            # --- PHASE 2: VIDEO ANIMATION (CONDITIONALLY TRIGGERED) ---
            if job_type in ["video", "both"]:
                safe_update({"status": "processing", "message": "Starting Video Generation..."})
                page.goto("https://gemini.google.com/app", wait_until="domcontentloaded")
                page.wait_for_timeout(5000) 
                page.wait_for_selector(prompt_selector, timeout=120000, state="visible")
                chat_box = page.locator(prompt_selector).first

                with open(downloaded_img_path, "rb") as final_image_file:
                    gen_image_b64 = base64.b64encode(final_image_file.read()).decode("ascii")

                chat_box.click(force=True)
                chat_box.press("Control+A")
                chat_box.press("Backspace")
                page.evaluate("async ({ text }) => { await navigator.clipboard.writeText(text); }", {"text": video_prompt_text})
                chat_box.press("Control+V")
                page.wait_for_timeout(2000)

                page.evaluate(
                    """async ({ imageB64, mimeType, selector }) => {
                        const target = document.querySelector(selector);
                        if (!target) throw new Error("Prompt element not found.");
                        const dataUrl = `data:${mimeType};base64,${imageB64}`;
                        const response = await fetch(dataUrl);
                        const blob = await response.blob();
                        const item = new ClipboardItem({ [mimeType]: blob });
                        await navigator.clipboard.write([item]);
                    }""",
                    {"imageB64": gen_image_b64, "mimeType": "image/png", "selector": prompt_selector},
                )
                chat_box.click(force=True)
                chat_box.press("Control+V")
                page.wait_for_timeout(4000)
                
                try:
                    page.keyboard.press("Escape")
                except:
                    pass
                send_btn.click(force=True)
                
                page.wait_for_timeout(8000)
                
                try:
                    page.locator('text=Generating video, text=Creating your video, div[aria-label*="video"]').first.wait_for(state="visible", timeout=25000)
                except PlaywrightTimeoutError: pass

                video_rendered = False
                for check in range(12): 
                    if page.locator('video').count() > 0:
                        video_rendered = True
                        break
                    page.wait_for_timeout(5000)

                if not video_rendered:
                    page.reload(wait_until="domcontentloaded")
                    page.wait_for_timeout(8000)

                safe_update({"status": "processing", "message": "Downloading video asset..."})
                vid_download_success = False
                try:
                    video_b64 = page.evaluate("""async () => {
                        const video = document.querySelector('video');
                        if (!video || !video.src) return null;
                        const res = await fetch(video.src);
                        const blob = await res.blob();
                        return new Promise((resolve) => {
                            const reader = new FileReader();
                            reader.onloadend = () => resolve(reader.result);
                            reader.readAsDataURL(blob);
                        });
                    }""")
                    if video_b64 and "base64," in video_b64:
                        with open(downloaded_vid_path, "wb") as f:
                            f.write(base64.b64decode(video_b64.split("base64,")[1]))
                        vid_download_success = True
                except: pass

                if not vid_download_success:
                    try:
                        video_download_button = page.locator('button[aria-label*="Download video"], a[download][href*="video"], button:has(svg[aria-label*="Download"])').last
                        with page.expect_download(timeout=60000) as video_download_info:
                            video_download_button.click(force=True)
                        video_download_info.value.save_as(downloaded_vid_path)
                        vid_download_success = True
                    except: pass

                if not vid_download_success:
                    raise RuntimeError("Failed to capture video asset.")
            
            context.close()

    except Exception as exc:
        safe_update({"status": "error", "message": f"Automation error: {str(exc)}"})
        job_done.set()
        timeout_timer.cancel()
        return

    # Dynamic successful response construction
    final_payload = {
        "status": "success",
        "image_url": f"/image/{job_id}.jpg",
        "message": "Success!"
    }
    if job_type in ["video", "both"]:
        final_payload["video_url"] = f"/video/{job_id}.mp4"

    safe_update(final_payload)

@app.route("/generate-collage", methods=["POST"])
def generate_collage():
    data = request.get_json(silent=True) or {}
    price = data.get("price")
    image_url = data.get("image_url")
    custom_prompt = data.get("prompt", "")
    job_type = data.get("job_type", "image") # Read job_type target if passed via webhook

    if not price or not image_url:
        return jsonify({"status": "error", "message": "Missing price or image_url"}), 400

    job_id = uuid.uuid4().hex
    update_result(job_id, {"status": "pending"})
    thread = threading.Thread(target=run_job, args=(job_id, price, image_url, custom_prompt, job_type), daemon=True)
    thread.start()

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
    out_path = os.path.join(IMAGE_DIR, f"{job_id}.jpg")
    if os.path.exists(out_path): return send_file(out_path, mimetype="image/jpeg")
    
    if os.path.exists(IMAGE_DIR):
        for f in os.listdir(IMAGE_DIR):
            if f.startswith(job_id):
                return send_file(os.path.join(IMAGE_DIR, f))
    return jsonify({"status": "error", "message": "Image not found"}), 404

@app.route("/video/<filename>", methods=["GET"])
def get_video(filename):
    job_id = filename.split('.')[0]
    video_path = os.path.join(IMAGE_DIR, f"{job_id}.mp4")
    if os.path.exists(video_path): return send_file(video_path, mimetype="video/mp4")
    return jsonify({"status": "error", "message": "Video not found"}), 404

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)