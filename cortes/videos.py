"""Cortes de vídeos já publicados (não só de lives).

O link de um vídeo (YouTube, VOD da Twitch, Kick...) entra pelo mesmo campo das lives. O Recorder percebe que
não é uma live e chama processar(): baixa o vídeo, quebra em segmentos iguais aos da live e deixa a IA
percorrer tudo, janela por janela, sem esperar o tempo passar. Daí em diante é o mesmo caminho da live:
analisador -> criar_corte -> fila de cortes -> curadoria.

O progresso fica em dados/videos.json: se o programa fechar no meio, continua de onde parou (baixa de novo,
mas não repete os cortes). Vídeo terminado não é refeito; tirar do painel apaga o progresso.
"""
import glob
import os
import shutil
import subprocess
import threading

import yt_dlp

from . import config

ARQ = os.path.join(config.DATA_DIR, "videos.json")
ALTURA = 1080
ALTURA_LONGO = 720          # vídeo acima de LONGO_S baixa em 720p (ocupa menos disco)
LONGO_S = 2 * 3600
FOLGA_S = 150               # segmentos mais velhos que isso atrás da análise são apagados

_lock = threading.Lock()
_prog = {}
if os.path.exists(ARQ):
    try:
        import json
        with open(ARQ, encoding="utf-8") as _f:
            _prog = json.load(_f)
    except Exception:
        _prog = {}


class Parado(Exception):
    pass


def _chave(url):
    return url.strip().rstrip("/").lower()


def progresso(url):
    with _lock:
        return dict(_prog.get(_chave(url)) or {})


def _gravar(url, **kw):
    with _lock:
        _prog.setdefault(_chave(url), {}).update(kw)
        config.salvar_json(ARQ, _prog, indent=1)


def esquecer(url):
    """Tirou o vídeo do painel: se colar o link de novo, começa do zero."""
    with _lock:
        if _prog.pop(_chave(url), None) is not None:
            config.salvar_json(ARQ, _prog, indent=1)


def _hms(s):
    s = int(s)
    return f"{s // 3600}h{s % 3600 // 60:02d}" if s >= 3600 else f"{s // 60}min"


def _baixar(rec, info, pasta):
    altura = ALTURA_LONGO if (info.get("duracao") or 0) > LONGO_S else ALTURA
    h264 = f"bv*[height<={altura}][vcodec^=avc1]+ba[acodec^=mp4a]/b[height<={altura}][vcodec^=avc1][acodec!=none]"

    def gancho(d):
        if rec._stop.is_set():
            raise Parado()
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            feito = d.get("downloaded_bytes") or 0
            rec.status_detail = (f"Baixando o vídeo: {100 * feito // total}% de {total // 2 ** 20} MB" if total
                                 else f"Baixando o vídeo: {feito // 2 ** 20} MB")

    opts = {
        "quiet": True, "no_warnings": True, "noplaylist": True, "noprogress": True,
        "js_runtimes": {"node": {}, "deno": {}},
        "format": f"{h264}/bv*[height<={altura}]+ba/b[height<={altura}]/b",
        "outtmpl": os.path.join(pasta, "video.%(ext)s"),
        "ffmpeg_location": os.path.dirname(config.ffmpeg()),
        "progress_hooks": [gancho],
        "retries": 5, "fragment_retries": 5, "concurrent_fragment_downloads": 4,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        baixado = ydl.extract_info(info["webpage_url"], download=True)
    arqs = [a for a in glob.glob(os.path.join(pasta, "video.*")) if not a.endswith((".part", ".ytdl"))]
    if not arqs:
        raise RuntimeError("o vídeo não foi baixado")
    copia = str(baixado.get("vcodec") or "").startswith("avc1") and str(baixado.get("acodec") or "").startswith("mp4a")
    return max(arqs, key=os.path.getsize), copia


def _segmentar(rec, arq, pasta, copia):
    """Quebra o vídeo em segmentos .ts de 4 s, como os da live. Fora de H.264/AAC, converte (demora mais)."""
    from .recorder import SEG_SECONDS
    lista = os.path.join(pasta, "lista.csv")
    codec = ["-c", "copy"] if copia else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                                         "-force_key_frames", f"expr:gte(t,n_forced*{SEG_SECONDS})",
                                         "-c:a", "aac", "-b:a", "160k"]
    cmd = [config.ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", arq,
           "-map", "0:v:0", "-map", "0:a:0", *codec, "-f", "segment", "-segment_time", str(SEG_SECONDS),
           "-segment_list", lista, "-segment_list_type", "csv", os.path.join(pasta, "seg_%06d.ts")]
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    rec._proc = proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=flags)
    _, err = proc.communicate()
    if rec._stop.is_set():
        raise Parado()
    if proc.returncode != 0:
        raise RuntimeError("não deu para preparar o vídeo: " + err.decode(errors="ignore").strip()[-200:])
    rec._read_segment_list(lista, 0.0, set())
    if not rec.segments:
        raise RuntimeError("o vídeo ficou sem segmentos")


