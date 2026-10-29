"""Prova da interceptação: recusa sem efeito, aprovação única e falha fechada.

O banco é simulado. Nenhum teste chama modelo, AwesomeAPI, Google ou Postgres real.
"""

from __future__ import annotations

import copy
from unittest.mock import patch

import pytest

from app.juiz_escrita import (
    VereditoEscrita,
    chave_operacao,
    contexto_atual,
    definir_contexto,
    hash_argumentos,
    limpar_contexto,
    preencher_contexto_da_chamada,
    reiniciar_esquema_idempotencia,
)
from app.observabilidade import (
    abrir_turno,
    anotar_status_escrita,
    fechar_turno,
    limpar_status_escrita,
    listar_turnos,
    registrar_chamada_juiz,
    status_do_estado,
)
from app.tools.agenda import _gravar_add_event, add_event, query_events
from app.tools.financeiro import (
    _args_add_transaction,
    _args_update_transaction,
    _gravar_add_transaction,
    _gravar_update_transaction,
    add_transaction,
    update_transaction,
)

# Marcadores fixos: um por cenário e tool. O UUID é estável para a decisão técnica.
M_RECUSA_TX = "JT-RECUSA-add_transaction-11111111-1111-4111-8111-111111111111"
M_RECUSA_UP = "JT-RECUSA-update_transaction-22222222-2222-4222-8222-222222222222"
M_RECUSA_EV = "JT-RECUSA-add_event-33333333-3333-4333-8333-333333333333"
M_OK_TX = "JT-APROVACAO-add_transaction-44444444-4444-4444-8444-444444444444"
M_OK_TX_B = "JT-APROVACAO-add_transaction-44444444-4444-4444-8444-444444444445"
M_OK_UP = "JT-APROVACAO-update_transaction-55555555-5555-4555-8555-555555555555"
M_OK_EV = "JT-APROVACAO-add_event-66666666-6666-4666-8666-666666666666"
M_ERRO_TX = "JT-ERRO-add_transaction-77777777-7777-4777-8777-777777777777"
M_ERRO_UP = "JT-ERRO-update_transaction-88888888-8888-4888-8888-888888888888"
M_ERRO_EV = "JT-ERRO-add_event-99999999-9999-4999-8999-999999999999"
M_PG_TX = "JT-ERRO-postgres-add_transaction-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

APROVA = VereditoEscrita(decision="approve", reason="pedido explicito e campos coerentes")
NEGA = VereditoEscrita(decision="deny", reason="operacao nao autorizada pela instrucao")


class BancoSimulado:
    def __init__(self):
        self.idempotencia: dict = {}
        self.transactions: list = []
        self.events: list = []
        self.ddl_idempotencia = 0
        self.falhar_insert_tx = False


class ConexaoSimulada:
    def __init__(self, banco: BancoSimulado):
        self.banco = banco
        self.idem = copy.deepcopy(banco.idempotencia)
        self.transactions = copy.deepcopy(banco.transactions)
        self.events = copy.deepcopy(banco.events)
        self.fechada = False

    def cursor(self):
        return CursorSimulado(self)

    def commit(self):
        self.banco.idempotencia = copy.deepcopy(self.idem)
        self.banco.transactions = copy.deepcopy(self.transactions)
        self.banco.events = copy.deepcopy(self.events)

    def rollback(self):
        self.idem = copy.deepcopy(self.banco.idempotencia)
        self.transactions = copy.deepcopy(self.banco.transactions)
        self.events = copy.deepcopy(self.banco.events)

    def close(self):
        self.fechada = True


