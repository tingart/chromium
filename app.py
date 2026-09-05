from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse, JSONResponse
from pydantic import BaseModel
from playwright.async_api import async_playwright
from typing import Optional, List
from collections import deque
import asyncio, urllib.parse, urllib.error, os, time, json, tarfile, tempfile, shutil, urllib.request, threading, secrets, hashlib, hmac, base64

app = FastAPI(title="Remote Chromium Browser")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

DATA_DIR = os.getenv("BROWSER_DATA_DIR", "/app/user_data")
MAX_TABS = int(os.getenv("MAX_TABS", "8"))
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SECRET_KEY = os.getenv("SUPABASE_SECRET_KEY", "")
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET", "browser-backup")
SUPABASE_BACKUP_PATH = os.getenv("SUPABASE_BACKUP_PATH", "chromium-profile.tar.gz")
AUTO_BACKUP_MINUTES = max(0, int(os.getenv("AUTO_BACKUP_MINUTES", "15")))
AUTH_USERNAME = os.getenv("AUTH_USERNAME", "admin")
AUTH_PASSWORD = os.getenv("AUTH_PASSWORD", "")
AUTH_SECRET = os.getenv("AUTH_SECRET", "")
SESSION_DAYS = max(1, int(os.getenv("SESSION_DAYS", "7")))

stream_settings = {"format":"png","quality":70,"interval":180,"width":854,"height":480,"max_pixels":854*480,"chromium_zoom":1.0}
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
cpu_prev = cpu_prev_time = None
backup_lock = threading.Lock()
backup_status = {"configured":bool(SUPABASE_URL and SUPABASE_SECRET_KEY),"busy":False,"last_backup":None,"last_restore":None,"last_error":None,"size_mb":None}

class LoginPayload(BaseModel):
    username: str; password: str

# ---------- Login/session protection ----------
def auth_configured():
    return bool(AUTH_USERNAME and AUTH_PASSWORD and AUTH_SECRET)

def make_session():
    ts = str(int(time.time()))
    nonce = secrets.token_urlsafe(24)
    raw = ts + "." + nonce
    sig = hmac.new(AUTH_SECRET.encode(), raw.encode(), hashlib.sha256).digest()
    return raw + "." + base64.urlsafe_b64encode(sig).decode().rstrip("=")

def valid_session(token):
    if not token or not auth_configured(): return False
    try:
        ts, nonce, sig = token.split(".", 2)
        if abs(int(time.time()) - int(ts)) > SESSION_DAYS * 86400: return False
        raw = ts + "." + nonce
        expected = base64.urlsafe_b64encode(hmac.new(AUTH_SECRET.encode(), raw.encode(), hashlib.sha256).digest()).decode().rstrip("=")
        return hmac.compare_digest(sig, expected)
    except Exception:
        return False

@app.middleware("http")
async def authentication_middleware(request: Request, call_next):
    path = request.url.path
    public = path in {"/", "/login", "/logout", "/auth/status", "/favicon.ico"}
    if not public and not valid_session(request.cookies.get("remote_session")):
        return JSONResponse({"status":"error","message":"Authentication required"}, status_code=401)
    return await call_next(request)

@app.post("/login")
async def login(payload: LoginPayload):
    if not auth_configured():
        return JSONResponse({"status":"error","message":"Authentication is not configured. Set AUTH_USERNAME, AUTH_PASSWORD and AUTH_SECRET in Render."}, status_code=500)
    if not (hmac.compare_digest(payload.username, AUTH_USERNAME) and hmac.compare_digest(payload.password, AUTH_PASSWORD)):
        return JSONResponse({"status":"error","message":"Invalid username or password"}, status_code=401)
    response = JSONResponse({"status":"success"})
    response.set_cookie("remote_session", make_session(), max_age=SESSION_DAYS*86400, httponly=True, secure=True, samesite="lax", path="/")
    return response

@app.post("/logout")
async def logout():
    response = JSONResponse({"status":"success"})
    response.delete_cookie("remote_session", path="/")
    return response

@app.get("/auth/status")
async def auth_status(request: Request):
    return {"authenticated": valid_session(request.cookies.get("remote_session")), "configured": auth_configured()}

class NavigatePayload(BaseModel): url: str
class TouchPayload(BaseModel):
    x: float; y: float; action: Optional[str] = "click"; delta_x: float = 0; delta_y: float = 0
