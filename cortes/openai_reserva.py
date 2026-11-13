"""Reserva da IA: quando o limite do Gemini acaba, o mesmo pedido vai para a OpenAI.

Entra por titulos.gemini_gerar, que é por onde passam todas as chamadas (escolha dos cortes, títulos, tela
dividida, curadoria, thumbnails, roteiros, pesquisa de assuntos). Devolve um objeto com .text e .model_version,
como o Gemini.

A OpenAI não recebe vídeo: o trecho da live vira um quadro a cada 2 s (com o tempo escrito antes de cada um)
mais a transcrição do áudio com os tempos. Ela vê e lê, mas não ouve o tom da voz (grito, risada).
A API da OpenAI é paga (não tem plano gratuito).
"""
import base64
import glob
import json
import os
import subprocess
import tempfile
import threading
import time

from . import config

URL = "https://api.openai.com/v1"
MODELOS = ("gpt-4o-mini", "gpt-4.1-mini", "gpt-5-mini")  # tentados em ordem, depois do escolhido nas configurações
MODELO_AUDIO = ("whisper-1", "gpt-4o-mini-transcribe")  # o whisper-1 devolve o tempo de cada fala
QUADRO_A_CADA_S = 2
TIMEOUT_S = 180
COTA = ("429", "RESOURCE_EXHAUSTED", "quota")

_lock = threading.Lock()
_pausa_gemini = 0.0   # até quando o Gemini fica de fora (o limite acabou)
_modelo_ok = None     # modelo da OpenAI que já respondeu


class Resposta:
    def __init__(self, text, modelo):
        self.text = text
        self.model_version = f"openai:{modelo}"


def chave():
    return (config.get("chave_openai").strip() or os.environ.get("OPENAI_API_KEY") or "").strip()


def e_cota(erro):
    txt = f"{type(erro).__name__} {erro}"
    return any(x in txt for x in COTA)


def pausar_gemini(erro):
    """O limite do Gemini acabou: as próximas chamadas vão direto para a OpenAI por um tempo
    (30 min se foi o limite do dia; 1 min se foi o limite por minuto)."""
    global _pausa_gemini
    por_dia = "PerDay" in str(erro) or "per day" in str(erro).lower()
    with _lock:
        _pausa_gemini = time.time() + (30 * 60 if por_dia else 60)


def gemini_em_pausa():
    return bool(chave()) and time.time() < _pausa_gemini


# ------------------------------------------------------------------ pedido
def _post(caminho, **kw):
    import httpx
    r = httpx.post(URL + caminho, headers={"Authorization": f"Bearer {chave()}"}, timeout=TIMEOUT_S, **kw)
    if r.status_code >= 400:
        try:
            msg = r.json().get("error", {}).get("message") or r.text
        except ValueError:
            msg = r.text
        raise RuntimeError(f"OpenAI {r.status_code}: {str(msg)[:300]}")
    return r.json()


def _mmss(s):
    return f"{int(s) // 60:02d}:{int(s) % 60:02d}"


def _imagem(dados, mime="image/jpeg", detalhe="auto"):
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(dados).decode()}",
                                               "detail": detalhe}}


def _transcrever(mp3):
    erro = None
    for modelo in MODELO_AUDIO:
        try:
            with open(mp3, "rb") as f:
                dados = {"model": modelo, "response_format": "verbose_json" if modelo == "whisper-1" else "json"}
                r = _post("/audio/transcriptions", data=dados, files={"file": ("audio.mp3", f, "audio/mpeg")})
            segs = r.get("segments") or []
            if segs:
                return "\n".join(f"[{_mmss(s.get('start', 0))}] {str(s.get('text', '')).strip()}" for s in segs)
            return str(r.get("text") or "").strip()
        except Exception as e:  # noqa - tenta o próximo modelo de áudio
            erro = e
    raise erro


