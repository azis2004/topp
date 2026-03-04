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
from urllib.parse import urlencode
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
#  CONFIG — ISI LANGSUNG DI SINI
# ============================================================
ACCESS_KEY = "PS9jcJCkqIYi79PnOzXoEFDrPxsfXOXB"  # GANTI
SECRET_KEY = "iugve27EONOZ9Hl1JvvYEWKa"          # GANTI
HOST       = "api.vsphone.com"
PAD_CODES  = [
    "APP5AV4BTI6XWCGG",
    "APP5BT4QV9UVNUAW",
]

ACCOUNTS_TARGET = 5
REFF_PER_MASTER = 5
AKUN_PER_VSP    = 2

APK_URL     = "https://statistic.topnod.com/TopNod.apk"
APK_LOCAL   = "/sdcard/Download/TopNod.apk"
OUTPUT_FILE = "akun_topnod.json"
LOG_FILE    = "autoreff.log"

# ============================================================
#  LOGGING — Console + File
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
def retry_on_failure(max_retries=3, delay=2, backoff=1.5):
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
                    logwarn(f"{func.__name__} gagal ({i+1}/{max_retries}), coba lagi dalam {sleep_time:.1f}s...")
                    time.sleep(sleep_time)
            raise last_exc
        return wrapper
    return decorator

# ============================================================
#  VSPHONE API — AK/SK AUTH
# ============================================================
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
        "X-Access-Key"  : ACCESS_KEY,
        "X-Timestamp"   : timestamp,
        "X-Nonce"       : nonce,
        "X-Signature"   : signature,
        "Content-Type"  : "application/json",
    }

def api(endpoint, payload=None, method="POST"):
    url = f"https://{HOST}{endpoint}"
    headers = _sign_request(method, endpoint, body=payload)
    try:
        r = requests.request(method, url, headers=headers, json=payload or {}, timeout=30)
        data = r.json() if r.text else {}
        code = data.get("code") or data.get("status") or data.get("retCode")
        if str(code) in ("200", "0", "success"):
            return data.get("data") or data.get("result") or data
        else:
            msg = data.get("msg") or data.get("message") or "unknown"
            logerr(f"API {endpoint}: {code} | {msg}")
            return None
    except Exception as e:
        logerr(f"Req {endpoint}: {e}")
        return None

# ── Device & App ───────────────────────────────────────────
def clear_app(pad_code, pkg):
    api("/vsphone/api/padApi/asyncCmd", {"padCodes": [pad_code], "cmd": f"pm clear {pkg}"})
    time.sleep(2)

def open_app(pad_code, pkg):
    api("/vsphone/api/padApi/asyncCmd", {"padCodes": [pad_code], "cmd": f"monkey -p {pkg} 1"})
    time.sleep(4)

def get_package_name(pad_code):
    res = api("/vsphone/api/padApi/asyncCmd", {"padCodes": [pad_code], "cmd": "pm list packages | grep -i topnod"})
    time.sleep(2)
    if res:
        m = re.search(r'package:([\w.]+)', str(res))
        return m.group(1) if m else "com.topnod.app"
    return "com.topnod.app"

def tap(pad_code, x, y):
    api("/vsphone/api/padApi/simulateTouch", {"padCode": pad_code, "x": x, "y": y, "eventType": 0})
    time.sleep(random.uniform(1.0, 2.0))

def swipe(pad_code, x1, y1, x2, y2, dur=800):
    api("/vsphone/api/padApi/simulateTouch", {
        "padCode": pad_code, "startX": x1, "startY": y1, "endX": x2, "endY": y2,
        "duration": dur, "eventType": 1
    })
    time.sleep(1)

def input_text(pad_code, text):
    esc = text.replace("'", "'\"'\"'").replace(" ", "%s")
    api("/vsphone/api/padApi/asyncCmd", {"padCodes": [pad_code], "cmd": f"input text '{esc}'"})
    time.sleep(1)

def read_clipboard(pad_code):
    res = api("/vsphone/api/padApi/asyncCmd", {"padCodes": [pad_code], "cmd": "dumpsys clipboard 2>/dev/null | grep -o 'text=[^ ]*' | head -1"})
    time.sleep(2)
    if res:
        m = re.search(r'text=([A-Z0-9_]{6,25})', str(res))
        return m.group(1) if m else None
    return None

# ── Screenshot & OCR (fallback safe) ───────────────────────
def get_screenshot(pad_code):
    res = api("/vsphone/api/padApi/getLongGenerateUrl", {"padCodes": [pad_code]})
    if not res:
        return None
    try:
        url = res[0].get("url") if isinstance(res, list) else res.get("url")
        if not url:
            return None
        r = requests.get(url, timeout=15)
        arr = np.frombuffer(r.content, dtype=np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR) if cv2 else None
    except Exception as e:
        logerr(f"Screenshot gagal: {e}")
        return None