class KeyPayload(BaseModel): key: str
class TypePayload(BaseModel): text: str
class SettingsPayload(BaseModel):
    format: Optional[str]=None; quality: Optional[int]=None; interval: Optional[int]=None
    width: Optional[int]=None; height: Optional[int]=None; max_pixels: Optional[int]=None; chromium_zoom: Optional[float]=None

def mark_screen_dirty(action="unknown"):
    global last_action
    last_action = action
    screen_event.set()

async def get_active_page():
    global pages, active_tab_index
    pages = [p for p in pages if not p.is_closed()]
    if not pages: return None
    active_tab_index = min(active_tab_index, len(pages)-1)
    return pages[active_tab_index]

def sanitize_url(raw_input):
    url=(raw_input or "").strip()
    if not url: return "https://www.google.com"
    if url.startswith(("http://","https://","about:")): return url
    if "." in url and " " not in url: return "https://"+url
    return "https://www.google.com/search?q="+urllib.parse.quote(url)

def clamp_int(v,a,b): return max(a,min(b,int(v)))
def current_viewport(): return {"width":stream_settings["width"],"height":stream_settings["height"]}

async def apply_chromium_zoom(page):
    try:
        cdp = await context.new_cdp_session(page)
        await cdp.send("Emulation.setPageScaleFactor", {"pageScaleFactor": float(stream_settings.get("chromium_zoom",1.0))})
        await cdp.detach()
    except Exception:
        pass

async def apply_zoom_all():
    for pg in list(pages):
        try:
            if not pg.is_closed(): await apply_chromium_zoom(pg)
        except Exception: pass
def cgroup_value(path):
    try:
        with open(path,encoding="utf-8") as f:return f.read().strip()
    except:return None

def get_memory_stats():
    cur=cgroup_value("/sys/fs/cgroup/memory.current"); mx=cgroup_value("/sys/fs/cgroup/memory.max")
    if cur is not None:
        try:
            cb=int(cur); mb=int(mx) if mx and mx!="max" else 0
            return {"used_mb":round(cb/1048576,1),"limit_mb":round(mb/1048576,1) if mb else None}
        except: pass
    try:
        info={}
        with open("/proc/meminfo",encoding="utf-8") as f:
            for line in f:
                p=line.split();
                if len(p)>=2: info[p[0].rstrip(":")]=int(p[1])*1024
        total=info.get("MemTotal",0); avail=info.get("MemAvailable",0)
        return {"used_mb":round(max(0,total-avail)/1048576,1),"limit_mb":round(total/1048576,1) if total else None}
    except:return {"used_mb":None,"limit_mb":None}

def get_cgroup_cpu_percent():
    global cpu_prev,cpu_prev_time
    stat=cgroup_value("/sys/fs/cgroup/cpu.stat")
    if not stat:return None
    usage=None
    for line in stat.splitlines():
        p=line.split()
        if len(p)==2 and p[0]=="usage_usec": usage=int(p[1]);break
    if usage is None:return None
    now=time.monotonic()
    if cpu_prev is None: cpu_prev,cpu_prev_time=usage,now;return 0.0
    elapsed=now-cpu_prev_time; delta=(usage-cpu_prev)/1e6;cpu_prev,cpu_prev_time=usage,now
    return round(max(0,min(999,(delta/elapsed)*100)),1) if elapsed>0 else 0.0

def get_process_memory():
    try:
        with open("/proc/self/statm") as f:p=int(f.read().split()[1])
        return round(p*os.sysconf("SC_PAGE_SIZE")/1048576,1)
    except:return None

async def browser_memory_estimate():
    total=0.0
    try:
        for n in os.listdir("/proc"):
            if not n.isdigit():continue
            try:
                with open(f"/proc/{n}/cmdline","rb") as f: cmd=f.read().decode("utf-8","ignore").lower()
                if "chrom" not in cmd:continue
                with open(f"/proc/{n}/statm") as f:r=int(f.read().split()[1])
                total+=r*os.sysconf("SC_PAGE_SIZE")/1048576
            except:pass
    except:pass
    return round(total,1)

