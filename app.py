from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from pydantic import BaseModel
from playwright.async_api import async_playwright
from typing import Optional, List
import urllib.parse
import asyncio
import os
import math


# ============================================================
# APP
# ============================================================

app = FastAPI(title="Remote Browser Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# GLOBAL BROWSER STATE
# ============================================================

playwright_instance = None
browser = None
context = None

pages: List = []
active_tab_index = 0


# ============================================================
# SCREEN / STREAM SETTINGS
# ============================================================

stream_settings = {
    "format": "jpeg",
    "quality": 35,

    # Idle update interval in milliseconds.
    # 1000 = 1 FPS
    # 500  = 2 FPS
    # 250  = 4 FPS
    "interval": 700,

    "width": 854,
    "height": 480,

    # Maximum screenshot pixel budget.
    "max_pixels": 500000,
}


# Event used to immediately wake the stream after interaction.
screen_event = asyncio.Event()

# Prevent multiple Playwright operations from colliding.
page_lock = asyncio.Lock()

# Last generated frame.
last_frame = None

# Frame sequence.
frame_id = 0


# ============================================================
# VIEWPORT
# ============================================================

current_viewport = {
    "width": 854,
    "height": 480
}


# ============================================================
# HELPERS
# ============================================================

def mark_screen_dirty():
    """
    Tell the streaming system that the page changed.
    """
    try:
        screen_event.set()
    except Exception:
        pass


def clamp(value, minimum, maximum):
    return max(minimum, min(maximum, value))


def calculate_viewport(width: int, height: int):
    """
    Keep viewport lightweight while preserving aspect ratio.
    """

    width = max(240, int(width))
    height = max(180, int(height))

    # Hard maximum dimensions.
    max_width = 1200
    max_height = 1000

    width = min(width, max_width)
    height = min(height, max_height)

    max_pixels = int(stream_settings.get("max_pixels", 500000))

    pixels = width * height

    if pixels > max_pixels:
        scale = math.sqrt(max_pixels / pixels)
        width = max(240, int(width * scale))
        height = max(180, int(height * scale))

    return width, height


def sanitize_url(raw_input: str) -> str:
    url = (raw_input or "").strip()

    if not url:
        return "https://www.google.com"

    if url.startswith(("http://", "https://")):
        return url

    if "." in url and " " not in url:
        return "https://" + url

    return (
        "https://www.google.com/search?q="
        + urllib.parse.quote(url)
    )


async def get_active_page():
    if not pages:
        return None

    if active_tab_index >= len(pages):
        return pages[-1]

    return pages[active_tab_index]


# ============================================================
# STARTUP
# ============================================================

@app.on_event("startup")
async def startup_event():

    global playwright_instance
    global browser
    global context
    global pages
    global active_tab_index
    global current_viewport

    playwright_instance = await async_playwright().start()

    browser = await playwright_instance.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",

            # Reduce unnecessary background work.
            "--disable-background-networking",
            "--disable-backgrounding-occluded-windows",
            "--disable-breakpad",
            "--disable-component-update",
            "--disable-default-apps",
            "--disable-extensions",
            "--disable-sync",
            "--no-first-run",
            "--no-default-browser-check",
        ]
    )

    current_viewport["width"] = stream_settings["width"]
    current_viewport["height"] = stream_settings["height"]

    context = await browser.new_context(
        viewport={
            "width": current_viewport["width"],
            "height": current_viewport["height"]
        },

        device_scale_factor=1,

        is_mobile=False,
        has_touch=False,

        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        )
    )

    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined
        });
    """)

    initial_page = await context.new_page()

    try:
        await initial_page.goto(
            "https://www.google.com",
            wait_until="domcontentloaded",
            timeout=15000
        )
    except Exception:
        pass

    pages.append(initial_page)
    active_tab_index = 0

    mark_screen_dirty()


# ============================================================
# SHUTDOWN
# ============================================================

@app.on_event("shutdown")
async def shutdown_event():

    global browser
    global playwright_instance

    if browser:
        try:
            await browser.close()
        except Exception:
            pass

    if playwright_instance:
        try:
            await playwright_instance.stop()
        except Exception:
            pass


# ============================================================
# FRONTEND
# ============================================================

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


# ============================================================
# SCREENSHOT ENGINE
# ============================================================

async def make_screenshot():

    global last_frame
    global frame_id

    page = await get_active_page()

    if not page:
        return None

    async with page_lock:

        try:

            if stream_settings["format"] == "png":

                image = await page.screenshot(
                    type="png",
                    animations="disabled",
                    scale="css"
                )

            else:

                quality = int(
                    clamp(
                        stream_settings.get("quality", 35),
                        10,
                        80
                    )
                )

                image = await page.screenshot(
                    type="jpeg",
                    quality=quality,
                    animations="disabled",
                    scale="css"
                )

            last_frame = image
            frame_id += 1

            return image

        except Exception:
            return last_frame


async def frame_generator():

    global last_frame

    # Always send an initial frame.
    image = await make_screenshot()

    if image:
        content_type = (
            "image/png"
            if stream_settings["format"] == "png"
            else "image/jpeg"
        )

        yield (
            b"--frame\r\n"
            + f"Content-Type: {content_type}\r\n\r\n".encode()
            + image
            + b"\r\n"
        )

    while True:

        interval = int(
            clamp(
                stream_settings.get("interval", 700),
                100,
                5000
            )
        )

        # Wait for either:
        # 1. user interaction / page change
        # 2. idle timer
        try:
            await asyncio.wait_for(
                screen_event.wait(),
                timeout=interval / 1000
            )
        except asyncio.TimeoutError:
            pass

        # Clear BEFORE screenshot.
        # If an action happens while screenshotting,
        # the Event will become set again.
        screen_event.clear()

        image = await make_screenshot()

        if image:

            content_type = (
                "image/png"
                if stream_settings["format"] == "png"
                else "image/jpeg"
            )

            yield (
                b"--frame\r\n"
                + f"Content-Type: {content_type}\r\n\r\n".encode()
                + image
                + b"\r\n"
            )


@app.get("/screen")
async def stream_screen():

    return StreamingResponse(
        frame_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Connection": "keep-alive",
        }
    )


# ============================================================
# MODELS
# ============================================================

class NavigatePayload(BaseModel):
    url: str


class TouchPayload(BaseModel):
    x: float
    y: float

    action: Optional[str] = "click"


class KeyPayload(BaseModel):
    key: str


class ViewportPayload(BaseModel):
    width: int
    height: int


class StreamSettingsPayload(BaseModel):
    format: Optional[str] = None
    quality: Optional[int] = None
    interval: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    max_pixels: Optional[int] = None


# ============================================================
# SETTINGS API
# ============================================================

@app.get("/settings")
async def get_settings():

    return {
        "stream": stream_settings,
        "viewport": current_viewport
    }


@app.post("/settings")
async def update_settings(
    payload: StreamSettingsPayload
):

    global stream_settings

    data = payload.model_dump(exclude_none=True)

    if "format" in data:
        if data["format"] not in ("jpeg", "png"):
            data["format"] = "jpeg"

    if "quality" in data:
        data["quality"] = int(
            clamp(data["quality"], 10, 80)
        )

    if "interval" in data:
        data["interval"] = int(
            clamp(data["interval"], 100, 5000)
        )

    if "max_pixels" in data:
        data["max_pixels"] = int(
            clamp(data["max_pixels"], 100000, 1000000)
        )

    for key in (
        "format",
        "quality",
        "interval",
        "max_pixels"
    ):
        if key in data:
            stream_settings[key] = data[key]

    if "width" in data or "height" in data:

        width = int(
            data.get(
                "width",
                current_viewport["width"]
            )
        )

        height = int(
            data.get(
                "height",
                current_viewport["height"]
            )
        )

        width, height = calculate_viewport(
            width,
            height
        )

        stream_settings["width"] = width
        stream_settings["height"] = height

        await set_browser_viewport(width, height)

    mark_screen_dirty()

    return {
        "status": "success",
        "stream": stream_settings,
        "viewport": current_viewport
    }


# ============================================================
# VIEWPORT API
# ============================================================

async def set_browser_viewport(width, height):

    global current_viewport

    width, height = calculate_viewport(
        width,
        height
    )

    async with page_lock:

        if context:

            try:
                await context.set_viewport_size({
                    "width": width,
                    "height": height
                })

                current_viewport["width"] = width
                current_viewport["height"] = height

            except Exception:
                pass

    return current_viewport


@app.post("/viewport")
async def update_viewport(
    payload: ViewportPayload
):

    width, height = calculate_viewport(
        payload.width,
        payload.height
    )

    await set_browser_viewport(
        width,
        height
    )

    stream_settings["width"] = current_viewport["width"]
    stream_settings["height"] = current_viewport["height"]

    mark_screen_dirty()

    return {
        "status": "success",
        "width": current_viewport["width"],
        "height": current_viewport["height"]
    }


@app.get("/viewport")
async def get_viewport():

    return current_viewport


# ============================================================
# TABS
# ============================================================

@app.get("/tabs")
async def get_tabs():

    global pages
    global active_tab_index

    tabs_data = []

    for idx, page in enumerate(pages):

        try:

            title = await page.title()

            tabs_data.append({
                "id": idx,
                "title": title or "New Tab",
                "url": page.url
            })

        except Exception:

            tabs_data.append({
                "id": idx,
                "title": "New Tab",
                "url": "about:blank"
            })

    return {
        "tabs": tabs_data,
        "active_index": active_tab_index
    }


@app.post("/tabs/new")
async def new_tab():

    global active_tab_index

    async with page_lock:

        new_page = await context.new_page()

        try:
            await new_page.goto(
                "https://www.google.com",
                wait_until="commit",
                timeout=10000
            )
        except Exception:
            pass

        pages.append(new_page)

        active_tab_index = len(pages) - 1

    mark_screen_dirty()

    return await get_tabs()


@app.post("/tabs/switch")
async def switch_tab(payload: dict):

    global active_tab_index

    idx = int(payload.get("index", 0))

    if 0 <= idx < len(pages):
        active_tab_index = idx

    mark_screen_dirty()

    return await get_tabs()


@app.post("/tabs/close")
async def close_tab(payload: dict):

    global active_tab_index

    idx = int(payload.get("index", 0))

    if len(pages) > 1 and 0 <= idx < len(pages):

        async with page_lock:

            page = pages.pop(idx)

            try:
                await page.close()
            except Exception:
                pass

            if active_tab_index >= len(pages):
                active_tab_index = len(pages) - 1

            elif idx < active_tab_index:
                active_tab_index -= 1

    mark_screen_dirty()

    return await get_tabs()


# ============================================================
# NAVIGATION
# ============================================================

@app.post("/navigate")
async def navigate(payload: NavigatePayload):

    global active_tab_index

    target_url = sanitize_url(payload.url)

    page = await get_active_page()

    if not page:
        return {
            "status": "error",
            "message": "No active page"
        }

    try:

        async with page_lock:

            # Commit is intentionally used instead of waiting
            # for the whole page to finish.
            await page.goto(
                target_url,
                wait_until="commit",
                timeout=12000
            )

        mark_screen_dirty()

        # Ask browser to update again when DOM is ready.
        async def later_update():

            try:

                await page.wait_for_load_state(
                    "domcontentloaded",
                    timeout=10000
                )

            except Exception:
                pass

            mark_screen_dirty()

        asyncio.create_task(later_update())

        return {
            "status": "success",
            "url": page.url
        }

    except Exception as e:

        mark_screen_dirty()

        return {
            "status": "error",
            "message": str(e),
            "url": page.url
        }


# ============================================================
# TOUCH / MOUSE
# ============================================================

@app.post("/touch")
async def handle_touch(
    payload: TouchPayload
):

    page = await get_active_page()

    if not page:
        return {
            "status": "error",
            "message": "No active page"
        }

    x = float(payload.x)
    y = float(payload.y)

    width = current_viewport["width"]
    height = current_viewport["height"]

    # Absolute safety clamp.
    x = clamp(x, 0, width - 1)
    y = clamp(y, 0, height - 1)

    action = payload.action or "click"

    try:

        async with page_lock:

            await page.mouse.move(x, y)

            if action == "click":

                await page.mouse.click(
                    x,
                    y,
                    delay=30
                )

            elif action == "right_click":

                await page.mouse.click(
                    x,
                    y,
                    button="right",
                    delay=30
                )

            elif action == "double_click":

                await page.mouse.dblclick(
                    x,
                    y,
                    delay=30
                )

            elif action == "middle_click":

                await page.mouse.click(
                    x,
                    y,
                    button="middle",
                    delay=30
                )

            elif action == "scroll_down":

                await page.mouse.wheel(
                    0,
                    350
                )

            elif action == "scroll_up":

                await page.mouse.wheel(
                    0,
                    -350
                )

            elif action == "scroll_left":

                await page.mouse.wheel(
                    -350,
                    0
                )

            elif action == "scroll_right":

                await page.mouse.wheel(
                    350,
                    0
                )

        mark_screen_dirty()

        return {
            "status": "success",
            "x": x,
            "y": y,
            "action": action
        }

    except Exception as e:

        return {
            "status": "error",
            "message": str(e)
        }


# ============================================================
# KEYBOARD
# ============================================================

@app.post("/key")
async def handle_key(
    payload: KeyPayload
):

    page = await get_active_page()

    if not page:
        return {
            "status": "error"
        }

    key = payload.key or ""

    try:

        async with page_lock:

            if key in (
                "Backspace",
                "Enter",
                "Tab",
                "Escape",
                "ArrowUp",
                "ArrowDown",
                "ArrowLeft",
                "ArrowRight",
                "Delete",
                "Home",
                "End",
                "PageUp",
                "PageDown",
                "Space"
            ):

                actual_key = (
                    " "
                    if key == "Space"
                    else key
                )

                await page.keyboard.press(
                    actual_key
                )

            else:

                await page.keyboard.type(
                    key
                )

        mark_screen_dirty()

        return {
            "status": "success"
        }

    except Exception as e:

        return {
            "status": "error",
            "message": str(e)
        }


# ============================================================
# FORCE REFRESH FRAME
# ============================================================

@app.post("/refresh")
async def force_refresh():

    mark_screen_dirty()

    return {
        "status": "success"
    }


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
async def health():

    return {
        "status": "online",
        "browser": browser is not None,
        "pages": len(pages),
        "active_tab": active_tab_index,
        "viewport": current_viewport,
        "stream": stream_settings
    }
