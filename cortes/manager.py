"""Junta gravador + IA de cortes de uma live e decide quando cortar.

Modo "ia" (com chave do Gemini): a IA assiste a live (imagem + áudio) e escolhe os cortes.
Modo "audio" (sem chave): reserva simples, corta nos picos de áudio (grito, risada, explosão).
O chat não é usado para decidir cortes.
"""
import datetime
import os
import shutil
import threading
import time
import uuid

from . import clipper, config, curadoria, videos
from .analisador import Analisador, chave_gemini, limiar_nota
from .chat import platform_of, slug_of
from .detector import Detector
from .recorder import Recorder


def threshold():
    # modo áudio: sensibilidade 1 → 4.2 (raro) ... 10 → 1.5 (muitos cortes)
    return 4.5 - 0.3 * config.get("sensibilidade")


def modo():
    return "ia" if chave_gemini() else "audio"


class Stream:
    def __init__(self, url):
        self.id = uuid.uuid4().hex[:8]
        self.url = url.strip()
        self.platform = platform_of(self.url)
        self.name = slug_of(self.url) or self.url
        self.title = ""
        self.workdir = os.path.join(config.BUFFER_DIR, self.id)
        self.detector = Detector()
        self.recorder = Recorder(self.url, self.workdir, self.detector.add_audio, on_info=self._on_info)
        self.analisador = None
        self.pending = None        # corte (modo áudio) esperando o vídeo "chegar"
        self.last_trigger = -1e9
        self.clip_count = 0
        self.score = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def _on_info(self, info):
        if info.get("channel"):
            self.name = info["channel"]
        self.title = info.get("title", "")

    # ---------- ciclo ----------
    def start(self):
        self.recorder.start()
        self.analisador = Analisador(self)
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self._stop.set()
        self.recorder.stop()
        if self.analisador:
            self.analisador.stop()
        threading.Timer(3, lambda: shutil.rmtree(self.workdir, ignore_errors=True)).start()

    def _loop(self):
        last_t = 0.0
        fechou = False
        while not self._stop.wait(1.0):
            if self.recorder.video:
                # vídeo: quem corta é só a IA. Acabou -> a curadoria fecha o lote com os cortes que saíram
                if self.recorder.status == "terminado" and not fechou:
                    fechou = True
                    if self.clip_count:
                        curadoria.fechar_canal(self.name)
                continue
            if self.recorder.status != "gravando":
                self._check_pending()
                continue
            t = self.recorder.t
            if t <= last_t:
                continue
            last_t = t
            score, reason = self.detector.tick(t)   # mede o áudio (gráfico + modo reserva)
            self.score = score
            if modo() == "audio" and score >= threshold():
                self._trigger(t, score)
            self._check_pending()

    # ---------- modo áudio (reserva, sem chave do Gemini) ----------
    def _trigger(self, t, score):
        before, after = config.get("segundos_antes"), config.get("segundos_depois")
        max_len = config.get("duracao_maxima")
        p = self.pending
        if p and t <= p["end"] + 5:
            p["end"] = min(t + after, p["start"] + max_len)
            p["meta"]["pontuacao"] = round(max(p["meta"]["pontuacao"], score), 1)
            return
        if t - self.last_trigger < config.get("intervalo_minimo") or p:
            return
        self.last_trigger = t
        start = max(0.0, t - before)
        self.pending = {"start": start, "end": t + after, "created": time.time(),
                        "meta": {"motivo": "Pico de áudio", "pontuacao": round(score, 1),
                                 "pico_rel": round(t - start, 1)}}

    def _check_pending(self):
        p = self.pending
        if not p:
            return
        stale = time.time() - p["created"] > 180
        if self.recorder.covered_until() < p["end"] and not stale:
            return
        self.pending = None
        self.criar_corte(p["start"], p["end"], p["meta"])

    # ---------- cria o corte (usado pela IA e pelo modo áudio) ----------
    def criar_corte(self, start, end, meta):
        segs = self.recorder.segments_between(start, end)
        if not segs:
            return None
        livre = config.disco_livre_mb()
        if livre < config.DISCO_MIN_CORTES_MB:  # cada corte guarda o vídeo bruto até terminar: não deixa o disco encher
            print(f"[{self.name}] corte pulado: só {livre} MB livres no disco", flush=True)
            return None
        with self._lock:
            cid = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
            self.clip_count += 1
        d = os.path.join(config.CLIPS_DIR, cid)
        os.makedirs(d, exist_ok=True)
        raw = os.path.join(d, "bruto.ts")
        with open(raw, "wb") as out:
            for s in segs:
                try:
                    with open(s["path"], "rb") as f:
                        shutil.copyfileobj(f, out)
                except OSError:
                    pass
        offset = max(0.0, start - segs[0]["start"])
        dur = min(end, segs[-1]["end"]) - start
        clip = {
            "id": cid, "canal": self.name, "plataforma": self.platform, "titulo_live": self.title,
            "criado": datetime.datetime.now().isoformat(timespec="seconds"),
            "duracao": round(dur, 1), "status": "na fila", "url_live": self.url,
            "motivo": "Corte", "pontuacao": 0, "pico_rel": min(3.0, dur / 2),
            "bruto_offset": round(offset, 3),  # onde o trecho começa no bruto.ts (para refazer o corte)
        }
        clip.update(meta)
        clipper.add_clip(clip)
        clipper.enqueue(cid, raw, offset, dur)
        return cid

    def to_dict(self, series=True):
        ia = self.analisador
        d = {
            "id": self.id, "url": self.url, "nome": self.name, "plataforma": self.platform,
            "titulo": self.title, "status": self.recorder.status, "detalhe": self.recorder.status_detail,
            "tempo": round(self.recorder.t), "cortes": self.clip_count, "modo": modo(),
            "pontuacao": round(self.score, 2), "limite": round(threshold(), 2),
            "aguardando_corte": bool(self.pending),
            "video": self.recorder.video, "total": round(self.recorder.total),
            "ia_status": ia.status if ia else "", "limite_nota": limiar_nota(),
        }
        if series:
            d["serie"] = list(self.detector.series)[-300:]
            d["analises"] = list(ia.analises) if ia else []
        return d


streams: dict[str, Stream] = {}


def add_stream(url):
    for s in streams.values():
        if s.url.rstrip("/").lower() == url.strip().rstrip("/").lower():
            return s
    s = Stream(url)
    streams[s.id] = s
    s.start()
    return s


def remove_stream(sid):
    s = streams.pop(sid, None)
    if s:
        s.stop()
        videos.esquecer(s.url)  # vídeo tirado do painel: colar o link de novo refaz do começo
