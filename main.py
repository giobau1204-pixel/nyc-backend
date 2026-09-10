"""
BACKEND: Texto -> SQL -> Resultados
------------------------------------
Flujo general:
1. El usuario manda una pregunta en lenguaje natural (ej: "cuántos homicidios hay por barrio").
2. Le pedimos a la IA (Groq) que la convierta en una consulta SQL, usando el esquema
   real de la base de datos (que leemos automáticamente, sin escribirlo a mano).
3. Ejecutamos esa consulta SQL contra PostgreSQL/PostGIS.
4. Devolvemos el resultado en JSON para que la página web lo muestre.
"""

import os
import requests
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
import psycopg

# ---------------------------------------------------------------------------
# BLOQUE 1: Configuración inicial
# ---------------------------------------------------------------------------
# Carga las variables del archivo .env (credenciales de la base y API key de Groq)
# para no dejarlas escritas directamente en el código.
load_dotenv(override=True)

app = FastAPI()

# CORS: permite que tu página HTML (que corre en el navegador, otro "origen")
# pueda llamar a este backend sin que el navegador la bloquee por seguridad.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Credenciales de conexión a PostgreSQL, leídas desde el .env
# sslmode="require": Neon (y la mayoría de Postgres en la nube) exige conexión
# cifrada (SSL); sin esto, la conexión falla aunque las credenciales sean correctas.
DB_CONFIG = {
    "dbname": os.getenv("DB_NAME"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
    "host": os.getenv("DB_HOST"),
    "sslmode": "require",
    "port": os.getenv("DB_PORT"),
}

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

print(">>> Conectando a base de datos:", DB_CONFIG.get("dbname"), "en host:", DB_CONFIG.get("host"))

# Tablas internas de PostGIS que NO queremos mostrarle a la IA
# (son metadatos del sistema, no datos de NY que el usuario vaya a consultar).
TABLAS_A_IGNORAR = {"geography_columns", "geometry_columns", "spatial_ref_sys"}


# ---------------------------------------------------------------------------
# BLOQUE 2: Leer el esquema real de la base de datos automáticamente
# ---------------------------------------------------------------------------
def obtener_esquema() -> str:
    """
    Consulta information_schema (un catálogo interno de PostgreSQL) para
    obtener todas las tablas y columnas reales de la base 'nyc'.
    Así el prompt de la IA siempre queda actualizado, sin tener que
    escribir el esquema a mano ni mantenerlo sincronizado manualmente.
    """
    query = """
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'public'
        ORDER BY table_name, ordinal_position;
    """
    with psycopg.connect(**DB_CONFIG) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database(), current_user, current_schema();")
            print(">>> Sesión real de conexión (base, usuario, schema):", cur.fetchone())

            cur.execute(query)
            filas = cur.fetchall()

    print(">>> Filas crudas obtenidas de information_schema:", len(filas))
    if filas:
        print(">>> Primeras 3 filas:", filas[:3])

    # Agrupa las columnas por tabla, ignorando las tablas internas de PostGIS
    tablas = {}
    for tabla, columna, tipo in filas:
        if tabla in TABLAS_A_IGNORAR:
            continue
        tablas.setdefault(tabla, []).append((columna, tipo))

    # Guardamos los nombres reales de tabla en una variable global, para poder
    # validar más adelante si el SQL que genera la IA usa nombres inventados.
    global TABLAS_VALIDAS
    TABLAS_VALIDAS = set(tablas.keys())

    # Arma un texto legible tipo: nombre_tabla(columna1 (tipo), columna2 (tipo), ...)
    # y, para columnas de texto, agrega ejemplos de valores REALES ya guardados.
    lineas = [
        "Tablas disponibles en la base de datos (PostgreSQL + PostGIS). "
        "USA EXACTAMENTE estos nombres de tabla, letra por letra, nunca los abrevies "
        f"ni les quites el prefijo 'nyc_': {sorted(tablas.keys())}",
        "",
    ]
    with psycopg.connect(**DB_CONFIG) as conn:
        with conn.cursor() as cur:
            for tabla, columnas in tablas.items():
                desc_columnas = [f"{c} ({t})" for c, t in columnas]
                lineas.append(f"{tabla}({', '.join(desc_columnas)})")

                # Para columnas de texto (character varying), muestra valores REALES
                # que existen en la tabla. Esto evita que la IA traduzca o invente
                # valores (ej: preguntar 'cuchillo' y que la IA busque 'Cuchillo'
                # cuando el dato real guardado es 'knife').
                for columna, tipo in columnas:
                    if tipo == "character varying":
                        try:
                            cur.execute(
                                f'SELECT DISTINCT "{columna}" FROM "{tabla}" '
                                f'WHERE "{columna}" IS NOT NULL LIMIT 8;'
                            )
                            valores = [str(r[0]) for r in cur.fetchall()]
                            if valores:
                                lineas.append(f"    valores reales de {tabla}.{columna}: {valores}")
                        except Exception:
                            pass  # si falla la muestra de una columna, seguimos sin ella

    lineas.append("")
    lineas.append(
        "Nota: las columnas de tipo USER-DEFINED son 'geometry' (PostGIS). "
        "Para coordenadas legibles usa ST_AsText(geom) o ST_X/ST_Y con ST_Centroid(geom)."
    )
    lineas.append("")
    lineas.append(
        "IMPORTANTE: NO existen tablas separadas para 'población', 'demografía' ni "
        "similares. Los datos de población (total, por raza/etnia) están DENTRO de "
        "la tabla nyc_census_blocks, en las columnas popn_total, popn_white, "
        "popn_black, popn_asian, popn_other, popn_nativ. Nunca inventes un nombre "
        "de tabla como 'nyc_population' o 'population' — no existe."
    )
    return "\n".join(lineas)


# Se llena dentro de obtener_esquema() con los nombres reales de tabla,
# para poder validar el SQL que devuelva la IA antes de ejecutarlo.
TABLAS_VALIDAS = set()

# Se calcula UNA sola vez, al arrancar el servidor (no en cada pregunta),
# para no golpear la base de datos innecesariamente.
ESQUEMA = obtener_esquema()
print("\n>>> TABLAS_VALIDAS detectadas al arrancar:", TABLAS_VALIDAS, "\n")


# ---------------------------------------------------------------------------
# BLOQUE 3: Modelo de datos que recibe el endpoint
# ---------------------------------------------------------------------------
class Pregunta(BaseModel):
    pregunta: str  # La pregunta en texto plano que escribe el usuario en la página web


# ---------------------------------------------------------------------------
# BLOQUE 4: Llamar a la IA (Groq) para convertir texto -> SQL
# ---------------------------------------------------------------------------
def preguntar_a_ia(pregunta_usuario: str) -> str:
    """
    Envía el esquema de la base + la pregunta del usuario a Groq,
    y le pide que responda ÚNICAMENTE con una consulta SQL (sin explicaciones).
    """
    prompt_sistema = f"""Eres un experto en SQL y PostGIS. Dado el siguiente esquema de base de datos:

{ESQUEMA}

Convierte la pregunta del usuario en UNA SOLA consulta SQL válida para PostgreSQL/PostGIS.
Reglas:
- Responde SOLO con la consulta SQL, sin explicaciones, sin marcado de código, sin comentarios.
- La consulta debe empezar con SELECT (nunca generes INSERT, UPDATE, DELETE, DROP, ALTER).
- Usa nombres de tabla y columna exactamente como están en el esquema.
- Para CUALQUIER filtro sobre una columna de texto (WHERE columna = 'algo'), usa ILIKE
  con comodines en vez de '=', por ejemplo: WHERE weapon ILIKE '%knife%'. Esto evita
  fallar por mayúsculas/minúsculas o pequeñas diferencias de escritura.
- Los "valores reales" listados junto a cada columna en el esquema son ejemplos tal
  cual están guardados en la base (probablemente en inglés). NUNCA traduzcas la
  pregunta del usuario al armar el filtro: si el usuario pregunta en español por
  "cuchillo", usa el valor real en inglés que corresponda (ej: 'knife'), no la
  palabra en español.
"""

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        # Groq retiró varios modelos llama-3.x de su catálogo gratuito; el catálogo
        # actual usa modelos gpt-oss. Usamos el de 120B (más grande) porque este
        # backend necesita razonar bien: la pregunta llega en español pero los
        # valores reales en la base están en inglés, y el modelo debe darse cuenta
        # de esa diferencia y traducir el filtro correctamente.
        "model": "openai/gpt-oss-120b",
        "messages": [
            {"role": "system", "content": prompt_sistema},
            {"role": "user", "content": pregunta_usuario},
        ],
        "temperature": 0,  # 0 = respuestas más consistentes/predecibles, ideal para generar SQL
    }

    resp = requests.post(GROQ_URL, headers=headers, json=body, timeout=30)
    resp.raise_for_status()  # lanza error si Groq responde con un código de error HTTP
    data = resp.json()
    sql = data["choices"][0]["message"]["content"].strip()

    # Por si la IA igual devuelve el SQL envuelto en ```sql ... ```, lo limpiamos
    sql = sql.replace("```sql", "").replace("```", "").strip()
    return sql


