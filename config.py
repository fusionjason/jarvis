import os
from pathlib import Path

from dotenv import load_dotenv

# Jarvis's own home — where its .env, general file/system tools default to, etc.
ROOT_DIR = Path(__file__).resolve().parent
load_dotenv(ROOT_DIR / ".env")

# The Trading folder Jarvis was originally built alongside. Kept as a separate,
# named location so trading tools work regardless of where Jarvis itself lives.
# Override with a TRADING_DIR entry in .env if the Trading folder ever moves.
TRADING_DIR = Path(os.environ.get("TRADING_DIR", r"C:\Users\fusio\OneDrive\Documents\Trading")).resolve()

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY")

# Named Gmail accounts — each needs an app password (Google Account > Security > 2-Step
# Verification > App Passwords), not the regular account password. Add more by following the
# same ADDRESS/APP_PASSWORD env var pattern and adding an entry below.
EMAIL_ACCOUNTS = {
    "personal": {
        "address": os.environ.get("EMAIL_PERSONAL_ADDRESS"),
        "app_password": os.environ.get("EMAIL_PERSONAL_APP_PASSWORD"),
    },
    "business": {
        "address": os.environ.get("EMAIL_BUSINESS_ADDRESS"),
        "app_password": os.environ.get("EMAIL_BUSINESS_APP_PASSWORD"),
    },
}
MODEL = "claude-haiku-4-5"  # cheapest current Claude model (~$1/$5 per MTok); swap to claude-opus-5 for harder reasoning
ASSISTANT_NAME = "Jarvis"
