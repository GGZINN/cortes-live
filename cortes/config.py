"""Configuração global, caminhos e localização do ffmpeg."""
import glob
import json
import os
import shutil
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "dados")
BUFFER_DIR = os.path.join(DATA_DIR, "buffer")
CLIPS_DIR = os.path.join(ROOT, "cortes_prontos")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")

for d in (DATA_DIR, BUFFER_DIR, CLIPS_DIR):
    os.makedirs(d, exist_ok=True)

DEFAULTS = {
    "sensibilidade": 5,        # 1 (poucos cortes) .. 10 (muitos cortes)
    "segundos_antes": 25,      # quanto pegar antes do pico
    "segundos_depois": 10,     # quanto pegar depois do pico
    "duracao_maxima": 60,      # limite de um corte (picos seguidos são unidos)
    "intervalo_minimo": 60,    # segundos mínimos entre dois cortes da mesma live
    "legenda": True,
    "modelo_whisper": "small", # tiny / base / small / medium
    "idioma": "pt",            # "pt", "en", ... ou "auto"
    "buffer_minutos": 6,       # quanto da live fica guardado em disco
    "chave_gemini": "",        # opcional e grátis: títulos gerados por IA (Gemini)
    "chave_claude": "",        # opcional: títulos gerados por IA (Claude)
}

SECRET_KEYS = {"chave_gemini", "chave_claude"}


def _mask(v):
    return ("••••••" + v[-4:]) if v else ""

_lock = threading.Lock()
_settings = dict(DEFAULTS)
if os.path.exists(SETTINGS_FILE):
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            _settings.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
    except Exception:
        pass


def get(key):
    with _lock:
        return _settings[key]


def all_settings(mask=True):
    with _lock:
        s = dict(_settings)
    if mask:
        for k in SECRET_KEYS:
            s[k] = _mask(s[k])
    return s


def update(new: dict):
    with _lock:
        for k, v in new.items():
            if k not in DEFAULTS:
                continue
            if k in SECRET_KEYS and str(v).startswith("••"):
                continue  # campo mascarado, não mudou
            cur = DEFAULTS[k]
            if isinstance(cur, bool):
                v = bool(v)
            elif isinstance(cur, int):
                v = int(float(v))
            else:
                v = str(v).strip()
            _settings[k] = v
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(_settings, f, indent=2, ensure_ascii=False)
    return all_settings()


def find_ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [os.path.join(local, "Microsoft", "WinGet", "Links", "ffmpeg.exe")]
    candidates += glob.glob(os.path.join(local, "Microsoft", "WinGet", "Packages", "Gyan.FFmpeg*", "*", "bin", "ffmpeg.exe"))
    candidates += glob.glob(os.path.join(ROOT, "ffmpeg", "**", "ffmpeg.exe"), recursive=True)
    for c in candidates:
        if os.path.exists(c):
            return c
    raise RuntimeError("ffmpeg não encontrado. Rode: winget install Gyan.FFmpeg")


FFMPEG = None


def ffmpeg():
    global FFMPEG
    if FFMPEG is None:
        FFMPEG = find_ffmpeg()
    return FFMPEG
