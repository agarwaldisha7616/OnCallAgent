import os, asyncio, socket, contextlib
from dataclasses import dataclass
from typing import Dict, Optional, List
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import subprocess
import json
import uvicorn
import sys
import time
import httpx

SD_PATH = os.getenv("PM_SD_FILE", "inventory_targets.json")
HOST = os.getenv("PM_HOST", "127.0.0.1")
PORT = int(os.getenv("PM_PORT", "7070"))
BASE_PORT = int(os.getenv("PM_BASE_PORT", "8001"))
MAX_PORT = int(os.getenv("PM_MAX_PORT", "8010"))   # simple safety cap

UVICORN_ARGS = [ "-m", "uvicorn", "inventory_service:app", "--workers", "1", "--log-level", "warning"]

@dataclass
class Instance:
    port: int
    pid: int
    service: str
    started_at: float
    process: subprocess.Popen

instances: Dict[int, Instance] = {}  # key = port
lock = asyncio.Lock()

app = FastAPI(title="Inentory Process Manager", version="0.1.0")

#  ---------- utils ----------

def port_free(p: int) -> bool:
    # AF_INET: socket withh use IPv4 addresses.
    # SOCK_STREAM: socket will use TCP protocol.
    # socket is an endpoint for communication btw 2 machines over a network
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.settimeout(0.2)
        # 0 means its busy else non busy
        return s.connect_ex(("127.0.0.1", p)) != 0


def pick_next_port() -> int:
    # choose smallest free port >= BASE_PORT
    busy = set(instances.keys())
    for p in range(BASE_PORT, MAX_PORT + 1):
        if p not in busy and port_free(p):
            if port_free(p):
                return p
    raise RuntimeError("no free ports available in configured range")

def build_env(port:int) -> dict:
    env=os.environ.copy()
    idx = port - BASE_PORT + 1
    env["SERVICE"] = f"inv{idx}"


def spawn_instance(port: int) -> Instance:
    if not port_free(port):
        raise RuntimeError(f"port {port} is not free")
    cmd = [sys.executable, *UVICORN_ARGS, "--port", str(port)]
    # logs per-instance file
    os.makedirs("logs", exist_ok=True)
    # write log file in append + binary mode im
    logfile = open(f"logs/inv_{port}.log", "ab", buffering=0)
    # start new server
    proc = subprocess.Popen(cmd, stdout=logfile, stderr=subprocess.STDOUT, env=build_env(port))
    return Instance(port=port, pid=proc.pid, service=f"inv{port-BASE_PORT+1}", started_at=time.time(), process= proc)

async def wait_healthy(port: int, timeout_s: float = 10.0) -> bool:
    url = f"http://127.0.0.1:{port}/healthz"
    async with httpx.AsyncClient(timeout=2.0) as client:
        start = time.time()
        while time.time() - start < timeout_s:
            try:
                r = await client.get(url)
                if r.status_code == 200:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.2)
    return False

async def graceful_stop(inst: Instance, timeout_s: float = 5.0):
    try:
        inst.process.terminate()   # SIGTERM
    except Exception:
        pass
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if inst.process.poll() is not None:
            return
        await asyncio.sleep(0.1)
    with contextlib.suppress(Exception):
        inst.process.kill()  # SIGKILL if still alive

# ---------- models ----------
class StartReq(BaseModel):
    port: Optional[int] = Field(None, description="Port to start on, if omitted picks next free")
class StopReq(BaseModel):
    port: Optional[int] = None
    pid: Optional[int] = None
class ScaleReq(BaseModel):
    replicas: int = Field(..., ge=0, description="Target number of running instances")
class FaultReq(BaseModel):
    mode: str  # Type of fault to inject {latenct or error or cpu}
    latency_ms: Optional[int] = None
    p_error: Optional[float] = None # probability of error
    cpu_ms: Optional[int] = None
    ports: Optional[List[int]] = None # if None, apply to all


# ---------- API ----------
@app.get("/healthz")
def healthz():
    return {"ok": True, "instances": len(instances)}

@app.get("/instances")
def list_instances():
    out = []
    for inst in sorted(instances.values(), key=lambda x: x.port):
        status = "running" if inst.process.poll() is None else f"exited({inst.process.returncode})"
        out.append({"port": inst.port, "pid": inst.pid, "service": inst.service,
                    "started_at": inst.started_at, "status": status,
                    "url": f"http://127.0.0.1:{inst.port}"})
    return {"instances": out}

