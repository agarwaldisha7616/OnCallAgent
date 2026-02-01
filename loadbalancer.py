
import os, asyncio
from typing import List






BACKENDS: List[str] = [
    for u in os.getenv("BACKENDS", "http://localhost:8001, http://localhost:8002").split()
]