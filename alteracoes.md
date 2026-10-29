# Alterações

Interceptação do juiz antes de `add_transaction`, `update_transaction` e `add_event`. O juiz de fidelidade do fim do grafo permanece.

## Arquivos criados

- `app/juiz_escrita.py`: juiz de autorização com veredito `approve|deny`, timeout de 8 s e falha fechada. Também gera a chave idempotente e grava essa chave, a escrita de negócio e o resultado na mesma transação do PostgreSQL.
- `tests/test_juiz_escrita.py`: testes com banco e juiz simulados. Cada cenário usa um marcador fixo. Não chama modelo, AwesomeAPI, Google nem Postgres de produção.
- `desisao_tecnica.md`: decisão, alternativas, efeito nas métricas, limites e evidências.

## Arquivos modificados

- `app/tools/financeiro.py`: `add_transaction` e `update_transaction` passam pelo juiz antes do SQL. O `INSERT`/`UPDATE` usa o cursor da transação idempotente e não dá `commit` sozinho. Consultas financeiras não mudaram.
- `app/tools/agenda.py`: `add_event` entrou na mesma barreira. `query_events` continua sem juiz.
- `app/graph.py`: os agentes financeiro e de agenda recebem um middleware que completa sessão e instrução quando o turno ainda não definiu. `executar_fluxo_assessor` grava a instrução original e a sessão antes do grafo. O nó `no_juiz` não foi movido.
- `app/observabilidade.py`: o turno passa a registrar `erro` ou `bloqueado` vindo da escrita. A chamada do juiz entra na coleta do turno. Sem tokens ou sem preço na tabela, o custo fica desconhecido em vez de zero.
- `app/main.py`: na subida da API, tenta criar a tabela `escrita_idempotencia` uma vez. Se o banco estiver fora, a API continua e a primeira escrita tenta de novo.

## O que não mudou

Rotas, schemas, `app/juiz.py`, `add_google_event`, `salvar_mensagem`, tools de leitura e `app/routes/monitor.py`. Esse router continua sem `GET /monitor`.