def ocr_region(img, x, y, w, h, cfg="--psm 6"):
    if not OCR_OK or img is None or cv2 is None:
        return ""
    try:
        crop = img[y:y+h, x:x+w]
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        scaled = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        _, th = cv2.threshold(scaled, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        return pytesseract.image_to_string(Image.fromarray(th), config=cfg).strip()
    except Exception as e:
        logerr(f"OCR gagal: {e}")
        return ""

def get_spins_left(pad_code):
    screen = get_screenshot(pad_code)
    if screen is None:
        return 0
    text = ocr_region(screen, 250, 580, 200, 100, "--psm 7")
    m = re.search(r'(\d+)\s*left', text, re.IGNORECASE)
    return int(m.group(1)) if m else 0

# ── Auto-close popup (fallback safety)
def auto_close_popup(pad_code):
    """Tap area bawah tengah — umumnya close dialog/error"""
    tap(pad_code, 360, 1400)  # 720p: ~bottom center
    time.sleep(1)

# ── Password Generator ──────────────────────────────────────
def gen_pass():
    chars = string.ascii_letters + string.digits + "!@#$"
    return ''.join(random.choices(chars, k=12))

# ── Install APK
@retry_on_failure(max_retries=3, delay=3)
def install_apk(pad_code):
    loginfo("📥 Download & install APK...")
    # Step 1: Download
    try:
        r = requests.get(APK_URL, timeout=20)
        r.raise_for_status()
        with open("/tmp/topnod.apk", "wb") as f:
            f.write(r.content)
        loginfo("✅ APK diunduh lokal")
    except Exception as e:
        logerr(f"Download gagal: {e}")
        raise

    # Step 2: Upload ke device via VSPHONE (simulasi push)
    api("/vsphone/api/padApi/asyncCmd", {
        "padCodes": [pad_code],
        "cmd": f"mkdir -p /sdcard/Download && echo 'APK downloaded' > /sdcard/Download/status.txt"
    })
    time.sleep(1)

    # Cek keberadaan file
    res = api("/vsphone/api/padApi/asyncCmd", {"padCodes": [pad_code], "cmd": f"ls {APK_LOCAL}"})
    if not res or "TopNod.apk" not in str(res):
        logwarn("APK tidak ditemukan di device — coba install via curl")
        api("/vsphone/api/padApi/asyncCmd", {
            "padCodes": [pad_code],
            "cmd": f"curl -o {APK_LOCAL} {APK_URL} && pm install {APK_LOCAL}"
        })
    else:
        api("/vsphone/api/padApi/asyncCmd", {"padCodes": [pad_code], "cmd": f"pm install -r {APK_LOCAL}"})
    time.sleep(5)

    # Verifikasi
    pkg = get_package_name(pad_code)
    if "topnod" in pkg.lower():
        loginfo(f"✅ APK terinstall: {pkg}")
        return pkg
    else:
        logerr("❌ Install gagal — package tidak ditemukan")
        raise RuntimeError("APK install failed")

# ── Captcha Solver (dengan retry internal)
def solve_captcha(pad_code):
    BG_X, BG_Y, BG_W, BG_H = 165, 535, 370, 440
    PIECE_X, PIECE_Y = 90, 760
    SLIDER_X, SLIDER_Y = 137, 1053

    for attempt in range(3):
        screen = get_screenshot(pad_code)
        if screen is None:
            time.sleep(2)
            continue

        gap_x = BG_X + (BG_W // 2)
        try:
            bg_crop = screen[BG_Y:BG_Y+BG_H, BG_X:BG_X+BG_W]
            if cv2:
                gray = cv2.cvtColor(bg_crop, cv2.COLOR_BGR2GRAY)
                edges = cv2.Canny(gray, 30, 100)
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
            t = i / 7
            ease = 3 * t * t - 2 * t * t * t
            step = int(ease * distance)
            next_x = cur + step
            swipe(pad_code, cur, SLIDER_Y, next_x, SLIDER_Y)
            cur = next_x
        time.sleep(2.5)

        # Verifikasi
        screen2 = get_screenshot(pad_code)
        if screen2 is not None:
            try:
                resized1 = cv2.resize(screen[BG_Y:BG_Y+BG_H, BG_X:BG_X+BG_W], (100, 50))
                resized2 = cv2.resize(screen2[BG_Y:BG_Y+BG_H, BG_X:BG_X+BG_W], (100, 50))
                diff = cv2.absdiff(resized1, resized2)
                if diff.mean() > 10:
                    loginfo("✅ Captcha solved!")
                    return True
            except Exception as e:
                logerr(f"Verifikasi captcha gagal: {e}")
        time.sleep(1.5)

    logerr("Captcha gagal setelah 3 percobaan")
    return False

# ── KUKU.LU EMAIL
_KUKULU_BASE = "https://m.kuku.lu"
_sess = requests.Session()
_sess.headers.update({"User-Agent": "Mozilla/5.0"})

def get_temp_email():
    user = ''.join(random.choices(string.ascii_lowercase, k=8)) + ''.join(random.choices(string.digits, k=4))
    domain = "boxfi.uk"
    try:
        _sess.post(f"{_KUKULU_BASE}/create.php", data={"address": user, "domain": domain}, timeout=5)
    except Exception as e:
        logerr(f"Create email gagal: {e}")
    email = f"{user}@{domain}"
    loginfo(f"📧 {email}")
    return email, {"user": user, "domain": domain}

def check_inbox(meta, timeout=60):
    user, dom = meta["user"], meta["domain"]
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = _sess.get(f"{_KUKULU_BASE}/inbox.php", params={"address": user, "domain": dom}, timeout=5)
            soup = BeautifulSoup(r.text, "html.parser")
            link = soup.select_one("div.mail a[href]")
            if link:
                href = link["href"]
                if not href.startswith("http"):
                    href = f"{_KUKULU_BASE}/{href}"
                r2 = _sess.get(href, timeout=5)
                body = soup.find("div", class_=re.compile(r"body|content"))
                txt = body.get_text(" ", strip=True) if body else r2.text
                return txt
        except Exception as e:
            logerr(f"Inbox check error: {e}")
        time.sleep(4)
    return None

def extract_otp(txt):
    if not txt:
        return None
    m = re.findall(r'\b\d{4,6}\b', txt)
    return m[0] if m else None

# ============================================================
#  UI COORDINATES — DETEKSI RESOLUSI
# ============================================================
pad_code_global = None

def get_device_resolution(pad_code):
    """Coba deteksi resolusi via screenshot atau default ke 720p"""
    screen = get_screenshot(pad_code)
    if screen is not None:
        h, w = screen.shape[:2]
        loginfo(f"📱 Device resolution: {w}x{h}")
        return w, h
    return 720, 1280  # default 720p portrait

def scale_coord(x, y, ref_w=720, ref_h=1280):
    """Scale koordinat dari 720x1280 ke resolusi device"""
    dev_w, dev_h = get_device_resolution(pad_code_global)
    return int(x * dev_w / ref_w), int(y * dev_h / ref_h)

UI_720 = {
    "email"  : (353, 490),
    "otp"    : (353, 660),
    "reff"   : (353, 835),
    "next"   : (353, 1007),
    "pass"   : (353, 620),
    "confirm": (353, 880),
    "continue": (353, 1355),
    "skip"   : (611, 140),
    "event"  : (353, 210),
    "spin"   : (353, 660),
    "claim"  : (551, 1330),
    "ok"     : (353, 955),
    "invite" : (563, 1023),
    "copy"   : (463, 1152),
    "close"  : (637, 670),
}

def get_ui_coords(pad_code):
    global pad_code_global
    pad_code_global = pad_code
    w, h = get_device_resolution(pad_code)
    scale = w / 720
    coords = {k: (int(v[0] * scale), int(v[1] * scale)) for k, v in UI_720.items()}
    return coords

# ============================================================
#  CORE: REGISTER + SPIN
# ============================================================
@retry_on_failure(max_retries=2, delay=5)
def register_and_spin(pad_code, pkg, reff_code=""):
    clear_app(pad_code, pkg)
    open_app(pad_code, pkg)
    time.sleep(5)

    ui = get_ui_coords(pad_code)

    # Step 1: Email
    tap(pad_code, *ui["email"])
    email, meta = get_temp_email()
    input_text(pad_code, email)
    tap(pad_code, *ui["next"])
    time.sleep(3)

    # Step 2: OTP
    body = check_inbox(meta, timeout=90)
    otp = extract_otp(body)
    if not otp:
        logerr("OTP tidak ditemukan dalam 90 detik")
        auto_close_popup(pad_code)
        raise RuntimeError("OTP missing")
    tap(pad_code, *ui["otp"])
    input_text(pad_code, otp)
    tap(pad_code, *ui["next"])
    time.sleep(3)

    # Step 3: Referral
    if reff_code:
        tap(pad_code, *ui["reff"])
        input_text(pad_code, reff_code)
    tap(pad_code, *ui["next"])
    time.sleep(3)

    # Step 4: Password
    pwd = gen_pass()
    tap(pad_code, *ui["pass"])
    input_text(pad_code, pwd)
    tap(pad_code, *ui["confirm"])
    input_text(pad_code, pwd)
    tap(pad_code, *ui["continue"])
    time.sleep(3)

    # Skip biometric
    tap(pad_code, *ui["skip"])
    time.sleep(2)

    # Claim & Spin
    tap(pad_code, *ui["event"])
    time.sleep(3)
    tap(pad_code, *ui["claim"])
    time.sleep(2)
    tap(pad_code, *ui["ok"])
    time.sleep(2)
    tap(pad_code, *ui["spin"])
    time.sleep(5)
    tap(pad_code, 353, 800)  # fallback close
    time.sleep(1)

    save_account({"email": email, "password": pwd, "reff_code": reff_code})
    loginfo(f"✅ Akun selesai: {email}")
    return True

def save_account(data):
    try:
        accs = json.load(open(OUTPUT_FILE)) if os.path.exists(OUTPUT_FILE) else []
        accs.append(data)
        with open(OUTPUT_FILE, "w") as f:
            json.dump(accs, f, indent=2)
        loginfo(f"💾 Simpan akun ke {OUTPUT_FILE}")
    except Exception as e:
        logerr(f"Simpan gagal: {e}")

# ============================================================
#  GET REFERRAL CODE
# ============================================================
def get_reff_code(pad_code):
    loginfo("🔍 Mengambil referral code...")

    # 1. Clipboard
    time.sleep(2)
    code = read_clipboard(pad_code)
    if code and len(code) >= 6:
        loginfo(f"📋 Dapat dari clipboard: {code}")
        return code

    # 2. OCR di area Invite Code
    screen = get_screenshot(pad_code)
    if screen is not None:
        text = ocr_region(screen, 200, 1100, 400, 100, "--psm 7")
        loginfo(f"🔍 OCR text: '{text}'")
        m = re.search(r'[A-Z0-9]{6,12}', text)
        if m:
            code = m.group(0)
            loginfo(f"✅ Dapat dari OCR: {code}")
            return code

    # 3. Tap "Copy" lalu baca clipboard
    ui = get_ui_coords(pad_code)
    tap(pad_code, *ui["invite"])  # go to invite page
    time.sleep(2)
    tap(pad_code, *ui["copy"])   # tap copy
    time.sleep(1)
    code = read_clipboard(pad_code)
    if code and len(code) >= 6:
        loginfo(f"📋 Copy+Clipboard: {code}")
        return code

    logerr("❌ Gagal ambil referral code")
    return None

# ============================================================
#  MAIN LOOP
# ============================================================
if __name__ == "__main__":
    loginfo("🔥 Mulai Auto-Reff Bot (v2.1) — dengan retry, install APK, & log file")

    if not ACCESS_KEY or not SECRET_KEY:
        logerr("❌ ACCESS_KEY / SECRET_KEY belum diisi di kode!")
        exit(1)
    if not PAD_CODES:
        logerr("❌ PAD_CODES kosong! Isi di kode.")
        exit(1)

    pkg = None
    master_code = None

    # Install APK di semua pad
    for i, pad in enumerate(PAD_CODES):
        loginfo(f"📦 Siapkan device {pad}...")
        try:
            pkg = install_apk(pad)
        except Exception as e:
            logerr(f"Install gagal di {pad}, lanjut... ({e})")
            pkg = "com.topnod.app"  # fallback

    # Buat akun master
    loginfo("🔑 Membuat akun master...")
    try:
        if register_and_spin(PAD_CODES[0], pkg):
            master_code = get_reff_code(PAD_CODES[0])
            if not master_code:
                logerr("Gagal ambil referral code master — abort")
                exit(1)
            loginfo(f"✅ Referral master: {master_code}")
    except Exception as e:
        logerr(f"Master akun gagal: {e}")
        exit(1)

    # Buat akun reff
    total_to_make = min(ACCOUNTS_TARGET - 1, len(PAD_CODES) - 1)
    for i in range(total_to_make):
        pad = PAD_CODES[i + 1]
        loginfo(f"🔄 Buat akun ke-{i+2} di pad {pad}")
        try:
            register_and_spin(pad, pkg, master_code)
        except Exception as e:
            logerr(f"Akun ke-{i+2} gagal: {e}")
            auto_close_popup(pad)
        time.sleep(10)

    loginfo(f"🎉 Selesai! Akun tersimpan di {OUTPUT_FILE}, log di {LOG_FILE}")
