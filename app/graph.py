from typing import Annotated
import inspect
import re
from langgraph.graph import StateGraph, MessagesState, END
from langgraph.checkpoint.memory import MemorySaver
from app.observabilidade import abrir_turno, fechar_turno, status_do_estado
from langchain.agents import create_agent
from app.memoryMongo import salvar_mensagem
from langchain_core.messages import RemoveMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from app.tools.financeiro import TOOLS
from app.tools.agenda import TOOLS_AGENDA
from app.tools.calendario_google import TOOLS_GOOGLE
from app.memory import TOOLS_MEMORIA
from app.tools.faq import faq_retriever
from app.guardrail import anonimizar_entrada, guardrail_entrada, guardrail_saida
from app.llm import llm, llmRapido
from app.juiz import no_juiz
from app.prompts import (
    ROUTER_PROMPT_COMPLETO,
    FINANCEIRO_PROMPT_COMPLETO,
    AGENDA_PROMPT_COMPLETO,
    ORQUESTRADOR_PROMPT_COMPLETO,
    FAQ_PROMPT_COMPLETO,
)

def _middleware_escrita():
    """Repassa sessão e instrução ao wrapper. A autorização continua na tool."""
    try:
        from langchain.agents.middleware import AgentMiddleware
    except ImportError:
        return []
    if "middleware" not in inspect.signature(create_agent).parameters:
        return []

    class ContextoEscritaMiddleware(AgentMiddleware):
        def wrap_tool_call(self, request, handler):
            chamada = getattr(request, "tool_call", None)
            nome = chamada.get("name") if isinstance(chamada, dict) else ""
            if nome in {"add_transaction", "update_transaction", "add_event"}:
                from app.juiz_escrita import preencher_contexto_da_chamada

                preencher_contexto_da_chamada(getattr(request, "state", None))
            return handler(request)

    try:
        return [ContextoEscritaMiddleware()]
    except Exception as exc:
        print(f"[juiz_escrita] middleware indisponível, contexto fica no turno: {exc}")
        return []


_KW_MIDDLEWARE = {}
_middleware = _middleware_escrita()
if _middleware:
    _KW_MIDDLEWARE["middleware"] = _middleware

routerApp = create_agent(
    model=llmRapido,
    system_prompt=ROUTER_PROMPT_COMPLETO, 
    tools=None,
    
)

financeiroApp = create_agent(
    model=llm,
    system_prompt=FINANCEIRO_PROMPT_COMPLETO,
    tools=TOOLS + TOOLS_MEMORIA,
    **_KW_MIDDLEWARE,
)

agendaApp = create_agent(
    model=llm,
    system_prompt=AGENDA_PROMPT_COMPLETO,
    tools=TOOLS_AGENDA + TOOLS_GOOGLE + TOOLS_MEMORIA,
    **_KW_MIDDLEWARE,
)

orquestradorApp = create_agent(
    model=llmRapido,
    system_prompt=ORQUESTRADOR_PROMPT_COMPLETO,
    tools=[],
)

faqApp = create_agent(
    model=llm,
    system_prompt=FAQ_PROMPT_COMPLETO,
    tools=[faq_retriever],
)

class Estado(MessagesState):
    agentes: Annotated[list[str], "Lista dos agentes chamados durante o fluxo."]
    rota: str
    mapa_pii: dict
    session_id: str


def _lista_agentes(estado: Estado) -> list[str]:
    return list(estado.get("agentes") or [])


def _conteudo_mensagem(msg) -> str:
    if hasattr(msg, "content"):
        conteudo = msg.content
    elif isinstance(msg, dict):
        conteudo = msg.get("content", "")
    else:
        conteudo = str(msg)
    if isinstance(conteudo, list):
        partes = []
        for bloco in conteudo:
            if isinstance(bloco, str):
                partes.append(bloco)
            elif isinstance(bloco, dict):
                partes.append(bloco.get("text", ""))
            else:
                partes.append(getattr(bloco, "text", "") or "")
        return "".join(partes)
    return conteudo or ""



def no_roteador(estado: Estado) -> dict:
    saida = routerApp.invoke({"messages": list(estado["messages"])})
    texto = _conteudo_mensagem(saida["messages"][-1])

    match = re.search(r"ROUTE\s*=\s*\*?\*?(financeiro|agenda|faq)\*?\*?", texto, re.IGNORECASE)

    if not match:
        return {
            "agentes": _lista_agentes(estado) + ["roteador"],
            "rota": "fim",
            "messages": [saida["messages"][-1]],
        }

    rota = match.group(1).lower()

    return {
        "agentes": _lista_agentes(estado) + ["roteador", rota],
        "rota": rota,
    }

