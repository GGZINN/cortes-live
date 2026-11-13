"""Grava a live em segmentos curtos (buffer circular) e entrega o nível de áudio em tempo real."""
import math
import os
import subprocess
import sys
import threading
import time
from collections import deque

import numpy as np
import yt_dlp

from . import config

SAMPLE_RATE = 16000
SEG_SECONDS = 4


class Offline(Exception):
    pass


class _Silent:
    def debug(self, msg):
        pass

    warning = info = error = debug


def resolve(url):
    """Usa o yt-dlp para achar o endereço HLS da live (Twitch, YouTube, Kick...)."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "logger": _Silent(),
        "js_runtimes": {"node": {}, "deno": {}},
        # 1) stream com áudio junto (Twitch/Kick)  2) vídeo+áudio separados (YouTube)
        "format": "best[height<=1080][protocol^=m3u8][acodec!=none][vcodec!=none]/"
                  "bv*[height<=1080][protocol^=m3u8]+ba[protocol^=m3u8]/best",
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e).replace("ERROR: ", "")
        if "not currently live" in msg or "is offline" in msg.lower():
            msg = "Canal não está ao vivo agora"
        raise Offline(msg[:200])
    lista = info.get("_type") == "playlist"
    if lista and info.get("entries"):
        info = next(iter(info["entries"]))
    # link de um vídeo já publicado (não de um canal): vira cortes pelo modo vídeo (videos.py)
    if not lista and info.get("duration") and (info.get("live_status") in ("not_live", "was_live")
                                               or (info.get("live_status") is None and info.get("is_live") is False)):
        tam = info.get("filesize_approx") or info.get("filesize") or 0
        return {
            "video": True, "duracao": float(info["duration"]), "tamanho_mb": int(tam) // 2 ** 20,
            "title": info.get("title") or "",
            "channel": info.get("uploader") or info.get("channel") or info.get("uploader_id") or "",
            "webpage_url": info.get("webpage_url") or url,
        }
    if info.get("live_status") not in (None, "is_live") or info.get("is_live") is False:
        raise Offline("Canal não está ao vivo agora")
    if info.get("requested_formats"):
        urls = [f["url"] for f in info["requested_formats"]]
    else:
        urls = [info.get("url")]
    if not urls or not urls[0]:
        raise Offline("Não achei o vídeo da live")
    return {
        "urls": urls,
        "headers": info.get("http_headers") or {},
        "title": info.get("title") or "",
        "channel": info.get("uploader") or info.get("channel") or info.get("uploader_id") or "",
        "webpage_url": info.get("webpage_url") or url,
    }


class Recorder:
    def __init__(self, url, workdir, on_audio, on_info=None, log=print):
        self.url = url
        self.workdir = workdir
        self.on_audio = on_audio
        self.on_info = on_info
        self.log = log
        os.makedirs(workdir, exist_ok=True)
        self.status = "conectando"
        self.status_detail = ""
        self.t = 0.0               # linha do tempo da gravação (segundos)
        self.video = False         # o link é de um vídeo já publicado (videos.py), não de uma live
        self.total = 0.0           # vídeo: duração
        self.cursor = 0.0          # vídeo: até onde a IA pode assistir agora
        self.terminou = False      # vídeo: a IA já viu até o fim
        self.segments = deque()    # dicts {path, start, end}
        self._seg_lock = threading.Lock()
        self._stop = threading.Event()
        self._proc = None
        self._feeder = None
        self._run = 0
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self._stop.set()
        for p in (self._proc, self._feeder):
            if p and p.poll() is None:
                try:
                    p.kill()
                except Exception:
                    pass

    # ---------- segmentos ----------
    def segments_between(self, start, end):
        with self._seg_lock:
            return [s for s in self.segments if s["end"] > start and s["start"] < end]

    @property
    def ativo(self):
        """Tem imagem chegando para a IA assistir (live gravando ou vídeo sendo analisado)."""
        return self.status in ("gravando", "analisando")

    @property
    def ultima(self):
        """Vídeo: a janela atual vai até o fim (não tem próxima análise para pegar o que ficou na emenda)."""
        return self.video and self.cursor >= self.total - 0.5

    def avancar(self, analisado_ate, proxima):
        """Vídeo: a IA terminou de ver até `analisado_ate`; libera o próximo trecho (ou encerra)."""
        if not self.video:
            return
        if analisado_ate >= self.total - 0.5:
            self.terminou = True
        else:
            self.cursor = min(self.total, proxima)

    def covered_until(self):
        if self.video:
            return self.cursor
        with self._seg_lock:
            return self.segments[-1]["end"] if self.segments else 0.0

    def _prune(self):
        keep = config.get("buffer_minutos") * 60
        limit = self.t - keep
        with self._seg_lock:
            while self.segments and self.segments[0]["end"] < limit:
                seg = self.segments.popleft()
                try:
                    os.remove(seg["path"])
                except OSError:
                    pass

    def _read_segment_list(self, csv_path, offset, known):
        if not os.path.exists(csv_path):
            return
        try:
            with open(csv_path, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            return
        for line in lines:
            parts = line.split(",")
            if len(parts) < 3 or parts[0] in known:
                continue
            known.add(parts[0])
            try:
                s, e = float(parts[1]), float(parts[2])
            except ValueError:
                continue
            with self._seg_lock:
                self.segments.append({
                    "path": os.path.join(os.path.dirname(csv_path), parts[0]),
                    "start": offset + s,
                    "end": offset + e,
                })

    # ---------- loop principal ----------
    def _loop(self):
        while not self._stop.is_set():
            livre = config.disco_livre_mb()
            if livre < config.DISCO_MIN_GRAVAR_MB:
                self.status = "disco cheio"
                self.status_detail = f"Só {livre} MB livres no disco. A gravação volta sozinha quando liberar espaço."
                self._stop.wait(60)
                continue
            self.status = "conectando"
            try:
                info = resolve(self.url)
            except Offline as e:
                self.status, self.status_detail = "offline", str(e)
                self._stop.wait(45)
                continue
            except Exception as e:  # noqa
                self.status, self.status_detail = "erro", str(e)[:200]
                self._stop.wait(30)
                continue
            if self.on_info:
                self.on_info(info)
            if info.get("video"):
                from . import videos
                try:
                    if videos.processar(self, info):
                        return  # vídeo terminado (ou parado): não tem o que reconectar
                except Exception as e:  # noqa
                    self.status, self.status_detail = "erro", str(e)[:200]
                self._stop.wait(60)
                continue
            try:
                self._record(info)
            except Exception as e:  # noqa  (ex.: disco cheio) — antes isso matava a gravação de vez
                self.status, self.status_detail = "erro", str(e)[:200]
                self._stop.wait(30)
                continue
            if not self._stop.is_set():
                self.status = "reconectando"
                self._stop.wait(5)

    def _record(self, info):
        self._run += 1
        offset = self.t
        run_dir = os.path.join(self.workdir, f"run{self._run}")
        os.makedirs(run_dir, exist_ok=True)
        csv_path = os.path.join(run_dir, "lista.csv")

        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        headers = dict(info["headers"])
        ua = headers.pop("User-Agent", None)
        cmd = [config.ffmpeg(), "-hide_banner", "-loglevel", "error"]
        feeder = None
        if "twitch.tv" in self.url:
            # Twitch: Streamlink pula os intervalos comerciais, que senão iriam parar dentro dos cortes
            feeder = subprocess.Popen(
                [sys.executable, "-m", "streamlink", "--twitch-disable-ads", "--twitch-low-latency",
                 "--loglevel", "error", "--stdout", self.url, "1080p60,1080p,936p60,720p60,720p,best"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=flags)
            cmd += ["-i", "pipe:0"]
            a = 0
        else:
            cmd += ["-nostdin"]
            for u in info["urls"]:
                if ua:
                    cmd += ["-user_agent", ua]
                if headers:
                    cmd += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
                cmd += ["-i", u]
            a = len(info["urls"]) - 1  # entrada que tem o áudio
        cmd += [
            # saída 1: segmentos de vídeo (sem recodificar)
            "-map", "0:v:0", "-map", f"{a}:a:0", "-c", "copy",
            "-f", "segment", "-segment_time", str(SEG_SECONDS),
            "-segment_list", csv_path, "-segment_list_type", "csv",
            os.path.join(run_dir, "seg_%06d.ts"),
            # saída 2: áudio cru para análise
            # async=1 preenche buracos com silêncio, mantendo o áudio alinhado com os segmentos
            "-map", f"{a}:a:0", "-af", "aresample=async=1", "-ac", "1", "-ar", str(SAMPLE_RATE),
            "-f", "s16le", "pipe:1",
        ]
        self._proc = proc = subprocess.Popen(cmd, stdin=feeder.stdout if feeder else subprocess.DEVNULL,
                                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags)
        self._feeder = feeder
        if feeder:
            feeder.stdout.close()  # só o ffmpeg lê
        errors = deque(maxlen=5)
        threading.Thread(target=lambda: [errors.append(l.decode(errors="ignore").strip()) for l in proc.stderr], daemon=True).start()

        self.status, self.status_detail = "gravando", info.get("title", "")
        chunk = SAMPLE_RATE // 2  # meio segundo
        samples = 0
        known = set()
        last_list = 0.0
        while not self._stop.is_set():
            data = proc.stdout.read(chunk * 2)
            if not data:
                break
            arr = np.frombuffer(data, dtype=np.int16).astype(np.float32)
            rms = math.sqrt(float(np.mean(arr * arr)) + 1e-9)
            db = 20 * math.log10(rms / 32768 + 1e-9)
            samples += len(arr)
            self.t = offset + samples / SAMPLE_RATE
            self.on_audio(self.t, db)
            now = time.monotonic()
            if now - last_list > 1.0:
                last_list = now
                self._read_segment_list(csv_path, offset, known)
                self._prune()
        for p in (proc, feeder):
            try:
                p and p.kill()
            except Exception:
                pass
        self._read_segment_list(csv_path, offset, known)
        if errors and not self._stop.is_set():
            self.status_detail = " | ".join(errors)[-200:]