class CursorSimulado:
    def __init__(self, conn: ConexaoSimulada):
        self.conn = conn
        self._fetchone = None
        self._fetchall: list = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        texto = " ".join(sql.split())
        params = params or ()
        self._fetchone = None
        self._fetchall = []
        self.rowcount = 0
        banco = self.conn.banco

        if "CREATE TABLE IF NOT EXISTS escrita_idempotencia" in texto:
            banco.ddl_idempotencia += 1
            return
        if "CREATE TABLE IF NOT EXISTS events" in texto:
            return
        if "UPDATE escrita_idempotencia SET resultado" in texto:
            self.conn.idem[params[1]]["resultado"] = params[0]
            self.rowcount = 1
            return
        if "INSERT INTO escrita_idempotencia" in texto:
            from app.juiz_escrita import ChaveDuplicada

            if params[0] in self.conn.idem:
                raise ChaveDuplicada(params[0])
            self.conn.idem[params[0]] = {
                "tool_name": params[1],
                "args_hash": params[2],
                "resultado": params[3],
            }
            return
        if "FROM escrita_idempotencia WHERE chave" in texto:
            row = self.conn.idem.get(params[0])
            self._fetchone = None if row is None else (row["args_hash"], row["resultado"])
            return
        if "AS type_name" in texto:
            alvo = params[0]
            row = next((item for item in self.conn.transactions if item["id"] == alvo), None)
            if row is None:
                self._fetchone = None
                return
            tipos = {1: "INCOME", 2: "EXPENSES", 3: "TRANSFER"}
            categorias = {1: "comida", 2: "outros"}
            self._fetchone = (
                row["id"],
                row["occurred_at"],
                row["amount"],
                tipos.get(row["type"]),
                categorias.get(row["category_id"]),
                row["description"],
                row["payment_method"],
                row["source_text"],
            )
            return
        if "transaction_types" in texto:
            nome = params[0]
            achou = {"INCOME": 1, "EXPENSES": 2, "TRANSFER": 3}.get(nome)
            self._fetchone = None if achou is None else (achou,)
            return
        if "FROM categories" in texto:
            self._fetchall = [(1, "comida"), (2, "outros")]
            return
        if "INSERT INTO transactions" in texto:
            if banco.falhar_insert_tx:
                raise RuntimeError("falha simulada do postgres")
            if len(params) == 7:
                amount, type_id, category_id, description, payment_method, occurred_at, source_text = params
            else:
                amount, type_id, category_id, description, payment_method, source_text = params
                occurred_at = "now"
            novo_id = max((item["id"] for item in self.conn.transactions), default=0) + 1
            self.conn.transactions.append({
                "id": novo_id,
                "amount": amount,
                "type": type_id,
                "category_id": category_id,
                "description": description,
                "payment_method": payment_method,
                "occurred_at": occurred_at,
                "source_text": source_text,
            })
            self._fetchone = (novo_id, occurred_at)
            return
        if "UPDATE transactions SET" in texto:
            conjunto = texto.split("SET", 1)[1].split("WHERE", 1)[0]
            colunas = [parte.split("=")[0].strip() for parte in conjunto.split(",") if parte.strip()]
            alvo = params[-1]
            row = next((item for item in self.conn.transactions if item["id"] == alvo), None)
            if row is None:
                self.rowcount = 0
                return
            for coluna, valor in zip(colunas, params[:-1]):
                row[coluna] = valor
            self.rowcount = 1
            return
        if "INSERT INTO events" in texto:
            title, start_time, end_time, location, notes, source_text = params
            novo_id = max((item["id"] for item in self.conn.events), default=0) + 1
            self.conn.events.append({
                "id": novo_id,
                "title": title,
                "start_time": start_time,
                "end_time": end_time,
                "location": location,
                "notes": notes,
                "source_text": source_text,
            })
            self._fetchone = (novo_id, start_time, end_time)
            return
        if "LIMIT 1" in texto and "FROM transactions" in texto:
            self._fetchone = None
            return
        if "FROM events" in texto:
            self._fetchall = []
            return

    def fetchone(self):
        return self._fetchone

    def fetchall(self):
        return self._fetchall

    def close(self):
        return None


@pytest.fixture(autouse=True)
def _isola_turno():
    reiniciar_esquema_idempotencia()
    limpar_status_escrita()
    limpar_contexto()
    yield
    limpar_status_escrita()
    limpar_contexto()


@pytest.fixture
def banco():
    simulado = BancoSimulado()

    def fabrica():
        return ConexaoSimulada(simulado)

    with (
        patch("app.tools.db.get_conn", side_effect=fabrica),
        patch("app.tools.agenda.get_conn", side_effect=fabrica),
        patch("app.tools.financeiro.get_conn", side_effect=fabrica),
    ):
        yield simulado


def _tx(marker: str, amount: float = 12.5) -> dict:
    return {
        "amount": amount,
        "source_text": marker,
        "type_name": "EXPENSES",
        "category_name": "comida",
        "currency": "BRL",
        "description": "teste",
        "payment_method": "pix",
    }


def _evento(marker: str) -> dict:
    return {
        "title": marker,
        "source_text": marker,
        "start_time": "2026-10-29T14:00:00-03:00",
        "location": "Sala 1",
    }


