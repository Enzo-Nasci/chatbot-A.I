# Decisão técnica

## 1. A decisão tomada, e por quê

A fronteira que autoriza a escrita é o wrapper em `proteger_escrita`, chamado por `add_transaction`, `update_transaction` e `add_event` antes de qualquer SQL de negócio. O agente pode propor a chamada. A função que grava só recebe o cursor se o veredito for `approve`.

Cabe neste grafo porque o efeito nasce dentro da tool, no `get_conn()` do PostgreSQL, e não no nó final. `app/juiz.py` continua no fim e só avalia a resposta. Ele não desfaz uma linha já confirmada.

Garantia nas três tools: recusa e falha do juiz não chamam a gravação; aprovação executa uma vez e a repetição da mesma chave devolve o resultado já gravado.

### Antes

1. Mensagem do usuário.
2. Guardrail de entrada e roteador.
3. Agente financeiro ou de agenda chama a tool.
4. A tool abre o PostgreSQL e pode gravar na hora.
5. Orquestrador, guardrail de saída e juiz de fidelidade.
6. `salvar_mensagem` grava a conversa no MongoDB.

### Depois

1. Mensagem do usuário.
2. Guardrail de entrada e roteador, com o mesmo contrato de API.
3. O agente propõe a tool e os argumentos.
4. O interceptador lê a instrução do turno, o nome da tool, os argumentos e a sessão.
5. O juiz de escrita devolve `approve` ou `deny`. Sem veredito válido, a escrita não corre.
6. Se aprovado, a mesma transação insere a chave, executa o `INSERT`/`UPDATE` e guarda o resultado.
7. Orquestrador, guardrail de saída, juiz de fidelidade e `salvar_mensagem` seguem como estavam.

O middleware `ContextoEscritaMiddleware` nos agentes financeiro e de agenda só preenche sessão e instrução quando o turno ainda não as definiu. A decisão não está no prompt do agente. LangChain 1.4.7 expõe `create_agent(..., middleware=)` e `AgentMiddleware.wrap_tool_call`. Foi isso que ficou ligado. A barreira em si está na função da tool, então o `.invoke()` do agente, do MCP e dos testes passa pelo mesmo ponto.

## 2. As decisões que o grupo não tomou, e por quê

### Só instruir o prompt do agente

O prompt diria para não chamar a tool sem certeza. A vantagem é não acrescentar uma chamada de modelo nem a espera do veredito. O custo é que o modelo ainda pode emitir `add_transaction`, `update_transaction` ou `add_event`, e o PostgreSQL gravaria. Não há fronteira técnica. Ficou de fora porque a prática pede impedir o efeito, não apenas pedir boa conduta.

### Julgar depois da execução, reusando `no_juiz`

O juiz de fidelidade já roda depois das tools. A vantagem é não somar espera antes da gravação. O custo é que a transação ou o evento já pode estar confirmado quando o veredito chega. Corrigir a frase final não apaga a linha. Ficou de fora como proteção da mutação. O nó continua no fim, com a função de checar a resposta, não de autorizar a escrita.

### Idempotência só em memória

Um `set` no processo reconheceria a repetição até o processo cair. A vantagem é ser simples e rápido. O custo é que, depois de um restart, a mesma operação gravaria de novo. A chave ficou na tabela `escrita_idempotencia`, com `UNIQUE` em `chave`.

## 3. O efeito nas métricas do SRE, e como

Não houve medição de p95 com o modelo real nem amostra de tráfego. Os números abaixo são o mecanismo e o que o teste simulado mostrou. Não são um SLO medido em produção.

### Interceptação, a opção tomada

- Latência e p95 de 8 s: cada tentativa de escrita espera o juiz antes do banco. O timeout está em 8 s (`TIMEOUT_JUIZ_S`). Essa espera entra no tempo do turno porque acontece dentro do `invoke`. Uma resposta lenta pode, sozinha, encostar ou passar o limite de 8 s. Leitura e turnos sem essas três tools não pagam essa chamada. Não foi medido um p95 antes/depois com Gemini.
- Taxa de erro de 5%: timeout, exceção, veredito inválido e falha de banco marcam `erro` e entram no numerador. Recusa explícita marca `bloqueado` e fica de fora. O bloqueio do guardrail de entrada continua `bloqueado` quando o estado traz `guardrail_entrada:` diferente de `aprovado`, e não vira `erro`. No teste com um turno bloqueado e um turno com erro, a taxa foi 0,5 nesse par. Isso prova a conta, não o valor operacional. Não há denominador de produção para dizer se o SLO de 5% estourou.
- Status: aprovação com gravação concluída deixa o turno em `ok`. Recusa deixa `bloqueado`. Juiz indisponível, veredito inválido ou exceção do banco deixam `erro`.
- Tokens e custo: a chamada do juiz é registrada no turno aberto. O modelo previsto é `gemini-2.5-flash`, que está na tabela de `app/observabilidade.py` (0,30 / 2,50 dólares por milhão de tokens). Sem tokens devolvidos ou sem preço para o modelo, `custo_usd` fica `None` e `custo_indisponivel` fica verdadeiro. O teste com 1000 tokens de entrada e 100 de saída aplicou a tabela e obteve 0,00055 dólar. Isso não é uma fatura do provedor: o teste não chamou o Gemini. O juiz real, portanto, não teve uso medido nesta execução.

`GET /monitor` não existe em `app/routes/monitor.py`. A validação foi nas funções `status_do_estado`, `fechar_turno` e `listar_turnos`.

### Alternativa descartada: só o prompt

