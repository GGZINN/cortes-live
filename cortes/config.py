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
    "youtube_categoria": "20", # categoria dos vídeos enviados ao YouTube (20 = Jogos)
    "thumb_automatica": True,  # ao enviar ao YouTube, manda junto a thumbnail escolhida (gera se não tiver)
    "aprendizado_ativo": True, # o que funcionou no canal entra nas instruções da IA (títulos e escolha dos cortes)
    "aprendizado_base": "cortes",  # aprender com: "cortes" (só vídeos postados pelo app) ou "canal" (todos os vídeos)
    "formato_corte": "auto",   # "auto": gameplay com câmera vira tela dividida (câmera em cima, jogo embaixo); "padrao": sempre o de antes
    "selo_live": True,         # logo da plataforma + link da live, pequeno, no meio do vídeo
    "cortar_pausas": True,     # pausa entre frases acima de 0,3 s vira 0,2 s (fora do gameplay)
    "zoom_cortes": True,       # jump cuts: a imagem do streamer alterna 100% / 110% a cada ~2,8 s
}

SECRET_KEYS = {"chave_gemini", "chave_claude"}


def _mask(v):
    return ("••••••" + v[-4:]) if v else ""


DISCO_MIN_CORTES_MB = 1024   # abaixo disso não cria cortes novos (cada um guarda ~20-40 MB de vídeo bruto)
DISCO_MIN_GRAVAR_MB = 512    # abaixo disso a gravação da live espera (o buffer ocupa ~300 MB)
DISCO_MIN_PROCESSAR_MB = 200 # abaixo disso a fila de cortes espera em vez de falhar


def disco_livre_mb():
    try:
        return shutil.disk_usage(ROOT).free // (1024 * 1024)
    except OSError:
        return 10 ** 9


def salvar_json(caminho, dados, **kw):
    """Grava num arquivo temporário e só então troca pelo de verdade: se o disco estiver cheio,
    o arquivo antigo fica intacto (gravar direto por cima deixava o arquivo zerado). Não levanta erro:
    os dados continuam na memória e vão para o disco no próximo salvamento."""
    tmp = caminho + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(dados, f, ensure_ascii=False, **kw)
        os.replace(tmp, caminho)
        return True
    except OSError as e:
        print(f"[salvar] não deu para gravar {os.path.basename(caminho)}: {e}", flush=True)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False

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
        salvar_json(SETTINGS_FILE, _settings, indent=2)
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
