# Justificativa de Implementação - Módulo de Perfil

# 1

o arquivo chat.py (app/routes/chat.py) com a rota post, o arquivo de tools do financeiro (app/tools/financeiro.py) para criação das 2 tools novas, o memoryMongo (que irei altear a nomeclatura para melhor compreensão) (app/memoryMongo.py) alterado para as funções de salvar perfil no mongo e salvar preferencia no qdrant (posteriormente tambem alterarei pois a busca está sendo feita na tool e não tem uma função própria pra isso) e o schemas (app/schemas.py) para criaç~~ao de um schema necessário do post

# 2 
O perfil entra pela requisição do frontend perfil, ele completo é gravado no mongo enquanto que ele com a preferenci como vetor é gravada no qdrant

# 3
São guardadas no Qdrant convertendo o texto em embeddings vetoriais.
É melhor por vetor para não gerar erro ao procurar de maneira diferente

# 4
2, uma para pegar as preferencias pelo qdrant e outra para pegar o perfil pelo mongo

# 5 

O argumento `user_id` não é preenchido pelo LLM, ele é expliitamente dito ue sera entregue pelo backend

# 6 
Consulta o banco diretamente, achei que seria mais pratico

# 7
Porque perfil de usuário é um dado contextual e de apoio.

# 8 

porque não é pedido e ambos fazem parte de contextos diferentes (cadastro e perfil, por enquanto).