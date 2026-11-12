"""Servidor do painel web."""
import json
import os
import shutil
import webbrowser

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import agenda, canal, clipper, config, curadoria, manager, roteiros, thumbs, youtube

LIVES_FILE = os.path.join(config.DATA_DIR, "lives.json")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

app = FastAPI(title="Cortes Live")
app.mount("/arquivos", StaticFiles(directory=config.CLIPS_DIR), name="arquivos")


def _save_lives():
    config.salvar_json(LIVES_FILE, [s.url for s in manager.streams.values()])


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
    curadoria.verificar()  # lotes completos que ainda não tiveram os 2 escolhidos


@app.on_event("shutdown")
def _shutdown():
    for s in list(manager.streams.values()):
        s.stop()


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
    fim = s.recorder.covered_until()
    s.criar_corte(max(0.0, fim - 30), fim, {"motivo": "Teste manual", "pontuacao": 0, "pico_rel": 3.0})
    return {"ok": True}


@app.get("/api/estado")
def estado():
    return {
        "lives": [s.to_dict() for s in manager.streams.values()],
        "cortes": clipper.list_clips(),
        "fila": clipper.jobs.qsize(),
        "lotes": curadoria.estado(),
        "tam_lote": curadoria.TAM_LOTE,
        "disco_livre_mb": config.disco_livre_mb(),
        "disco_min_mb": config.DISCO_MIN_CORTES_MB,
    }


class LoteReq(BaseModel):
    canal: str
    lote: int


@app.post("/api/lotes/escolher")
def escolher_lote(req: LoteReq):
    """Escolhe agora os melhores de um lote ainda incompleto."""
    curadoria.verificar(forcar=(req.canal.lower(), req.lote))
    return {"ok": True}


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


@app.post("/api/cortes/{cid}/reprocessar")
def reprocessar_clip(cid: str):
    try:
        return clipper.reprocessar(cid)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/cortes/{cid}")
def del_clip(cid: str):
    clipper.delete_clip(cid)
    return {"ok": True}


# ------------------------------------------------------------------ YouTube
@app.get("/api/youtube/status")
def yt_status(recarregar: bool = False):
    return youtube.status(recarregar)


class TextoReq(BaseModel):
    texto: str


@app.post("/api/youtube/credencial")
def yt_credencial(req: TextoReq):
    try:
        youtube.salvar_client_secret(req.texto)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return youtube.status()


@app.post("/api/youtube/conectar")
def yt_conectar():
    try:
        youtube.conectar()
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    return youtube.status()


@app.post("/api/youtube/desconectar")
def yt_desconectar():
    youtube.desconectar()
    canal.esquecer()  # a cópia local dos dados do canal vai junto (política de privacidade)
    return youtube.status()


class EnvioReq(BaseModel):
    titulo: str
    descricao: str = ""
    privacidade: str = "public"
    publicar_em: str | None = None
    tags: list[str] = []
    forcar: bool = False


@app.post("/api/cortes/{cid}/youtube")
def yt_enviar(cid: str, req: EnvioReq):
    try:
        return youtube.enviar(cid, req.titulo, req.descricao, req.privacidade, req.publicar_em, req.tags, req.forcar)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/cortes/{cid}/youtube/atualizar")
