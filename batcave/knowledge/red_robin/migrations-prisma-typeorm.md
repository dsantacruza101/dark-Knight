keywords: migration, migrations, migracion, prisma, typeorm, alembic, schema, column, rollback, deploy
> 🌱 Nota semilla (2026-10-17, escrita a mano). El radar de Red Robin la complementa
> con notas `<paquete>-<version>.md` en su modo semanal; refrescar si cambia el major.

# Migraciones seguras (Prisma, TypeORM, Alembic)

## Reglas
- Ningun cambio de esquema ni de datos fuera de una migracion versionada.
- Toda migracion tiene vuelta atras: `down` (TypeORM), `downgrade` (Alembic); en Prisma
  (sin down) se documenta el SQL inverso en el PR o se genera con `prisma migrate diff`.
- Nunca `synchronize: true` (TypeORM) ni `prisma db push` fuera de desarrollo local.
- Nunca editar una migracion ya aplicada en otro entorno: crear una nueva.

## Prisma
- Crear: `npx prisma migrate dev --name add_user_status` (solo local, genera SQL en
  `prisma/migrations/<ts>_<name>/migration.sql`). Revisar el SQL antes de comitear.
- Aplicar en CI/prod: `npx prisma migrate deploy` (nunca `migrate dev` ni `reset`).
- `--create-only` para editar el SQL generado (p. ej. `CREATE INDEX CONCURRENTLY`).
- Tras cambiar `schema.prisma`: `npx prisma generate` (el build lo necesita).

## TypeORM
- Generar: `npx typeorm migration:generate src/migrations/AddUserStatus -d src/data-source.ts`.
- Correr / revertir: `migration:run` / `migration:revert` (prueba ambos en local).
- `up` y `down` con `queryRunner.query('... $1', [valor])`: parametrizado siempre.
- `migrationsRun: true` solo si el deploy lo controla; nunca junto a `synchronize`.

## Alembic
- `alembic revision --autogenerate -m "add user status"`; revisar el script (autogenerate
  no detecta renombres: los ve como drop + add y PIERDE datos).
- `alembic upgrade head` / `alembic downgrade -1` en local antes de comitear.

## Cambios sin downtime (expand / contract)
1. Agregar columna nullable (o con DEFAULT constante: en PG >= 11 no reescribe la tabla).
2. Backfill en lotes (`UPDATE ... WHERE id BETWEEN ...`) en migracion aparte.
3. Desplegar codigo que escribe en ambas / lee la nueva.
4. Recien entonces `NOT NULL`, drop de la columna vieja o renombre.
- Renombrar = agregar + copiar + borrar despues; nunca `RENAME` con codigo viejo vivo.
- Indices en tablas grandes: `CONCURRENTLY`, en migracion sin transaccion.
- `ALTER TABLE` toma lock: `SET lock_timeout = '5s'` para no colgar la base.
