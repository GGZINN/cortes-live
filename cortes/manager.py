"""Junta gravador + chat + detector de uma live e decide quando cortar."""
import datetime
import os
import shutil
import threading
import time
import uuid

from . import clipper, config
from .chat import ChatReader, platform_of, slug_of
from .detector import Detector
from .recorder import Recorder


def threshold():
    # sensibilidade 1 → 4.2 (raro) ... 10 → 1.5 (muitos cortes)
    return 4.5 - 0.3 * config.get("sensibilidade")


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
        self.chat = ChatReader(self.url, self._on_chat)
        self.pending = None        # corte esperando o vídeo "chegar"
        self.last_trigger = -1e9
        self.clip_count = 0
        self.score = 0.0
        self._stop = threading.Event()
        self._chat_started = False

    # ---------- callbacks ----------
    def _on_info(self, info):
        if info.get("channel"):
            self.name = info["channel"]
        self.title = info.get("title", "")
        if not self._chat_started:
            self.chat.video_url = info.get("webpage_url")
            self.chat.start()
            self._chat_started = True

    def _on_chat(self, text):
        if self.recorder.status == "gravando":
            self.detector.add_chat(self.recorder.t, text)

    # ---------- ciclo ----------
    def start(self):
        self.recorder.start()
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self._stop.set()
        self.recorder.stop()
        self.chat.stop()
        threading.Timer(3, lambda: shutil.rmtree(self.workdir, ignore_errors=True)).start()

    def _loop(self):
        last_t = 0.0
        while not self._stop.wait(1.0):
            if self.recorder.status != "gravando":
                self._check_pending()
                continue
            t = self.recorder.t
            if t <= last_t:
                continue
            last_t = t
            score, reason = self.detector.tick(t)
            self.score = score
            if score >= threshold():
                self._trigger(t, score, reason)
            self._check_pending()

    def _trigger(self, t, score, reason):
        before, after = config.get("segundos_antes"), config.get("segundos_depois")
        max_len = config.get("duracao_maxima")
        p = self.pending
        if p and t <= p["end"] + 5:
            # momento continua quente: estica o corte atual
            p["end"] = min(t + after, p["start"] + max_len)
            p["score"] = max(p["score"], score)
            return
        if t - self.last_trigger < config.get("intervalo_minimo"):
            return
        if p:  # já tem um esperando
            return
        self.last_trigger = t
        self.pending = {"start": max(0.0, t - before), "end": t + after, "peak": t,
                        "score": score, "reason": reason, "created": time.time()}

    def _check_pending(self):
        p = self.pending
        if not p:
            return
        covered = self.recorder.covered_until()
        stale = time.time() - p["created"] > 180
        if covered < p["end"] and not stale:
            return
        self.pending = None
        segs = self.recorder.segments_between(p["start"], p["end"])
        if not segs:
            return
        cid = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
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
        offset = max(0.0, p["start"] - segs[0]["start"])
        dur = min(p["end"], segs[-1]["end"]) - p["start"]
        motivos = {"chat": "Chat explodiu", "hype": "Chat reagindo (kkkk / emotes)", "áudio": "Pico de áudio"}
        palavras = self.detector.top_words(p["peak"] - 3, p["peak"] + 10)
        self.clip_count += 1
        clipper.add_clip({
            "id": cid, "canal": self.name, "plataforma": self.platform, "titulo_live": self.title,
            "criado": datetime.datetime.now().isoformat(timespec="seconds"),
            "motivo": motivos.get(p["reason"], p["reason"]), "pontuacao": round(p["score"], 1),
            "palavras_chat": palavras, "duracao": round(dur, 1), "status": "na fila",
            "url_live": self.url, "pico_rel": round(p["peak"] - p["start"], 1),
        })
        clipper.enqueue(cid, raw, offset, dur)

    def to_dict(self, series=True):
        d = {
            "id": self.id, "url": self.url, "nome": self.name, "plataforma": self.platform,
            "titulo": self.title, "status": self.recorder.status, "detalhe": self.recorder.status_detail,
            "chat": self.chat.status, "msgs_chat": self.detector.total_chat,
            "tempo": round(self.recorder.t), "pontuacao": round(self.score, 2),
            "limite": round(threshold(), 2), "cortes": self.clip_count,
            "aguardando_corte": bool(self.pending),
        }
        if series:
            d["serie"] = list(self.detector.series)[-300:]
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
