"""Detecta momentos marcantes combinando explosão de chat, emotes de hype e picos de áudio."""
import re
import statistics
import threading
from collections import Counter, deque

from .chat import hype_score

HISTORY = 300  # segundos de histórico para a "linha de base"
WARMUP = 60    # não corta nada antes de ter esse tanto de histórico


def _z(value, hist, floor, rel=0.0):
    """Quantos desvios-padrão o valor atual está acima do normal recente."""
    if len(hist) < 10:
        return 0.0
    mean = statistics.fmean(hist)
    sd = max(statistics.pstdev(hist), floor, rel * abs(mean))
    return (value - mean) / sd


class Detector:
    def __init__(self):
        self.lock = threading.Lock()
        self.audio = deque()      # (t, db)
        self.chat = deque()       # (t, hype, texto)
        self.hist_rate = deque(maxlen=HISTORY)
        self.hist_hype = deque(maxlen=HISTORY)
        self.hist_audio = deque(maxlen=HISTORY)
        self.series = deque(maxlen=600)  # para o gráfico
        self.total_chat = 0
        self.last_score_t = 0.0

    def add_audio(self, t, db):
        with self.lock:
            self.audio.append((t, db))
            while self.audio and self.audio[0][0] < t - 30:
                self.audio.popleft()

    def add_chat(self, t, text):
        with self.lock:
            self.chat.append((t, hype_score(text), text or ""))
            self.total_chat += 1
            while self.chat and self.chat[0][0] < t - 60:
                self.chat.popleft()

    def top_words(self, start, end, n=3):
        with self.lock:
            msgs = [m for (t, _, m) in self.chat if start <= t <= end]
        c = Counter()
        for m in msgs:
            for w in set(re.findall(r"[\wÀ-ÿ?!]{2,}", m)):
                c[w.lower()] += 1
        return [w for w, _ in c.most_common(n)]

    def tick(self, t):
        """Chamado 1x por segundo. Retorna (score, motivo) ou None durante aquecimento."""
        with self.lock:
            win = [c for c in self.chat if c[0] > t - 5]
            rate = len(win) / 5
            hype = sum(c[1] for c in win) / 5
            aud = [a[1] for a in self.audio if a[0] > t - 2]
            level = sum(aud) / len(aud) if aud else -90.0

            zc = _z(rate, self.hist_rate, 0.5, 0.25)
            zh = _z(hype, self.hist_hype, 0.3, 0.25)
            za = _z(level, self.hist_audio, 2.5)
            has_chat = sum(self.hist_rate) > 30  # chat ativo nos últimos minutos

            if has_chat:
                score = 0.45 * max(zc, 0) + 0.30 * max(zh, 0) + 0.25 * max(za, 0)
            else:
                score = 0.8 * max(za, 0)

            warm = len(self.hist_audio) >= WARMUP
            self.hist_rate.append(rate)
            self.hist_hype.append(hype)
            self.hist_audio.append(level)

            parts = {"chat": 0.45 * max(zc, 0), "hype": 0.30 * max(zh, 0), "áudio": 0.25 * max(za, 0) if has_chat else score}
            reason = max(parts, key=parts.get)
            self.series.append({"t": round(t, 1), "s": round(score, 2), "chat": round(rate, 2), "db": round(level, 1)})
            return (score if warm else 0.0), reason
