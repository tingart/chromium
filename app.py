from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse, JSONResponse
from pydantic import BaseModel
from playwright.async_api import async_playwright
from typing import Optional, List
from collections import deque
import asyncio
import urllib.parse
import os
import time
import json
import traceback
import tarfile
import tempfile
import shutil
import hmac
import hashlib
import base64
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError

app = FastAPI(title="Remote Chromium Browser")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------

DATA_DIR = os.getenv("BROWSER_DATA_DIR", "/app/user_data")
MAX_TABS = int(os.getenv("MAX_TABS", "8"))

AUTH_USERNAME = os.getenv("AUTH_USERNAME", "admin")
AUTH_PASSWORD = os.getenv("AUTH_PASSWORD", "change-me")
AUTH_SECRET = os.getenv("AUTH_SECRET", "change-this-to-a-long-random-secret")
AUTH_COOKIE = "remote_chromium_session"

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET", "browser-backup")
SUPABASE_OBJECT = os.getenv("SUPABASE_OBJECT", "chromium-profile.tar.gz")
BACKUP_INTERVAL = max(60, int(os.getenv("BACKUP_INTERVAL", "300")))

backup_task = None
backup_lock = asyncio.Lock()
last_backup_time = None
last_backup_error = None
last_backup_status = "no backup yet"

stream_settings = {
    "format": "png",          # png / jpeg / webp
    "quality": 70,            # jpeg/webp quality
    "interval": 180,          # target frame interval in ms
    "width": 854,
    "height": 480,
    "max_pixels": 854 * 480,
    "auto_device_size": True,
    "device_width": 360,
    "device_height": 740,
    "chromium_ui_scale": 1.0,
}

# ------------------------------------------------------------
# Global browser state
# ------------------------------------------------------------

playwright_instance = None
context = None
pages: List = []
active_tab_index = 0

page_lock = asyncio.Lock()
screen_event = asyncio.Event()
last_frame = None

frame_times = deque(maxlen=240)
last_encode_ms = 0.0
last_frame_bytes = 0
last_action = "startup"

cpu_prev = None
cpu_prev_time = None

# ------------------------------------------------------------
# Models
# ------------------------------------------------------------

class NavigatePayload(BaseModel):
    url: str

class TouchPayload(BaseModel):
    x: float
    y: float
    action: Optional[str] = "click"
    delta_x: float = 0
    delta_y: float = 0

class KeyPayload(BaseModel):
    key: str

class TypePayload(BaseModel):
    text: str

class SettingsPayload(BaseModel):
    format: Optional[str] = None
    quality: Optional[int] = None
    interval: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    max_pixels: Optional[int] = None
    auto_device_size: Optional[bool] = None
    device_width: Optional[int] = None
    device_height: Optional[int] = None
    chromium_ui_scale: Optional[float] = None

# ------------------------------------------------------------
# Authentication + Supabase profile backup
# ------------------------------------------------------------

