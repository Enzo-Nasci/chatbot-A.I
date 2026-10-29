"""Juiz de autorização das escritas, antes de qualquer efeito no PostgreSQL.

Não substitui `app/juiz.py`. Aquele continua no fim do grafo e só olha a
resposta. Este módulo decide se `add_transaction`, `update_transaction` e
`add_event` podem rodar, e grava a chave de idempotência na mesma transação
da escrita de negócio.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturoSemResposta
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field

from app.observabilidade import anotar_status_escrita, registrar_chamada_juiz

logger = logging.getLogger(__name__)

TIMEOUT_JUIZ_S = 8.0
MODELO_JUIZ = "gemini-2.5-flash"
TOOLS_PROTEGIDAS = frozenset({"add_transaction", "update_transaction", "add_event"})

_PROMPT = (
    "Você autoriza ou recusa uma única escrita no banco do Assessor. "
    "Decida somente se esta operação específica corresponde à última instrução do usuário. "
    "Aprove apenas quando a intenção e os campos necessários estão claros e os argumentos "
    "são coerentes com o pedido. Recuse pedido ambíguo, incompleto, sem relação com a "
    "instrução, ou com instrução vazia. Não altere argumentos. Não execute a operação. "
    "Não invente dados. Responda só com decision approve ou deny e reason em uma frase."
)

_DDL = """
CREATE TABLE IF NOT EXISTS escrita_idempotencia (
    chave TEXT PRIMARY KEY,
    tool_name TEXT NOT NULL,
    args_hash TEXT NOT NULL,
    resultado TEXT NOT NULL,
    criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

_SELECT = (
    "SELECT args_hash, resultado FROM escrita_idempotencia WHERE chave = %s FOR UPDATE"
)
_INSERT = """
INSERT INTO escrita_idempotencia (chave, tool_name, args_hash, resultado)
VALUES (%s, %s, %s, %s)
"""
_UPDATE = "UPDATE escrita_idempotencia SET resultado = %s WHERE chave = %s"

_schema_ok = False
_schema_lock = threading.Lock()
_ctx = threading.local()


class FalhaJuiz(Exception):
    """Timeout, exceção ou veredito que não pode ser aceito como aprovação."""


class ChaveDuplicada(Exception):
    """A chave de idempotência já foi inserida por outra execução."""


class VereditoEscrita(BaseModel):
    decision: Literal["approve", "deny"]
    reason: str = Field(min_length=1)


def _erros_chave_unica() -> tuple[type[BaseException], ...]:
    erros: tuple[type[BaseException], ...] = (ChaveDuplicada,)
    try:
        from psycopg2.errors import UniqueViolation
    except Exception:
        return erros
    return erros + (UniqueViolation,)


def definir_contexto(session_id: str, instrucao: str) -> None:
    _ctx.session_id = session_id or ""
    _ctx.instrucao = instrucao or ""


def definir_sessao(session_id: str) -> None:
    _ctx.session_id = session_id or ""


def definir_instrucao(instrucao: str) -> None:
    _ctx.instrucao = instrucao or ""


def contexto_atual() -> tuple[str, str]:
    return (
        getattr(_ctx, "session_id", "") or "",
        getattr(_ctx, "instrucao", "") or "",
    )


def limpar_contexto() -> None:
    _ctx.session_id = ""
    _ctx.instrucao = ""


def _texto_conteudo(conteudo: Any) -> str:
    if isinstance(conteudo, str):
        return conteudo
    if isinstance(conteudo, list):
        partes: list[str] = []
        for bloco in conteudo:
            if isinstance(bloco, str):
                partes.append(bloco)
            elif isinstance(bloco, dict):
                partes.append(str(bloco.get("text") or ""))
        return "".join(partes)
    return str(conteudo or "")


def instrucao_de_mensagens(mensagens: Any) -> str:
    ultima = ""
    for mensagem in mensagens or []:
        if isinstance(mensagem, dict):
            tipo = mensagem.get("role") or mensagem.get("type") or ""
            conteudo = mensagem.get("content") or ""
        else:
            tipo = getattr(mensagem, "type", "") or getattr(mensagem, "role", "")
            conteudo = getattr(mensagem, "content", "") or ""
        if tipo in ("human", "user"):
            texto = _texto_conteudo(conteudo).strip()
            if texto:
                ultima = texto
    return ultima


def preencher_contexto_da_chamada(estado: Any) -> None:
    """Completa sessão e instrução a partir do estado do agente, sem apagar o que o turno já definiu."""
    sessao, instrucao = contexto_atual()
    mensagens = None
    session_id = None
    if isinstance(estado, dict):
        mensagens = estado.get("messages")
        session_id = estado.get("session_id")
    elif estado is not None:
        mensagens = getattr(estado, "messages", None)
        session_id = getattr(estado, "session_id", None)
    if session_id and not sessao:
        definir_sessao(str(session_id))
    if not instrucao and mensagens:
        texto = instrucao_de_mensagens(mensagens)
        if texto:
            definir_instrucao(texto)


def _canonico(valor: Any) -> Any:
    if isinstance(valor, dict):
        return {chave: _canonico(valor[chave]) for chave in sorted(valor)}
    if isinstance(valor, (list, tuple)):
        return [_canonico(item) for item in valor]
    return valor


def _material_args(args: dict) -> str:
    return json.dumps(
        _canonico(args),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def hash_argumentos(args: dict) -> str:
    return hashlib.sha256(_material_args(args).encode("utf-8")).hexdigest()


def chave_operacao(session_id: str, tool_name: str, args: dict) -> str:
    material = json.dumps(
        {
            "args": _canonico(args),
            "sessao": session_id or "",
            "tool": tool_name,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def interpretar_veredito(valor: Any) -> tuple[str, str] | None:
    if isinstance(valor, dict):
        decision = valor.get("decision")
        reason = valor.get("reason")
    else:
        decision = getattr(valor, "decision", None)
        reason = getattr(valor, "reason", None)
    if decision not in ("approve", "deny"):
        return None
    if not isinstance(reason, str) or not reason.strip():
        return None
    return decision, reason.strip()


def _uso_da_resposta(mensagem: Any) -> tuple[int | None, int | None]:
    usage = getattr(mensagem, "usage_metadata", None) if mensagem is not None else None
    if not isinstance(usage, dict):
        if usage is None:
            return None, None
        usage = {
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
        }
    entrada = usage.get("input_tokens")
    saida = usage.get("output_tokens")
    if entrada is None or saida is None:
        return None, None
    return int(entrada), int(saida)


def _registrar(entrada: int | None, saida: int | None, inicio: float) -> None:
    latencia_ms = int((time.perf_counter() - inicio) * 1000)
    registrar_chamada_juiz(MODELO_JUIZ, entrada, saida, latencia_ms)


def _invocar_modelo(
    tool_name: str,
    args: dict,
    instrucao: str,
    session_id: str,
) -> tuple[Any, int | None, int | None]:
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_google_genai import ChatGoogleGenerativeAI

    from app.config import GEMINI_API_KEY

    if not GEMINI_API_KEY:
        raise FalhaJuiz("credencial ausente do juiz de escrita")

    modelo = ChatGoogleGenerativeAI(
        model=MODELO_JUIZ,
        temperature=0,
        google_api_key=GEMINI_API_KEY,
        timeout=TIMEOUT_JUIZ_S,
        max_retries=0,
    )
    runnable = modelo.with_structured_output(VereditoEscrita, include_raw=True)
    pedido = json.dumps(
        {
            "instrucao_usuario": instrucao,
            "tool": tool_name,
            "argumentos": _canonico(args),
            "session_id": session_id,
        },
        ensure_ascii=False,
        default=str,
    )
    resposta = runnable.invoke(
        [
            SystemMessage(content=_PROMPT),
            HumanMessage(content=pedido),
        ]
    )
    parsed = resposta.get("parsed") if isinstance(resposta, dict) else None
    raw = resposta.get("raw") if isinstance(resposta, dict) else None
    entrada, saida = _uso_da_resposta(raw)
    if parsed is None:
        raise FalhaJuiz("veredito inválido do juiz de escrita")
    return parsed, entrada, saida


def julgar(tool_name: str, args: dict, instrucao: str, session_id: str) -> Any:
    """Chama o modelo. Sem veredito válido, levanta FalhaJuiz e não aprova."""
    inicio = time.perf_counter()
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        futuro = pool.submit(_invocar_modelo, tool_name, args, instrucao, session_id)
        try:
            parsed, entrada, saida = futuro.result(timeout=TIMEOUT_JUIZ_S)
        except FuturoSemResposta as exc:
            _registrar(None, None, inicio)
            raise FalhaJuiz("timeout do juiz de escrita") from exc
        except Exception as exc:
            _registrar(None, None, inicio)
            if isinstance(exc, FalhaJuiz):
                raise
            raise FalhaJuiz(f"falha do juiz de escrita: {exc}") from exc
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    _registrar(entrada, saida, inicio)
    if interpretar_veredito(parsed) is None:
        raise FalhaJuiz("veredito inválido do juiz de escrita")
    return parsed


def reiniciar_esquema_idempotencia() -> None:
    global _schema_ok
    with _schema_lock:
        _schema_ok = False


def _conectar():
    from app.tools.db import get_conn

    return get_conn()


def _rollback(conn) -> None:
    if conn is None:
        return
    try:
        conn.rollback()
    except Exception:
        logger.exception("rollback da escrita protegida falhou")


def _fechar(cur, conn) -> None:
    try:
        if cur is not None:
            cur.close()
    except Exception:
        logger.exception("falha ao fechar cursor da escrita protegida")
    try:
        if conn is not None:
            conn.close()
    except Exception:
        logger.exception("falha ao fechar conexão da escrita protegida")


def garantir_esquema() -> None:
    """Cria a tabela de idempotência uma vez por processo."""
    global _schema_ok
    if _schema_ok:
        return
    with _schema_lock:
        if _schema_ok:
            return
        conn = _conectar()
        cur = None
        try:
            cur = conn.cursor()
            cur.execute(_DDL)
            conn.commit()
            _schema_ok = True
        except Exception:
            _rollback(conn)
            raise
        finally:
            _fechar(cur, conn)


def _carregar_resultado(valor: Any) -> dict:
    if isinstance(valor, dict):
        return valor
    if isinstance(valor, (bytes, bytearray)):
        valor = valor.decode("utf-8")
    if isinstance(valor, str):
        carregado = json.loads(valor)
        if isinstance(carregado, dict):
            return carregado
    raise FalhaJuiz("resultado de idempotência ilegível")


def _recuperar(chave: str, args_hash: str, tool_name: str) -> dict:
    conn = None
    cur = None
    try:
        conn = _conectar()
        cur = conn.cursor()
        cur.execute(
            "SELECT args_hash, resultado FROM escrita_idempotencia WHERE chave = %s",
            (chave,),
        )
        row = cur.fetchone()
        _rollback(conn)
    except Exception as exc:
        _rollback(conn)
        anotar_status_escrita("erro")
        return {
            "status": "erro",
            "message": f"Não foi possível recuperar a escrita já registrada: {exc}",
            "tool": tool_name,
        }
    finally:
        _fechar(cur, conn)

    if row is None:
        anotar_status_escrita("erro")
        return {
            "status": "erro",
            "message": "Conflito de idempotência sem resultado persistido. A gravação não foi feita.",
            "tool": tool_name,
        }
    if row[0] != args_hash:
        anotar_status_escrita("erro")
        return {
            "status": "erro",
            "message": "Chave de idempotência reutilizada com argumentos diferentes. A gravação não foi feita.",
            "tool": tool_name,
        }
    try:
        return _carregar_resultado(row[1])
    except Exception as exc:
        anotar_status_escrita("erro")
        return {"status": "erro", "message": str(exc), "tool": tool_name}


def _falha_negocio(resultado: Any, tool_name: str) -> dict:
    if isinstance(resultado, dict):
        falha = dict(resultado)
        falha["status"] = "erro"
        falha.setdefault("message", "Falha na escrita.")
        falha.setdefault("tool", tool_name)
        return falha
    return {"status": "erro", "message": "Escrita sem resultado válido.", "tool": tool_name}


def _aplicar_idempotente(
    tool_name: str,
    args: dict,
    escrever: Callable[[Any], dict],
) -> dict:
    session_id, _ = contexto_atual()
    chave = chave_operacao(session_id, tool_name, args)
    args_hash = hash_argumentos(args)
    try:
        garantir_esquema()
    except Exception as exc:
        anotar_status_escrita("erro")
        return {
            "status": "erro",
            "message": f"Esquema de idempotência indisponível: {exc}",
            "tool": tool_name,
        }

    conn = None
    cur = None
    try:
        conn = _conectar()
        cur = conn.cursor()
        cur.execute(_SELECT, (chave,))
        existente = cur.fetchone()
        if existente is not None:
            hash_salvo, resultado_salvo = existente[0], existente[1]
            _rollback(conn)
            if hash_salvo != args_hash:
                anotar_status_escrita("erro")
                return {
                    "status": "erro",
                    "message": "Chave de idempotência reutilizada com argumentos diferentes. A gravação não foi feita.",
                    "tool": tool_name,
                }
            try:
                return _carregar_resultado(resultado_salvo)
            except Exception as exc:
                anotar_status_escrita("erro")
                return {"status": "erro", "message": str(exc), "tool": tool_name}

        try:
            cur.execute(_INSERT, (chave, tool_name, args_hash, "{}"))
        except _erros_chave_unica() as exc:
            _rollback(conn)
            _fechar(cur, conn)
            cur = None
            conn = None
            logger.info("chave de idempotência já existe para %s: %s", tool_name, exc)
            return _recuperar(chave, args_hash, tool_name)

        resultado = escrever(cur)
        if not isinstance(resultado, dict) or resultado.get("status") != "ok":
            _rollback(conn)
            anotar_status_escrita("erro")
            return _falha_negocio(resultado, tool_name)

        cur.execute(
            _UPDATE,
            (json.dumps(resultado, ensure_ascii=False, default=str), chave),
        )
        conn.commit()
        return resultado
    except Exception as exc:
        _rollback(conn)
        anotar_status_escrita("erro")
        return {"status": "erro", "message": str(exc), "tool": tool_name}
    finally:
        _fechar(cur, conn)


def proteger_escrita(
    tool_name: str,
    args: dict,
    escrever: Callable[[Any], dict],
) -> dict:
    """Julga e só então deixa `escrever` ver o cursor da transação."""
    session_id, instrucao = contexto_atual()
    try:
        veredito = julgar(tool_name, args, instrucao, session_id)
    except Exception as exc:
        anotar_status_escrita("erro")
        return {
            "status": "erro",
            "message": f"Juiz de escrita indisponível: {exc}",
            "tool": tool_name,
        }

    interpretado = interpretar_veredito(veredito)
    if interpretado is None:
        anotar_status_escrita("erro")
        return {
            "status": "erro",
            "message": "Veredito inválido do juiz de escrita. A gravação não foi feita.",
            "tool": tool_name,
        }

    decision, reason = interpretado
    if decision == "deny":
        anotar_status_escrita("bloqueado")
        return {
            "status": "bloqueado",
            "message": "A gravação não foi feita.",
            "motivo": reason,
            "tool": tool_name,
        }
    return _aplicar_idempotente(tool_name, args, escrever)
