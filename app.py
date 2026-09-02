from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from pydantic import BaseModel
from typing import Optional, List
from playwright.async_api import async_playwright
import urllib.parse
import asyncio
import os

app = FastAPI(title="Remote Playwright Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

playwright_instance = None
browser = None
context = None
pages: List = []
active_tab_index: int = 0

# Default viewport (will be overridden by client size if provided)
DEFAULT_WIDTH = 854
DEFAULT_HEIGHT = 480

@app.on_event("startup")
async def startup_event():
    global playwright_instance, browser, context, pages, active_tab_index
    playwright_instance = await async_playwright().start()
    
    browser = await playwright_instance.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--disable-web-security",
            "--disable-features=IsolateOrigins,site-per-process"
        ]
    )
    
    context = await browser.new_context(
        viewport={"width": DEFAULT_WIDTH, "height": DEFAULT_HEIGHT},
        device_scale_factor=1,
        is_mobile=True,   # mobile mode enabled
        has_touch=True,   # touch events enabled
        user_agent="Mozilla/5.0 (Linux; Android 12; Mobile) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36"
    )
    
    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined
        });
        const style = document.createElement('style');
        style.innerHTML = '* { animation: none !important; transition: none !important; }';
        document.head.appendChild(style);
    """)
    
    initial_page = await context.new_page()
    await initial_page.goto("https://www.google.com")
    pages.append(initial_page)
    active_tab_index = 0

@app.on_event("shutdown")
async def shutdown_event():
    global playwright_instance, browser
    if browser:
        await browser.close()
    if playwright_instance:
        await playwright_instance.stop()

@app.get("/", response_class=HTMLResponse)
async def read_root():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    html_path = os.path.join(base_dir, "index.html")
    if os.path.exists(html_path):
        return FileResponse(html_path)
    return HTMLResponse(f"<h2>Error: index.html not found</h2>", status_code=404)

async def frame_generator():
    global pages, active_tab_index
    while True:
        if pages and active_tab_index < len(pages):
            try:
                current_page = pages[active_tab_index]
                screenshot = await current_page.screenshot(type="jpeg", quality=25)  # lower quality
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + screenshot + b'\r\n')
            except Exception:
                pass
        await asyncio.sleep(0.35)  # ~3 FPS for stability

@app.get("/screen")
async def stream_screen():
    return StreamingResponse(frame_generator(), media_type="multipart/x-mixed-replace; boundary=frame")

class NavigatePayload(BaseModel):
    url: str

class TouchPayload(BaseModel):
    x: int
    y: int
    action: Optional[str] = "click"
    screen_width: Optional[int] = DEFAULT_WIDTH
    screen_height: Optional[int] = DEFAULT_HEIGHT

class KeyPayload(BaseModel):
    key: str

def sanitize_url(raw_input: str) -> str:
    url = raw_input.strip()
    if not url:
        return "https://www.google.com"
    if url.startswith(("http://", "https://")):
        return url
    if "." in url and " " not in url:
        return f"https://{url}"
    return f"https://www.google.com/search?q={urllib.parse.quote(url)}"

@app.get("/tabs")
async def get_tabs():
    global pages, active_tab_index
    tabs_data = []
    for idx, p in enumerate(pages):
        try:
            title = await p.title()
            tabs_data.append({"id": idx, "title": title or p.url, "url": p.url})
        except Exception:
            tabs_data.append({"id": idx, "title": "New Tab", "url": "about:blank"})
    return {"tabs": tabs_data, "active_index": active_tab_index}

@app.post("/tabs/new")
async def new_tab():
    global context, pages, active_tab_index
    new_page = await context.new_page()
    await new_page.goto("https://www.google.com")
    pages.append(new_page)
    active_tab_index = len(pages) - 1
    return await get_tabs()

@app.post("/tabs/switch")
async def switch_tab(payload: dict):
    global active_tab_index, pages
    idx = payload.get("index", 0)
    if 0 <= idx < len(pages):
        active_tab_index = idx
    return await get_tabs()

@app.post("/tabs/close")
async def close_tab(payload: dict):
    global pages, active_tab_index
    idx = payload.get("index", 0)
    if len(pages) > 1 and 0 <= idx < len(pages):
        p = pages.pop(idx)
        await p.close()
        active_tab_index = min(active_tab_index, len(pages) - 1)
    return await get_tabs()

@app.post("/navigate")
async def navigate(payload: NavigatePayload):
    global pages, active_tab_index
    target_url = sanitize_url(payload.url)
    try:
        current_page = pages[active_tab_index]
        await current_page.goto(target_url, timeout=15000)
        return {"status": "success", "url": current_page.url}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/touch")
@app.post("/touch")
async def handle_touch(payload: TouchPayload):
    global pages, active_tab_index
    if not pages:
        return {"status": "error"}
    
    current_page = pages[active_tab_index]
    viewport = current_page.viewport_size  # <-- FIXED
    
    # Scale based on client screen size
    scale_x = viewport["width"] / payload.screen_width
    scale_y = viewport["height"] / payload.screen_height
    
    real_x = int(payload.x * scale_x)
    real_y = int(payload.y * scale_y)
    
    await current_page.mouse.move(real_x, real_y)
    
    if payload.action == "click":
        await current_page.mouse.click(real_x, real_y, delay=50)
    elif payload.action == "right_click":
        await current_page.mouse.click(real_x, real_y, button="right")
    elif payload.action == "scroll_down":
        await current_page.mouse.wheel(0, 350)
    elif payload.action == "scroll_up":
        await current_page.mouse.wheel(0, -350)
        
    return {"status": "success"}

@app.post("/key")
async def handle_key(payload: KeyPayload):
    global pages, active_tab_index
    if pages:
        current_page = pages[active_tab_index]
        if payload.key == "Backspace":
            await current_page.keyboard.press("Backspace")
        elif payload.key == "Enter":
            await current_page.keyboard.press("Enter")
        else:
            await current_page.keyboard.type(payload.key)
    return {"status": "success"}
