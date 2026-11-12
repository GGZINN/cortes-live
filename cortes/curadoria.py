"""Curadoria: a cada 10 cortes de um mesmo streamer (um "lote"), escolhe os 2 que valem postar.

Com a chave do Gemini, a IA compara os 10 cortes (nota, gancho, por que retém, fala, duração)
e escolhe os 2 com mais chance de viralizar, evitando dois parecidos. Sem chave, usa a nota.
"""
import json
import os
import threading
from typing import List

from pydantic import BaseModel

from . import config

TAM_LOTE = 10
POR_LOTE = 2
ARQ = os.path.join(config.CLIPS_DIR, "lotes.json")

_lock = threading.RLock()
_lotes = {}  # "canal|lote" -> {"status": "escolhendo"|"feito", "resumo": str, "fonte": "ia"|"nota"}
if os.path.exists(ARQ):
    try:
        with open(ARQ, encoding="utf-8") as f:
            _lotes = json.load(f)
        for v in _lotes.values():  # escolha interrompida ao fechar o programa: refaz
            if v.get("status") == "escolhendo":
                v["status"] = "pendente"
    except Exception:
        _lotes = {}


def _salvar():
    config.salvar_json(ARQ, _lotes, indent=1)


def canal_key(clip):
    return (clip.get("canal") or "").strip().lower()


def _k(canal, lote):
    return f"{canal}|{lote}"


def estado():
    with _lock:
        return dict(_lotes)


# ------------------------------------------------------------------ lotes
def atribuir_lote(clip, clips):
    """Define clip['lote'] (chamado com a lista atual de cortes, antes de inserir o novo)."""
    canal = canal_key(clip)
    meus = [c for c in clips if canal_key(c) == canal and c.get("lote")]
    if not meus:
        clip["lote"] = 1
        return
    ultimo = max(c["lote"] for c in meus)
    n = sum(1 for c in meus if c["lote"] == ultimo)
    st = _lotes.get(_k(canal, ultimo), {}).get("status")
    clip["lote"] = ultimo + 1 if (n >= TAM_LOTE or st in ("escolhendo", "feito")) else ultimo


def preencher_lotes_antigos(clips):
    """Cortes de antes da curadoria existir: distribui em lotes por ordem de criação."""
    sem = [c for c in clips if not c.get("lote")]
    if not sem:
        return False
    for c in sorted(sem, key=lambda x: x.get("criado", "")):
        atribuir_lote(c, [x for x in clips if x.get("lote")])
    return True


def verificar(forcar=None):
    """Dispara a escolha dos lotes completos (10 cortes, todos processados) ainda não escolhidos.
    forcar=(canal, lote) escolhe um lote incompleto agora."""
    from . import clipper
    clips = clipper.list_clips()
    grupos = {}
    for c in clips:
        if c.get("lote"):
            grupos.setdefault((canal_key(c), c["lote"]), []).append(c)
    for (canal, lote), cs in grupos.items():
        st = _lotes.get(_k(canal, lote), {}).get("status")
        if st in ("escolhendo", "feito"):
            continue
        fechado = len(cs) >= TAM_LOTE or (forcar == (canal, lote))
        processados = all(c["status"] in ("pronto", "erro") for c in cs)
        if fechado and processados:
            with _lock:
                _lotes[_k(canal, lote)] = {"status": "escolhendo"}
                _salvar()
            threading.Thread(target=_escolher, args=(canal, lote), daemon=True).start()


# ------------------------------------------------------------------ escolha
class Escolhido(BaseModel):
    id: str
    motivo: str


class Escolha(BaseModel):
    escolhidos: List[Escolhido]
    resumo: str