async def build_stats():
    now=time.monotonic(); recent=[t for t in frame_times if now-t<=2]
    fps=round((len(recent)-1)/max(.001,recent[-1]-recent[0]),1) if len(recent)>=2 else 0.0
    mem=get_memory_stats()
    return {"server_fps":fps,"frame_bytes":last_frame_bytes,"frame_kb":round(last_frame_bytes/1024,1),"encode_ms":round(last_encode_ms,1),"ram_mb":mem["used_mb"],"ram_limit_mb":mem["limit_mb"],"python_ram_mb":get_process_memory(),"chromium_ram_mb":await browser_memory_estimate(),"cpu_percent":get_cgroup_cpu_percent(),"tabs":len(pages),"active_tab":active_tab_index,"viewport":current_viewport(),"format":stream_settings["format"],"quality":stream_settings["quality"],"interval":stream_settings["interval"],"last_action":last_action,"storage_path":DATA_DIR}

# ---------- Supabase Storage via REST API (no new Python package required) ----------
def supabase_headers():
    if not SUPABASE_URL or not SUPABASE_SECRET_KEY: raise RuntimeError("Supabase is not configured")
    return {"Authorization":"Bearer "+SUPABASE_SECRET_KEY,"apikey":SUPABASE_SECRET_KEY}

def supabase_upload(path):
    with open(path,"rb") as f:data=f.read()
    url=f"{SUPABASE_URL}/storage/v1/object/{urllib.parse.quote(SUPABASE_BUCKET,safe='')}/{urllib.parse.quote(SUPABASE_BACKUP_PATH,safe='/')}"
    req=urllib.request.Request(url,data=data,method="POST",headers={**supabase_headers(),"Content-Type":"application/gzip","x-upsert":"true"})
    try:
        with urllib.request.urlopen(req,timeout=120) as r:r.read()
    except urllib.error.HTTPError as e:
        body=e.read().decode("utf-8","ignore")
        if e.code not in (409,): raise RuntimeError(f"Supabase upload HTTP {e.code}: {body[:300]}")
        # Some gateways ignore x-upsert; use PUT for replacement.
        req=urllib.request.Request(url,data=data,method="PUT",headers={**supabase_headers(),"Content-Type":"application/gzip"})
        with urllib.request.urlopen(req,timeout=120) as r:r.read()
    return len(data)

def supabase_download(path):
    url=f"{SUPABASE_URL}/storage/v1/object/{urllib.parse.quote(SUPABASE_BUCKET,safe='')}/{urllib.parse.quote(SUPABASE_BACKUP_PATH,safe='/')}"
    req=urllib.request.Request(url,method="GET",headers=supabase_headers())
    try:
        with urllib.request.urlopen(req,timeout=120) as r:data=r.read()
    except urllib.error.HTTPError as e: raise RuntimeError(f"Supabase download HTTP {e.code}: {e.read().decode('utf-8','ignore')[:300]}")
    with open(path,"wb") as f:f.write(data)
    return len(data)

def make_archive():
    os.makedirs(DATA_DIR,exist_ok=True)
    fd,path=tempfile.mkstemp(suffix=".tar.gz");os.close(fd)
    skip_dirs={"Cache","Code Cache","GPUCache","ShaderCache","GrShaderCache"}
    skip_files={"SingletonCookie","SingletonLock","SingletonSocket","DevToolsActivePort"}
    with tarfile.open(path,"w:gz",compresslevel=6) as tar:
        base=os.path.abspath(DATA_DIR)
        for root,dirs,files in os.walk(base):
            dirs[:]=[d for d in dirs if d not in skip_dirs]
            for name in files:
                if name in skip_files:continue
                full=os.path.join(root,name)
                try:tar.add(full,arcname=os.path.relpath(full,base),recursive=False)
                except:pass
    return path

def backup_worker():
    if not SUPABASE_URL or not SUPABASE_SECRET_KEY:return
    if not backup_lock.acquire(blocking=False):return
    backup_status["busy"]=True;backup_status["last_error"]=None;temp=None
    try:
        temp=make_archive();size=os.path.getsize(temp)
        if size>50*1024*1024:raise RuntimeError(f"Backup is {size/1048576:.1f} MB; Supabase Free individual-file limit is 50 MB.")
        n=supabase_upload(temp);backup_status["last_backup"]=time.strftime("%Y-%m-%d %H:%M:%S UTC",time.gmtime());backup_status["size_mb"]=round(n/1048576,2)
    except Exception as e:backup_status["last_error"]=str(e)
    finally:
        if temp:
            try:os.remove(temp)
            except:pass
        backup_status["busy"]=False;backup_lock.release()

