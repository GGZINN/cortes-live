"""Transforma um trecho da live em corte vertical 9:16 com legenda animada."""
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import traceback

from . import config, curadoria, titulos

FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
INDEX = os.path.join(config.CLIPS_DIR, "cortes.json")

_lock = threading.Lock()
clips = []  # lista de dicts (mais novo primeiro)
if os.path.exists(INDEX):
    try:
        with open(INDEX, encoding="utf-8") as f:
            clips = json.load(f)
        for c in clips:  # trabalhos interrompidos ao fechar o programa
            if c["status"] not in ("pronto", "erro"):
                c["status"], c["erro"] = "erro", "programa foi fechado durante o processamento"
    except Exception:
        clips = []


def _save():
    with open(INDEX, "w", encoding="utf-8") as f:
        json.dump(clips, f, indent=1, ensure_ascii=False)


if curadoria.preencher_lotes_antigos(clips):  # cortes de antes dos lotes existirem
    _save()


def update_clip(cid, **kw):
    with _lock:
        for c in clips:
            if c["id"] == cid:
                c.update(kw)
        _save()


def add_clip(clip):
    with _lock:
        curadoria.atribuir_lote(clip, clips)
        clips.insert(0, clip)
        _save()


def list_clips():
    with _lock:
        return [dict(c) for c in clips]


def get_clip(cid):
    with _lock:
        for c in clips:
            if c["id"] == cid:
                return dict(c)
    return None


def delete_clip(cid):
    with _lock:
        for c in list(clips):
            if c["id"] == cid:
                clips.remove(c)
                shutil.rmtree(os.path.join(config.CLIPS_DIR, cid), ignore_errors=True)
        _save()


def run_ffmpeg(args, cwd=None):
    p = subprocess.run([config.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *args],
                       cwd=cwd, capture_output=True, creationflags=FLAGS)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode(errors="ignore")[-600:])


# ------------------------------------------------------------------ Whisper
_model = None
_model_name = None
_model_lock = threading.Lock()


def _whisper():
    global _model, _model_name
    name = config.get("modelo_whisper")
    with _model_lock:
        if _model is None or _model_name != name:
            from faster_whisper import WhisperModel
            try:
                import ctranslate2
                gpu = ctranslate2.get_cuda_device_count() > 0
            except Exception:
                gpu = False
            if gpu:
                try:
                    _model = WhisperModel(name, device="cuda", compute_type="float16")
                except Exception:
                    _model = WhisperModel(name, device="cpu", compute_type="int8")
            else:
                _model = WhisperModel(name, device="cpu", compute_type="int8")
            _model_name = name
        return _model


def transcribe(pcm_path):
    import numpy as np
    audio = np.fromfile(pcm_path, dtype=np.int16).astype(np.float32) / 32768.0
    lang = config.get("idioma")
    segs, _ = _whisper().transcribe(
        audio, language=None if lang == "auto" else lang, word_timestamps=True,
        vad_filter=True, beam_size=1,
    )
    words = []
    for s in segs:
        for w in s.words or []:
            txt = w.word.strip()
            if txt:
                words.append({"w": txt, "s": float(w.start), "e": float(w.end)})
    return words


# ------------------------------------------------------------------ Legenda (ASS)
def _ts(t):
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _esc(s):
    return s.replace("\\", "").replace("{", "(").replace("}", ")")


# Cores de destaque (formato ASS: &HBBGGRR&) - alterna a cada grupo de palavras
CORES_DESTAQUE = ["&H00E6FF&", "&H14FF39&", "&H00E6FF&", "&H5C3BFF&"]  # amarelo, verde, amarelo, rosa
LEG_X, LEG_Y = 540, 1450  # centro-baixo, logo abaixo do vídeo


def _ts_cs(cs):
    """centésimos de segundo -> h:mm:ss.cc"""
    cs = max(0, int(cs))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def _palavra(w):
    t = w.strip().strip(".,;:\"'“”…-").upper()
    return _esc(t)


def _agrupar(words, max_words=3, max_chars=15):
    """Grupos curtos (1-3 palavras): quebra em pausa, pontuação ou se ficar comprido."""
    groups, cur = [], []
    for w in words:
        if not _palavra(w["w"]):
            continue
        chars = sum(len(x["w"]) + 1 for x in cur) + len(w["w"])
        if cur and (len(cur) >= max_words or chars > max_chars
                    or w["s"] - cur[-1]["e"] > 0.45
                    or re.search(r"[.!?…,;:]$", cur[-1]["w"].strip())):
            groups.append(cur)
            cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