Não haveria a chamada extra, então latência, p95, tokens e custo do juiz não subiriam por essa causa. A taxa de erro também não veria timeout de juiz, porque o juiz não existiria. Uma escrita indevida ainda poderia terminar `ok`, com a linha gravada. O status `bloqueado` dessa prática não apareceria.

### Alternativa descartada: juiz só no fim

Não soma a espera antes da tool, então o p95 não paga esses 8 s de autorização. Tokens e custo do juiz de escrita também não entram. A escrita confirmada permanece mesmo se a resposta final for corrigida. O turno pode ficar `ok` com efeito que ninguém autorizou antes do `commit`.

## 4. As outras decisões

### O que ficou de fora

`add_google_event` continua em `app/tools/calendario_google.py`, fora do wrapper. Recusar `add_event` não prova que o Google Calendar ficou intacto. Este interceptador não protege a agenda externa.

`salvar_mensagem` continua depois do turno, no MongoDB. Uma recusa de negócio ainda pode gravar a conversa. "Sem efeito" aqui é sem `INSERT`/`UPDATE` na transação ou no evento local.

O servidor MCP chama as mesmas funções financeiras, então também passa pelo wrapper. Ele não percorre o grafo. Sem sessão e sem instrução no contexto da thread, o juiz recebe instrução vazia e o prompt manda recusar. Isso não foi exercitado com o processo MCP no ar.

### Juiz indisponível

Sem credencial, timeout, exceção ou veredito fora de `approve|deny` com `reason`, a tool original não roda. O retorno é `status: erro`. Não há segunda tentativa automática da escrita.

### Queda do processo

A chave, o efeito e o resultado estão na mesma transação. Se o processo cai antes do `commit`, o PostgreSQL desfaz os dois. Uma nova tentativa pode executar. Se cai depois do `commit` e antes da resposta, a repetição da mesma chave lê o resultado gravado e não insere de novo. Não há atomicidade com MongoDB nem com o Google Calendar.

### Repetição e idempotência

A chave é o SHA-256 da sessão, do nome da tool e do JSON canônico dos argumentos. Duas escritas diferentes na mesma sessão geram chaves diferentes. O hash dos argumentos também fica na linha. A mesma chave com hash diferente devolve `erro` e não executa. A tabela é criada uma vez por processo, com flag, e de novo na subida da API. Nesta sessão a tabela não foi aplicada num Postgres ao vivo: os testes usaram banco simulado. O teste de aprovação contou um único `CREATE` da tabela de controle para a repetição e para uma segunda escrita diferente.

### Evidências com marcador

Banco simulado. A função de gravação foi espionada. Recusa: a função não rodou e a coleção simulada não mudou. Aprovação: uma execução da função, um efeito, e a repetição devolveu o mesmo id.

Recusa sem efeito:

- `JT-RECUSA-add_transaction-11111111-1111-4111-8111-111111111111`: nenhuma transação, status `bloqueado`.
- `JT-RECUSA-update_transaction-22222222-2222-4222-8222-222222222222`: o registro de teste permaneceu com amount 10 e a mesma `source_text`, status `bloqueado`.
- `JT-RECUSA-add_event-33333333-3333-4333-8333-333333333333`: nenhum evento, status `bloqueado`.

Aprovação uma vez e repetição da mesma chave:

- `JT-APROVACAO-add_transaction-44444444-4444-4444-8444-444444444444`: uma linha e o mesmo id na repetição. A escrita diferente `JT-APROVACAO-add_transaction-44444444-4444-4444-8444-444444444445` criou uma segunda linha.
- `JT-APROVACAO-update_transaction-55555555-5555-4555-8555-555555555555`: o amount foi para 80 uma vez; a repetição devolveu o mesmo id e o mesmo amount.
- `JT-APROVACAO-add_event-66666666-6666-4666-8666-666666666666`: um evento; a repetição devolveu o mesmo id.

Falha do juiz, sem linha nova:

- `JT-ERRO-add_transaction-77777777-7777-4777-8777-777777777777`: timeout, status `erro`.
- `JT-ERRO-update_transaction-88888888-8888-4888-8888-888888888888`: veredito `talvez`, registro original intocado, status `erro`.
- `JT-ERRO-add_event-99999999-9999-4999-8999-999999999999`: exceção do juiz, nenhum evento, status `erro`.

`JT-ERRO-postgres-add_transaction-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa`: o `INSERT` simulado levantou erro, a transação fez rollback e a chave idempotente também não ficou gravada. Status `erro`.

`query_events` foi chamada com o juiz espionado e o espião não rodou.

Comando: `python -m pytest -q tests/test_juiz_escrita.py`. Resultado: 19 passed em 35,60 s. Houve avisos já existentes do LangSmith e do parâmetro `top_p` em `app/llm.py`. `python -m compileall -q app` concluiu sem erro. Não rodei `uvicorn` nem `POST /chat`: isso usaria credenciais e serviços reais. No Windows, importar `zoneinfo` exigiu o pacote `tzdata` no ambiente de teste. Ele não foi adicionado ao repositório, que não tem `requirements.txt`.

## 5. Arquivos criados e arquivos modificados

### Criados

- `app/juiz_escrita.py`
- `tests/test_juiz_escrita.py`
- `alteracoes.md`
- `desisao_tecnica.md`

### Modificados

- `app/tools/financeiro.py`: as duas escritas passaram a depender do juiz e da transação idempotente.
- `app/tools/agenda.py`: `add_event` passou a depender do mesmo mecanismo.
- `app/graph.py`: contexto do turno e middleware só nos agentes que registram essas tools.
- `app/observabilidade.py`: status `erro`/`bloqueado` da escrita e custo desconhecido do juiz.
- `app/main.py`: criação única da tabela de idempotência na subida, sem derrubar a API se o banco falhar.
