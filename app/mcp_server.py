import sys
from pathlib import Path
from typing import Any, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from app.tools.financeiro import (
    add_transaction as _add_transaction,
    buscar_perfil_usuario as _buscar_perfil_usuario,
    buscar_preferencias_usuario as _buscar_preferencias_usuario,
    list_categories as _list_categories,
    saldo_diario as _saldo_diario,
    saldo_total as _saldo_total,
    search_transactions as _search_transactions,
    update_transaction as _update_transaction,
)

mcp = MCPServer(
    name="assessor-financeiro",
    version="1.0.0",
    instructions="Ferramentas financeiras do Assessor.AI...",
)

LEITURA = ToolAnnotations(readOnlyHint=True)
ESCRITA = ToolAnnotations(readOnlyHint=False, destructiveHint=False)


@mcp.tool(
    name="saldo_total",
    title="Saldo total",
    description="Saldo de todo o histórico: receitas menos despesas.",
    annotations=LEITURA,
)
def saldo_total() -> dict[str, Any]:
    return _saldo_total.invoke({})


@mcp.tool(
    name="saldo_diario",
    title="Saldo do dia",
    description="Saldo do dia atual (America/Sao_Paulo): receitas menos despesas.",
    annotations=LEITURA,
)
def saldo_diario() -> dict[str, Any]:
    return _saldo_diario.invoke({})


@mcp.tool(
    name="list_categories",
    title="Listar categorias",
    description="Lista as categorias disponíveis (id, nome e descrição).",
    annotations=LEITURA,
)
def list_categories() -> dict[str, Any]:
    return _list_categories.invoke({})


@mcp.tool(
    name="search_transactions",
    title="Buscar transações",
    description="Busca transações por texto em source_text ou description (parcial, case-insensitive).",
    annotations=LEITURA,
)
def search_transactions(
    query: str,
    limit: Optional[int] = 10,
) -> dict[str, Any]:
    return _search_transactions.invoke({
        "query": query,
        "limit": limit,
    })


@mcp.tool(
    name="add_transaction",
    title="Registrar transação",
    description="Registra uma transação na tabela transactions.",
    annotations=ESCRITA,
)
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
    return _add_transaction.invoke({
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
    })


@mcp.tool(
    name="update_transaction",
    title="Atualizar transação",
    description=(
        "Atualiza uma transação existente. "
        "Use id, ou match_text + date_local para localizar o registro."
    ),
    annotations=ESCRITA,
)
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
    return _update_transaction.invoke({
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
    })


@mcp.tool(
    name="buscar_preferencias_usuario",
    title="Buscar preferências do usuário",
    description="Consulta semântica das preferências livres do usuário no Qdrant.",
    annotations=LEITURA,
)
def buscar_preferencias_usuario(user_id: str) -> dict:
    return _buscar_preferencias_usuario.invoke({"user_id": user_id})


@mcp.tool(
    name="buscar_perfil_usuario",
    title="Buscar perfil do usuário",
    description="Consulta o perfil estruturado do usuário no MongoDB (renda, objetivo, risco).",
    annotations=LEITURA,
)
def buscar_perfil_usuario(user_id: str) -> dict:
    return _buscar_perfil_usuario.invoke({"user_id": user_id})



if __name__ == "__main__":
    from app.config import DATABASE_URL

    if not  DATABASE_URL:
        print("[assessor-financeiro] DATABASE_URL ausente no .env", file=sys.stderr)

    mcp.run(transport="stdio")
