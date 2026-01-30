import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
BASE_URL = (os.getenv("BASE_URL") or "").strip()
WEBHOOK_PATH = (os.getenv("WEBHOOK_PATH") or "/telegram/webhook").strip()

INBOUND_SECRET = (os.getenv("INBOUND_SECRET") or "").strip()
DOMAIN = (os.getenv("DOMAIN") or "").strip()

# Turso
TURSO_DATABASE_URL = (os.getenv("TURSO_DATABASE_URL") or "").strip()
TURSO_AUTH_TOKEN = (os.getenv("TURSO_AUTH_TOKEN") or "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing")
if not BASE_URL:
    raise RuntimeError("BASE_URL missing")
if not INBOUND_SECRET:
    raise RuntimeError("INBOUND_SECRET missing")
if not DOMAIN:
    raise RuntimeError("DOMAIN missing")
if not TURSO_DATABASE_URL:
    raise RuntimeError("TURSO_DATABASE_URL missing")
if not TURSO_AUTH_TOKEN:
    raise RuntimeError("TURSO_AUTH_TOKEN missing")

if not WEBHOOK_PATH.startswith("/"):
    raise RuntimeError("WEBHOOK_PATH must start with '/'")