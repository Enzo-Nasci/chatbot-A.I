"""
Cliente Qdrant e função de embedding — centralizados aqui.

Dois consumidores:
  - memoryMongo.py → salva/busca resumos na collection "historico"
  - tools/faq.py   → busca chunks do PDF na collection "faq_chunks"

O modelo de embedding é o mesmo para ambos (gemini-embedding-2-preview, 768d),
então instanciamos uma vez só.
"""

from qdrant_client import QdrantClient, models
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from app.config import QDRANT_API, QDRANT_URL,  GEMINI_API_KEY

qdrant = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API)

COLLECTION_MEMORIA = "historico"
COLLECTION_FAQ     = "faq_chunks"
COLLECTION_PREFERENCIAS = "preferencias_user"
EMBEDDING_DIM      = 768

_embeddings = GoogleGenerativeAIEmbeddings(
    model="gemini-embedding-2-preview",
    google_api_key=GEMINI_API_KEY,
)


def garantir_collection(nome: str) -> None:
    """Cria a collection no Qdrant se ela ainda não existir (768d, Cosine)."""
    if qdrant.collection_exists(nome):
        return
    qdrant.create_collection(
        collection_name=nome,
        vectors_config=models.VectorParams(size=EMBEDDING_DIM, distance=models.Distance.COSINE),
    )


def gerar_embedding(texto: str) -> list[float]:
    """Gera um vetor de 768 dimensões para o texto informado."""
    return _embeddings.embed_query(texto, output_dimensionality=EMBEDDING_DIM)


def gerar_embeddings_batch(textos: list[str]) -> list[list[float]]:
    """Gera embeddings para uma lista de textos de uma vez (mais eficiente)."""
    return _embeddings.embed_documents(textos, output_dimensionality=EMBEDDING_DIM)