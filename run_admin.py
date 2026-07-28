"""Inicializa painel e API FastAPI."""

import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv


def main() -> None:
    load_dotenv(Path(__file__).parent / ".env")
    from machine_admin.config import Settings
    from machine_admin.web import create_app

    settings = Settings.from_environment()
    app = create_app(settings)
    uvicorn.run(
        app,
        host=os.getenv("ADMIN_HOST", os.getenv("BACKEND_HOST", "127.0.0.1")),
        port=int(os.getenv("ADMIN_PORT", os.getenv("BACKEND_PORT", "8000"))),
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )


if __name__ == "__main__":
    main()