# ---------------------------------------------------------------------------
# BLOQUE 5: Ejecutar el SQL generado, con validaciones de seguridad
# ---------------------------------------------------------------------------
def ejecutar_sql(sql: str):
    """
    Ejecuta la consulta SQL generada por la IA, pero antes valida que sea
    de SOLO LECTURA (SELECT) para evitar que un error de la IA borre o
    modifique datos por accidente.
    """
    sql_limpio = sql.strip().rstrip(";")

    if not sql_limpio.lower().startswith("select"):
        raise ValueError("Por seguridad, solo se permiten consultas SELECT.")

    palabras_prohibidas = ["insert", "update", "delete", "drop", "alter", "truncate", "grant"]
    if any(palabra in sql_limpio.lower() for palabra in palabras_prohibidas):
        raise ValueError("La consulta contiene una operación no permitida.")

    with psycopg.connect(**DB_CONFIG) as conn:
        with conn.cursor() as cur:
            cur.execute(sql_limpio)
            columnas = [desc[0] for desc in cur.description]
            filas = cur.fetchall()
            # Convierte cada fila en un diccionario {columna: valor} para que sea JSON fácil de usar en el front
            resultados = [dict(zip(columnas, fila)) for fila in filas]
            return resultados


# ---------------------------------------------------------------------------
# BLOQUE 5.5: Validar que el SQL generado use nombres de tabla reales
# ---------------------------------------------------------------------------
import re

