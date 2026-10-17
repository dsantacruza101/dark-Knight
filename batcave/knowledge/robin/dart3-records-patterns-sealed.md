keywords: dart, dart3, records, record, patterns, pattern, sealed, switch, destructuring, null, safety, model
> 🌱 Nota semilla (2026-10-17, escrita a mano). El radar de Robin la complementa
> con notas `<paquete>-<version>.md` en su modo semanal; refrescar si cambia el major.

# Dart 3: records, patterns y sealed classes

## Records
- Tuplas inmutables con tipo estructural: `(int, String)` o con nombre `({int id, String name})`.
- Ideal para devolver varios valores sin crear una clase: `(User, List<Order>) load()`.
- Igualdad por valor incluida (`==`/`hashCode`); no sirven como modelo de dominio publico
  (sin metodos ni `fromJson`): para eso, clase `final`/`@immutable` con constructor `const`.

## Patterns
- Destructuring: `final (user, orders) = await load();`, `final {'id': int id} = json;`.
- `if (json case {'name': String name, 'age': int age}) { ... }` valida tipo y forma a la vez:
  reemplaza cadenas de `is`/`as` y `!` al parsear JSON.
- Guardas: `case Point(:final x) when x > 0:`; comodin `_`; or-patterns `case 'a' || 'b':`.
- Switch expression (exhaustiva, devuelve valor):
  ```dart
  final label = switch (status) { Status.ok => 'OK', Status.error => 'Error' };
  ```

## Sealed classes (estado de UI, resultados)
```dart
sealed class Result<T> { const Result(); }
final class Ok<T> extends Result<T> { const Ok(this.value); final T value; }
final class Err<T> extends Result<T> { const Err(this.error); final Object error; }

Widget build(Result<User> r) => switch (r) {
  Ok(:final value) => Text(value.name),
  Err(:final error) => Text('$error'),
};
```
- El compilador exige cubrir todos los subtipos: agregar uno rompe los `switch` que lo
  olvidan (eso es lo que queremos). Sin `default:` en switches sobre sealed.
- Subtipos en la misma libreria (mismo archivo o `part`). Modificadores: `final` (no se
  extiende fuera), `base`, `interface`; `sealed` implica `abstract`.

## Null safety (sound)
- Evitar `!`: preferir `?.`, `??`, `if (x case final v?)` o `late final` solo si se
  inicializa seguro antes de usarse.
- `required` en parametros nombrados no opcionales; nada de `dynamic` en modelos.
- `flutter analyze` debe quedar limpio: no silenciar lints con `// ignore:` sin motivo.
