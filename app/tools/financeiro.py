from typing import List, Optional
from langchain.tools import tool
from pydantic import BaseModel, Field
import json
import logging
import unicodedata
import urllib.error
import urllib.request

from app.juiz_escrita import proteger_escrita
from app.tools.db import get_conn
from app.vectordb import gerar_embedding, qdrant, COLLECTION_PREFERENCIAS
from qdrant_client import models
from bson import ObjectId
from app.memoryMongo import col_perfil

class AddTransactionArgs(BaseModel):
    amount: float = Field(..., description="Valor da transação (use positivo).")
    source_text: str = Field(..., description="Texto original do usuário.")
    occurred_at: Optional[str] = Field(
        default=None,
        description="Timestamp ISO 8601; se ausente, usa NOW() no banco."
    )
    type_id: Optional[int] = Field(default=None, description="ID em transaction_types (1=INCOME, 2=EXPENSES, 3=TRANSFER).")
    type_name: Optional[str] = Field(default=None, description="Nome do tipo: INCOME | EXPENSES | TRANSFER.")
    
    category_name: Optional[str] = Field(
        default=None, 
        description="Nome da categoria ('comida', 'besteira', 'estudo', 'férias', 'transporte', 'moradia', 'saúde', 'lazer', 'contas', 'investimento', 'presente', caso não for nenhuma dessas categorias, use 'outro'); será resolvida para category_id (optional)."
    )
    
    category_id: Optional[int] = Field(default=None, description="FK de categories (opcional).")
    description: Optional[str] = Field(default=None, description="Descrição (opcional).")
    payment_method: Optional[str] = Field(default=None, description="Forma de pagamento (opcional).")
    currency: Optional[str] = Field(
        default="BRL",
        description="Moeda do amount informado pelo usuário. Use BRL se for real. "
                    "Se for outra (dólar, euro, bitcoin etc.), envie o código ISO "
                    "(USD, EUR, BTC, GBP...) — a tool converte para reais via AwesomeAPI.",
    )

class QueryTransactionsArgs(BaseModel):
    query: str = Field(..., description="Texto em source_text ou description para buscar (case-insensitive, parcial).")
    limit: Optional[int] = Field(default=10, description="Número máximo de resultados a retornar.")


TYPE_ALIASES = {
    "INCOME": "INCOME", "ENTRADA": "INCOME", "RECEITA": "INCOME", "SALARIO": "INCOME",
    "EXPENSES": "EXPENSES", "EXPENSE": "EXPENSES", "DESPESA": "EXPENSES", "GASTO": "EXPENSES",
    "TRANSFER": "TRANSFER", "TRANSFERENCIA": "TRANSFER"
}

AWESOME_API_LAST = "https://economia.awesomeapi.com.br/json/last/{par}"

MOEDA_ALIASES = {
    "BRL": "BRL", "REAL": "BRL", "REAIS": "BRL", "R$": "BRL",
    "USD": "USD", "DOLAR": "USD", "DOLARES": "USD", "DOLLAR": "USD", "DOLLARS": "USD",
    "EUR": "EUR", "EURO": "EUR", "EUROS": "EUR",
    "GBP": "GBP", "LIBRA": "GBP", "LIBRAS": "GBP",
    "JPY": "JPY", "IENE": "JPY", "IENES": "JPY",
    "ARS": "ARS", "CAD": "CAD", "AUD": "AUD", "CHF": "CHF",
    "CNY": "CNY", "YUAN": "CNY",
    "BTC": "BTC", "BITCOIN": "BTC",
}


def _normalize_text(s: str) -> str:
    s = s.strip().lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return " ".join(s.split())

def _resolve_type_id(cur, type_id: Optional[int], type_name: Optional[str]) -> Optional[int]:
    if type_name:
        t = type_name.strip().upper()
        t = TYPE_ALIASES.get(t, t)
        cur.execute("SELECT id FROM transaction_types WHERE UPPER(type)=%s LIMIT 1;", (t,))
        row = cur.fetchone()
        return row[0] if row else None
    if type_id:
        return int(type_id)
    return 2

def _resolve_category_id(cur, category_id: Optional[int], category_name: Optional[str]) -> Optional[int]:
    if category_id is not None:
        return int(category_id)

    if category_name:
        raw = category_name.strip()
        normalized = _normalize_text(raw)

        if normalized in {"outro", "outros"}:
            normalized = "outros"

        cur.execute("SELECT id, name FROM categories;")
        rows = cur.fetchall() or []
        by_name = {_normalize_text(name): int(cid) for (cid, name) in rows}

        if normalized in by_name:
            return by_name[normalized]

        if "outros" in by_name:
            return by_name["outros"]

    return None


