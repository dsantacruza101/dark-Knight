keywords: postgres, postgresql, index, indexes, indice, explain, analyze, query, slow, btree, gin, performance
> 🌱 Nota semilla (2026-10-17, escrita a mano). El radar de Red Robin la complementa
> con notas `<paquete>-<version>.md` en su modo semanal; refrescar si cambia el major.

# PostgreSQL: indices y EXPLAIN

## Leer un plan
- `EXPLAIN (ANALYZE, BUFFERS) SELECT ...` ejecuta la query de verdad: en UPDATE/DELETE
  envolver en `BEGIN; ... ROLLBACK;`. Sin `ANALYZE` solo estima.
- Mirar: `Seq Scan` sobre tablas grandes con filtro selectivo = falta indice;
  `rows=` estimado vs real muy distinto = estadisticas viejas (`ANALYZE tabla`).
- `Index Only Scan` es lo mejor (todas las columnas en el indice); `Bitmap Heap Scan`
  es normal para filtros con muchas filas.
- `Sort` con `external merge` = `work_mem` corto o falta indice que de el orden.

## Que indice crear
- B-tree (default): igualdad, rangos, `ORDER BY`. Compuesto: columnas de igualdad
  primero, luego la de rango/orden: `(user_id, created_at DESC)`.
- Un compuesto `(a, b)` sirve para `WHERE a = ?` pero NO para `WHERE b = ?` solo.
- Parcial: `CREATE INDEX ... WHERE deleted_at IS NULL` si casi siempre se filtra asi.
- Cubriente: `CREATE INDEX ... (user_id) INCLUDE (status)` para Index Only Scan.
- GIN: `jsonb` (`@>`), arrays, full-text (`to_tsvector`); `pg_trgm` para `ILIKE '%x%'`.
- Expresion: `CREATE INDEX ON users (lower(email))` si la query usa `lower(email)`.
- Toda FK que se usa en JOIN o `ON DELETE CASCADE` necesita su indice (Postgres no lo crea).

## En produccion
- `CREATE INDEX CONCURRENTLY` (no bloquea escrituras); no corre dentro de una
  transaccion: en Prisma/TypeORM/Alembic va en una migracion propia sin transaccion.
- Si falla queda un indice `INVALID`: `DROP INDEX CONCURRENTLY` y reintentar.
- Indices de mas cuestan en cada INSERT/UPDATE: revisar los no usados con
  `pg_stat_user_indexes` (`idx_scan = 0`) antes de agregar otro.

## Queries
- Siempre parametrizadas (`$1`, `:param`, `?`): nunca f-strings ni concatenacion.
- Evitar funciones sobre la columna en el WHERE (`date(created_at) = ...`): usa rango.
- Paginacion por cursor (`WHERE id > $1 ORDER BY id LIMIT 50`) en vez de `OFFSET` grande.
