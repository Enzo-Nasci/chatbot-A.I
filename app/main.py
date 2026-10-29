from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import validate_config, FRONTEND_DIR
from app.routes import chat, monitor

for _problem in validate_config():
    print(f"[config] ATTENTION: { _problem }") # CORREÇÃO: Erro de digitação

try:
    from app.juiz_escrita import garantir_esquema

    garantir_esquema()
except Exception as _esquema_exc:
    print(f"[juiz_escrita] tabela de idempotência não criada agora: {_esquema_exc}")

app = FastAPI(  
    title="Assessor IA",
    description="Assessor financeiro e de agenda com langchain e langgraph",
    version="0.1.0"
)

@app.get("/health")
def health_check() -> dict:
    """API health check endpoint. Returns a simple status message."""
    problem = validate_config()
    return {
            "status": "ok",
            "config_problems": problem,
        }

app.include_router(chat.router)
app.include_router(monitor.router)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

# frontend

if (FRONTEND_DIR / "index.html").exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
else:
    @app.get("/", tags=["infra"])
    def raiz() -> dict:
        return{
            "mensagem": "API do assessor no ar. o frontend ainda n foi criado"
        }

# Compatibilidade com clientes que usavam a grafia anterior.
app.add_api_route("/healt", health_check, methods=["GET"], include_in_schema=False)


