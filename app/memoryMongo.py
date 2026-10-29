"""
=================
Modelagem
---------
Um documento por sessão (sessão = uma conversa completa).
O _id é um UUID interno.
session_id identifica a conversa; user_id identifica o usuário entre sessões.

Documento
---------
{
  "_id":           "uuid-gerado-internamente",
  "session_id":    "id_da_conversa",
  "user_id":       "id_do_usuario",
  "iniciada_em":   datetime,
  "atualizada_em": datetime,
  "resumo":        "Usuário registrou Pix de R$50...",
  "mensagens":     [
    {"role": "human",     "content": "oi"},
    {"role": "assistant", "content": "Olá!"}
  ]
}

Funções
----------------
  iniciar_sessao(session_id, user_id)        → cria documento no MongoDB
  salvar_mensagem(session_id, role, content) → adiciona mensagem na sessão ativa
  encerrar_sessao(session_id)                → gera resumo, salva no Mongo e indexa no Qdrant
"""

import uuid
from datetime import datetime, timezone
from pymongo import MongoClient
from app.llm import _llm_resumo
from app.config import MONGODB_URI, MONGODB_DB
from qdrant_client import models
from app.vectordb import (
    qdrant,
    gerar_embedding,
    garantir_collection,
    COLLECTION_MEMORIA,
    COLLECTION_PREFERENCIAS,
)
from app.schemas import PerfilUsuario



# ==============================================================================
# CONEXÃO
# ==============================================================================

_mongo      = MongoClient(MONGODB_URI)
db          = _mongo[MONGODB_DB]
col_sessoes = db["sessoes"]

col_sessoes.create_index("session_id")
col_sessoes.create_index("user_id")
col_sessoes.create_index("iniciada_em")
col_perfil = db["perfil"]

_PROMPT_RESUMO = """\
Você é um assistente que resume conversas de assessoria financeira e agenda.
Gere um resumo conciso em 2-4 frases capturando:
- O que o usuário fez (transações registradas, eventos agendados)
- O que o usuário perguntou
- Informações relevantes mencionadas (valores, datas, categorias)

Responda APENAS com o resumo, sem introdução ou explicação.

Conversa:
{conversa}
"""
_sessoes_ativas: dict = {}

def _agora() -> datetime:
    return datetime.now(timezone.utc)

def _formatar_conversa(mensagens: list[dict]) -> str:
    """Formata o array de mensagens em texto para o prompt de resumo."""
    linhas = []
    for msg in mensagens:
        linhas.append(f"{msg['role']}: {msg['content']}")
    return "\n".join(linhas)


def _gerar_resumo(mensagens: list[dict]) -> str:
    """Chama o LLM para gerar o resumo da sessão."""
    conversa = _formatar_conversa(mensagens)
    return _llm_resumo.invoke(
        _PROMPT_RESUMO.format(conversa=conversa)
    ).content.strip()


def _doc_id_da_sessao(session_id: str) -> str | None:
    doc_id = _sessoes_ativas.get(session_id)
    if doc_id:
        return doc_id

    doc = col_sessoes.find_one(
        {"session_id": session_id, "resumo": {"$in": ["", None]}},
        {"_id": 1},
        sort=[("iniciada_em", -1)],
    )
    if not doc:
        return None

    _sessoes_ativas[session_id] = doc["_id"]
    return doc["_id"]


def iniciar_sessao(session_id: str, user_id: str = "usuario_teste") -> None:
    if _doc_id_da_sessao(session_id):
        return

    doc_id = str(uuid.uuid4())
    agora  = _agora()

    col_sessoes.insert_one({
        "_id":           doc_id,
        "session_id":    session_id,
        "user_id":       user_id,
        "iniciada_em":   agora,
        "atualizada_em": agora,
        "resumo":        "",
        "mensagens":     [],
    })

    _sessoes_ativas[session_id] = doc_id


def salvar_mensagem(
    session_id: str, role: str, content: str, user_id: str = "usuario_teste"
) -> None:
    iniciar_sessao(session_id, user_id=user_id)
    doc_id = _doc_id_da_sessao(session_id)

    col_sessoes.update_one(
        {"_id": doc_id},
        {
            "$push": {"mensagens": {"role": role, "content": content}},
            "$set":  {"atualizada_em": _agora()},
        },
    )


def encerrar_sessao(session_id: str) -> str:
    doc_id = _doc_id_da_sessao(session_id)

    if not doc_id:
        return ""

    doc = col_sessoes.find_one({"_id": doc_id})

    if not doc or not doc.get("mensagens"):
        _sessoes_ativas.pop(session_id, None)
        return ""

    resumo = _gerar_resumo(doc["mensagens"])

    col_sessoes.update_one(
        {"_id": doc_id},
        {"$set": {"resumo": resumo, "atualizada_em": _agora()}},
    )


    user_id = doc.get("user_id", "usuario_teste")
    vetor = gerar_embedding(resumo)
    qdrant.upsert(
        collection_name=COLLECTION_MEMORIA,
        points=[
            models.PointStruct(
                id=doc_id,
                vector=vetor,
                payload={
                    "user_id":     user_id,
                    "session_id":  session_id,
                    "resumo":      resumo,
                    "iniciada_em": doc["iniciada_em"].isoformat(),
                },
            )
        ],
    )

    _sessoes_ativas.pop(session_id)

    return resumo


def recuperar_historico(user_id: str, busca: str = "", limite: int = 3) -> list[dict]:
    if busca:
        vetor = gerar_embedding(busca)
        resultados = qdrant.query_points(
            collection_name=COLLECTION_MEMORIA,
            query=vetor,
            query_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="user_id",
                        match=models.MatchValue(value=user_id),
                    )
                ]
            ),
            limit=limite,
        )

        if resultados.points:
            return [
                {
                    "doc_id":      ponto.id,
                    "iniciada_em": ponto.payload.get("iniciada_em", ""),
                    "resumo":      ponto.payload["resumo"],
                }
                for ponto in resultados.points
            ]

    filtro = {"user_id": user_id, "resumo": {"$nin": ["", None]}}
    docs = (
        col_sessoes
        .find(filtro, {"resumo": 1, "iniciada_em": 1})
        .sort("iniciada_em", -1)
        .limit(limite)
    )

    return [
        {"doc_id": d["_id"], "iniciada_em": d["iniciada_em"], "resumo": d["resumo"]}
        for d in docs
    ]


def recuperar_mensagens(doc_id: str) -> list[dict]:
    doc = col_sessoes.find_one({"_id": doc_id}, {"mensagens": 1})
    return doc["mensagens"] if doc else []



def salvar_perfil_mongo(perfil: PerfilUsuario) -> None:
    col_perfil.update_one(
        {"user_id": perfil.user_id},
        {"$set": perfil.model_dump()},
        upsert=True,
    )


def salvar_perfil_qdrant(perfil: PerfilUsuario) -> None:
    texto = (perfil.preferencias or "").strip()
    if not texto:
        return

    garantir_collection(COLLECTION_PREFERENCIAS)
    vetor = gerar_embedding(texto)
    point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, perfil.user_id))
    qdrant.upsert(
        collection_name=COLLECTION_PREFERENCIAS,
        points=[
            models.PointStruct(
                id=point_id,
                vector=vetor,
                payload=perfil.model_dump(),
            )
        ],
    )

