keywords: riverpod, bloc, cubit, state, estado, provider, notifier, asyncnotifier, flutter_bloc, management
> 🌱 Nota semilla (2026-10-17, escrita a mano). El radar de Robin la complementa
> con notas `<paquete>-<version>.md` en su modo semanal; refrescar si cambia el major.

# Manejo de estado: Riverpod vs Bloc (uno solo por app)

Regla: cada app usa UN enfoque. Antes de tocar estado, mirar `pubspec.yaml`:
`flutter_riverpod`/`hooks_riverpod` -> Riverpod; `flutter_bloc` -> Bloc. Nunca agregar el otro.

## Riverpod (2.x/3.x)
- `ProviderScope` en la raiz (`runApp(const ProviderScope(child: App()))`).
- Estado async: `AsyncNotifier` + `AsyncValue`; en la UI `ref.watch(p).when(data:, loading:, error:)`
  (o `switch` sobre `AsyncData`/`AsyncLoading`/`AsyncError`).
- `ref.watch` en `build` (reconstruye), `ref.read` en callbacks (no reconstruye),
  `ref.listen` para efectos (snackbars, navegacion). Nunca `ref.read` dentro de `build`.
- Codegen (`riverpod_annotation` + `build_runner`) si el repo ya lo usa: no mezclar estilos.
  Tras cambiar un `@riverpod`: `dart run build_runner build --delete-conflicting-outputs`.
- `autoDispose` por defecto para pantallas; `keepAlive` solo con motivo.
- Tests: `ProviderContainer(overrides: [repoProvider.overrideWithValue(FakeRepo())])`,
  `addTearDown(container.dispose)`.

## Bloc / Cubit (flutter_bloc)
- Cubit para estado simple (metodos -> `emit`); Bloc cuando hay eventos que transformar
  (debounce, `transformer: restartable()` de `bloc_concurrency`).
- Estados inmutables: sealed class (ver nota de Dart 3) o `Equatable` con `copyWith`.
- `BlocProvider` arriba, `BlocBuilder` para UI, `BlocListener` para efectos,
  `context.read<T>()` en callbacks, `context.watch<T>()`/`select` en build.
- Nunca `emit` despues de `close` (chequear `isClosed` tras un `await`).
- Tests: `bloc_test` (`blocTest(build:, act:, expect:)`) + mocktail para repositorios.

## Comun a ambos
- La logica vive en notifiers/blocs y repositorios, no en widgets.
- Inyectar repositorios (no instanciar `FirebaseFirestore.instance` dentro del estado)
  para poder mockearlos con mocktail.
- Mocktail: `class MockRepo extends Mock implements Repo {}`; `when(() => repo.get())
  .thenAnswer((_) async => x)`; `registerFallbackValue` para tipos propios en `any()`.
