# Lecciones

Aprendizajes de runs anteriores (lo escribe el Librarian). Revisalo y podalo cada tanto.

## Agregar receta de mayonesa casera (2026-11-15, run 20261115-232156-41445c)
- Replicar estructura HTML exacta de archivos de referencia es crítico; CSS inline o markup distinto rompe coherencia visual aunque los tests pasen.
- Leer código existente antes de implementar evita dos iteraciones de rechazo por desviación de diseño.
- La aprobación en iteración 3 confirma que copiar locro.html como plantilla y reemplazar solo contenido cumple la spec de 'replicar exactamente'.

## Cambio UI (2026-11-16, run 20261116-002755-b4b25f)
- Playwright requiere pytest-playwright como plugin además de la librería base
- Los tests E2E necesitan un servidor HTTP local; threading.Thread con daemon=True permite servidor en background sin bloquear pytest
- Los estados :hover en CSS no siempre se capturan en computed styles de Playwright; verificar definición en CSS via contract tests es suficiente
- Para sitios estáticos sin backend, time.sleep(0.5) tras start_server() evita race conditions en fixture de sesión