@app.get("/backends")
def backends():
    # LB can call this: returns list of backend base URLs
    b = [f"http://127.0.0.1:{inst.port}" for inst in sorted(instances.values(), key=lambda x: x.port)
         if inst.process.poll() is None]
    return {"backends": b}

@app.post("/start")
async def start(req: StartReq):
    async with lock:
        port = req.port or pick_next_port()
        if port in instances and instances[port].process.poll() is None:
            raise HTTPException(409, f"instance already running on port {port}")
        inst = spawn_instance(port)
        # Instance(port=8002, pid=32584, service='inv2', started_at=1769670661.3003798, process=<Popen: returncode: None args: ['C:\\Users\\AgrawalDisha\\Documents\\PP_AI\\...>)
        instances[port] = inst
    # Check if the service is running
    ok = await wait_healthy(port)
    write_sd_file()
    return {"ok": ok, "port": port, "url": f"http://127.0.0.1:{port}"}


@app.post("/stop")
async def stop(req: StopReq):
    async with lock:
        target: Optional[Instance] = None
        if req.port is not None:
            target = instances.get(req.port)
        elif req.pid is not None:
            for inst in instances.values():
                if inst.pid == req.pid:
                    target = inst
                    break
        if not target:
            raise HTTPException(404, "instance not found")
        await graceful_stop(target)
        instances.pop(target.port, None)
    write_sd_file()
    return {"ok": True, "stopped_port": target.port}

# Scale number of instances
@app.post("/scale")
async def scale(req: ScaleReq):
    if req.replicas > (MAX_PORT - BASE_PORT + 1):
        raise HTTPException(400, f"replicas exceed port range capacity ({BASE_PORT}-{MAX_PORT})")
    to_start, to_stop = [], []
    async with lock:
        running_ports = [p for p,i in sorted(instances.items()) if i.process.poll() is None]
        current = len(running_ports)
        if req.replicas > current:
            # start new ones
            need = req.replicas - current
            for _ in range(need):
                to_start.append(pick_next_port())
        elif req.replicas < current:
            # stop highest ports first
            extra = current - req.replicas
            to_stop = list(reversed(running_ports))[:extra]
        # execute changes outside lock for health waits
    started, stopped = [], []
    for p in to_start:
        async with lock:
            # Check port is still free inside the lock
            if not port_free(p):
                # Try to find another free port
                try:
                    p = pick_next_port()
                except RuntimeError:
                    continue # No free ports available
            inst = spawn_instance(p)
            instances[p] = inst
        await wait_healthy(p)
        started.append(p)

    for p in to_stop():
        async with lock:
            inst = instances.get(p)
        if inst:
            await graceful_stop(inst)
            async with lock:
                instances.pop(p, None)
            stopped.append(p)
    write_sd_file()
    return {"ok": True, "started": started, "stopped": stopped, "replicas": req.replicas}   

@app.post("/fault")
async def set_fault(req: FaultReq):
    payload = { k:v for k,v in req.model_dump().items() if k not in ("ports",) and v is not None}
    ports = req.ports or [p for p,i in instances.items() if i.process.poll() is not None]
    results = {}
    async with httpx.AsyncClient(timeout=3.0) as client:
        for p in ports:
            try:
                r = await client.post(f"http://127.0.0.1:{p}/faults", json=payload)
                results[str(p)] = {"status": r.status_code}
            except Exception as e:
                results[str(p)] = {"error": str(e)}
    return {"ok": True, "results": results}
            

def _sd_payload():
    # only running instances
    targets = [f"localhost:{p}" for p, inst in sorted(instances.items()) if inst.process.poll() is None]
    # Returns payload
    return [
        {
            "labels": {"job" : "inventory"},
            "targets": targets
        }
    ]

def write_sd_file():
    tmp = SD_PATH + ".tmp"  # Creates temporary file "inventory_targets.json.tmp"
    with open(tmp, "w") as f:
        json.dump(_sd_payload(),f)
    os.replace(tmp, SD_PATH) # Rename temporary file

if __name__ == "__main__":
    print(f"Process Manager starting on http://{HOST}:{PORT} (range {BASE_PORT}- {MAX_PORT})")
    write_sd_file()
    # Process manager is now running and ready to accept API request on http://127.0.0.1:7070
    uvicorn.run(app, host=HOST, port=PORT)

