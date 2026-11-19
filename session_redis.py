# session_redis.py
import os
import json
from typing import Optional, Dict, Any
import redis.asyncio as redis
from dotenv import load_dotenv
load_dotenv()

# local redis connection parameters (for development)
# REDIS_HOST     = os.getenv("REDIS_HOST", "localhost")
# REDIS_PORT     = int(os.getenv("REDIS_PORT", "6379"))
# REDIS_DB       = int(os.getenv("REDIS_DB", "0"))
# REDIS_PASSWORD = os.getenv("REDIS_PASSWORD")

# redis_client = redis.Redis(
#     host=REDIS_HOST,
#     port=REDIS_PORT,
#     db=REDIS_DB,
#     password=REDIS_PASSWORD,
#     decode_responses=True,
# )

# server redis connection string format: redis://[:password]@host:port/db
redis_client = redis.from_url(
    os.getenv("REDIS_URL"),
    decode_responses=True
)

async def check_redis_connection():
    try:
        response = await redis_client.ping()
        print(f"[REDIS] Connected: {response}")
    except Exception as e:
        print(f"[REDIS] Connection FAILED: {e}")

async def get_session(file_id: str) -> Optional[Dict[str, Any]]:
    raw = await redis_client.get(file_id)
    return json.loads(raw) if raw else None

async def set_session(file_id: str, data: Dict[str, Any]):
    await redis_client.setex(file_id, 86400, json.dumps(data, default=str))

async def delete_session(file_id: str):
    await redis_client.delete(file_id)