def extraer_tablas_usadas(sql: str) -> set:
    """
    Busca en el SQL qué tablas se mencionan después de FROM o JOIN.
    Es una búsqueda simple (no un parser completo de SQL), pero suficiente
    para detectar si la IA inventó o abrevió un nombre de tabla.
    """
    encontradas = re.findall(r'(?:FROM|JOIN)\s+"?(\w+)"?', sql, flags=re.IGNORECASE)
    return set(encontradas)


def sql_usa_tablas_validas(sql: str) -> bool:
    """
    True solo si el SQL menciona al menos una tabla real y TODAS las que
    menciona existen. Si no se detecta ninguna tabla (ej: un SELECT sin
    FROM, como un placeholder inválido que a veces devuelve la IA cuando
    se confunde), lo tratamos como INVÁLIDO — no como válido por defecto,
    que era el bug anterior.
    """
    usadas = {t.lower() for t in extraer_tablas_usadas(sql)}
    if not usadas:
        return False
    validas_lower = {t.lower() for t in TABLAS_VALIDAS}
    return usadas.issubset(validas_lower)


# ---------------------------------------------------------------------------
# BLOQUE 6: Endpoints (las "puertas de entrada" que la página web va a llamar)
# ---------------------------------------------------------------------------
@app.post("/consulta")
def consulta(datos: Pregunta):
    """
    Endpoint principal: recibe la pregunta en texto, la convierte en SQL,
    lo ejecuta y devuelve tanto el SQL generado como los resultados.

    Si la IA usa un nombre de tabla que no existe (le pasa ocasionalmente,
    aunque el esquema esté bien armado), se reintenta hasta 2 veces más
    (3 intentos en total), validando CADA intento antes de ejecutarlo.
    Si ninguno funciona, se devuelve un error claro en vez de ejecutar
    SQL que sabemos que va a fallar.
    """
    try:
        pregunta_actual = datos.pregunta
        sql_generado = None

        for intento in range(3):
            sql_generado = preguntar_a_ia(pregunta_actual)
            print(f"\n--- Intento {intento + 1} ---")
            print("SQL generado:", sql_generado)
            print("¿Tablas válidas?:", sql_usa_tablas_validas(sql_generado))

            if sql_usa_tablas_validas(sql_generado):
                break  # este intento usa tablas reales, seguimos con este SQL

            # Si no es válido, preparamos un recordatorio más fuerte para el
            # siguiente intento (incluyendo qué tabla inventada usó, para que
            # la IA entienda su propio error).
            tablas_usadas = extraer_tablas_usadas(sql_generado)
            pregunta_actual = (
                f"{datos.pregunta}\n\n"
                f"(Tu intento anterior usó la tabla inexistente {sorted(tablas_usadas)}. "
                f"Los ÚNICOS nombres de tabla válidos son exactamente: "
                f"{sorted(TABLAS_VALIDAS)}. Revisa bien a qué tabla real pertenece "
                f"el dato que se pide antes de responder.)"
            )
        else:
            # Se agotaron los 3 intentos y ninguno usó una tabla válida.
            return {
                "error": (
                    "No logré identificar en qué tabla está esa información. "
                    "Intenta reformular la pregunta mencionando barrios, calles, "
                    "estaciones de metro, homicidios o población."
                )
            }

        resultados = ejecutar_sql(sql_generado)
        return {
            "pregunta": datos.pregunta,
            "sql_generado": sql_generado,
            "resultados": resultados,
        }
    except Exception as e:
        # Si algo falla (IA, conexión a la base, SQL inválido), devolvemos el error
        # en vez de que el servidor se caiga.
        return {"error": str(e)}


@app.get("/")
def raiz():
    """Endpoint simple para confirmar que el backend está vivo."""
    return {"mensaje": "Backend activo. Usa POST /consulta con {'pregunta': '...'}"}