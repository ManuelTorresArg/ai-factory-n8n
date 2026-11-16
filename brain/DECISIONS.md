# Decisiones

Registro append-only de decisiones de arquitectura (lo escribe el Librarian y lo podés editar a mano).

## Cambio UI (2026-11-16, run 20261116-002755-b4b25f)
- Crear tests/test_server.py con servidor HTTP simple en puerto 8888 para servir archivos estáticos durante E2E tests
- Organizar tests E2E en clases por área: TestModernLayout, TestIndexPage, TestRecipePages, TestModernStyling, TestAccessibility, TestCrossPageNavigation, TestFooter
- Usar @pytest.mark.parametrize para ejecutar los mismos tests en las 4 páginas HTML, evitando duplicación
- Verificar estilos modernos via window.getComputedStyle() en lugar de solo verificar clases CSS, confirmando que los estilos se aplican realmente
- Incluir tests de navegación cross-page (index→receta→back, receta→receta) para validar que los href son correctos y funcionales
- Generar reporte markdown con pasos exactos, mensajes de error potenciales y comandos para reproducir, siguiendo el formato pedido por el sistema
