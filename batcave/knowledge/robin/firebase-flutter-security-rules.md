keywords: firebase, firestore, rules, security, auth, storage, appcheck, emulator, flutterfire, reglas
> 🌱 Nota semilla (2026-10-17, escrita a mano). El radar de Robin la complementa
> con notas `<paquete>-<version>.md` en su modo semanal; refrescar si cambia el major.

# Firebase + Flutter: reglas de seguridad

## Principio
- La app movil es publica: la config de Firebase (`firebase_options.dart`,
  `google-services.json`) NO es secreta, pero tampoco protege nada. La seguridad real son
  las reglas de Firestore/Storage + Auth + App Check. Nunca confiar en validaciones del cliente.
- Nunca editar `firebase_options.dart` a mano (lo genera `flutterfire configure`) ni
  comitear service accounts / llaves de admin.

## Firestore rules
```
rules_version = '2';
service cloud.firestore {
  match /databases/{db}/documents {
    match /users/{uid} {
      allow read: if request.auth != null && request.auth.uid == uid;
      allow create, update: if request.auth != null && request.auth.uid == uid
        && request.resource.data.keys().hasOnly(['name', 'photoUrl', 'updatedAt'])
        && request.resource.data.name is string && request.resource.data.name.size() <= 80;
      allow delete: if false;
    }
  }
}
```
- Denegar por defecto; nunca `allow read, write: if true;` ni `if request.auth != null`
  a secas sobre colecciones de otros usuarios.
- Validar forma (`keys().hasOnly`, tipos, tamanos) y campos inmutables
  (`request.resource.data.ownerId == resource.data.ownerId`).
- Roles por custom claims (`request.auth.token.admin == true`), no por un campo que el
  propio usuario puede escribir.
- Las reglas no filtran: la query del cliente debe incluir el mismo filtro
  (`where('ownerId', isEqualTo: uid)`) o falla con permission-denied.

## Storage rules
- Ruta por usuario (`/users/{uid}/...`), limitar `request.resource.size` y
  `request.resource.contentType.matches('image/.*')`.

## App Check y emulador
- App Check (Play Integrity / App Attest) en produccion; debug provider solo en debug.
- Probar reglas con el Emulator Suite (`firebase emulators:start`) y
  `@firebase/rules-unit-testing`; en Flutter, `useFirestoreEmulator('localhost', 8080)`
  solo detras de `kDebugMode`.
- Cambios de reglas van en `firestore.rules`/`storage.rules` versionados; nunca desde la consola.