def _auth_token():
    user = AUTH_USERNAME.encode("utf-8")
    sig = hmac.new(AUTH_SECRET.encode("utf-8"), user, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(user + b"." + sig).decode("ascii")

def _valid_token(token):
    if not token:
        return False
    return hmac.compare_digest(token, _auth_token())

def _storage_url():
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return None
    bucket = urllib.parse.quote(SUPABASE_BUCKET, safe="")
    obj = "/".join(urllib.parse.quote(p, safe="") for p in SUPABASE_OBJECT.split("/"))
    return f"{SUPABASE_URL}/storage/v1/object/{bucket}/{obj}"

def _profile_has_data():
    try:
        return os.path.isdir(DATA_DIR) and any(True for _ in os.scandir(DATA_DIR))
    except Exception:
        return False

def _supabase_download():
    global last_backup_error, last_backup_status
    url = _storage_url()
    if not url:
        last_backup_status = "cloud disabled"
        return False
    req = urllib_request.Request(url, headers={
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
    })
    try:
        with urllib_request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        if not data:
            return False
        with tempfile.NamedTemporaryFile(delete=False, suffix=".tar.gz") as tmp:
            tmp.write(data)
            archive = tmp.name
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with tarfile.open(archive, "r:gz") as tar:
                tar.extractall(DATA_DIR)
            last_backup_status = "restored from cloud"
            last_backup_error = None
            return True
        finally:
            try: os.unlink(archive)
            except Exception: pass
    except HTTPError as e:
        # No object yet is normal on the first launch.
        if e.code == 404:
            last_backup_status = "no cloud backup yet"
            last_backup_error = None
        else:
            last_backup_error = f"Supabase download HTTP {e.code}: {e.read().decode('utf-8','replace')}"
        return False
    except Exception as e:
        last_backup_error = f"Supabase download: {e}"
        return False

def _make_profile_archive():
    fd, archive = tempfile.mkstemp(suffix=".tar.gz")
    os.close(fd)
    with tarfile.open(archive, "w:gz") as tar:
        if os.path.isdir(DATA_DIR):
            for root, dirs, files in os.walk(DATA_DIR):
                for name in files:
                    # Chromium lock/socket files must not be restored.
                    if name in {"SingletonLock", "SingletonSocket", "SingletonCookie"}:
                        continue
                    path = os.path.join(root, name)
                    arc = os.path.relpath(path, DATA_DIR)
                    try:
                        tar.add(path, arcname=arc, recursive=False)
                    except FileNotFoundError:
                        pass
    return archive

def _supabase_upload_archive(archive):
    global last_backup_time, last_backup_error, last_backup_status
    url = _storage_url()
    if not url:
        last_backup_status = "cloud disabled"
        return False
    data = Path(archive).read_bytes()
    req = urllib_request.Request(
        url,
        data=data,
        method="PUT",
        headers={
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Content-Type": "application/gzip",
            "x-upsert": "true",
        },
    )
    try:
        with urllib_request.urlopen(req, timeout=60) as resp:
            resp.read()
        last_backup_time = time.time()
        last_backup_error = None
        last_backup_status = "cloud backup complete"
        return True
    except HTTPError as e:
        last_backup_error = f"Supabase upload HTTP {e.code}: {e.read().decode('utf-8','replace')}"
        last_backup_status = "backup failed"
        return False
    except Exception as e:
        last_backup_error = f"Supabase upload: {e}"
        last_backup_status = "backup failed"
        return False

async def backup_profile():
    # Never closes Chromium: backup is a filesystem snapshot while the browser stays alive.
    if backup_lock.locked():
        return False
    async with backup_lock:
        archive = None
        try:
            archive = await asyncio.to_thread(_make_profile_archive)
            return await asyncio.to_thread(_supabase_upload_archive, archive)
        finally:
            if archive:
                try: os.unlink(archive)
                except Exception: pass

async def backup_loop():
    while True:
        await asyncio.sleep(BACKUP_INTERVAL)
        try:
            await backup_profile()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    public = {"/", "/login", "/auth/status", "/favicon.ico", "/health"}
    if path not in public and not _valid_token(request.cookies.get(AUTH_COOKIE)):
        return JSONResponse({"detail": "Authentication required"}, status_code=401)
    return await call_next(request)

# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

def mark_screen_dirty(action="unknown"):
    global last_action
    last_action = action
    screen_event.set()

async def get_active_page():
    global pages, active_tab_index
    if not pages:
        return None
    if active_tab_index >= len(pages):
        active_tab_index = max(0, len(pages) - 1)
    return pages[active_tab_index]

def sanitize_url(raw_input: str) -> str:
    url = (raw_input or "").strip()

    if not url:
        return "https://www.google.com"

    if url.startswith(("http://", "https://", "about:")):
        return url

    if "." in url and " " not in url:
        return "https://" + url

    return "https://www.google.com/search?q=" + urllib.parse.quote(url)

def clamp_int(value, minimum, maximum):
    return max(minimum, min(maximum, int(value)))

def current_viewport():
    if stream_settings.get("auto_device_size", True):
        return {
            "width": clamp_int(stream_settings.get("device_width", 360), 280, 2560),
            "height": clamp_int(stream_settings.get("device_height", 740), 400, 3200),
        }
    return {
        "width": stream_settings["width"],
        "height": stream_settings["height"],
    }

def cgroup_value(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return None

def get_memory_stats():
    # Linux cgroup v2 first.
    current = cgroup_value("/sys/fs/cgroup/memory.current")
    maximum = cgroup_value("/sys/fs/cgroup/memory.max")

    if current is not None:
        try:
            current_b = int(current)
            if maximum and maximum != "max":
                max_b = int(maximum)
            else:
                max_b = 0
            return {
                "used_mb": round(current_b / 1024 / 1024, 1),
                "limit_mb": round(max_b / 1024 / 1024, 1) if max_b else None,
            }
        except Exception:
            pass

    # Fallback: /proc.
    try:
        info = {}
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    info[parts[0].rstrip(":")] = int(parts[1]) * 1024

        total = info.get("MemTotal", 0)
        available = info.get("MemAvailable", 0)
        used = max(0, total - available)

        return {
            "used_mb": round(used / 1024 / 1024, 1),
            "limit_mb": round(total / 1024 / 1024, 1) if total else None,
        }
    except Exception:
        return {"used_mb": None, "limit_mb": None}

def get_cgroup_cpu_percent():
    global cpu_prev, cpu_prev_time

    stat = cgroup_value("/sys/fs/cgroup/cpu.stat")
    if not stat:
        return None

    usage_usec = None
    for line in stat.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "usage_usec":
            usage_usec = int(parts[1])
            break

    if usage_usec is None:
        return None

    now = time.monotonic()

    if cpu_prev is None:
        cpu_prev = usage_usec
        cpu_prev_time = now
        return 0.0

    elapsed = now - cpu_prev_time
    delta_cpu = (usage_usec - cpu_prev) / 1_000_000

    cpu_prev = usage_usec
    cpu_prev_time = now

    if elapsed <= 0:
        return 0.0

    # 100% means one full CPU core.
    return round(max(0.0, min(999.0, (delta_cpu / elapsed) * 100)), 1)

def get_process_memory():
    try:
        with open("/proc/self/statm", "r", encoding="utf-8") as f:
            pages = int(f.read().split()[1])
        return round(pages * os.sysconf("SC_PAGE_SIZE") / 1024 / 1024, 1)
    except Exception:
        return None

async def browser_memory_estimate():
    # Add RSS of Chromium processes when /proc is available.
    total = 0.0

    try:
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue

            cmd_path = f"/proc/{name}/cmdline"
            statm_path = f"/proc/{name}/statm"

            try:
                with open(cmd_path, "rb") as f:
                    cmd = f.read().decode("utf-8", "ignore").lower()

                if "chrom" not in cmd:
                    continue

                with open(statm_path, "r", encoding="utf-8") as f:
                    rss_pages = int(f.read().split()[1])

                total += rss_pages * os.sysconf("SC_PAGE_SIZE") / 1024 / 1024
            except Exception:
                continue
    except Exception:
        pass

    return round(total, 1)

async def build_stats():
    now = time.monotonic()

    # Server stream FPS over the latest ~2 seconds.
    recent = [t for t in frame_times if now - t <= 2.0]
    if len(recent) >= 2:
        span = max(0.001, recent[-1] - recent[0])
        fps = round((len(recent) - 1) / span, 1)
    else:
        fps = 0.0

    mem = get_memory_stats()
    browser_mem = await browser_memory_estimate()
    cpu = get_cgroup_cpu_percent()

    return {
        "server_fps": fps,
        "frame_bytes": last_frame_bytes,
        "frame_kb": round(last_frame_bytes / 1024, 1),
        "encode_ms": round(last_encode_ms, 1),
        "ram_mb": mem["used_mb"],
        "ram_limit_mb": mem["limit_mb"],
        "python_ram_mb": get_process_memory(),
        "chromium_ram_mb": browser_mem,
        "cpu_percent": cpu,
        "tabs": len(pages),
        "active_tab": active_tab_index,
        "viewport": current_viewport(),
        "format": stream_settings["format"],
        "quality": stream_settings["quality"],
        "interval": stream_settings["interval"],
        "last_action": last_action,
        "storage_path": DATA_DIR,
    }

# ------------------------------------------------------------
# Startup / shutdown
# ------------------------------------------------------------

@app.on_event("startup")
async def startup_event():
    global playwright_instance, context, pages, active_tab_index

    os.makedirs(DATA_DIR, exist_ok=True)
    if not _profile_has_data():
        await asyncio.to_thread(_supabase_download)

    playwright_instance = await async_playwright().start()

    # Persistent Chromium profile.
    # Cookies/localStorage/session data are stored here.
    context = await playwright_instance.chromium.launch_persistent_context(
        user_data_dir=DATA_DIR,
        headless=True,
        viewport=current_viewport(),
        device_scale_factor=1,
        is_mobile=False,
        has_touch=False,
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--disable-background-networking",
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-features=Translate,MediaRouter",
            "--no-first-run",
            "--no-default-browser-check",
        ],
    )

    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined
        });
    """)

    pages = context.pages

    if not pages:
        initial_page = await context.new_page()
        try:
            await initial_page.goto(
                "https://www.google.com",
                wait_until="commit",
                timeout=15000
            )
        except Exception:
            pass
        pages.append(initial_page)

    active_tab_index = 0
    global backup_task
    backup_task = asyncio.create_task(backup_loop())
    mark_screen_dirty("startup")

@app.on_event("shutdown")
async def shutdown_event():
    global playwright_instance, context, backup_task

    try:
        if backup_task:
            backup_task.cancel()
            try:
                await backup_task
            except asyncio.CancelledError:
                pass
        # Final snapshot before shutdown, without restarting Chromium.
        try:
            await backup_profile()
        except Exception:
            pass
        if context:
            await context.close()
    finally:
        if playwright_instance:
            await playwright_instance.stop()

# ------------------------------------------------------------
# Basic routes
# ------------------------------------------------------------


class LoginPayload(BaseModel):
    username: str
    password: str

@app.get("/auth/status")
async def auth_status(request: Request):
    return {"authenticated": _valid_token(request.cookies.get(AUTH_COOKIE))}

@app.post("/login")
async def login(payload: LoginPayload, response: Response):
    if not (
        hmac.compare_digest(payload.username, AUTH_USERNAME) and
        hmac.compare_digest(payload.password, AUTH_PASSWORD)
    ):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    response.set_cookie(
        AUTH_COOKIE, _auth_token(),
        httponly=True, secure=True, samesite="lax", max_age=60*60*24*30, path="/"
    )
    return {"status": "success"}

@app.post("/logout")
async def logout(response: Response):
    response.delete_cookie(AUTH_COOKIE, path="/")
    return {"status": "success"}

@app.get("/backup/status")
async def backup_status():
    return {
        "status": last_backup_status,
        "last_backup": last_backup_time,
        "error": last_backup_error,
        "cloud_enabled": bool(_storage_url()),
        "bucket": SUPABASE_BUCKET,
        "object": SUPABASE_OBJECT,
    }

@app.post("/backup/now")
async def backup_now():
    ok = await backup_profile()
    return {
        "status": "success" if ok else "error",
        "message": last_backup_status,
        "last_backup": last_backup_time,
        "error": last_backup_error,
    }

@app.get("/", response_class=HTMLResponse)
async def read_root():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    html_path = os.path.join(base_dir, "index.html")

    if os.path.exists(html_path):
        return FileResponse(html_path)

    return HTMLResponse(
        "<h2>Error: index.html not found</h2>",
        status_code=404
    )

@app.get("/health")
async def health():
    return {
        "status": "online",
        "browser": "chromium",
        "tabs": len(pages),
        "persistent_profile": True,
        "storage_path": DATA_DIR,
    }

@app.get("/settings")
async def get_settings():
    return {
        "settings": stream_settings,
        "viewport": current_viewport(),
    }

@app.post("/settings")
async def update_settings(payload: SettingsPayload):
    allowed_formats = {"png", "jpeg", "webp"}

    if payload.format is not None:
        fmt = payload.format.lower()
        if fmt in allowed_formats:
            stream_settings["format"] = fmt

    if payload.quality is not None:
        stream_settings["quality"] = clamp_int(payload.quality, 10, 100)

    if payload.interval is not None:
        stream_settings["interval"] = clamp_int(payload.interval, 30, 2000)

    if payload.width is not None:
        stream_settings["width"] = clamp_int(payload.width, 320, 1280)

    if payload.height is not None:
        stream_settings["height"] = clamp_int(payload.height, 240, 900)

    if payload.max_pixels is not None:
        stream_settings["max_pixels"] = clamp_int(
            payload.max_pixels, 100000, 2_000_000
        )
    if payload.auto_device_size is not None:
        stream_settings["auto_device_size"] = bool(payload.auto_device_size)
    if payload.device_width is not None:
        stream_settings["device_width"] = clamp_int(payload.device_width, 280, 2560)
    if payload.device_height is not None:
        stream_settings["device_height"] = clamp_int(payload.device_height, 400, 3200)
    if payload.chromium_ui_scale is not None:
        stream_settings["chromium_ui_scale"] = max(0.7, min(1.8, float(payload.chromium_ui_scale)))

    if context:
        try:
            await context.set_viewport_size(
                current_viewport()
            )
        except Exception:
            pass

    mark_screen_dirty("settings")

    return {
        "status": "success",
        "settings": stream_settings,
        "viewport": current_viewport(),
    }

# ------------------------------------------------------------
# Screen streaming
# ------------------------------------------------------------

async def capture_frame(page):
    global last_frame, last_encode_ms, last_frame_bytes

    fmt = stream_settings["format"]
    quality = stream_settings["quality"]

    started = time.perf_counter()

    try:
        # Do not disable animations here. That can make live pages/videos
        # behave strangely and was a source of bad visual behavior.
        if fmt == "jpeg":
            data = await page.screenshot(
                type="jpeg",
                quality=quality,
                animations="allow",
                scale="css",
            )
        elif fmt == "webp":
            # Chromium/Playwright versions differ on WebP screenshot support.
            # Try WebP first; fall back to PNG if unsupported.
            try:
                data = await page.screenshot(
                    type="webp",
                    quality=quality,
                    animations="allow",
                    scale="css",
                )
            except Exception:
                data = await page.screenshot(
                    type="png",
                    animations="allow",
                    scale="css",
                )
        else:
            data = await page.screenshot(
                type="png",
                animations="allow",
                scale="css",
            )

        last_encode_ms = (time.perf_counter() - started) * 1000
        last_frame_bytes = len(data)
        last_frame = data
        frame_times.append(time.monotonic())

        return data

    except Exception:
        return None

async def frame_generator():
    global last_frame

    while True:
        page = await get_active_page()

        if page:
            try:
                data = await capture_frame(page)

                if data:
                    content_type = {
                        "png": "image/png",
                        "jpeg": "image/jpeg",
                        "webp": "image/webp",
                    }.get(stream_settings["format"], "image/png")

                    yield (
                        b"--frame\r\n"
                        + f"Content-Type: {content_type}\r\n".encode()
                        + f"Content-Length: {len(data)}\r\n\r\n".encode()
                        + data
                        + b"\r\n"
                    )
            except Exception:
                pass

        # Wake immediately after user interaction/settings, otherwise
        # respect the selected stream interval.
        try:
            await asyncio.wait_for(
                screen_event.wait(),
                timeout=max(0.03, stream_settings["interval"] / 1000)
            )
            screen_event.clear()
        except asyncio.TimeoutError:
            pass

@app.get("/screen")
async def stream_screen():
    return StreamingResponse(
        frame_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Connection": "keep-alive",
        },
    )

# ------------------------------------------------------------
# Navigation
# ------------------------------------------------------------

@app.post("/navigate")
async def navigate(payload: NavigatePayload):
    page = await get_active_page()

    if not page:
        return {"status": "error", "message": "No active page"}

    target_url = sanitize_url(payload.url)

    try:
        async with page_lock:
            await page.goto(
                target_url,
                wait_until="commit",
                timeout=20000
            )

        mark_screen_dirty("navigate")

        return {
            "status": "success",
            "url": page.url,
        }

    except Exception as e:
        mark_screen_dirty("navigate-error")
        return {
            "status": "error",
            "message": str(e),
            "url": page.url,
        }

@app.post("/history/back")
async def history_back():
    page = await get_active_page()

    if not page:
        return {"status": "error"}

    try:
        async with page_lock:
            await page.go_back(
                wait_until="commit",
                timeout=10000
            )

        mark_screen_dirty("back")
        return {"status": "success", "url": page.url}

    except Exception as e:
        return {"status": "error", "message": str(e), "url": page.url}

@app.post("/history/forward")
async def history_forward():
    page = await get_active_page()

    if not page:
        return {"status": "error"}

    try:
        async with page_lock:
            await page.go_forward(
                wait_until="commit",
                timeout=10000
            )

        mark_screen_dirty("forward")
        return {"status": "success", "url": page.url}

    except Exception as e:
        return {"status": "error", "message": str(e), "url": page.url}

@app.post("/reload")
async def reload_page():
    page = await get_active_page()

    if not page:
        return {"status": "error"}

    try:
        async with page_lock:
            await page.reload(
                wait_until="commit",
                timeout=15000
            )

        mark_screen_dirty("reload")
        return {"status": "success", "url": page.url}

    except Exception as e:
        return {"status": "error", "message": str(e), "url": page.url}

@app.post("/stop")
async def stop_page():
    page = await get_active_page()

    if not page:
        return {"status": "error"}

    try:
        await page.evaluate("window.stop()")
        mark_screen_dirty("stop")
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/home")
async def home():
    return await navigate(NavigatePayload(url="https://www.google.com"))

# ------------------------------------------------------------
# Tabs
# ------------------------------------------------------------

@app.get("/tabs")
async def get_tabs():
    global pages, active_tab_index

    tabs_data = []

    for idx, p in enumerate(pages):
        try:
            title = await p.title()
            url = p.url

            tabs_data.append({
                "id": idx,
                "title": title or "New Tab",
                "url": url,
            })

        except Exception:
            tabs_data.append({
                "id": idx,
                "title": "New Tab",
                "url": "about:blank",
            })

    return {
        "tabs": tabs_data,
        "active_index": active_tab_index,
    }

@app.post("/tabs/new")
async def new_tab():
    global pages, active_tab_index

    if len(pages) >= MAX_TABS:
        return {
            "status": "error",
            "message": f"Maximum {MAX_TABS} tabs allowed"
        }

    try:
        # Do not wait for Google to finish loading.
        # This makes the new-tab action much faster.
        new_page = await context.new_page()
        pages.append(new_page)
        active_tab_index = len(pages) - 1

        asyncio.create_task(
            background_open_home(new_page)
        )

        mark_screen_dirty("new-tab")
        return await get_tabs()

    except Exception as e:
        return {"status": "error", "message": str(e)}

async def background_open_home(page):
    try:
        await page.goto(
            "https://www.google.com",
            wait_until="commit",
            timeout=15000
        )
    except Exception:
        pass
    mark_screen_dirty("new-tab-loaded")

@app.post("/tabs/switch")
async def switch_tab(payload: dict):
    global active_tab_index, pages

    idx = int(payload.get("index", 0))

    if 0 <= idx < len(pages):
        active_tab_index = idx

        try:
            await pages[idx].bring_to_front()
        except Exception:
            pass

        mark_screen_dirty("switch-tab")

    return await get_tabs()

@app.post("/tabs/close")
async def close_tab(payload: dict):
    global pages, active_tab_index

    idx = int(payload.get("index", 0))

    if len(pages) <= 1:
        return await get_tabs()

    if 0 <= idx < len(pages):
        p = pages.pop(idx)

        try:
            await p.close()
        except Exception:
            pass

        if idx < active_tab_index:
            active_tab_index -= 1
        elif active_tab_index >= len(pages):
            active_tab_index = len(pages) - 1

        mark_screen_dirty("close-tab")

    return await get_tabs()

# ------------------------------------------------------------
# Touch / mouse
# ------------------------------------------------------------

@app.post("/touch")
async def handle_touch(payload: TouchPayload):
    page = await get_active_page()

    if not page:
        return {"status": "error"}

    try:
        vw = stream_settings["width"]
        vh = stream_settings["height"]

        x = max(0, min(float(payload.x), vw - 1))
        y = max(0, min(float(payload.y), vh - 1))

        async with page_lock:
            if payload.action == "click":
                await page.mouse.click(x, y, delay=30)

            elif payload.action == "right_click":
                await page.mouse.click(
                    x, y,
                    button="right",
                    delay=30
                )

            elif payload.action == "double_click":
                await page.mouse.dblclick(
                    x, y,
                    delay=30
                )

            elif payload.action == "scroll":
                dx = max(-1200, min(1200, payload.delta_x))
                dy = max(-1600, min(1600, payload.delta_y))
                await page.mouse.wheel(dx, dy)

            elif payload.action == "move":
                await page.mouse.move(x, y)

        mark_screen_dirty(payload.action or "touch")

        return {"status": "success"}

    except Exception as e:
        return {
            "status": "error",
            "message": str(e),
        }

# ------------------------------------------------------------
# Exact text input
# ------------------------------------------------------------

@app.post("/type")
async def type_text(payload: TypePayload):
    page = await get_active_page()

    if not page:
        return {"status": "error"}

    try:
        # insert_text sends the whole string as text instead of generating
        # one HTTP request per character. This prevents fast typing loss.
        await page.keyboard.insert_text(payload.text)

        mark_screen_dirty("type")

        return {
            "status": "success",
            "length": len(payload.text),
        }

    except Exception as e:
        return {
            "status": "error",
            "message": str(e),
        }

@app.post("/key")
async def handle_key(payload: KeyPayload):
    page = await get_active_page()

    if not page:
        return {"status": "error"}

    try:
        key = payload.key

        # Playwright key names.
        aliases = {
            "Backspace": "Backspace",
            "Enter": "Enter",
            "Tab": "Tab",
            "Escape": "Escape",
            "Delete": "Delete",
            "ArrowLeft": "ArrowLeft",
            "ArrowRight": "ArrowRight",
            "ArrowUp": "ArrowUp",
            "ArrowDown": "ArrowDown",
            "Home": "Home",
            "End": "End",
        }

        await page.keyboard.press(
            aliases.get(key, key)
        )

        mark_screen_dirty("key")

        return {"status": "success"}

    except Exception as e:
        return {
            "status": "error",
            "message": str(e),
        }

# ------------------------------------------------------------
# Device clipboard bridge
# ------------------------------------------------------------

async def selected_text_from_page(page):
    return await page.evaluate("""
    () => {
        const el = document.activeElement;

        if (
            el &&
            typeof el.selectionStart === "number" &&
            typeof el.selectionEnd === "number"
        ) {
            return String(el.value || "").substring(
                el.selectionStart,
                el.selectionEnd
            );
        }

        const sel = window.getSelection();
        return sel ? sel.toString() : "";
    }
    """)

@app.get("/selection")
async def get_selection():
    page = await get_active_page()

    if not page:
        return {"status": "error", "text": ""}

    try:
        text = await selected_text_from_page(page)
        return {"status": "success", "text": text}
    except Exception as e:
        return {"status": "error", "text": "", "message": str(e)}

@app.post("/cut")
async def cut_selection():
    page = await get_active_page()

    if not page:
        return {"status": "error", "text": ""}

    try:
        text = await selected_text_from_page(page)

        async with page_lock:
            await page.keyboard.press("Control+X")

        mark_screen_dirty("cut")

        return {
            "status": "success",
            "text": text,
        }

    except Exception as e:
        return {
            "status": "error",
            "text": "",
            "message": str(e),
        }

# ------------------------------------------------------------
# Diagnostics
# ------------------------------------------------------------

@app.get("/stats")
async def stats():
    return await build_stats()

@app.get("/storage")
async def storage():
    total_size = 0
    file_count = 0

    try:
        for root, dirs, files in os.walk(DATA_DIR):
            for name in files:
                try:
                    total_size += os.path.getsize(
                        os.path.join(root, name)
                    )
                    file_count += 1
                except Exception:
                    pass
    except Exception:
        pass

    return {
        "path": DATA_DIR,
        "exists": os.path.exists(DATA_DIR),
        "files": file_count,
        "size_mb": round(total_size / 1024 / 1024, 2),
        "persistent_profile": True,
    }
