from dotenv import load_dotenv
import os
from pathlib import Path

BASE_DIR     = Path(__file__).resolve().parent.parent
DATA_DIR     = BASE_DIR / "data"
FRONTEND_DIR = BASE_DIR / "frontend"

FAQ_PDF_PATH = DATA_DIR / "FAQ_assessor_v1.1.pdf"

load_dotenv(BASE_DIR / ".env")          

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GROQ_API_KEY   = os.getenv("GROQ_API_KEY")
DATABASE_URL   = os.getenv("DATABASE_URL")
MONGODB_URI    = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
MONGODB_DB     = os.getenv("MONGODB_DB", "assessor") # CORREÇÃO: Variável adicionada
QDRANT_URL = os.getenv("QDRANT_URL")
QDRANT_API = os.getenv("QDRANT_API")
TYPESAFE_API_KEY = os.getenv("TYPESAFE_API_KEY")

GOOGLE_OAUTH_CREDENTIALS = os.getenv(
    "GOOGLE_OAUTH_CREDENTIALS",
    str(BASE_DIR / "gcp-oauth.keys.json"),
)
GOOGLE_CALENDAR_MCP_URL = os.getenv(
    "GOOGLE_CALENDAR_MCP_URL",
    "https://calendarmcp.googleapis.com/mcp/v1",
)
GOOGLE_CALENDAR_ACCESS_TOKEN = os.getenv("GOOGLE_CALENDAR_ACCESS_TOKEN")


OBRIGATORIES = {
    "GEMINI_API_KEY": GEMINI_API_KEY,
    "GROQ_API_KEY":   GROQ_API_KEY,
    "DATABASE_URL":   DATABASE_URL,
    "MONGODB_URI":    MONGODB_URI,
    "QDRANT_URL": QDRANT_URL,
    "QDRANT_API": QDRANT_API,
    "TYPESAFE_API_KEY": TYPESAFE_API_KEY,
}

def validate_config() -> list[str]:
    problems = []
    for nome, valor in OBRIGATORIES.items():
        if not valor:
            problems.append(f"Variável ausente no .env: {nome}")
    if not FAQ_PDF_PATH.exists():
        problems.append(f"PDF do FAQ não encontrado em: {FAQ_PDF_PATH}")
    return problems