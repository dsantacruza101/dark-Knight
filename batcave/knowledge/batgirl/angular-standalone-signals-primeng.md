keywords: angular, standalone, signals, signal, primeng, primeflex, testbed, rxjs, component
> 🌱 Nota semilla (2026-10-15, escrita a mano). El radar de Batgirl la complementa
> con notas `<paquete>-<version>.md` en su modo semanal; refrescar si cambia el major.

# Angular moderno: standalone + signals + PrimeNG

## Standalone (default desde Angular 19)
- Sin NgModules: cada componente declara sus `imports: [...]` (otros componentes,
  pipes, directivas, modulos de PrimeNG). `standalone: true` ya es el default.
- Arranque: `bootstrapApplication(App, appConfig)` con `provideRouter(routes)`,
  `provideHttpClient(withInterceptors([authInterceptor]))` (interceptores funcion).
- Rutas lazy: `loadComponent: () => import('./x/x.component').then(m => m.X)`.
- DI con `inject(Servicio)` en vez de constructor; `providedIn: 'root'` en servicios.
- Control flow: `@if (...) {} @else {}`, `@for (item of items(); track item.id) {} @empty {}`,
  `@switch`, `@defer (on viewport) {}`. `track` es obligatorio en `@for`.

## Signals
- Estado: `signal(0)`, `.set()`, `.update(v => v + 1)`; derivados con `computed()`;
  efectos con `effect()` (solo para sincronizar con el exterior, no para derivar estado).
- Componentes: `input()`, `input.required<T>()`, `output<T>()`, `model()` (two-way),
  `viewChild()`, `contentChildren()`. Se leen llamandolos: `nombre()`.
- `linkedSignal()` para estado editable derivado de otro signal.
- RxJS: `toSignal(obs$, { initialValue })` y `toObservable(sig)` de `@angular/core/rxjs-interop`;
  `takeUntilDestroyed()` para cerrar suscripciones.
- `ChangeDetectionStrategy.OnPush` en todos los componentes; zoneless con
  `provideZonelessChangeDetection()` cuando el proyecto ya lo adopto.

## Tests con TestBed
- Standalone va en `imports`, no en `declarations`:
  `TestBed.configureTestingModule({ imports: [MiComponente], providers: [...] })`.
- Inputs signal: `fixture.componentRef.setInput('nombre', valor)` + `fixture.detectChanges()`.
- Async: `await fixture.whenStable()`. HTTP: `provideHttpClient(), provideHttpClientTesting()`
  + `HttpTestingController.expectOne(url).flush(data)`; `verify()` en afterEach.
- Angular 21+ genera proyectos con Vitest; los viejos usan Karma/Jasmine o Jest.
- Preferir Testing Library (`@testing-library/angular`, `render(Comp, { inputs })`) cuando exista.

## PrimeNG (v18+)
- Temas por configuracion, no por CSS: `providePrimeNG({ theme: { preset: Aura } })`
  desde `primeng/config` (presets en `@primeng/themes` / `@primeuix/themes` segun version).
- Importar solo lo usado en el componente: `ButtonModule`, `TableModule`, `DialogModule`...
- Componentes con animaciones requieren `provideAnimationsAsync()` en el appConfig.
- Accesibilidad: `inputId` + `<label for>`, `ariaLabel` en botones solo-icono,
  `p-table` con `caption`/encabezados reales.
- PrimeFlex (`flex`, `gap-2`, `grid` + `col-12 md:col-6`) esta en mantenimiento: en proyectos
  nuevos PrimeNG recomienda Tailwind + `tailwindcss-primeui`. No mezclar ambos en un componente.
- Nada de `::ng-deep` para tematizar: usar design tokens del preset (`definePreset`).
