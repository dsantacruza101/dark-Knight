# Red Hood — Skills

## Repos auditados (`config.yaml` -> `red_hood.local_repos`)
- `my_Portfolio_React_v2` (React + Vite + Vitest)
- `portfolioServiceLauncher/client-gateway` (NestJS + Jest)
- `portfolioServiceLauncher/nodeMailer-ms` (sin framework de test configurado)
- `telegram-bot` (Python, sin `requirements.txt` ni `tests/` por ahora)
- `night-agent` (Python)

Deteccion de stack por archivos: `package.json` -> Node, `requirements.txt`
o `*.py` -> Python.

## Flujo por repo
- `git fetch` + se posiciona en `dev` si existe; sin ella, audita `main`
  en modo solo-lectura (nunca comitea ahi).
- Compara el HEAD contra `batcave/memory/red_hood_state.json`; sin commits
  nuevos desde la ultima corrida, se omite el repo.
- Node: `npm ci` solo si falta `node_modules` o cambio `package-lock.json`;
  `npm test -- --passWithNoTests --coverage`; `npm run lint --if-present`;
  `npm audit --json`.
- Python: `pytest --tb=short -q` solo si existe `tests/` (si no, "sin tests");
  `py_compile` en todos los `.py`; `pip-audit` si hay un venv (`venv/` o
  `.venv/`) con `requirements.txt`.
- Timeout de 10 minutos por corrida de tests.

## Secretos hardcodeados (FASE 3, critico inmediato)
- Regex: `sk-ant-`, `ghp_`, `apikey_`, `bot<digitos>:AA`, passwords en
  texto plano (`git grep -InP` sobre archivos trackeados).
- Nunca se muestran completos: solo los primeros 6 caracteres + `***`.

## Decisiones (TypeSafe / Jev)
- Por hallazgo: `severity` (Score), `category` (Choice:
  failing_test/missing_tests/vulnerability/hardcoded_secret/lint_error/
  low_coverage), `should_generate_tests` (Noul), `who_to_notify` (Choice:
  alfred/nightwing/batman/none).
- `hardcoded_secret` no pasa por Jev: siempre `critical` + avisa a Alfred
  (y a Lucius via `comms.json`).
- Sin `TYPESAFE_API_KEY`: fallback deterministico segun la categoria.

## Generacion de tests (FASE 5)
- Solo si `should_generate_tests > 0.7`, cobertura bajo
  `red_hood.coverage_threshold` y el repo esta en `dev` (no solo-lectura).
- Hasta `red_hood.max_tests_per_repo` archivos por corrida, elegidos por
  menor cobertura (Node) o sin test correspondiente (Python).
- Aider (`ollama/mistral`) solo puede tocar el archivo fuente y su test;
  se valida despues que solo cambiaron archivos de test
  (`*.spec.ts`/`*.test.ts`/`test_*.py`, `modes.development.is_forbidden_path`).
  Si Aider toca algo mas, o el test generado falla al corerlo, se descarta
  con `git checkout` y se registra como error. Si pasa, se comitea (y
  pushea) a `dev` como `test(red-hood): add tests for <archivo>`.

## Reportes
- GitHub: issues con labels `red-hood` + `bug` (buscando duplicados antes)
  para tests fallidos o vulnerabilidades high/critical; agrega `night-agent`
  si Nightwing podria resolverlo.
- `batcave/comms.json`: mensajes `qa_finding` para Batman/Nightwing/Lucius.
- Telegram via Alfred: inicio, hallazgos criticos inmediatos, y resumen
  final por repo (tests, cobertura, vulnerabilidades, tests generados).
- Reporte diario en `reports/red-hood-YYYY-MM-DD.md`.
- `--dry-run`: audita igual pero no genera tests, no crea issues, no
  comitea/pushea y no envia Telegram.
