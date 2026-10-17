keywords: rag, pgvector, vector, embedding, embeddings, ollama, similarity, retrieval, chunk, hnsw, chat
> 🌱 Nota semilla (2026-10-17, escrita a mano). El radar de Red Robin la complementa
> con notas `<paquete>-<version>.md` en su modo semanal; refrescar si cambia el major.

# RAG con pgvector + embeddings de Ollama

## Embeddings con Ollama (localhost:11434)
- `POST /api/embed` con `{"model": "nomic-embed-text", "input": ["texto1", "texto2"]}`
  -> `{"embeddings": [[...], ...]}` (acepta lotes). `/api/embeddings` es el endpoint viejo.
- La dimension depende del modelo (nomic-embed-text 768, mxbai-embed-large 1024):
  la columna `vector(N)` debe coincidir; cambiar de modelo = re-embeber todo.
- nomic-embed-text usa prefijos: `search_document: ` al indexar, `search_query: ` al buscar.
- Guardar `embedding_model` junto a cada fila para saber que re-embeber.

## Esquema (en una migracion, nunca a mano)
```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE doc_chunks (
  id bigserial PRIMARY KEY, doc_id bigint NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
  content text NOT NULL, embedding vector(768) NOT NULL, embedding_model text NOT NULL);
CREATE INDEX ON doc_chunks (doc_id);
CREATE INDEX ON doc_chunks USING hnsw (embedding vector_cosine_ops);
```
- HNSW: mejor recall, sin entrenamiento; IVFFlat requiere datos antes de crear el indice.
- El operador de la query debe calzar con el opclass: `<=>` coseno, `<->` L2, `<#>` producto.

## Busqueda (parametrizada)
```sql
SELECT id, content, 1 - (embedding <=> $1::vector) AS score
FROM doc_chunks WHERE doc_id = ANY($2) ORDER BY embedding <=> $1::vector LIMIT 5;
```
- Python: `pgvector.psycopg.register_vector(conn)` y pasar `numpy`/lista como parametro.
- Filtros + HNSW: subir `SET hnsw.ef_search = 100` o usar `hnsw.iterative_scan`
  (pgvector >= 0.8) para no quedarse con menos de LIMIT filas.
- Verificar con `EXPLAIN` que usa el indice (sin `ORDER BY ... <=>` + `LIMIT` no lo usa).

## Pipeline
- Chunks de ~500-800 tokens con solape de ~10-15%; guardar fuente/titulo para citar.
- Re-embeber solo lo que cambio (hash del contenido por chunk).
- Prompt: contexto recuperado delimitado + "si no esta en el contexto, dilo"; tratar el
  contenido recuperado como datos, no instrucciones (prompt injection).
- Medir: set pequeno de preguntas con respuesta esperada antes/despues de cambiar chunking.