def test_chave_distingue_duas_escritas_no_mesmo_turno():
    base = _args_add_transaction(12.5, M_OK_TX, None, None, "EXPENSES", "comida", None, "teste", "pix", "BRL")
    outra = dict(base)
    outra["source_text"] = M_OK_TX_B
    assert chave_operacao("sessao", "add_transaction", base) == chave_operacao("sessao", "add_transaction", base)
    assert chave_operacao("sessao", "add_transaction", base) != chave_operacao("sessao", "add_transaction", outra)
    assert chave_operacao("sessao-a", "add_transaction", base) != chave_operacao("sessao-b", "add_transaction", base)
    assert hash_argumentos(base) != hash_argumentos(outra)


def test_contexto_do_turno_nao_e_apagado_pelo_agente():
    definir_contexto("sessao-grafo", "registre o almoco de 12 reais")
    preencher_contexto_da_chamada({
        "messages": [{"role": "human", "content": "texto anonimizado"}],
        "session_id": "outra",
    })
    assert contexto_atual() == ("sessao-grafo", "registre o almoco de 12 reais")


def test_contexto_preenchido_quando_o_turno_nao_definiu():
    preencher_contexto_da_chamada({
        "messages": [{"role": "human", "content": "marca reuniao amanha"}],
        "session_id": "s-agenda",
    })
    assert contexto_atual() == ("s-agenda", "marca reuniao amanha")


def test_guardrail_de_entrada_nao_conta_como_erro():
    assert status_do_estado({"agentes_chamados": ["guardrail_entrada:conteudo"]}) == "bloqueado"
    assert status_do_estado({"agentes_chamados": ["guardrail_entrada:aprovado"]}) == "ok"
    anotar_status_escrita("erro")
    assert status_do_estado({"agentes_chamados": ["guardrail_entrada:conteudo"]}) == "erro"


def test_recusa_add_transaction_sem_efeito(banco):
    definir_contexto("sessao-recusa-tx", "nao registre nada")
    with (
        patch("app.juiz_escrita.julgar", return_value=NEGA),
        patch("app.tools.financeiro._gravar_add_transaction") as gravar,
    ):
        resposta = add_transaction.invoke(_tx(M_RECUSA_TX))
    assert resposta["status"] == "bloqueado"
    assert "A gravação não foi feita." in resposta["message"]
    assert gravar.call_count == 0
    assert banco.transactions == []
    assert banco.idempotencia == {}
    assert status_do_estado({}) == "bloqueado"


def test_recusa_update_transaction_deixa_o_registro(banco):
    banco.transactions.append({
        "id": 41,
        "amount": 10.0,
        "type": 2,
        "category_id": 1,
        "description": "antes",
        "payment_method": "pix",
        "occurred_at": "2026-10-29T10:00:00-03:00",
        "source_text": M_RECUSA_UP,
    })
    definir_contexto("sessao-recusa-up", "nao altere a transacao")
    with (
        patch("app.juiz_escrita.julgar", return_value=NEGA),
        patch("app.tools.financeiro._gravar_update_transaction") as gravar,
    ):
        resposta = update_transaction.invoke({"id": 41, "amount": 99.0})
    assert resposta["status"] == "bloqueado"
    assert gravar.call_count == 0
    assert banco.transactions[0]["amount"] == 10.0
    assert banco.transactions[0]["source_text"] == M_RECUSA_UP
    assert banco.transactions[0]["description"] == "antes"
    assert status_do_estado({}) == "bloqueado"


def test_recusa_add_event_sem_efeito(banco):
    definir_contexto("sessao-recusa-ev", "nao marque o compromisso")
    with (
        patch("app.juiz_escrita.julgar", return_value=NEGA),
        patch("app.tools.agenda._gravar_add_event") as gravar,
    ):
        resposta = add_event.invoke(_evento(M_RECUSA_EV))
    assert resposta["status"] == "bloqueado"
    assert "A gravação não foi feita." in resposta["message"]
    assert gravar.call_count == 0
    assert banco.events == []
    assert status_do_estado({}) == "bloqueado"