def _codigo_moeda(currency: Optional[str]) -> str:
    if not currency:
        return "BRL"
    chave = _normalize_text(currency).upper().replace(" ", "")
    return MOEDA_ALIASES.get(chave, chave)


def _converter_para_brl(amount: float, currency: Optional[str]) -> dict:
    """Converte amount para BRL pela última cotação da AwesomeAPI (par MOEDA-BRL)."""
    codigo = _codigo_moeda(currency)
    origem = float(amount)
    if codigo == "BRL":
        return {
            "ok": True,
            "amount_brl": origem,
            "amount_origem": origem,
            "moeda_origem": "BRL",
            "cotacao": 1.0,
        }

    par = f"{codigo}-BRL"
    url = AWESOME_API_LAST.format(par=par)
    try:
        pedido = urllib.request.Request(url, headers={"User-Agent": "AssessorAI/1.0"})
        with urllib.request.urlopen(pedido, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detalhe = exc.read().decode("utf-8", errors="replace")
        return {
            "ok": False,
            "message": f"Moeda não encontrada na cotação ({par}): {detalhe}",
        }
    except Exception as exc:
        return {"ok": False, "message": f"Falha ao consultar cotação AwesomeAPI: {exc}"}

    cotacao = payload.get(f"{codigo}BRL") or next(iter(payload.values()), None)
    if not cotacao or "bid" not in cotacao:
        return {"ok": False, "message": f"Cotação {par} sem dados de compra (bid)."}

    taxa = float(cotacao["bid"])
    return {
        "ok": True,
        "amount_brl": round(origem * taxa, 2),
        "amount_origem": origem,
        "moeda_origem": codigo,
        "cotacao": taxa,
        "par": cotacao.get("name") or par,
        "atualizado_em": cotacao.get("create_date"),
    }


@tool("search_transactions", args_schema=QueryTransactionsArgs)
def search_transactions(query: str, limit: Optional[int] = 10) -> dict:
    """Busca transações por texto."""

    conn = get_conn()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            SELECT 
                t.id,
                t.amount,
                tt.type AS type_name,
                t.category_id,
                t.description,
                t.payment_method,
                t.occurred_at,
                t.source_text
            FROM transactions t
            JOIN transaction_types tt ON tt.id = t.type
            WHERE t.source_text ILIKE %s OR t.description ILIKE %s
            ORDER BY t.occurred_at DESC
            LIMIT %s;
            """,
            (f"%{query}%", f"%{query}%", limit)
        )

        rows = cur.fetchall() or []

        return {
            "status": "ok",
            "transactions": [
                {
                    "id": r[0],
                    "amount": float(r[1]),
                    "type": r[2],
                    "category_id": r[3],
                    "description": r[4],
                    "payment_method": r[5],
                    "occurred_at": str(r[6]),
                    "source_text": r[7],
                }
                for r in rows
            ],
        }

    except Exception as e:
        return {"status": "error", "message": str(e)}

    finally:
        try:
            cur.close()
            conn.close()
        except Exception as e:
            logging.error(f"Erro ao fechar conexão no search_transactions: {e}")

@tool("saldo_total")
def saldo_total() -> dict:
    """Retorna saldo total (INCOME - EXPENSES)."""

    conn = get_conn()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            SELECT 
                COALESCE(SUM(CASE WHEN tt.type = 'INCOME' THEN amount END), 0) -
                COALESCE(SUM(CASE WHEN tt.type = 'EXPENSES' THEN amount END), 0)
            FROM transactions t
            JOIN transaction_types tt ON tt.id = t.type;
            """
        )

        row = cur.fetchone()

        return {
            "status": "ok",
            "saldo_total": float(row[0]) if row and row[0] else 0.0
        }

    except Exception as e:
        return {"status": "error", "message": str(e)}

    finally:
        try:
            cur.close()
            conn.close()
        except Exception as e:
            logging.error(f"Erro ao fechar conexão no saldo_total: {e}")

