keywords: nestjs, class-validator, dto, controller, validationpipe
- Cada endpoint recibe un DTO (clase, no interface) con decoradores de class-validator
  (`@IsString()`, `@IsEmail()`, `@IsOptional()`...) y class-transformer si hace falta.
- La validacion global vive en `main.ts` (`ValidationPipe` con `whitelist: true`);
  no la repitas por controller.
- Un modulo por feature: `<feature>.module.ts`, `<feature>.controller.ts`,
  `<feature>.service.ts`, `dto/`. Registra el modulo en el modulo padre.
- En el gateway, los microservicios se llaman por NATS con `ClientProxy.send()`
  y el patron como string constante; maneja `RpcException` en vez de lanzar errores HTTP crudos.
- Nada de `any`: tipa los retornos de `send<T>()`.