def build_ass(words, path):
    """Legenda estilo cortes: as palavras aparecem conforme são faladas, a palavra
    atual "pula" na tela (zoom com elástico) colorida e cada frase entra subindo."""
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Corte,Arial Black,92,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,1,0,1,8,4,2,50,50,470,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    groups = _agrupar(words)
    cs = lambda t: int(round(t * 100))  # noqa: E731
    lines = []
    for gi, g in enumerate(groups):
        cor = CORES_DESTAQUE[gi % len(CORES_DESTAQUE)]
        # até quando o grupo fica na tela: emenda no próximo se a pausa for curta (sem piscar)
        prox = cs(groups[gi + 1][0]["s"]) if gi + 1 < len(groups) else None
        fim = cs(g[-1]["e"] + 0.35)
        if prox is not None and (prox - cs(g[-1]["e"]) < 60 or fim > prox):
            fim = prox
        inicios = [cs(w["s"]) for w in g]
        for i in range(len(g)):
            ini = inicios[i] if i else inicios[0]
            end = inicios[i + 1] if i + 1 < len(g) else fim
            if i + 1 < len(g):
                end = max(end, ini + 1)
            if end <= ini:
                continue
            # entrada do grupo: sobe 25px e aparece em 120ms; depois fica parado
            if i == 0:
                pos = rf"\move({LEG_X},{LEG_Y + 25},{LEG_X},{LEG_Y},0,120)\fad(80,0)"
            else:
                pos = rf"\pos({LEG_X},{LEG_Y})"
            parts = []
            for j, x in enumerate(g):
                t = _palavra(x["w"])
                if j < i:     # já falada: branca
                    parts.append(r"{\alpha&H00&\c&HFFFFFF&\fscx100\fscy100}" + t)
                elif j == i:  # falando agora: colorida, com "pulo"
                    parts.append(rf"{{\alpha&H00&\c{cor}\fscx75\fscy75\t(0,90,\fscx122\fscy122)"
                                 rf"\t(90,180,\fscx110\fscy110)}}" + t)
                else:         # ainda não falada: reserva o espaço, invisível (a linha não fica pulando de lugar)
                    parts.append(r"{\alpha&HFF&\c&HFFFFFF&\fscx100\fscy100}" + t)
            lines.append(f"Dialogue: 0,{_ts_cs(ini)},{_ts_cs(end)},Corte,,0,0,0,,{{{pos}}}" + " ".join(parts))
    with open(path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(lines) + "\n")


# ------------------------------------------------------------------ Processamento
jobs = queue.Queue()


def enqueue(clip_id, raw_path, offset, duration):
    jobs.put((clip_id, raw_path, offset, duration))


def _process(cid, raw, offset, dur):
    d = os.path.dirname(raw)
    t0 = time.time()
    words = []
    has_subs = False
    if config.get("legenda"):
        update_clip(cid, status="transcrevendo")
        run_ffmpeg(["-ss", f"{offset:.2f}", "-t", f"{dur:.2f}", "-i", "bruto.ts",
                    "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "audio.pcm"], cwd=d)
        words = transcribe(os.path.join(d, "audio.pcm"))
        if words:
            build_ass(words, os.path.join(d, "legenda.ass"))
            has_subs = True

    update_clip(cid, status="renderizando")
    vf = ("[0:a:0]asetpts=PTS-STARTPTS[aud];"
          "[0:v]setpts=PTS-STARTPTS,split=2[a][b];"
          "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
          "boxblur=30:2,eq=brightness=-0.10:saturation=1.2[bg];"
          "[b]scale=1080:-2[fg];"
          "[bg][fg]overlay=(W-w)/2:(H-h)/2" + (",ass=legenda.ass" if has_subs else "") +
          ",format=yuv420p[v]")
    run_ffmpeg(["-ss", f"{offset:.2f}", "-t", f"{dur:.2f}", "-i", "bruto.ts",
                "-filter_complex", vf, "-map", "[v]", "-map", "[aud]",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
                "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "corte.mp4"], cwd=d)
    run_ffmpeg(["-ss", f"{min(dur / 2, 8):.2f}", "-i", "corte.mp4", "-frames:v", "1",
                "-vf", "scale=360:-2:out_range=full,format=yuvj420p", "capa.jpg"], cwd=d)

    text = " ".join(w["w"] for w in words)
    clip = get_clip(cid) or {}
    pico = clip.get("pico_rel", dur * 0.7)
    frase = " ".join(w["w"] for w in words if pico - 6 <= w["s"] <= pico + 3)
    for f in ("bruto.ts", "audio.pcm"):
        try:
            os.remove(os.path.join(d, f))
        except OSError:
            pass
    update_clip(cid, transcricao=text, frase_pico=frase,
                arquivo=f"/arquivos/{cid}/corte.mp4", capa=f"/arquivos/{cid}/capa.jpg")

    update_clip(cid, status="escrevendo títulos")
    sug = titulos.gerar(get_clip(cid))
    update_clip(cid, status="pronto", sugestoes=sug, titulo=sug["titulos"][0] if sug["titulos"] else None,
                tempo_processo=round(time.time() - t0, 1))


def regenerate_suggestions(cid):
    clip = get_clip(cid)
    if not clip:
        return None
    sug = titulos.gerar(clip)
    update_clip(cid, sugestoes=sug, titulo=sug["titulos"][0] if sug["titulos"] else clip.get("titulo"))
    return get_clip(cid)


def _worker():
    while True:
        cid, raw, offset, dur = jobs.get()
        try:
            _process(cid, raw, offset, dur)
        except Exception as e:  # noqa
            traceback.print_exc()
            update_clip(cid, status="erro", erro=str(e)[-300:])
        try:
            curadoria.verificar()  # fechou um lote de 10? escolhe os 2 para postar
        except Exception:  # noqa
            traceback.print_exc()


threading.Thread(target=_worker, daemon=True).start()
