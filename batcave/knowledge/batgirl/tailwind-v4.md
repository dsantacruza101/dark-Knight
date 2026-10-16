keywords: tailwind, tailwindcss, css, estilos, styles, theme, dark, responsive, clases, utility
> 🌱 Nota semilla (2026-10-15, escrita a mano). El radar de Batgirl la complementa
> con notas `<paquete>-<version>.md` en su modo semanal; refrescar si cambia el major.

# Tailwind CSS v4: cambios clave respecto a v3

## Configuracion CSS-first
- Entrada: `@import "tailwindcss";` (reemplaza `@tailwind base/components/utilities`).
- Tema en CSS: `@theme { --color-brand: oklch(0.6 0.2 260); --font-display: "Inter"; }`
  genera `bg-brand`, `font-display`... `tailwind.config.js` ya no es necesario
  (si existe, se carga explicitamente con `@config "./tailwind.config.js";`).
- Deteccion automatica de fuentes (sin array `content`); rutas extra con `@source "../lib";`.
- Vite: plugin `@tailwindcss/vite`. PostCSS: `@tailwindcss/postcss`. Autoprefixer y
  postcss-import ya vienen incluidos (Lightning CSS).
- Utilidades propias: `@utility nombre { ... }` (no `@layer utilities`).
- Variantes propias: `@custom-variant dark (&:where(.dark, .dark *));` para dark mode por clase.
- Valores del tema como variables CSS: `var(--color-red-500)`; evitar `theme()`.

## Renombres y cambios de default (los que mas rompen)
- `shadow-sm`->`shadow-xs`, `shadow`->`shadow-sm`; igual con `rounded-*` y `blur-*`.
- `outline-none` -> `outline-hidden` (`outline-none` ahora pone `outline-style: none`).
- `ring` pasa de 3px a 1px: usar `ring-3` para el look anterior.
- Borde por defecto `currentColor` (antes gray-200): poner color explicito `border-gray-200`.
- Eliminados `bg-opacity-*`, `text-opacity-*`...: usar modificador `bg-black/50`.
- `flex-shrink-*`->`shrink-*`, `flex-grow-*`->`grow-*`, `overflow-ellipsis`->`text-ellipsis`.
- `!important` va al final: `bg-red-500!`.
- Variables arbitrarias: `bg-(--brand)` en vez de `bg-[--brand]`.
- Variantes apiladas se aplican de izquierda a derecha (v3 era al reves).
- `hover:` solo aplica en dispositivos con hover (`@media (hover: hover)`).
- `space-x/y-*` cambio de selector: preferir `gap-*` en flex/grid.

## Nuevo en v4
- Container queries nativas: `@container` en el padre, `@sm:`/`@lg:` en los hijos.
- Colores en `oklch`, `size-*`, `inset-shadow-*`, `not-*`, `starting:` (transiciones de entrada).
- Migracion asistida: `npx @tailwindcss/upgrade` (revisar el diff, nunca a ciegas).
- Navegadores minimos: Safari 16.4, Chrome 111, Firefox 128.

## Reglas de Batgirl
- Responsive first: estilos base = mobile, luego `sm:`/`md:`/`lg:`.
- Nada de `style={{...}}` si existe la utilidad; valores repetidos -> token en `@theme`.
- Foco visible siempre: `focus-visible:ring-2 focus-visible:ring-offset-2`, nunca solo `outline-hidden`.
