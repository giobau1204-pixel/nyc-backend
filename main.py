""""""
BACKEND: Capa de abstracción API REST - NYC Spatial Database
------------------------------------------------------------
Entry points específicos para Homicidios y Barrios, y consulta IA con soporte espacial.
"""

import os
import re
import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
import psycopg

load_dotenv(override=True)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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

TABLAS_A_IGNORAR = {"geography_columns", "geometry_columns", "spatial_ref_sys"}


def obtener_esquema() -> str:
    query = """
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'public'
        ORDER BY table_name, ordinal_position;
    """
    with psycopg.connect(**DB_CONFIG) as conn:
        with conn.cursor() as cur:
            cur.execute(query)
            filas = cur.fetchall()

    tablas = {}
    for tabla, columna, tipo in filas:
        if tabla in TABLAS_A_IGNORAR:
            continue
        tablas.setdefault(tabla, []).append((columna, tipo))

    global TABLAS_VALIDAS
    TABLAS_VALIDAS = set(tablas.keys())

    lineas = [f"Tablas disponibles: {sorted(tablas.keys())}", ""]
    with psycopg.connect(**DB_CONFIG) as conn:
        with conn.cursor() as cur:
            for tabla, columnas in tablas.items():
                desc_columnas = [f"{c} ({t})" for c, t in columnas]
                lineas.append(f"{tabla}({', '.join(desc_columnas)})")
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
                            pass
    return "\n".join(lineas)


TABLAS_VALIDAS = set()
ESQUEMA = obtener_esquema()


class Pregunta(BaseModel):
    pregunta: str


class SpatialQuery(BaseModel):
    wkt_polygon: str


def preguntar_a_ia(pregunta_usuario: str) -> str:
    prompt_sistema = f"""Eres un experto en SQL y PostGIS. Dado el siguiente esquema:
{ESQUEMA}
Convierte la pregunta en UNA SOLA consulta SQL válida para PostgreSQL/PostGIS.
Reglas:
- Responde SOLO con la consulta SQL, sin explicaciones ni bloques markdown.
- Empieza con SELECT.
- Usa nombres de tabla exactos. Si se requiere geometría, usa ST_AsText(geom) para visualizarla.
- Usa ILIKE para textos.
"""
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "model": "openai/gpt-oss-120b",
        "messages": [
            {"role": "system", "content": prompt_sistema},
            {"role": "user", "content": pregunta_usuario},
        ],
        "temperature": 0,
    }
    resp = requests.post(GROQ_URL, headers=headers, json=body, timeout=30)
    resp.raise_for_status()
    sql = resp.json()["choices"][0]["message"]["content"].strip()
    return sql.replace("```sql", "").replace("```", "").strip()


def ejecutar_sql(sql: str):
    sql_limpio = sql.strip().rstrip(";")
    if not sql_limpio.lower().startswith("select"):
        raise ValueError("Solo se permiten consultas SELECT.")
    
    palabras_prohibidas = ["insert", "update", "delete", "drop", "alter", "truncate", "grant"]
    if any(palabra in sql_limpio.lower() for palabra in palabras_prohibidas):
        raise ValueError("Operación no permitida.")

    with psycopg.connect(**DB_CONFIG) as conn:
        with conn.cursor() as cur:
            cur.execute(sql_limpio)
            columnas = [desc[0] for desc in cur.description]
            filas = cur.fetchall()
            return [dict(zip(columnas, fila)) for fila in filas]


# ENTRY POINTS ESPECÍFICOS
@app.get("/api/homicidios")
def get_homicidios(limit: int = 50):
    sql = f"SELECT id, weapon, suspect_race, victim_race, ST_AsText(geom) as geom_wkt FROM nyc_homicides LIMIT {limit};"
    try:
        resultados = ejecutar_sql(sql)
        return {"total": len(resultados), "resultados": resultados}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/barrios")
def get_barrios(limit: int = 50):
    sql = f"SELECT name, boroname, ST_AsText(geom) as geom_wkt FROM nyc_neighborhoods LIMIT {limit};"
    try:
        resultados = ejecutar_sql(sql)
        return {"total": len(resultados), "resultados": resultados}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/spatial-filter")
def spatial_filter(data: SpatialQuery):
    sql = f"""
        SELECT h.id, h.weapon, h.victim_race, ST_AsText(h.geom) as geom_wkt
        FROM nyc_homicides h
        WHERE ST_Intersects(h.geom, ST_GeomFromText('{data.wkt_polygon}', 4326));
    """
    try:
        resultados = ejecutar_sql(sql)
        return {"total": len(resultados), "resultados": resultados}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/consulta")
def consulta(datos: Pregunta):
    try:
        sql_generado = preguntar_a_ia(datos.pregunta)
        resultados = ejecutar_sql(sql_generado)
        return {"pregunta": datos.pregunta, "sql_generado": sql_generado, "resultados": resultados}
    except Exception as e:
        return {"error": str(e)}


@app.get("/")
def raiz():
    return {"mensaje": "API REST Espacial NYC Activa"}