def no_guardrail_entrada(estado: Estado) -> dict:
    pergunta_usuario = _conteudo_mensagem(estado["messages"][-1])
    texto_anonimizado, mapa_pii = anonimizar_entrada(pergunta_usuario)
    resposta_guardrail = guardrail_entrada(texto_anonimizado)

    if resposta_guardrail["bloqueado"]:
        return {
            "rota": "fim",
            "agentes": ["guardrail_entrada"],
            "messages": [{"role": "assistant", "content": resposta_guardrail["mensagem"]}],
        }
    else:
        return {
            "mapa_pii": mapa_pii,
            "agentes": ["guardrail_entrada"],
            "messages": [
                RemoveMessage(id=estado["messages"][-1].id),
                {"role": "human", "content": texto_anonimizado},
            ],
        }

def no_guardrail_saida(estado: Estado) -> dict:
    resposta_orquestrador = _conteudo_mensagem(estado["messages"][-1])
    mapa_pii = estado.get("mapa_pii") or {}
    resposta_final = guardrail_saida(resposta_orquestrador, mapa_pii)

    return {
        "messages": [{"role": "assistant", "content": resposta_final["conteudo"]}],
        "agentes": _lista_agentes(estado) + ["guardrail_saida"],
    }

def no_orquestrador(estado: Estado, config: RunnableConfig) -> dict:
    # Pega a última resposta do especialista (última AIMessage com conteúdo)
    ultima_especialista = ""
    for mensagem in reversed(estado["messages"]):
        if mensagem.type == "ai" and mensagem.content:
            ultima_especialista = mensagem.content
            break
    # config segue o mesmo motivo do roteador: sem ele, o modelo deste nó
    # não herda o metadata do grafo (e o registro de observabilidade perde o nó).
    saida = orquestradorApp.invoke({
        "messages": [{"role": "human", "content": ultima_especialista}]
    }, config=config)

    return {
        "agentes": _lista_agentes(estado) + ["orquestrador"],
        "messages": [{"role": "assistant", "content": saida["messages"][-1].text}],
    }

# Decisões
def decidir_especialista(estado: Estado) -> str:
    rota = estado.get("rota", "fim")

    return rota if rota in (
        "financeiro",
        "agenda",
        "faq",
    ) else "fim"

def decidir_pos_guardrail_entrada(estado: Estado) -> str:
    if estado.get("messages", []) and estado.get("rota", "") == "fim":
        return "fim"
    return "roteador"

# Construção do grafo
grafo = StateGraph(Estado)

grafo.add_node("guardrail_entrada", no_guardrail_entrada)
grafo.add_node("roteador", no_roteador)
grafo.add_node("financeiro", financeiroApp)
grafo.add_node("agenda", agendaApp)
grafo.add_node("faq", faqApp)
grafo.add_node("orquestrador", no_orquestrador)
grafo.add_node("guardrail_saida", no_guardrail_saida)
grafo.add_node("juiz", no_juiz)

grafo.set_entry_point("guardrail_entrada")
grafo.add_conditional_edges(
    "guardrail_entrada",
    decidir_pos_guardrail_entrada,
    {"roteador": "roteador", "fim": END},
)

grafo.add_conditional_edges(
    "roteador",
    decidir_especialista,
    {"financeiro": "financeiro", "agenda": "agenda", "faq": "faq", "fim": END},
)

grafo.add_edge("financeiro",      "orquestrador")
grafo.add_edge("agenda",          "orquestrador")
grafo.add_edge("orquestrador",    "guardrail_saida")
grafo.add_edge("guardrail_saida", "juiz")
grafo.add_edge("faq",             "juiz")
grafo.add_edge("juiz",            END)

memory = MemorySaver()
fluxo_agentes = grafo.compile(checkpointer=memory)

# Função principal
def executar_fluxo_assessor(
    pergunta_usuario: str, session_id: str, user_id: str = "usuario_teste"
) -> tuple:
    pergunta_usuario_anonimizado, _ = anonimizar_entrada(pergunta_usuario)
    estado_inicial = {
        "messages": [{"role": "human", "content": pergunta_usuario}],
        "agentes": [],
        "rota": "",
        "mapa_pii": {},
        "session_id": session_id,
    }

    # O registro abre antes do invoke e fecha no finally, inclusive quando
    # o grafo levanta. A rota /monitor só lê o que ficou anotado.
    from app.juiz_escrita import definir_contexto, limpar_contexto

    turno_id = abrir_turno()
    status = "erro"
    rota = ""
    try:
        definir_contexto(session_id, pergunta_usuario)
        estado_final = fluxo_agentes.invoke(
            estado_inicial,
            config={"configurable": {"thread_id": session_id, "user_id": user_id}},
        )

        resposta = estado_final["messages"][-1].text

        # Salva no MongoDB após cada interação
        salvar_mensagem(session_id, "human",     pergunta_usuario_anonimizado, user_id=user_id)
        salvar_mensagem(session_id, "assistant", resposta, user_id=user_id)

        status = status_do_estado(estado_final)
        rota = estado_final.get("rota") or ""
        return resposta, estado_final.get("agentes") or []
    finally:
        fechar_turno(turno_id, session_id=session_id, rota=rota, status=status)
        limpar_contexto()