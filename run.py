"""Inicia o painel: python run.py  →  abre http://localhost:8765"""
import os
import threading
import webbrowser

import uvicorn

PORT = 8765

if __name__ == "__main__":
    if not os.environ.get("CORTES_SEM_NAVEGADOR"):
        threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    uvicorn.run("cortes.app:app", host="127.0.0.1", port=PORT, log_level="warning")