def yt_atualizar(cid: str):
    try:
        return youtube.atualizar(cid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa
        raise HTTPException(502, youtube.msg_erro(e))


# ------------------------------------------------------------------ Thumbnails
@app.post("/api/cortes/{cid}/thumbs/gerar")
def thumbs_gerar(cid: str):
    try:
        return thumbs.gerar(cid)
    except ValueError as e:
        raise HTTPException(400, str(e))


class ThumbReq(BaseModel):
    quadro: str
    texto: str
    cor: str = "amarelo"
    layout: str = "lateral"


@app.post("/api/cortes/{cid}/thumbs/personalizar")
def thumbs_personalizar(cid: str, req: ThumbReq):
    try:
        return thumbs.personalizar(cid, req.quadro, req.texto, req.cor, req.layout)
    except ValueError as e:
        raise HTTPException(400, str(e))


class EscolhaReq(BaseModel):
    indice: int


@app.post("/api/cortes/{cid}/thumbs/escolher")
def thumbs_escolher(cid: str, req: EscolhaReq):
    try:
        return thumbs.escolher(cid, req.indice)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/cortes/{cid}/thumbs/youtube")
def thumbs_youtube(cid: str):
    try:
        return youtube.definir_thumb(cid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa
        raise HTTPException(502, str(e) if isinstance(e, RuntimeError) else youtube.msg_erro(e))


# ------------------------------------------------------------------ Roteiros
def _rot(fn, *args):
    try:
        return fn(*args)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/roteiros")
def rot_listar():
    return {"roteiros": roteiros.listar(), "cortes": roteiros.cortes_disponiveis()}


class NovoRoteiroReq(BaseModel):
    tipo: str
    tema: str = ""
    canal: str = ""
    duracao: int | None = None
    estilo: str = ""
    cortes: list[str] = []


@app.post("/api/roteiros")
def rot_criar(req: NovoRoteiroReq):
    return _rot(roteiros.criar, req.tipo, req.tema, req.canal, req.duracao, req.estilo, req.cortes)


@app.get("/api/roteiros/{rid}")
def rot_obter(rid: str):
    r = _rot(roteiros.obter, rid)
    r["descricao_completa"] = roteiros.descricao_completa(r)
    return r


@app.put("/api/roteiros/{rid}")
def rot_salvar(rid: str, novo: dict):
    r = _rot(roteiros.salvar, rid, novo)
    r["descricao_completa"] = roteiros.descricao_completa(r)
    return r


@app.delete("/api/roteiros/{rid}")
def rot_apagar(rid: str):
    _rot(roteiros.apagar, rid)
    return {"ok": True}


@app.post("/api/roteiros/{rid}/gerar")
def rot_regerar(rid: str):
    return _rot(roteiros.regerar, rid)


@app.post("/api/roteiros/{rid}/montar")
def rot_montar(rid: str):
    return _rot(roteiros.montar, rid)


@app.post("/api/roteiros/{rid}/thumb")
def rot_thumb(rid: str):
    return _rot(roteiros.refazer_thumb, rid)


class EnvioRoteiroReq(BaseModel):
    titulo: str
    descricao: str = ""
    privacidade: str = "public"
    publicar_em: str | None = None


@app.post("/api/roteiros/{rid}/youtube")
def rot_youtube(rid: str, req: EnvioRoteiroReq):
    return _rot(roteiros.enviar_youtube, rid, req.titulo, req.descricao, req.privacidade, req.publicar_em)


# ------------------------------------------------------------------ Painel do canal
@app.get("/api/canal")
def canal_estado():
    return canal.estado()


@app.post("/api/canal/atualizar")
def canal_atualizar():
    return _rot(canal.atualizar)


@app.post("/api/canal/aprender")
def canal_aprender():
    return _rot(canal.aprender)


@app.get("/api/canal/videos/{vid}")
def canal_video(vid: str):
    return _rot(canal.video, vid)


class EdicaoReq(BaseModel):
    titulo: str
    descricao: str = ""
    tags: list[str] = []
    privacidade: str


@app.post("/api/canal/videos/{vid}")
def canal_editar(vid: str, req: EdicaoReq):
    return _agenda(canal.editar, vid, req.titulo, req.descricao, req.tags, req.privacidade)


# ------------------------------------------------------------------ Agenda do YouTube
def _agenda(fn, *args):
    try:
        return fn(*args)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa  (erro da API do YouTube ao mudar um vídeo já agendado)
        raise HTTPException(502, youtube.msg_erro(e))


@app.get("/api/agenda")
def agenda_estado():
    return agenda.estado()


@app.post("/api/agenda/config")
def agenda_config(novo: dict):
    return _agenda(agenda.set_config, novo)


class AgendaReq(BaseModel):
    cid: str
    quando: str | None = None
    titulo: str | None = None
    descricao: str | None = None


@app.post("/api/agenda")
def agenda_adicionar(req: AgendaReq):
    return _agenda(agenda.adicionar, req.cid, req.quando, req.titulo, req.descricao)


class MoverReq(BaseModel):
    quando: str | None = None


@app.post("/api/agenda/{iid}/mover")
def agenda_mover(iid: str, req: MoverReq):
    return _agenda(agenda.mover, iid, req.quando)


class EditarReq(BaseModel):
    titulo: str | None = None
    descricao: str | None = None


@app.post("/api/agenda/{iid}/editar")
def agenda_editar(iid: str, req: EditarReq):
    return _agenda(agenda.editar, iid, req.titulo, req.descricao)


@app.delete("/api/agenda/{iid}")
def agenda_remover(iid: str):
    _agenda(agenda.remover, iid)
    return {"ok": True}


@app.post("/api/abrir-pasta")
def open_folder():
    os.startfile(config.CLIPS_DIR) if os.name == "nt" else webbrowser.open(config.CLIPS_DIR)
    return {"ok": True}
