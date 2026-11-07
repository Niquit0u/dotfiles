---
name: Leer y editar con modelos locales
description: Cómo usar las herramientas read, edit y write de OpenCode con modelos pequeños locales (Qwen2.5 Coder 7B). Úsala antes de editar archivos existentes o cuando read/edit devuelvan errores.
---

# Leer y editar archivos con un modelo local pequeño

Las herramientas de OpenCode son reales y tienen nombres y parámetros exactos.
No inventes parámetros: usa los que se muestran aquí.

## Leer

```json
{"path": "/ruta/absoluta/archivo.txt"}
```

- `path` es **obligatorio** y absoluto.
- Para archivos largos, pagina con `offset` (línea inicial, 1-based) y `limit`
  (máximo de líneas). Ejemplo: `{"path": "/a/b.ts", "offset": 120, "limit": 80}`.
- La salida trae números de línea (`12: ...`). **No los copies** al editar: son
  solo referencia.

## Editar

```json
{"path": "/ruta/absoluta/archivo.txt", "oldString": "texto exacto", "newString": "texto nuevo"}
```

Reglas que evitan el 90% de los fallos:

1. **Lee antes de editar.** Sin una lectura previa no conoces el texto exacto.
2. `oldString` debe existir **una sola vez** en el archivo y coincidir byte por
   byte: espacios, tabulaciones, saltos de línea, comillas, punto y coma.
3. Elige un fragmento corto pero único (2 a 10 líneas). Si hay varias
   coincidencias, incluye más contexto arriba o abajo para hacerlo único.
4. `newString` debe ser **distinto** de `oldString`.
5. Usa `"replaceAll": true` solo cuando quieras cambiar **todas** las
   apariciones.
6. No reescribas el archivo entero para un cambio pequeño.

Patrón correcto:

```
1. read {"path": "/proyecto/src/app.ts"}
2. edit {"path": "/proyecto/src/app.ts",
         "oldString": "const port = 3000",
         "newString": "const port = 8080"}
3. read {"path": "/proyecto/src/app.ts", "offset": 1, "limit": 40}
```

## Crear o reemplazar

```json
{"path": "/ruta/absoluta/nuevo.txt", "content": "contenido completo\n"}
```

`write` reemplaza **todo** el archivo. Úsalo solo para archivos nuevos o
reescrituras completas.

## Buscar antes de leer

- `glob`: `{"pattern": "src/**/*.ts"}` para localizar rutas.
- `grep`: `{"pattern": "DEFAULT_TIMEOUT", "path": "src", "include": "*.ts"}`
  para localizar texto.

## Si una herramienta falla

| Error | Causa | Solución |
| --- | --- | --- |
| `oldString` not found | Texto mal copiado o el archivo cambió | Vuelve a `read` y copia el fragmento exacto |
| Multiple matches | `oldString` aparece varias veces | Añade más contexto o usa `replaceAll` |
| File not found | Ruta relativa o incorrecta | Usa ruta absoluta, verifica con `glob` |
| `newString` igual a `oldString` | No hay cambio real | Cambia el contenido nuevo |

Nunca repitas la misma llamada que falló: corrige los argumentos primero.
Nunca inventes el contenido de un archivo: si no lo leíste, léelo.
