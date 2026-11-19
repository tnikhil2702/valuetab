import os
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_GENERAL_CHAT_MODEL = os.getenv("OPENAI_GENERAL_CHAT_MODEL")
SECURE_CODE = os.getenv("GENERAL_CHAT_SECURITY_CODE")