SISTEMA = """Você é o estrategista de um canal brasileiro de cortes de lives que posta no TikTok, YouTube Shorts e Reels.
Recebe um lote de cortes já prontos de um mesmo streamer e escolhe SÓ os que valem ser postados.

Escolha os cortes com maior chance de segurar a pessoa até o fim e viralizar:
- gancho forte logo no primeiro segundo (frase, grito, ação já acontecendo);
- desfecho claro (punchline, jogada, reação, revelação) sem enrolação;
- se entende sozinho, sem contexto da live;
- emoção ou reação forte e genuína, situação inesperada, frase marcante;
- duração boa para vídeo curto (o ideal é 20 a 40 s);
- variedade: não escolha dois cortes da mesma situação ou do mesmo tipo de piada.
Descarte cortes com começo parado, conversa morna, piada interna ou que dependem de contexto.
Use exatamente os ids recebidos. Para cada escolhido, explique em 1 frase curta por que ele vai performar.
"resumo": 1 frase sobre o lote (o que tinha de melhor e por que os outros ficaram de fora). Português do Brasil."""


def _descricao(c):
    fala = " ".join((c.get("transcricao") or "").split())
    if len(fala) > 500:
        fala = fala[:500] + "…"
    return (f"- id: {c['id']}\n  duração: {c.get('duracao')} s | nota da IA: {c.get('pontuacao')} | tipo: {c.get('motivo')}\n"
            f"  gancho: {c.get('gancho') or '-'}\n  por que retém: {c.get('por_que') or '-'}\n"
            f"  título: {c.get('titulo') or '-'}\n  fala: {fala or '(sem fala)'}")


def _por_nota(prontos, k):
    ordem = sorted(prontos, key=lambda c: (-(c.get("pontuacao") or 0), abs((c.get("duracao") or 30) - 30)))
    return [(c["id"], "Maior nota da IA no lote.") for c in ordem[:k]]


def _por_ia(prontos, k):
    from google import genai
    from google.genai import types
    from .analisador import chave_gemini
    from .titulos import MODELOS_GEMINI, _aprendizado, _schema_sem_refs, gemini_gerar
    if not chave_gemini():
        return None, None
    cliente = genai.Client(api_key=chave_gemini())
    texto = (f"Streamer: {prontos[0].get('canal')}. Escolha exatamente {k} corte(s) deste lote de {len(prontos)}.\n\n"
             + "\n\n".join(_descricao(c) for c in prontos) + _aprendizado())
    cfg = types.GenerateContentConfig(system_instruction=SISTEMA, response_mime_type="application/json",
                                      response_json_schema=_schema_sem_refs(Escolha))
    r = gemini_gerar(cliente, MODELOS_GEMINI, texto, cfg)
    e = Escolha.model_validate_json(r.text)
    ids = {c["id"] for c in prontos}
    out, vistos = [], set()
    for x in e.escolhidos:
        if x.id in ids and x.id not in vistos:
            vistos.add(x.id)
            out.append((x.id, x.motivo.strip()))
    return out[:k], e.resumo.strip()


def _escolher(canal, lote):
    from . import clipper
    try:
        cs = [c for c in clipper.list_clips() if canal_key(c) == canal and c.get("lote") == lote]
        prontos = [c for c in cs if c["status"] == "pronto"]
        k = min(POR_LOTE, len(prontos))
        escolha, resumo, fonte = [], "", "nota"
        if k:
            try:
                escolha, resumo = _por_ia(prontos, k)
                fonte = "ia" if escolha else "nota"
            except Exception as e:  # noqa - sem internet / limite grátis: usa a nota
                resumo = f"IA indisponível ({type(e).__name__}); escolhidos pela maior nota."
                escolha = None
            escolha = escolha or []
            if len(escolha) < k:  # completa com a maior nota se a IA devolveu menos
                usados = {i for i, _ in escolha}
                escolha += [x for x in _por_nota([c for c in prontos if c["id"] not in usados], k - len(escolha))]
        motivos = dict(escolha)
        for c in cs:
            if c["id"] in motivos:
                clipper.update_clip(c["id"], escolhido=True, motivo_escolha=motivos[c["id"]])
            else:
                clipper.update_clip(c["id"], escolhido=False, motivo_escolha=None)
        with _lock:
            _lotes[_k(canal, lote)] = {"status": "feito", "resumo": resumo, "fonte": fonte}
            _salvar()
    except Exception as e:  # noqa
        with _lock:
            _lotes[_k(canal, lote)] = {"status": "pendente", "resumo": f"erro: {type(e).__name__}"}
            _salvar()
