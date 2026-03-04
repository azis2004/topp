import os
import re
import json
import time
import hmac
import hashlib
import random
import string
import requests
import logging
import numpy as np
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from functools import wraps

# === TRY OCR DEPS (safe fallback) ===
try:
    import cv2
    from PIL import Image
    import pytesseract
    OCR_OK = True
except ImportError:
    OCR_OK = False
    cv2 = None
    Image = None
    pytesseract = None

# ============================================================
#  CONFIG
# ============================================================
ACCESS_KEY        = "PS9jcJCkqIYi79PnOzXoEFDrPxsfXOXB"
SECRET_KEY        = "iugve27EONOZ9Hl1JvvYEWKa"
HOST              = "api.vsphone.com"
PAD_CODES         = [
    "APP5AV4BTI6XWCGG",
    "APP5BT4QV9UVNUAW",
]
ACCOUNTS_TARGET   = 5
APK_URL           = "https://statistic.topnod.com/TopNod.apk"
APK_LOCAL         = "/sdcard/Download/TopNod.apk"
OUTPUT_FILE       = "akun_topnod.json"
LOG_FILE          = "autoreff.log"
API_CALL_INTERVAL = 4  # detik minimum antar setiap API call

# ============================================================
#  LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)-5s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8")
    ]
)
log = logging.getLogger("topnod")

def loginfo(msg): log.info(msg)
def logerr(msg):  log.error(f"❌ {msg}")
def logwarn(msg): log.warning(f"⚠️ {msg}")

# ============================================================
#  RETRY DECORATOR
# ============================================================
def retry_on_failure(max_retries=3, delay=5, backoff=1.5):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for i in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exc = e
                    if i == max_retries - 1:
                        raise
                    sleep_time = delay * (backoff ** i)
                    logwarn(f"{func.__name__} gagal ({i+1}/{max_retries}), retry {sleep_time:.1f}s...")
                    time.sleep(sleep_time)
            raise last_exc
        return wrapper
    return decorator

# ============================================================
#  VSPHONE API — rate limited + auto retry on 500
# ============================================================
_last_api_call = 0

