# Convenciones

> Plantilla: ajustá a tu stack. Todo lo que esté acá lo hacen cumplir el Builder y el Revisor de Código.

## Código
- Nombres en inglés para código; mensajes al usuario en español.
- Funciones chicas, una responsabilidad. Sin código muerto ni prints de debug.
- Errores: excepciones específicas, nunca `except:` desnudo.
- Configuración por variables de entorno; nunca secretos en el repo.

## Tests
- TDD: el test se escribe antes que la implementación.
- Tests en `tests/`, nombre `test_<qué_verifica>`; deterministas, sin red ni reloj real.
- Cada criterio de aceptación de una spec tiene al menos un test.

## Carga
- Prueba de carga en `loadtest/locustfile.py` (Locust): una clase `HttpUser` y un `@task(peso)` por endpoint.
- Cada endpoint nuevo o modificado suma su tarea. Los endpoints de escritura usan datos descartables y nunca apuntan a producción.

## Git
- La fábrica trabaja en ramas `factory/<run_id>`; nunca commitea directo a `main`.
- Mensajes de commit: `factory(<run_id>): <resumen>`.
