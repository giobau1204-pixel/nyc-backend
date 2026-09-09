import os
import requests
from dotenv import load_dotenv

load_dotenv()
api_key = os.getenv("GROQ_API_KEY")

headers = {"Authorization": f"Bearer {api_key}"}

resp = requests.get("https://api.groq.com/openai/v1/models", headers=headers)

print("Código de respuesta:", resp.status_code)
print("Modelos disponibles:", resp.text)
