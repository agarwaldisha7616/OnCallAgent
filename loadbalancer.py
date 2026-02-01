
import os, asyncio
from typing import List
from fastapi import FastAPI, Request
import httpx

BACKENDS: List[str] = [
    u.strip() for u in os.getenv("BACKENDS", "http://localhost:8001,http://localhost:8002").split(",")
    if u.strip()
]

if not BACKENDS:
    raise RuntimeError("Set BACKENDS env var, e.g. BACKENDS='http://localhost:8001,http://localhost:8002'")

# timeouts (works with your httpx version)
CONNECT_TIMEOUT = float(os.getenv("LB_CONNECT_TIMEOUT", "3.0"))  # max time to establish backend connection
READ_TIMEOUT    = float(os.getenv("LB_READ_TIMEOUT", "5.0"))     # max time to wait for backend response
WRITE_TIMEOUT   = float(os.getenv("LB_WRITE_TIMEOUT", "5.0"))    # max time to send data to backend
POOL_TIMEOUT    = float(os.getenv("LB_POOL_TIMEOUT", "3.0"))     # max time to wait for connection from connection pool

app = FastAPI(title="RR Load Balancer", version="0.2.0")

MANAGER_URL = os.getenv("PM_URL", "http://127.0.0.1:7070")
REFRESH_EVERY_SEC = int(os.getenv("LB_REFRESH_SEC", "5"))
# round-robin cursor + lock for concurrent requests
__rr_idx = 0    # track current backend in the rr sequence
__rr_lock = asyncio.Lock()  

async def pick_primary_and_alt() -> tuple[str,str | None]:
    """Advance RR exactly once, compute alt without advancing."""
    global __rr_idx
    # Use dynamic backends from process manager, fallback to static BACKENDS
    backends = getattr(app.state, 'backends', BACKENDS)
    n = len(backends)
    async with __rr_lock:
        primary = backends[__rr_idx % n]
        # advance the cursor ONCE for next request
        __rr_idx = (__rr_idx + 1) % n
        alt = backends[__rr_idx % n] if n > 1 else None
    return primary, alt


@app.on_event("startup")
async def _startup():
    app.state.client = httpx.AsyncClient(
        timeout = httpx.Timeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT, write=WRITE_TIMEOUT, pool=POOL_TIMEOUT),
        follow_redirects=False,
    )
    app.state.backends = BACKENDS[:]
    async def refresh():
        while True:
            try:
                r = await app.state.client.get(f"{MANAGER_URL}/backends")
                if r.status_code == 200:
                    app.state.backends = r.json().get("backends", app.state.backends) or app.state.backends
            except Exception:
                pass
            await asyncio.sleep(REFRESH_EVERY_SEC)
    asyncio.create_task(refresh())

@app.on_event("shutdown")
async def _shutdown():
    await app.state.client.aclose()

@app.get("/healthz")
async def healthz():
    # show dynamic backends from process manager
    backends = getattr(app.state, 'backends', BACKENDS)
    return {"status": "ok", "backends": backends}

@app.api_route("{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def proxy(path: str, request: Request):
    """
    - Round-robin pick a backend (advances once per request).
    - 
    """
    # Log incoming request
    method = request.method
    query = f"?{request.url.query}" if request.url.query else ""
    print(f"LB_REQUEST: {method} /{path}{query}")
    