def _video(dados):
    """Vídeo -> quadros com o tempo de cada um + transcrição do áudio."""
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with tempfile.TemporaryDirectory() as d:
        mp4 = os.path.join(d, "v.mp4")
        with open(mp4, "wb") as f:
            f.write(dados)
        ff = [config.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", mp4]
        subprocess.run([*ff, "-vf", f"fps=1/{QUADRO_A_CADA_S},scale=512:-2", "-q:v", "6", os.path.join(d, "q%04d.jpg")],
                       capture_output=True, creationflags=flags)
        subprocess.run([*ff, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k", os.path.join(d, "a.mp3")],
                       capture_output=True, creationflags=flags)
        partes = [{"type": "text", "text": f"Este vídeo chega como um quadro a cada {QUADRO_A_CADA_S} segundos (o tempo "
                                           "MM:SS vem escrito antes de cada quadro) e, no fim, a transcrição do áudio "
                                           "com os tempos. Você não ouve o som: reações só pelo que se vê e se lê."}]
        for i, q in enumerate(sorted(glob.glob(os.path.join(d, "q*.jpg")))):
            partes.append({"type": "text", "text": f"[{_mmss(i * QUADRO_A_CADA_S)}]"})
            with open(q, "rb") as f:
                partes.append(_imagem(f.read(), detalhe="low"))
        fala = ""
        if os.path.exists(os.path.join(d, "a.mp3")):
            try:
                fala = _transcrever(os.path.join(d, "a.mp3"))
            except Exception as e:  # noqa - sem a fala, ainda dá para analisar pela imagem
                fala = f"(não deu para transcrever: {e})"
        partes.append({"type": "text", "text": "TRANSCRIÇÃO DO ÁUDIO:\n" + (fala or "(sem fala)")})
        return partes


def _conteudo(contents):
    """Pedido no formato do Gemini (texto, lista de textos e Parts) -> lista de partes da OpenAI."""
    itens = contents if isinstance(contents, (list, tuple)) else [contents]
    out = []
    for it in itens:
        if isinstance(it, str):
            out.append({"type": "text", "text": it})
            continue
        blob = getattr(it, "inline_data", None)
        if blob is not None and blob.data:
            mime = blob.mime_type or ""
            if mime.startswith("video/"):
                out += _video(blob.data)
            elif mime.startswith("image/"):
                out.append(_imagem(blob.data, mime))
            continue
        if getattr(it, "text", None):
            out.append({"type": "text", "text": it.text})
    return out


def _sistema(cfg):
    si = getattr(cfg, "system_instruction", None)
    if not si:
        return None
    if isinstance(si, str):
        return si
    return "\n".join(getattr(p, "text", "") or "" for p in getattr(si, "parts", None) or []) or str(si)


def gerar(contents, cfg):
    """O mesmo pedido do Gemini, respondido pela OpenAI."""
    global _modelo_ok
    msgs = []
    sistema = _sistema(cfg)
    if sistema:
        msgs.append({"role": "system", "content": sistema})
    msgs.append({"role": "user", "content": _conteudo(contents)})
    corpo = {"messages": msgs}
    esquema = getattr(cfg, "response_json_schema", None)
    if esquema:
        corpo["response_format"] = {"type": "json_schema", "json_schema": {"name": "resposta", "schema": esquema}}
    elif getattr(cfg, "response_mime_type", None) == "application/json":
        corpo["response_format"] = {"type": "json_object"}
    escolhido = config.get("modelo_openai").strip()
    candidatos = list(dict.fromkeys([m for m in (escolhido, _modelo_ok, *MODELOS) if m]))
    erro = None
    for modelo in candidatos:
        try:
            r = _post("/chat/completions", json={**corpo, "model": modelo})
            texto = (r["choices"][0]["message"].get("content") or "").strip()
            if not texto:
                raise RuntimeError("resposta vazia da OpenAI")
            _modelo_ok = modelo
            return Resposta(texto, r.get("model") or modelo)
        except Exception as e:  # noqa
            erro = e
            txt = str(e)
            # modelo que não existe nesta conta: tenta o próximo. Chave errada, sem crédito ou limite: para.
            if not ("OpenAI 404" in txt or "model" in txt.lower() and ("does not exist" in txt or "not found" in txt
                                                                    or "not supported" in txt)):
                break
    raise erro


def testar():
    """Confere a chave com um pedido mínimo. Devolve o modelo que respondeu."""
    return gerar("Responda só: ok", None).model_version
