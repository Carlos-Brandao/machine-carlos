"""Inicializa a API interna da fila de consultas."""

from pathlib import Path

from dotenv import load_dotenv

from services.backend_api import serve


load_dotenv(Path(__file__).parent / ".env")
serve()
