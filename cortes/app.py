"""Servidor do painel web."""
import json
import os
import shutil
import webbrowser

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import clipper, config, manager

LIVES_FILE = os.path.join(config.DATA_DIR, "lives.json")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

app = FastAPI(title="Cortes Live")
app.mount("/arquivos", StaticFiles(directory=config.CLIPS_DIR), name="arquivos")


def _save_lives():
    with open(LIVES_FILE, "w", encoding="utf-8") as f:
        json.dump([s.url for s in manager.streams.values()], f)


@app.on_event("startup")
def _startup():
    config.ffmpeg()  # falha cedo se não houver ffmpeg
    shutil.rmtree(config.BUFFER_DIR, ignore_errors=True)
    os.makedirs(config.BUFFER_DIR, exist_ok=True)
    if os.path.exists(LIVES_FILE):
        try:
            for url in json.load(open(LIVES_FILE, encoding="utf-8")):
                manager.add_stream(url)
        except Exception:
            pass


@app.on_event("shutdown")
def _shutdown():
    for s in list(manager.streams.values()):
        s.recorder.stop()
        s.chat.stop()


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


class AddReq(BaseModel):
    url: str


@app.post("/api/lives")
def add_live(req: AddReq):
    url = req.url.strip()
    if not url:
        raise HTTPException(400, "URL vazia")
    if "://" not in url:
        url = "https://" + url
    s = manager.add_stream(url)
    _save_lives()
    return s.to_dict(series=False)


@app.delete("/api/lives/{sid}")
def del_live(sid: str):
    manager.remove_stream(sid)
    _save_lives()
    return {"ok": True}


@app.post("/api/lives/{sid}/testar-corte")
def force_clip(sid: str):
    """Só para teste: força um corte do momento atual."""
    s = manager.streams.get(sid)
    if not s or s.recorder.status != "gravando":
        raise HTTPException(400, "live não está gravando")
    s._trigger(s.recorder.t - 5, 9.9, "chat")
    return {"ok": True}


@app.get("/api/estado")
def estado():
    return {
        "lives": [s.to_dict() for s in manager.streams.values()],
        "cortes": clipper.list_clips(),
        "fila": clipper.jobs.qsize(),
    }


@app.get("/api/config")
def get_config():
    return config.all_settings()


@app.post("/api/config")
def set_config(new: dict):
    return config.update(new)


@app.post("/api/cortes/{cid}/sugestoes")
def regen_suggestions(cid: str):
    clip = clipper.regenerate_suggestions(cid)
    if not clip:
        raise HTTPException(404, "corte não encontrado")
    return clip


@app.delete("/api/cortes/{cid}")
def del_clip(cid: str):
    clipper.delete_clip(cid)
    return {"ok": True}


@app.post("/api/abrir-pasta")
def open_folder():
    os.startfile(config.CLIPS_DIR) if os.name == "nt" else webbrowser.open(config.CLIPS_DIR)
    return {"ok": True}