@tool("saldo_diario")
def saldo_diario() -> dict:
    """Retorna saldo do dia atual."""

    conn = get_conn()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            SELECT 
                COALESCE(SUM(CASE WHEN tt.type = 'INCOME' THEN amount END), 0) -
                COALESCE(SUM(CASE WHEN tt.type = 'EXPENSES' THEN amount END), 0)
            FROM transactions t
            JOIN transaction_types tt ON tt.id = t.type
            WHERE (t.occurred_at AT TIME ZONE 'America/Sao_Paulo')::date = CURRENT_DATE;
            """
        )

        row = cur.fetchone()

        return {
            "status": "ok",
            "saldo_diario": float(row[0]) if row and row[0] else 0.0
        }

    except Exception as e:
        return {"status": "error", "message": str(e)}

    finally:
        try:
            cur.close()
            conn.close()
        except Exception as e:
            logging.error(f"Erro ao fechar conexão no saldo_diario: {e}")


def _args_add_transaction(
    amount: float,
    source_text: str,
    occurred_at: Optional[str],
    type_id: Optional[int],
    type_name: Optional[str],
    category_name: Optional[str],
    category_id: Optional[int],
    description: Optional[str],
    payment_method: Optional[str],
    currency: Optional[str],
) -> dict:
    return {
        "amount": amount,
        "source_text": source_text,
        "occurred_at": occurred_at,
        "type_id": type_id,
        "type_name": type_name,
        "category_name": category_name,
        "category_id": category_id,
        "description": description,
        "payment_method": payment_method,
        "currency": currency,
    }


def _gravar_add_transaction(
    cur,
    amount: float,
    source_text: str,
    occurred_at: Optional[str],
    type_id: Optional[int],
    type_name: Optional[str],
    category_name: Optional[str],
    category_id: Optional[int],
    description: Optional[str],
    payment_method: Optional[str],
    currency: Optional[str],
) -> dict:
    conversao = _converter_para_brl(amount, currency)
    if not conversao.get("ok"):
        return {"status": "erro", "message": conversao.get("message", "Falha na conversão de moeda.")}

    amount_brl = conversao["amount_brl"]
    resolved_type_id = _resolve_type_id(cur, type_id, type_name)
    if not resolved_type_id:
        return {"status": "erro", "message": "Tipo inválido (use type_id ou type_name: INCOME/EXPENSES/TRANSFER)."}

    if category_id is None:
        category_id = _resolve_category_id(cur, category_id=None, category_name=category_name)

    if occurred_at:
        cur.execute(
            """
            INSERT INTO transactions
                (amount, type, category_id, description, payment_method, occurred_at, source_text)
            VALUES
                (%s, %s, %s, %s, %s, %s::timestamptz, %s)
            RETURNING id, occurred_at;
            """,
            (amount_brl, resolved_type_id, category_id, description, payment_method, occurred_at, source_text),
        )
    else:
        cur.execute(
            """
            INSERT INTO transactions
                (amount, type, category_id, description, payment_method, occurred_at, source_text)
            VALUES
                (%s, %s, %s, %s, %s, NOW(), %s)
            RETURNING id, occurred_at;
            """,
            (amount_brl, resolved_type_id, category_id, description, payment_method, source_text),
        )

    new_id, occurred = cur.fetchone()
    resultado = {
        "status": "ok",
        "id": new_id,
        "occurred_at": str(occurred),
        "amount": amount_brl,
        "moeda": "BRL",
    }
    if conversao["moeda_origem"] != "BRL":
        resultado.update({
            "amount_original": conversao["amount_origem"],
            "moeda_origem": conversao["moeda_origem"],
            "cotacao": conversao["cotacao"],
            "par": conversao.get("par"),
            "cotacao_em": conversao.get("atualizado_em"),
        })
    return resultado


@tool("add_transaction", args_schema=AddTransactionArgs)
def add_transaction(
    amount: float,
    source_text: str,
    occurred_at: Optional[str] = None,
    type_id: Optional[int] = None,
    type_name: Optional[str] = None,
    category_name: Optional[str] = None,
    category_id: Optional[int] = None,
    description: Optional[str] = None,
    payment_method: Optional[str] = None,
    currency: Optional[str] = "BRL",
) -> dict:
    """Insere uma transação no Postgres. amount na moeda informada; se não for BRL, converte pela AwesomeAPI e grava em reais."""
    argumentos = _args_add_transaction(
        amount,
        source_text,
        occurred_at,
        type_id,
        type_name,
        category_name,
        category_id,
        description,
        payment_method,
        currency,
    )
    return proteger_escrita(
        "add_transaction",
        argumentos,
        lambda cur: _gravar_add_transaction(
            cur,
            amount,
            source_text,
            occurred_at,
            type_id,
            type_name,
            category_name,
            category_id,
            description,
            payment_method,
            currency,
        ),
    )

def _local_date_filter_sql(field: str = "occurred_at") -> str:
    """
    Retorna um trecho SQL para filtragem por dia local em America/Sao_Paulo.
    Ex.: (occurred_at AT TIME ZONE 'America/Sao_Paulo')::date = %s::date
    """
    return f"(({field} AT TIME ZONE 'America/Sao_Paulo')::date = %s::date)"


@tool("list_categories")
def list_categories() -> dict:
    """Lista categorias disponíveis (id e nome)."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, name, description, created_at FROM categories ORDER BY id ASC;")
        rows = cur.fetchall() or []
        return {
            "status": "ok",
            "categories": [
                {
                    "id": int(r[0]),
                    "name": r[1],
                    "description": r[2],
                    "created_at": str(r[3]) if r[3] is not None else None,
                }
                for r in rows
            ],
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        try:
            cur.close()
            conn.close()
        except Exception as e:
            logging.error(f"Erro ao fechar conexão no list_categories: {e}")
        
class UpdateTransactionArgs(BaseModel):
    id: Optional[int] = Field(
        default=None,
        description="ID da transação a atualizar. Se ausente, será feita uma busca por (match_text + date_local)."
    )
    match_text: Optional[str] = Field(
        default=None,
        description="Texto para localizar transação quando id não for informado (busca em source_text/description)."
    )
    date_local: Optional[str] = Field(
        default=None,
        description="Data local (YYYY-MM-DD) em America/Sao_Paulo; usado em conjunto com match_text quando id ausente."
    )
    amount: Optional[float] = Field(default=None, description="Novo valor.")
    type_id: Optional[int] = Field(default=None, description="Novo type_id (1/2/3).")
    type_name: Optional[str] = Field(default=None, description="Novo type_name: INCOME | EXPENSES | TRANSFER.")
    category_id: Optional[int] = Field(default=None, description="Nova categoria (id).")
    category_name: Optional[str] = Field(default=None, description="Nova categoria (nome).")
    description: Optional[str] = Field(default=None, description="Nova descrição.")
    payment_method: Optional[str] = Field(default=None, description="Novo meio de pagamento.")
    occurred_at: Optional[str] = Field(default=None, description="Novo timestamp ISO 8601.")

def _args_update_transaction(
    id: Optional[int],
    match_text: Optional[str],
    date_local: Optional[str],
    amount: Optional[float],
    type_id: Optional[int],
    type_name: Optional[str],
    category_id: Optional[int],
    category_name: Optional[str],
    description: Optional[str],
    payment_method: Optional[str],
    occurred_at: Optional[str],
) -> dict:
    return {
        "id": id,
        "match_text": match_text,
        "date_local": date_local,
        "amount": amount,
        "type_id": type_id,
        "type_name": type_name,
        "category_id": category_id,
        "category_name": category_name,
        "description": description,
        "payment_method": payment_method,
        "occurred_at": occurred_at,
    }


def _gravar_update_transaction(
    cur,
    id: Optional[int],
    match_text: Optional[str],
    date_local: Optional[str],
    amount: Optional[float],
    type_id: Optional[int],
    type_name: Optional[str],
    category_id: Optional[int],
    category_name: Optional[str],
    description: Optional[str],
    payment_method: Optional[str],
    occurred_at: Optional[str],
) -> dict:
    if not any([amount, type_id, type_name, category_id, category_name, description, payment_method, occurred_at]):
        return {"status": "erro", "message": "Nada para atualizar: forneça pelo menos um campo (amount, type, category, description, payment_method, occurred_at)."}

    target_id = id
    if target_id is None:
        if not match_text or not date_local:
            return {"status": "erro", "message": "Sem 'id': informe match_text E date_local para localizar o registro."}

        cur.execute(
            f"""
            SELECT t.id
            FROM transactions t
            WHERE (t.source_text ILIKE %s OR t.description ILIKE %s)
              AND {_local_date_filter_sql("t.occurred_at")}
            ORDER BY t.occurred_at DESC
            LIMIT 1;
            """,
            (f"%{match_text}%", f"%{match_text}%", date_local),
        )
        row = cur.fetchone()
        if not row:
            return {"status": "erro", "message": "Nenhuma transação encontrada para os filtros fornecidos."}
        target_id = row[0]

    resolved_type_id = _resolve_type_id(cur, type_id, type_name) if (type_id or type_name) else None
    resolved_category_id = category_id
    if category_name and not category_id:
        resolved_category_id = _resolve_category_id(cur, category_id=None, category_name=category_name)

    sets = []
    params: List[object] = []
    if amount is not None:
        sets.append("amount = %s")
        params.append(amount)
    if resolved_type_id is not None:
        sets.append("type = %s")
        params.append(resolved_type_id)
    if resolved_category_id is not None:
        sets.append("category_id = %s")
        params.append(resolved_category_id)
    if description is not None:
        sets.append("description = %s")
        params.append(description)
    if payment_method is not None:
        sets.append("payment_method = %s")
        params.append(payment_method)
    if occurred_at is not None:
        sets.append("occurred_at = %s::timestamptz")
        params.append(occurred_at)

    if not sets:
        return {"status": "erro", "message": "Nenhum campo válido para atualizar."}

    params.append(target_id)
    cur.execute(
        f"UPDATE transactions SET {', '.join(sets)} WHERE id = %s;",
        params,
    )
    rows_affected = cur.rowcount
    cur.execute(
        """
        SELECT
          t.id, t.occurred_at, t.amount, tt.type AS type_name,
          c.name AS category_name, t.description, t.payment_method, t.source_text
        FROM transactions t
        JOIN transaction_types tt ON tt.id = t.type
        LEFT JOIN categories c ON c.id = t.category_id
        WHERE t.id = %s;
        """,
        (target_id,),
    )
    r = cur.fetchone()
    updated = None
    if r:
        updated = {
            "id": r[0],
            "occurred_at": str(r[1]),
            "amount": float(r[2]),
            "type": r[3],
            "category": r[4],
            "description": r[5],
            "payment_method": r[6],
            "source_text": r[7],
        }
    return {
        "status": "ok",
        "rows_affected": rows_affected,
        "id": target_id,
        "updated": updated,
    }


@tool("update_transaction", args_schema=UpdateTransactionArgs)
def update_transaction(
    id: Optional[int] = None,
    match_text: Optional[str] = None,
    date_local: Optional[str] = None,
    amount: Optional[float] = None,
    type_id: Optional[int] = None,
    type_name: Optional[str] = None,
    category_id: Optional[int] = None,
    category_name: Optional[str] = None,
    description: Optional[str] = None,
    payment_method: Optional[str] = None,
    occurred_at: Optional[str] = None,
) -> dict:
    """
    Atualiza uma transação existente.
    Estratégias:
      - Se 'id' for informado: atualiza diretamente por ID.
      - Caso contrário: localiza a transação mais recente que combine (match_text em source_text/description)
        E (date_local em America/Sao_Paulo), então atualiza.
    Retorna: status, rows_affected, id, e o registro atualizado.
    """
    argumentos = _args_update_transaction(
        id,
        match_text,
        date_local,
        amount,
        type_id,
        type_name,
        category_id,
        category_name,
        description,
        payment_method,
        occurred_at,
    )
    return proteger_escrita(
        "update_transaction",
        argumentos,
        lambda cur: _gravar_update_transaction(
            cur,
            id,
            match_text,
            date_local,
            amount,
            type_id,
            type_name,
            category_id,
            category_name,
            description,
            payment_method,
            occurred_at,
        ),
    )

def _serializar_perfil(doc: dict | None) -> dict | None:
    if not doc:
        return None
    dados = dict(doc)
    _id = dados.get("_id")
    if isinstance(_id, ObjectId):
        dados["_id"] = str(_id)
    return dados


@tool("buscar_preferencias_usuario")
def buscar_preferencias_usuario(user_id: str) -> dict:
    """Busca as preferências do usuário no Qdrant  e retorna as informações do usuário. O AGENTE NUNCA DEVE INSERIR O PARAMETRO user_id, POIS ELE É INJETADO PELO CONTEXTO DA REQUISIÇÃO WEB NO BACKEND."""
    vetor = gerar_embedding(user_id)
    resultados = qdrant.query_points(
        collection_name=COLLECTION_PREFERENCIAS,
        query=vetor,
        query_filter=models.Filter(
            must=[
                models.FieldCondition(
                    key="user_id",
                    match=models.MatchValue(value=user_id),
                )
            ]
        ),
        limit=1,
    )
    if not resultados.points:
        return {"status": "ok", "preferencias": None}
    return {"status": "ok", "preferencias": resultados.points[0].payload}

@tool("buscar_perfil_usuario")
def buscar_perfil_usuario(user_id: str) -> dict:
    """Busca o perfil do usuário no MongoDB e retorna as informações do usuário. O AGENTE NUNCA DEVE INSERIR O PARAMETRO user_id, POIS ELE É INJETADO PELO CONTEXTO DA REQUISIÇÃO WEB NO BACKEND."""
    perfil = _serializar_perfil(col_perfil.find_one({"user_id": user_id}))
    if not perfil:
        return {"status": "ok", "perfil": None}
    return {"status": "ok", "perfil": perfil}



# Exporta a lista de tools
TOOLS = [list_categories, add_transaction, search_transactions, saldo_total, saldo_diario, update_transaction, buscar_preferencias_usuario,buscar_perfil_usuario]