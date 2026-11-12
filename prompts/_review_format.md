Respondé SOLO con un objeto JSON con esta forma exacta:

```json
{
  "verdict": "pass | fail",
  "score": 0,
  "summary": "una línea",
  "findings": [
    {"severity": "blocker | major | minor", "file": "ruta", "issue": "qué está mal", "suggestion": "cómo arreglarlo"}
  ]
}
```

- `blocker`: impide aprobar (bug, vulnerabilidad, criterio de aceptación incumplido).
- `major`: debería corregirse en esta iteración.
- `minor`: mejora opcional.
- `verdict` = "fail" si hay al menos un blocker. `score` de 0 a 10.
- Revisá solo tu dimensión; no repitas lo que corresponde a otro revisor.
- Reconocé también lo que está bien hecho en `summary`: el feedback positivo orienta al Builder tanto como el negativo.
