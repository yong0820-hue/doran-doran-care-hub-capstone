import os
from dotenv import load_dotenv

load_dotenv()

FIREBASE_DATABASE_URL = os.getenv("FIREBASE_DATABASE_URL", "").rstrip("/")
ELDER_ID = os.getenv("ELDER_ID", "")
DEVICE_ID = os.getenv("DEVICE_ID", "")
LOCATION_NAME = os.getenv("LOCATION_NAME", "\uac70\uc2e4")

if not FIREBASE_DATABASE_URL:
    raise EnvironmentError(
        "FIREBASE_DATABASE_URL is required. "
        "Copy .env.example to .env and fill in your Firebase project URL."
    )

for _key, _val in {"ELDER_ID": ELDER_ID, "DEVICE_ID": DEVICE_ID}.items():
    if not _val:
        raise EnvironmentError(f"{_key} is required but not set in .env")