def test_aprovacao_add_transaction_uma_vez_e_repeticao(banco):
    definir_contexto("sessao-ok-tx", "registre a despesa de 12,50 em comida")
    with (
        patch("app.juiz_escrita.julgar", return_value=APROVA),
        patch("app.tools.financeiro._gravar_add_transaction", wraps=_gravar_add_transaction) as gravar,
    ):
        primeira = add_transaction.invoke(_tx(M_OK_TX))
        repetida = add_transaction.invoke(_tx(M_OK_TX))
        outra = add_transaction.invoke(_tx(M_OK_TX_B, amount=8.0))
    assert primeira["status"] == "ok"
    assert repetida["id"] == primeira["id"]
    assert repetida["amount"] == primeira["amount"]
    assert outra["id"] != primeira["id"]
    assert gravar.call_count == 2
    assert len(banco.transactions) == 2
    assert [item["source_text"] for item in banco.transactions] == [M_OK_TX, M_OK_TX_B]
    assert len(banco.idempotencia) == 2
    assert banco.ddl_idempotencia == 1
    assert status_do_estado({}) == "ok"


def test_aprovacao_update_transaction_uma_vez(banco):
    banco.transactions.append({
        "id": 41,
        "amount": 10.0,
        "type": 2,
        "category_id": 1,
        "description": "antes",
        "payment_method": "pix",
        "occurred_at": "2026-10-29T10:00:00-03:00",
        "source_text": M_OK_UP,
    })
    definir_contexto("sessao-ok-up", "atualize o valor da transacao para 80")
    with (
        patch("app.juiz_escrita.julgar", return_value=APROVA),
        patch("app.tools.financeiro._gravar_update_transaction", wraps=_gravar_update_transaction) as gravar,
    ):
        primeira = update_transaction.invoke({"id": 41, "amount": 80.0, "description": M_OK_UP})
        repetida = update_transaction.invoke({"id": 41, "amount": 80.0, "description": M_OK_UP})
    assert primeira["status"] == "ok"
    assert primeira["rows_affected"] == 1
    assert repetida["id"] == primeira["id"]
    assert repetida["updated"]["amount"] == 80.0
    assert gravar.call_count == 1
    assert banco.transactions[0]["amount"] == 80.0
    assert banco.transactions[0]["description"] == M_OK_UP
    assert banco.transactions[0]["source_text"] == M_OK_UP
    assert len(banco.idempotencia) == 1
    assert status_do_estado({}) == "ok"


def test_aprovacao_add_event_uma_vez(banco):
    definir_contexto("sessao-ok-ev", "marque reuniao amanha as 14h na sala 1")
    with (
        patch("app.juiz_escrita.julgar", return_value=APROVA),
        patch("app.tools.agenda._gravar_add_event", wraps=_gravar_add_event) as gravar,
    ):
        primeira = add_event.invoke(_evento(M_OK_EV))
        repetida = add_event.invoke(_evento(M_OK_EV))
    assert primeira["status"] == "ok"
    assert repetida["id"] == primeira["id"]
    assert repetida["title"] == M_OK_EV
    assert gravar.call_count == 1
    assert len(banco.events) == 1
    assert banco.events[0]["title"] == M_OK_EV
    assert len(banco.idempotencia) == 1
    assert status_do_estado({}) == "ok"


@pytest.mark.parametrize(
    ("ferramenta", "payload", "efeito", "mensagem"),
    [
        ("add_transaction", "_tx", TimeoutError("timeout do juiz"), M_ERRO_TX),
        ("update_transaction", "_up", "invalido", M_ERRO_UP),
        ("add_event", "_ev", RuntimeError("sem resposta"), M_ERRO_EV),
    ],
)
def test_falha_do_juiz_nao_grava(banco, ferramenta, payload, efeito, mensagem):
    if payload == "_tx":
        argumentos = _tx(mensagem)
        tool = add_transaction
        gravar_caminho = "app.tools.financeiro._gravar_add_transaction"
        colecao = banco.transactions
    elif payload == "_up":
        banco.transactions.append({
            "id": 41,
            "amount": 10.0,
            "type": 2,
            "category_id": 1,
            "description": "antes",
            "payment_method": "pix",
            "occurred_at": "2026-10-29T10:00:00-03:00",
            "source_text": mensagem,
        })
        argumentos = {"id": 41, "amount": 80.0}
        tool = update_transaction
        gravar_caminho = "app.tools.financeiro._gravar_update_transaction"
        colecao = banco.transactions
    else:
        argumentos = _evento(mensagem)
        tool = add_event
        gravar_caminho = "app.tools.agenda._gravar_add_event"
        colecao = banco.events

    if efeito == "invalido":
        juiz = patch("app.juiz_escrita.julgar", return_value={"decision": "talvez", "reason": "texto livre"})
    else:
        juiz = patch("app.juiz_escrita.julgar", side_effect=efeito)

    definir_contexto(f"sessao-erro-{ferramenta}", "tente a escrita")
    antes = copy.deepcopy(colecao)
    with juiz, patch(gravar_caminho) as gravar:
        resposta = tool.invoke(argumentos)
    assert resposta["status"] == "erro"
    assert "não foi feita" in resposta["message"] or "indisponível" in resposta["message"] or "inválido" in resposta["message"]
    assert gravar.call_count == 0
    assert colecao == antes
    assert status_do_estado({}) == "erro"


