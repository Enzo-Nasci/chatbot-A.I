from fastapi import APIRouter, HTTPException
from app.schemas import ChatRequest, ChatResponse, PerfilUsuario
from app.graph import executar_fluxo_assessor
from app.memoryMongo import encerrar_sessao, recuperar_historico, recuperar_mensagens, salvar_perfil_mongo, salvar_perfil_qdrant
from qdrant_client import QdrantClient, models
from qdrant_client.http.models import Distance, VectorParams, PointStruct
import uuid



router = APIRouter(tags=["chat"])

@router.post("/chat", response_model=ChatResponse)
def conversar(requisicao: ChatRequest) -> ChatResponse:
    """Uma mensagem do usuário, uma resposta do assessor."""
    try:
        resposta, agentes = executar_fluxo_assessor(
            requisicao.pergunta, requisicao.session_id, requisicao.user_id
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return ChatResponse(resposta=resposta, agentes_chamados=agentes)


@router.get("/historico/mensagens/{doc_id}")
def get_mensagens_sessao(doc_id: str):
    """Retorna todas as mensagens de uma sessão específica."""
    try:
        mensagens = recuperar_mensagens(doc_id)
        if not mensagens:
            raise HTTPException(status_code=404, detail="Sessão não encontrada ou sem mensagens.")
        return {"status": "ok", "mensagens": mensagens}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/historico/{user_id}")
def get_historico_usuario(user_id: str, busca: str = "", limite: int = 10):
    """Retorna a lista de sessões passadas (com resumo) do usuário."""
    try:
        historico = recuperar_historico(user_id, busca, limite)
        return {"status": "ok", "historico": historico}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sessao/{session_id}/encerrar")
def encerrar_sessao_usuario(session_id: str):
    """Gera o resumo da sessão e indexa no Qdrant para memória de longo prazo."""
    try:
        resumo = encerrar_sessao(session_id)
        return {"status": "ok", "resumo": resumo}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/perfil")
async def salvar_perfil(perfil: PerfilUsuario):
    try:

        salvar_perfil_mongo(perfil)
        salvar_perfil_qdrant(perfil)
        return {"status": "ok", "perfil": perfil}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))





