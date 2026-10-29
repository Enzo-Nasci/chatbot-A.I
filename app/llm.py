from langchain_groq import ChatGroq
from langchain_google_genai import ChatGoogleGenerativeAI
from app.config import GEMINI_API_KEY, GROQ_API_KEY

# =============================================================================
# LLMs
# =============================================================================

# Guardrail: Avaliações críticas de segurança usando o modelo mais atualizado da Alibaba
llmGuard = ChatGroq(
    model="openai/gpt-oss-20b", 
    temperature=0, 
    api_key=GROQ_API_KEY
)

llm_gemini = ChatGoogleGenerativeAI(
    model="gemini-2.5-flash",
    temperature=0.7,
    top_p=0.95,
    google_api_key=GEMINI_API_KEY
)

# LLM Principal: Fallback do Gemini
llm_groq = ChatGroq(
    model="openai/gpt-oss-20b",
    temperature=0.7,
    top_p=0.95,
    api_key=GROQ_API_KEY,
)

llm = llm_gemini.with_fallbacks([llm_groq])

llmRapido = ChatGroq(
    model="openai/gpt-oss-20b",
    temperature=0,
    api_key=GROQ_API_KEY,
)

# Resumidor
_llm_resumo = ChatGroq(
    model="openai/gpt-oss-20b",
    temperature=0,
    api_key=GROQ_API_KEY
)