def test_falha_de_postgres_faz_rollback(banco):
    banco.falhar_insert_tx = True
    definir_contexto("sessao-pg", "registre a despesa")
    with (
        patch("app.juiz_escrita.julgar", return_value=APROVA),
        patch("app.tools.financeiro._gravar_add_transaction", wraps=_gravar_add_transaction) as gravar,
    ):
        resposta = add_transaction.invoke(_tx(M_PG_TX))
    assert resposta["status"] == "erro"
    assert "falha simulada do postgres" in resposta["message"]
    assert gravar.call_count == 1
    assert banco.transactions == []
    assert banco.idempotencia == {}
    assert all(M_PG_TX not in str(item) for item in banco.transactions)
    assert status_do_estado({}) == "erro"


def test_chave_com_argumentos_diferentes_nao_executa(banco):
    args = _args_add_transaction(12.5, M_OK_TX, None, None, "EXPENSES", "comida", None, "teste", "pix", "BRL")
    chave = chave_operacao("sessao-conflito", "add_transaction", args)
    banco.idempotencia[chave] = {
        "tool_name": "add_transaction",
        "args_hash": "hash-diferente",
        "resultado": '{"status": "ok", "id": 1}',
    }
    definir_contexto("sessao-conflito", "registre a despesa")
    with (
        patch("app.juiz_escrita.julgar", return_value=APROVA),
        patch("app.tools.financeiro._gravar_add_transaction") as gravar,
    ):
        resposta = add_transaction.invoke(_tx(M_OK_TX))
    assert resposta["status"] == "erro"
    assert "argumentos diferentes" in resposta["message"]
    assert gravar.call_count == 0
    assert banco.transactions == []


def test_leitura_nao_chama_o_juiz(banco):
    with patch("app.juiz_escrita.julgar") as juiz:
        resposta = query_events.invoke({"limit": 5})
    assert juiz.call_count == 0
    assert resposta["status"] == "ok"
    assert banco.events == []
    assert status_do_estado({}) == "ok"


def test_recusa_nao_entra_na_taxa_de_erro_e_falha_entra():
    bloqueado = abrir_turno()
    anotar_status_escrita("bloqueado")
    fechar_turno(bloqueado, "taxa-bloqueado", "financeiro", status_do_estado({}))
    falha = abrir_turno()
    anotar_status_escrita("erro")
    fechar_turno(falha, "taxa-erro", "financeiro", status_do_estado({}))
    turnos = [item for item in listar_turnos() if item["session_id"] in {"taxa-bloqueado", "taxa-erro"}]
    assert [item["status"] for item in turnos] == ["bloqueado", "erro"]
    erros = [item for item in turnos if item["status"] == "erro"]
    assert len(erros) / len(turnos) == 0.5
    assert all(item["status"] != "erro" for item in turnos if item["session_id"] == "taxa-bloqueado")


def test_custo_desconhecido_nao_vira_zero():
    turno = abrir_turno()
    registrar_chamada_juiz("modelo-sem-tabela", None, None, 15)
    fechar_turno(turno, "custo-desconhecido", "financeiro", "erro")
    registro = next(item for item in listar_turnos() if item["session_id"] == "custo-desconhecido")
    assert registro["custo_indisponivel"] is True
    assert registro["custo_usd"] is None
    assert registro["tokens_total"] is None

    medido = abrir_turno()
    registrar_chamada_juiz("gemini-2.5-flash", 1000, 100, 20)
    fechar_turno(medido, "custo-medido", "financeiro", "ok")
    registro_medido = next(item for item in listar_turnos() if item["session_id"] == "custo-medido")
    assert registro_medido["custo_indisponivel"] is False
    assert registro_medido["custo_usd"] == pytest.approx(0.00055)
    assert registro_medido["tokens_total"] == 1100


def test_args_de_update_batem_com_a_chamada():
    args = _args_update_transaction(41, None, None, 80.0, None, None, None, None, M_OK_UP, None, None)
    assert args["id"] == 41
    assert args["amount"] == 80.0
    assert "update_transaction" not in args