def _limpar_atras(rec):
    limite = rec.cursor - FOLGA_S
    with rec._seg_lock:
        while rec.segments and rec.segments[0]["end"] < limite:
            seg = rec.segments.popleft()
            try:
                os.remove(seg["path"])
            except OSError:
                pass


def processar(rec, info):
    """Roda dentro da thread do Recorder. Devolve True quando o vídeo terminou (não precisa tentar de novo)."""
    from .analisador import JANELA
    url = rec.url
    rec.video = True
    rec.total = float(info.get("duracao") or 0)
    p = progresso(url)
    if p.get("feito"):
        rec.terminou = True
        rec.cursor = rec.t = rec.total
        rec.status, rec.status_detail = "terminado", "Vídeo já analisado inteiro. Tire do painel e cole o link de novo para refazer."
        return True
    pasta = os.path.join(rec.workdir, "video")
    shutil.rmtree(pasta, ignore_errors=True)
    os.makedirs(pasta, exist_ok=True)
    try:
        livre = config.disco_livre_mb()
        precisa = config.DISCO_MIN_CORTES_MB + int((info.get("tamanho_mb") or 0) * 2.2)
        if livre < precisa:
            rec.status = "disco cheio"
            rec.status_detail = f"O vídeo precisa de uns {precisa} MB livres e o disco só tem {livre} MB."
            return False
        rec.status, rec.status_detail = "baixando", "Baixando o vídeo…"
        arq, copia = _baixar(rec, info, pasta)
        rec.status = "preparando"
        rec.status_detail = "Preparando o vídeo para a IA…" if copia else "Convertendo o vídeo (formato diferente, demora mais)…"
        _segmentar(rec, arq, pasta, copia)
        try:
            os.remove(arq)  # os segmentos já têm tudo: o arquivo inteiro só ocuparia disco
        except OSError:
            pass
        rec.total = rec.segments[-1]["end"]
        rec.cursor = rec.t = min(rec.total, max(float(p.get("cursor") or 0), JANELA))
        rec.status, rec.status_detail = "analisando", info.get("title", "")
        ultimo = rec.cursor
        while not rec._stop.wait(1.0):
            if rec.terminou:
                break
            if rec.cursor != ultimo:
                ultimo = rec.t = rec.cursor
                _gravar(url, cursor=round(rec.cursor, 1), total=round(rec.total, 1))
                _limpar_atras(rec)
        if not rec.terminou:
            return True  # parado pelo usuário ou pelo fechamento do programa: o progresso já está salvo
        _gravar(url, cursor=round(rec.total, 1), total=round(rec.total, 1), feito=True)
        rec.t = rec.total
        rec.status, rec.status_detail = "terminado", f"Vídeo inteiro analisado ({_hms(rec.total)})."
        return True
    except Parado:
        return True
    finally:
        with rec._seg_lock:
            rec.segments.clear()
        shutil.rmtree(pasta, ignore_errors=True)
