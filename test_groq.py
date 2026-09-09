print("Script iniciado correctamente")
import os
import requests
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("GROQ_API_KEY")
print("Key leída (primeros 10 caracteres):", api_key[:10] if api_key else "NO SE ENCONTRÓ LA KEY")

resp = requests.post(
    "https://api.groq.com/openai/v1/chat/completions",
    headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    },
    json={
        "model": "groq/compound",  # Modelo vigente
        "messages": [{"role": "user", "content": "hola"}],
    },
)

print("Código de respuesta:", resp.status_code)
print("Respuesta:", resp.text)