def _sign_request(method, path, params=None, body=None):
    timestamp = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    nonce     = ''.join(random.choices(string.ascii_lowercase + string.digits, k=16))

    canonical_parts = [method.upper(), path, timestamp, nonce]
    if params:
        sorted_params = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        canonical_parts.append(sorted_params)
    if body:
        body_str = json.dumps(body, separators=(',', ':'), sort_keys=True)
        canonical_parts.append(body_str)

    canonical = "\n".join(canonical_parts)
    signature = hmac.new(
        SECRET_KEY.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    return {
        "Authorization": f"VSPHONE {ACCESS_KEY}:{signature}",
        "X-Access-Key" : ACCESS_KEY,
        "X-Timestamp"  : timestamp,
        "X-Nonce"      : nonce,
        "X-Signature"  : signature,
        "Content-Type" : "application/json",
    }

def api(endpoint, payload=None, method="POST", max_retry=6):
    global _last_api_call

    # Jeda global — hindari rate limit
    elapsed = time.time() - _last_api_call
    if elapsed < API_CALL_INTERVAL:
        time.sleep(API_CALL_INTERVAL - elapsed)

    url = f"https://{HOST}{endpoint}"

    for attempt in range(max_retry):
        headers = _sign_request(method, endpoint, body=payload)
        try:
            r = requests.request(method, url, headers=headers, json=payload or {}, timeout=30)
            _last_api_call = time.time()
            data = r.json() if r.text else {}
            code = data.get("code") or data.get("status") or data.get("retCode")
            msg  = data.get("msg") or data.get("message") or "unknown"

            if str(code) in ("200", "0", "success"):
                return data.get("data") or data.get("result") or data

            # 500 busy — retry dengan backoff
            if str(code) == "500" or "busy" in str(msg).lower():
                wait = 15 * (attempt + 1)  # 15s, 30s, 45s, 60s, 75s, 90s
                logwarn(f"Server busy [{endpoint}], retry {attempt+1}/{max_retry} dalam {wait}s...")
                time.sleep(wait)
                continue

            logerr(f"API {endpoint}: {code} | {msg}")
            return None

        except Exception as e:
            wait = 15 * (attempt + 1)
            logwarn(f"Req error [{endpoint}]: {e}, retry {attempt+1}/{max_retry} dalam {wait}s...")
            time.sleep(wait)

    logerr(f"API {endpoint} gagal setelah {max_retry} retry")
    return None

# ── Helper ADB ─────────────────────────────────────────────
def adb(pad_code, script):
    return api("/vsphone/api/padApi/asyncCmd", {
        "padCodes"     : [pad_code],
        "scriptContent": script
    })

# ── Device Actions ─────────────────────────────────────────
def tap(pad_code, x, y, max_retry=5):
    for attempt in range(max_retry):
        res = api("/vsphone/api/padApi/simulateTouch", {
            "padCode"  : pad_code,
            "x"        : x,
            "y"        : y,
            "eventType": 0
        })
        if res is not None:
            break
        wait = 10 * (attempt + 1)
        logwarn(f"Tap gagal ({attempt+1}/{max_retry}), retry {wait}s...")
        time.sleep(wait)
    time.sleep(random.uniform(2.0, 3.5))

def swipe(pad_code, x1, y1, x2, y2, dur=800, max_retry=5):
    for attempt in range(max_retry):
        res = api("/vsphone/api/padApi/simulateTouch", {
            "padCode"  : pad_code,
            "startX"   : x1, "startY": y1,
            "endX"     : x2, "endY"  : y2,
            "duration" : dur,
            "eventType": 1
        })
        if res is not None:
            break
        wait = 10 * (attempt + 1)
        logwarn(f"Swipe gagal ({attempt+1}/{max_retry}), retry {wait}s...")
        time.sleep(wait)
    time.sleep(1.5)

def input_text(pad_code, text, max_retry=5):
    for attempt in range(max_retry):
        res = api("/vsphone/api/padApi/inputText", {
            "padCodes": [pad_code],
            "text"    : text
        })
        if res is not None:
            break
        logwarn(f"inputText gagal ({attempt+1}/{max_retry}), fallback adb...")
        esc = text.replace("'", "'\"'\"'").replace(" ", "%s")
        adb(pad_code, f"input text '{esc}'")
        break
    time.sleep(1.5)

def clear_app(pad_code, pkg):
    adb(pad_code, f"pm clear {pkg}")
    time.sleep(3)

def open_app(pad_code, pkg):
    adb(pad_code, f"monkey -p {pkg} 1")
    time.sleep(5)

def get_package_name(pad_code):
    res = adb(pad_code, "pm list packages | grep -i topnod")
    time.sleep(2)
    if res:
        m = re.search(r'package:([\w.]+)', str(res))
        return m.group(1) if m else "com.topnod.app"
    return "com.topnod.app"

def read_clipboard(pad_code):
    res = adb(pad_code, "dumpsys clipboard 2>/dev/null | grep -o 'text=[^ ]*' | head -1")
    time.sleep(2)
    if res:
        m = re.search(r'text=([A-Z0-9_]{6,25})', str(res))
        return m.group(1) if m else None
    return None

def auto_close_popup(pad_code):
    tap(pad_code, 360, 1400)
    time.sleep(1)

def gen_pass():
    chars = string.ascii_letters + string.digits + "!@#$"
    return ''.join(random.choices(chars, k=12))

# ── Screenshot & OCR ───────────────────────────────────────
def get_screenshot(pad_code):
    res = api("/vsphone/api/padApi/getLongGenerateUrl", {
        "padCodes": [pad_code],
        "format"  : "jpg",
        "quality" : 70
    })
    if not res:
        return None
    try:
        items = res if isinstance(res, list) else [res]
        url   = items[0].get("url") if items else None
        if not url:
            return None
        r   = requests.get(url, timeout=15)
        arr = np.frombuffer(r.content, dtype=np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR) if cv2 else None
    except Exception as e:
        logerr(f"Screenshot gagal: {e}")
        return None

def ocr_region(img, x, y, w, h, cfg="--psm 6"):
    if not OCR_OK or img is None or cv2 is None:
        return ""
    try:
        crop   = img[y:y+h, x:x+w]
        gray   = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        scaled = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        _, th  = cv2.threshold(scaled, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        return pytesseract.image_to_string(Image.fromarray(th), config=cfg).strip()
    except Exception as e:
        logerr(f"OCR gagal: {e}")
        return ""

# ── Device Ready Check ─────────────────────────────────────
def wait_device_ready(pad_code, timeout=180):
    loginfo(f"⏳ Tunggu device {pad_code} siap...")
    start = time.time()
    while time.time() - start < timeout:
        res = api("/vsphone/api/padApi/getLongGenerateUrl", {
            "padCodes": [pad_code],
            "format"  : "jpg"
        })
        if res:
            items = res if isinstance(res, list) else [res]
            if items and items[0].get("success"):
                loginfo(f"✅ Device {pad_code} siap")
                return True
        logwarn("Device belum siap, coba lagi 15s...")
        time.sleep(15)
    logerr(f"❌ Device {pad_code} tidak siap dalam {timeout}s")
    return False

# ── Install APK ─────────────────────────────────────────────
@retry_on_failure(max_retries=3, delay=5)
def install_apk(pad_code):
    loginfo("📥 Install APK via installApp...")

    # Coba 1: installApp
    res = api("/vsphone/api/padApi/installApp", {
        "padCodes"   : [pad_code],
        "url"        : APK_URL,
        "packageName": "com.topnod.app",
    })

    if not res:
        # Coba 2: uploadFileV3
        logwarn("installApp gagal, coba uploadFileV3...")
        res = api("/vsphone/api/padApi/uploadFileV3", {
            "padCodes"       : [pad_code],
            "url"            : APK_URL,
            "autoInstall"    : 1,
            "packageName"    : "com.topnod.app",
            "fileName"       : "TopNod",
            "isAuthorization": True
        })

    if not res:
        # Coba 3: curl di device
        logwarn("uploadFileV3 gagal, fallback curl di device...")
        adb(pad_code, f"curl -L -o {APK_LOCAL} '{APK_URL}' && pm install -r {APK_LOCAL}")

    loginfo("⏳ Tunggu install selesai (25s)...")
    time.sleep(25)

    # Verifikasi
    pkg = get_package_name(pad_code)
    if "topnod" in pkg.lower():
        loginfo(f"✅ APK terinstall: {pkg}")
        return pkg

    res2 = adb(pad_code, "pm list packages | grep topnod")
    time.sleep(2)
    if res2 and "topnod" in str(res2).lower():
        loginfo("✅ APK verified via pm list")
        return "com.topnod.app"

    logerr("❌ Install gagal — package tidak ditemukan")
    raise RuntimeError("APK install failed")

# ── Captcha Solver ──────────────────────────────────────────
def solve_captcha(pad_code):
    BG_X, BG_Y, BG_W, BG_H = 165, 535, 370, 440
    SLIDER_X, SLIDER_Y      = 137, 1053

    for attempt in range(3):
        screen = get_screenshot(pad_code)
        if screen is None:
            time.sleep(5)
            continue

        gap_x = BG_X + (BG_W // 2)
        try:
            if cv2:
                bg_crop = screen[BG_Y:BG_Y+BG_H, BG_X:BG_X+BG_W]
                gray    = cv2.cvtColor(bg_crop, cv2.COLOR_BGR2GRAY)
                edges   = cv2.Canny(gray, 30, 100)
                cnts, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                for c in cnts:
                    if 40 < cv2.contourArea(c) < 2000:
                        x, _, w, _ = cv2.boundingRect(c)
                        if x > BG_X:
                            gap_x = BG_X + x + w // 2
                            break
        except Exception as e:
            logerr(f"Captcha detect error: {e}")

        distance = gap_x - SLIDER_X + random.randint(-4, 4)
        loginfo(f"Captcha attempt {attempt+1}: gap={gap_x}, swipe={distance}")

        cur = SLIDER_X
        for i in range(8):
            t      = i / 7
            ease   = 3 * t * t - 2 * t * t * t
            next_x = cur + int(ease * distance)
            swipe(pad_code, cur, SLIDER_Y, next_x, SLIDER_Y)
            cur = next_x
        time.sleep(3)

        screen2 = get_screenshot(pad_code)
        if screen2 is not None and cv2:
            try:
                r1   = cv2.resize(screen[BG_Y:BG_Y+BG_H, BG_X:BG_X+BG_W], (100, 50))
                r2   = cv2.resize(screen2[BG_Y:BG_Y+BG_H, BG_X:BG_X+BG_W], (100, 50))
                diff = cv2.absdiff(r1, r2)
                if diff.mean() > 10:
                    loginfo("✅ Captcha solved!")
                    return True
            except Exception as e:
                logerr(f"Verifikasi captcha gagal: {e}")
        time.sleep(2)

    logerr("Captcha gagal setelah 3 percobaan")
    return False

# ── Email ───────────────────────────────────────────────────
_KUKULU_BASE = "https://m.kuku.lu"
_sess = requests.Session()
_sess.headers.update({"User-Agent": "Mozilla/5.0"})

def get_temp_email():
    user   = ''.join(random.choices(string.ascii_lowercase, k=8)) + ''.join(random.choices(string.digits, k=4))
    domain = "boxfi.uk"
    try:
        _sess.post(f"{_KUKULU_BASE}/create.php", data={"address": user, "domain": domain}, timeout=5)
    except Exception as e:
        logerr(f"Create email gagal: {e}")
    email = f"{user}@{domain}"
    loginfo(f"📧 {email}")
    return email, {"user": user, "domain": domain}

def check_inbox(meta, timeout=120):
    user, dom = meta["user"], meta["domain"]
    start     = time.time()
    while time.time() - start < timeout:
        try:
            r    = _sess.get(f"{_KUKULU_BASE}/inbox.php", params={"address": user, "domain": dom}, timeout=5)
            soup = BeautifulSoup(r.text, "html.parser")
            link = soup.select_one("div.mail a[href]")
            if link:
                href = link["href"]
                if not href.startswith("http"):
                    href = f"{_KUKULU_BASE}/{href}"
                r2   = _sess.get(href, timeout=5)
                body = soup.find("div", class_=re.compile(r"body|content"))
                return body.get_text(" ", strip=True) if body else r2.text
        except Exception as e:
            logerr(f"Inbox check error: {e}")
        time.sleep(5)
    return None

def extract_otp(txt):
    if not txt:
        return None
    m = re.findall(r'\b\d{4,6}\b', txt)
    return m[0] if m else None

# ============================================================
#  UI COORDINATES
# ============================================================
UI_720 = {
    "email"   : (353, 490),
    "otp"     : (353, 660),
    "reff"    : (353, 835),
    "next"    : (353, 1007),
    "pass"    : (353, 620),
    "confirm" : (353, 880),
    "continue": (353, 1355),
    "skip"    : (611, 140),
    "event"   : (353, 210),
    "spin"    : (353, 660),
    "claim"   : (551, 1330),
    "ok"      : (353, 955),
    "invite"  : (563, 1023),
    "copy"    : (463, 1152),
    "close"   : (637, 670),
}

def get_ui_coords(pad_code):
    screen = get_screenshot(pad_code)
    if screen is not None:
        h, w  = screen.shape[:2]
        scale = w / 720
    else:
        scale = 1.0
    return {k: (int(v[0] * scale), int(v[1] * scale)) for k, v in UI_720.items()}

# ============================================================
#  CORE: REGISTER + SPIN
# ============================================================
@retry_on_failure(max_retries=2, delay=10)
def register_and_spin(pad_code, pkg, reff_code=""):
    clear_app(pad_code, pkg)
    open_app(pad_code, pkg)
    time.sleep(6)

    ui = get_ui_coords(pad_code)

    # Step 1: Email
    loginfo("📧 Input email...")
    tap(pad_code, *ui["email"])
    email, meta = get_temp_email()
    input_text(pad_code, email)
    time.sleep(2)
    tap(pad_code, *ui["next"])
    time.sleep(4)

    # Step 2: OTP
    loginfo("⏳ Tunggu OTP (120s)...")
    body = check_inbox(meta, timeout=120)
    otp  = extract_otp(body)
    if not otp:
        logerr("OTP tidak ditemukan")
        auto_close_popup(pad_code)
        raise RuntimeError("OTP missing")
    loginfo(f"✅ OTP: {otp}")
    tap(pad_code, *ui["otp"])
    input_text(pad_code, otp)
    time.sleep(2)
    tap(pad_code, *ui["next"])
    time.sleep(4)

    # Step 3: Referral
    if reff_code:
        loginfo(f"🔗 Input referral: {reff_code}")
        tap(pad_code, *ui["reff"])
        input_text(pad_code, reff_code)
        time.sleep(2)
    tap(pad_code, *ui["next"])
    time.sleep(4)

    # Step 4: Password
    pwd = gen_pass()
    loginfo("🔑 Input password...")
    tap(pad_code, *ui["pass"])
    input_text(pad_code, pwd)
    time.sleep(1)
    tap(pad_code, *ui["confirm"])
    input_text(pad_code, pwd)
    time.sleep(1)
    tap(pad_code, *ui["continue"])
    time.sleep(4)

    # Skip biometric
    tap(pad_code, *ui["skip"])
    time.sleep(3)

    # Claim & Spin
    loginfo("🎰 Claim & Spin...")
    tap(pad_code, *ui["event"])
    time.sleep(4)
    tap(pad_code, *ui["claim"])
    time.sleep(3)
    tap(pad_code, *ui["ok"])
    time.sleep(3)
    tap(pad_code, *ui["spin"])
    time.sleep(6)
    tap(pad_code, 353, 800)
    time.sleep(2)

    save_account({"email": email, "password": pwd, "reff_code": reff_code})
    loginfo(f"✅ Akun selesai: {email}")
    return True

def save_account(data):
    try:
        accs = json.load(open(OUTPUT_FILE)) if os.path.exists(OUTPUT_FILE) else []
        accs.append(data)
        with open(OUTPUT_FILE, "w") as f:
            json.dump(accs, f, indent=2)
        loginfo(f"💾 Simpan ke {OUTPUT_FILE}")
    except Exception as e:
        logerr(f"Simpan gagal: {e}")

# ============================================================
#  GET REFERRAL CODE
# ============================================================
def get_reff_code(pad_code):
    loginfo("🔍 Ambil referral code...")
    time.sleep(3)

    # 1. Clipboard
    code = read_clipboard(pad_code)
    if code and len(code) >= 6:
        loginfo(f"📋 Clipboard: {code}")
        return code

    # 2. OCR
    screen = get_screenshot(pad_code)
    if screen is not None:
        text = ocr_region(screen, 200, 1100, 400, 100, "--psm 7")
        loginfo(f"🔍 OCR: '{text}'")
        m = re.search(r'[A-Z0-9]{6,12}', text)
        if m:
            loginfo(f"✅ OCR result: {m.group(0)}")
            return m.group(0)

    # 3. Tap copy button
    ui = get_ui_coords(pad_code)
    tap(pad_code, *ui["invite"])
    time.sleep(3)
    tap(pad_code, *ui["copy"])
    time.sleep(2)
    code = read_clipboard(pad_code)
    if code and len(code) >= 6:
        loginfo(f"📋 Copy+Clipboard: {code}")
        return code

    logerr("❌ Gagal ambil referral code")
    return None

# ============================================================
#  MAIN
# ============================================================
if __name__ == "__main__":
    loginfo("🔥 Auto-Reff Bot v2.3 — rate limit fix + device ready check")

    if not ACCESS_KEY or not SECRET_KEY:
        logerr("ACCESS_KEY / SECRET_KEY kosong!")
        exit(1)
    if not PAD_CODES:
        logerr("PAD_CODES kosong!")
        exit(1)

    pkg         = None
    master_code = None

    # ── Siapkan semua device ───────────────────────────────
    for pad in PAD_CODES:
        loginfo(f"📦 Siapkan device {pad}...")
        if not wait_device_ready(pad, timeout=180):
         
