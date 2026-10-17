keywords: workflow, workflows, actions, github, permissions, uses, pin, sha, secrets, ci, pipeline, actionlint, oidc
> 🌱 Nota semilla (2026-10-17, escrita a mano). El radar de Spoiler la complementa
> con notas `<accion>-<version>.md` en su modo semanal; refrescar si cambia la practica.

# GitHub Actions: endurecimiento

## Fijar cada `uses:`
- Preferido: SHA completo (40 caracteres) + comentario con la version, que el radar lee:
  `uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2`
- Aceptable: version completa `@v4.2.2`. Nunca `@v4` (flotante), `@main`/`@master` (rama):
  quien controle la accion cambia el codigo que corre con nuestros secretos (caso
  tj-actions/changed-files, CVE-2025-30066).
- SHA de un tag: `gh api repos/<owner>/<repo>/git/ref/tags/v4.2.2 --jq .object.sha`;
  si `.object.type` es `tag` (anotado), seguir con `gh api repos/<o>/<r>/git/tags/<sha> --jq .object.sha`.
- Reusable workflows tambien: `uses: org/repo/.github/workflows/x.yml@<sha>`.

## Permisos minimos
```yaml
permissions:
  contents: read          # a nivel de workflow: el GITHUB_TOKEN por defecto es demasiado amplio
jobs:
  release:
    permissions:
      contents: write     # solo el job que publica
```
- `pull_request_target` + checkout del PR = ejecutar codigo ajeno con secretos: evitar.
- Deploy a la nube: OIDC (`id-token: write`) en vez de llaves de larga duracion.

## Secretos
- Nunca `echo ${{ secrets.X }}`, `set -x` con secretos, `env`/`printenv` ni subirlos como artifact.
- Valor derivado sensible: `echo "::add-mask::$valor"` antes de usarlo.
- Pasarlos por `env:` del paso, no interpolados en `run:` (inyeccion de shell):
  ```yaml
  - run: ./deploy.sh
    env:
      WEBHOOK_SECRET: ${{ secrets.WEBHOOK_SECRET }}
  ```
- Igual con datos del evento (`github.event.pull_request.title`): via `env:`, nunca `${{ }}` en `run:`.

## Higiene
- `actions/checkout` con `persist-credentials: false` si el job no hace push.
- `timeout-minutes` en cada job; `concurrency` para no solapar deploys.
- Nuestro deploy: GitHub Actions -> webhook HMAC -> `docker compose` (ver CLAUDE.md global);
  el secreto HMAC solo en `secrets`, la firma con `openssl dgst -sha256 -hmac`.

## Verificar
- `actionlint .github/workflows/<archivo>.yml` (incluye shellcheck de los `run:` si esta instalado).
- Despues del push a la rama de trabajo, revisar la corrida: `gh run list --branch dev`.
