keywords: react, vite, vitest, testing-library, test, tests, spec, jsdom, mock, coverage, hooks
> 🌱 Nota semilla (2026-10-15, escrita a mano). El radar de Batgirl la complementa
> con notas `<paquete>-<version>.md` en su modo semanal; refrescar si cambia el major.

# React 19 + Vite: tests con Vitest y Testing Library

## Correr tests
- `npm test` es `vitest` = modo watch. En CI/Batman: `CI=true npx vitest run`.
- Un archivo: `npx vitest run src/x/Foo.test.tsx` (el posicional es filtro por substring).
- Cobertura: `npx vitest run --coverage` (provider `@vitest/coverage-v8`).
- Reporte maquina: `--reporter=json --outputFile=out.json` (formato compatible con jest).

## Configuracion (vite.config.ts)
- `/// <reference types="vitest/config" />` y bloque `test: { environment: 'jsdom',
  globals: true, setupFiles: ['./src/test/setup.ts'] }`.
- setup: `import '@testing-library/jest-dom/vitest'` (matchers `toBeInTheDocument`...).
- jsdom no trae `IntersectionObserver`, `ResizeObserver` ni `matchMedia`: stubs en el
  setup con `vi.stubGlobal` (framer-motion `whileInView` los necesita).

## Testing Library
- Consultar como el usuario: `screen.getByRole('button', { name: /enviar/i })`,
  `getByLabelText`, `getByText`. Evitar `container.querySelector` y clases CSS.
- `getBy*` (debe existir), `queryBy*` (afirmar ausencia), `findBy*` (async, await).
- Eventos: `const user = userEvent.setup(); await user.click(btn); await user.type(input, 'x')`.
- Esperas: `await waitFor(() => expect(...))`; nunca `setTimeout` manual.

## Mocks (vi)
- `vi.fn()`, `vi.spyOn(obj, 'm')`, `vi.mock('modulo', () => ({ ... }))` (se hoistea al
  inicio del archivo; variables usadas dentro van en `vi.hoisted(() => ...)`).
- `afterEach(() => vi.restoreAllMocks())`; timers: `vi.useFakeTimers()` + `vi.advanceTimersByTime`.
- react-i18next: mock de `useTranslation` que devuelve `{ t: (k) => k, i18n: { changeLanguage: vi.fn() } }`.
- hCaptcha (`@hcaptcha/react-hcaptcha`): mock del componente que llama `onVerify('token')`.
- fetch: `vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(data))))`.

## React 19
- `act` se importa de `react` (`react-dom/test-utils` ya no existe).
- `ref` es una prop normal en componentes funcion: no hace falta `forwardRef`.
- Formularios con Actions: `useActionState`, `useFormStatus`, `useOptimistic`;
  en tests, enviar con `user.click(submit)` y esperar con `findBy*`.
- `use(promise|context)` permite leer contexto condicionalmente.
- StrictMode monta/desmonta dos veces en dev: los efectos deben ser idempotentes.

## Convenciones del repo (my_Portfolio_React_v2)
- Tests junto al componente (`Foo.test.tsx`), un `describe` por componente.
- Formularios con react-hook-form + zod: probar mensajes de validacion visibles, no el schema.