async def start_browser_context():
    global context,pages,active_tab_index
    context=await playwright_instance.chromium.launch_persistent_context(user_data_dir=DATA_DIR,headless=True,viewport=current_viewport(),device_scale_factor=1,is_mobile=False,has_touch=False,user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",args=["--no-sandbox","--disable-setuid-sandbox","--disable-dev-shm-usage","--disable-blink-features=AutomationControlled","--disable-background-networking","--disable-background-timer-throttling","--disable-renderer-backgrounding","--disable-features=Translate,MediaRouter","--no-first-run","--no-default-browser-check"])
    await context.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
    pages=context.pages
    if not pages:
        p=await context.new_page()
        try:await p.goto("https://www.google.com",wait_until="commit",timeout=15000)
        except:pass
        pages.append(p)
    active_tab_index=0
    await apply_zoom_all()
    mark_screen_dirty("browser-started")

async def restore_profile():
    global context,pages,active_tab_index
    if context:
        try:await context.close()
        except:pass
        context=None
    pages=[];active_tab_index=0
    fd,archive=tempfile.mkstemp(suffix=".tar.gz");os.close(fd);extract=DATA_DIR+".restore";old=DATA_DIR+".old"
    try:
        await asyncio.get_running_loop().run_in_executor(None,supabase_download,archive)
        if os.path.exists(extract):shutil.rmtree(extract,ignore_errors=True)
        os.makedirs(extract,exist_ok=True)
        with tarfile.open(archive,"r:gz") as tar:tar.extractall(extract)
        if os.path.exists(old):shutil.rmtree(old,ignore_errors=True)
        if os.path.exists(DATA_DIR):os.replace(DATA_DIR,old)
        os.replace(extract,DATA_DIR);shutil.rmtree(old,ignore_errors=True)
        await start_browser_context();backup_status["last_restore"]=time.strftime("%Y-%m-%d %H:%M:%S UTC",time.gmtime())
    finally:
        try:os.remove(archive)
        except:pass
        if os.path.exists(extract):shutil.rmtree(extract,ignore_errors=True)

@app.on_event("startup")
async def startup_event():
    global playwright_instance
    os.makedirs(DATA_DIR,exist_ok=True)
    playwright_instance=await async_playwright().start()
    await start_browser_context()
    if AUTO_BACKUP_MINUTES>0:
        asyncio.create_task(auto_backup_loop())

@app.on_event("shutdown")
async def shutdown_event():
    global playwright_instance,context
    try:
        if context:await context.close()
    finally:
        if playwright_instance:await playwright_instance.stop()

async def auto_backup_loop():
    await asyncio.sleep(120)
    while True:
        try:
            if not backup_status["busy"] and backup_status["configured"]:await asyncio.get_running_loop().run_in_executor(None,backup_worker)
        except:pass
        await asyncio.sleep(max(60,AUTO_BACKUP_MINUTES*60))

@app.get("/",response_class=HTMLResponse)
async def root():
    p=os.path.join(os.path.dirname(os.path.abspath(__file__)),"index.html")
    return FileResponse(p) if os.path.exists(p) else HTMLResponse("<h2>Error: index.html not found</h2>",404)
@app.get("/health")
async def health():return {"status":"online","browser":"chromium","tabs":len(pages),"persistent_profile":True,"supabase_configured":backup_status["configured"]}
@app.get("/settings")
async def settings():return {"settings":stream_settings,"viewport":current_viewport()}
@app.post("/settings")
async def update_settings(p:SettingsPayload):
    if p.format in {"png","jpeg","webp"}:stream_settings["format"]=p.format
    if p.quality is not None:stream_settings["quality"]=clamp_int(p.quality,10,100)
    if p.interval is not None:stream_settings["interval"]=clamp_int(p.interval,30,2000)
    if p.width is not None:stream_settings["width"]=clamp_int(p.width,320,1280)
    if p.height is not None:stream_settings["height"]=clamp_int(p.height,240,900)
    if p.max_pixels is not None:stream_settings["max_pixels"]=clamp_int(p.max_pixels,100000,2000000)
    if p.chromium_zoom is not None:stream_settings["chromium_zoom"]=max(0.25,min(2.0,float(p.chromium_zoom)))
    try:
        if context:
            await context.set_viewport_size(current_viewport())
            await apply_zoom_all()
    except:pass
    mark_screen_dirty("settings");return {"status":"success","settings":stream_settings,"viewport":current_viewport()}

@app.post("/device")
async def device_size(p: SettingsPayload):
    if p.width is not None: stream_settings["width"] = clamp_int(p.width, 280, 1280)
    if p.height is not None: stream_settings["height"] = clamp_int(p.height, 480, 1600)
    try:
        if context:
            await context.set_viewport_size(current_viewport())
            await apply_zoom_all()
    except Exception: pass
    mark_screen_dirty("device-resize")
    return {"status":"success","viewport":current_viewport()}

async def capture_frame(page):
    global last_frame,last_encode_ms,last_frame_bytes
    start=time.perf_counter();fmt=stream_settings["format"];q=stream_settings["quality"]
    try:
        if fmt=="jpeg":data=await page.screenshot(type="jpeg",quality=q,animations="allow",scale="css")
        elif fmt=="webp":
            try:data=await page.screenshot(type="webp",quality=q,animations="allow",scale="css")
            except:data=await page.screenshot(type="png",animations="allow",scale="css")
        else:data=await page.screenshot(type="png",animations="allow",scale="css")
        last_encode_ms=(time.perf_counter()-start)*1000;last_frame_bytes=len(data);last_frame=data;frame_times.append(time.monotonic());return data
    except:return None

async def frame_generator():
    global last_frame
    while True:
        page=await get_active_page()
        if page:
            data=await capture_frame(page)
            if not data:data=last_frame
            if data:
                c={"png":"image/png","jpeg":"image/jpeg","webp":"image/webp"}.get(stream_settings["format"],"image/png")
                yield b"--frame\r\n"+f"Content-Type: {c}\r\nContent-Length: {len(data)}\r\n\r\n".encode()+data+b"\r\n"
        try:await asyncio.wait_for(screen_event.wait(),timeout=max(.03,stream_settings["interval"]/1000));screen_event.clear()
        except asyncio.TimeoutError:pass

@app.get("/screen")
async def screen():return StreamingResponse(frame_generator(),media_type="multipart/x-mixed-replace; boundary=frame",headers={"Cache-Control":"no-cache, no-store, must-revalidate","Pragma":"no-cache","Connection":"keep-alive"})

@app.post("/navigate")
async def navigate(p:NavigatePayload):
    page=await get_active_page()
    if not page:return {"status":"error","message":"No active page"}
    try:
        async with page_lock:await page.goto(sanitize_url(p.url),wait_until="commit",timeout=20000)
        mark_screen_dirty("navigate");return {"status":"success","url":page.url}
    except Exception as e:return {"status":"error","message":str(e),"url":page.url}
@app.post("/history/back")
async def back():
    page=await get_active_page()
    try:
        async with page_lock:await page.go_back(wait_until="commit",timeout=10000)
        mark_screen_dirty("back");return {"status":"success","url":page.url}
    except Exception as e:return {"status":"error","message":str(e) if e else "No history","url":page.url if page else ""}
@app.post("/history/forward")
async def forward():
    page=await get_active_page()
    try:
        async with page_lock:await page.go_forward(wait_until="commit",timeout=10000)
        mark_screen_dirty("forward");return {"status":"success","url":page.url}
    except Exception as e:return {"status":"error","message":str(e) if e else "No history","url":page.url if page else ""}
@app.post("/reload")
async def reload_page():
    page=await get_active_page()
    try:
        async with page_lock:await page.reload(wait_until="commit",timeout=15000)
        mark_screen_dirty("reload");return {"status":"success","url":page.url}
    except Exception as e:return {"status":"error","message":str(e),"url":page.url if page else ""}
@app.post("/stop")
async def stop():
    page=await get_active_page()
    try:await page.evaluate("window.stop()");mark_screen_dirty("stop");return {"status":"success"}
    except Exception as e:return {"status":"error","message":str(e)}
@app.post("/home")
async def home():return await navigate(NavigatePayload(url="https://www.google.com"))

@app.get("/tabs")
async def tabs_route():
    data=[]
    for i,p in enumerate(pages):
        try:
            if p.is_closed():continue
            data.append({"id":i,"title":await p.title() or "New Tab","url":p.url})
        except:data.append({"id":i,"title":"New Tab","url":"about:blank"})
    return {"tabs":data,"active_index":active_tab_index}
@app.post("/tabs/new")
async def new_tab():
    global active_tab_index
    if len(pages)>=MAX_TABS:return {"status":"error","message":f"Maximum {MAX_TABS} tabs allowed"}
    try:
        p=await context.new_page();pages.append(p);active_tab_index=len(pages)-1;await apply_chromium_zoom(p);asyncio.create_task(background_home(p));mark_screen_dirty("new-tab");return await tabs_route()
    except Exception as e:return {"status":"error","message":str(e)}
async def background_home(p):
    try:await p.goto("https://www.google.com",wait_until="commit",timeout=15000)
    except:pass
    mark_screen_dirty("new-tab-loaded")
@app.post("/tabs/switch")
async def switch_tab(payload:dict):
    global active_tab_index
    i=int(payload.get("index",0))
    if 0<=i<len(pages):
        active_tab_index=i
        try:await pages[i].bring_to_front()
        except:pass
        mark_screen_dirty("switch-tab")
    return await tabs_route()
@app.post("/tabs/close")
async def close_tab(payload:dict):
    global active_tab_index,pages
    i=int(payload.get("index",0))
    if len(pages)>1 and 0<=i<len(pages):
        try:await pages[i].close()
        except:pass
        pages.pop(i);active_tab_index=min(active_tab_index,len(pages)-1);mark_screen_dirty("close-tab")
    return await tabs_route()

@app.post("/touch")
async def touch(p:TouchPayload):
    page=await get_active_page()
    if not page:return {"status":"error"}
    try:
        x=max(0,min(float(p.x),stream_settings["width"]-1));y=max(0,min(float(p.y),stream_settings["height"]-1))
        async with page_lock:
            if p.action=="click":await page.mouse.click(x,y,delay=30)
            elif p.action=="right_click":await page.mouse.click(x,y,button="right",delay=30)
            elif p.action=="double_click":await page.mouse.dblclick(x,y,delay=30)
            elif p.action=="scroll":await page.mouse.wheel(max(-1200,min(1200,p.delta_x)),max(-1600,min(1600,p.delta_y)))
            elif p.action=="move":await page.mouse.move(x,y)
        mark_screen_dirty(p.action or "touch");return {"status":"success"}
    except Exception as e:return {"status":"error","message":str(e)}
@app.post("/type")
async def type_text(p:TypePayload):
    page=await get_active_page()
    try:await page.keyboard.insert_text(p.text);mark_screen_dirty("type");return {"status":"success","length":len(p.text)}
    except Exception as e:return {"status":"error","message":str(e)}
@app.post("/key")
async def key(p:KeyPayload):
    page=await get_active_page()
    try:await page.keyboard.press(p.key);mark_screen_dirty("key");return {"status":"success"}
    except Exception as e:return {"status":"error","message":str(e)}

async def selected_text(page):
    return await page.evaluate("""()=>{const e=document.activeElement;if(e&&typeof e.selectionStart==='number'&&typeof e.selectionEnd==='number')return String(e.value||'').substring(e.selectionStart,e.selectionEnd);const s=window.getSelection();return s?s.toString():''}""")
@app.get("/selection")
async def selection():
    p=await get_active_page()
    try:return {"status":"success","text":await selected_text(p)}
    except Exception as e:return {"status":"error","text":"","message":str(e)}
@app.post("/cut")
async def cut():
    p=await get_active_page()
    try:
        t=await selected_text(p)
        async with page_lock:await p.keyboard.press("Control+X")
        mark_screen_dirty("cut");return {"status":"success","text":t}
    except Exception as e:return {"status":"error","text":"","message":str(e)}

@app.get("/storage")
async def storage():
    size=files=0
    for root,dirs,names in os.walk(DATA_DIR):
        for n in names:
            try:size+=os.path.getsize(os.path.join(root,n));files+=1
            except:pass
    return {"path":DATA_DIR,"exists":os.path.exists(DATA_DIR),"files":files,"size_mb":round(size/1048576,2),"persistent_profile":True}
@app.get("/backup/status")
async def backup_status_route():return backup_status
@app.post("/backup")
async def backup():
    if not backup_status["configured"]:return {"status":"error","message":"Supabase is not configured"}
    if backup_status["busy"]:return {"status":"busy","message":"Backup/restore already running"}
    asyncio.get_running_loop().run_in_executor(None,backup_worker);return {"status":"started"}
@app.post("/restore")
async def restore():
    if not backup_status["configured"]:return {"status":"error","message":"Supabase is not configured"}
    if backup_status["busy"]:return {"status":"busy","message":"Backup/restore already running"}
    backup_status["busy"]=True;backup_status["last_error"]=None
    try:
        await restore_profile();return {"status":"success","message":"Profile restored from Supabase"}
    except Exception as e:backup_status["last_error"]=str(e);return {"status":"error","message":str(e)}
    finally:backup_status["busy"]=False

@app.get("/stats")
async def stats():
    return await build_stats()
