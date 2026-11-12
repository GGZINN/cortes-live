"""Agenda do YouTube: horários fixos, fila automática com os cortes escolhidos e envio agendado.

Cada item da agenda é um corte marcado para um horário. Algumas horas antes do horário ele sobe
ao YouTube como privado com data de publicação (publishAt) e o próprio YouTube publica na hora,
mesmo com o PC desligado. Até subir, dá para trocar, remover ou mudar o título à vontade.

Os horários da configuração são no fuso deste computador.
"""
import json
import os
import re
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone

from . import clipper, config, youtube

ARQ = os.path.join(config.DATA_DIR, "agenda.json")
PADRAO = {
    "horarios": ["12:00", "18:00", "21:00"],
    "dias": [0, 1, 2, 3, 4, 5, 6],   # 0 = segunda ... 6 = domingo
    "auto": True,                    # preencher sozinho com os ⭐ escolhidos
    "dias_auto": 2,                  # até quantos dias à frente preencher sozinho
    "enviar_antes_horas": 3,         # quanto tempo antes do horário o vídeo sobe ao YouTube
}
MARGEM = timedelta(minutes=20)   # horário mais próximo que ainda dá para marcar
JANELA = timedelta(minutes=30)   # um horário está ocupado se já há vídeo a menos disso dele
DIAS_VISTA = 7
COTA = ("quotaExceeded", "uploadLimitExceeded", "dailyLimitExceeded", "rateLimitExceeded")
EDITAVEIS = ("planejado", "erro", "aguardando_cota")

_lock = threading.RLock()
_dados = {"config": dict(PADRAO), "itens": [], "pausa_ate": None}


# ------------------------------------------------------------------ utilidades
def _agora():
    return datetime.now(timezone.utc)


def _iso(d):
    return d.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _dt(s):
    d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return d if d.tzinfo else d.astimezone()


def _save():
    config.salvar_json(ARQ, _dados, indent=1)


def _carregar():
    if not os.path.exists(ARQ):
        return
    try:
        with open(ARQ, encoding="utf-8") as f:
            d = json.load(f)
        _dados["config"].update({k: v for k, v in (d.get("config") or {}).items() if k in PADRAO})
        _dados["itens"] = d.get("itens") or []
        _dados["pausa_ate"] = d.get("pausa_ate")
        for it in _dados["itens"]:  # envio interrompido ao fechar o programa: tenta de novo
            if it["status"] == "enviando":
                it["status"] = "planejado"
    except Exception:  # noqa
        traceback.print_exc()


def _reset_cota():
    """A cota da API volta à meia-noite do Pacífico; 08:00 UTC cobre horário de verão e de inverno."""
    a = _agora()
    r = a.replace(hour=8, minute=5, second=0, microsecond=0)
    return r if r > a else r + timedelta(days=1)


def _descricao(clip):
    s = clip.get("sugestoes") or {}
    p = (s.get("plataformas") or {}).get("shorts") or {}
    tags = " ".join("#" + h for h in p.get("hashtags") or [])
    return "\n\n".join(x for x in (p.get("legenda"), s.get("credito"), tags) if x)


def _tags(clip):
    s = clip.get("sugestoes") or {}
    p = (s.get("plataformas") or {}).get("shorts") or {}
    return list(p.get("hashtags") or []) + ([clip["canal"]] if clip.get("canal") else [])


def _titulo(clip):
    s = clip.get("sugestoes") or {}
    return clip.get("titulo") or (s.get("titulos") or [None])[0] or clip.get("motivo") or "Corte"


def _livre_para_agenda(c):
    """Corte pronto que ainda não foi pro YouTube nem está na agenda."""
    y = c.get("youtube") or {}
    return (c.get("status") == "pronto" and not y.get("id") and y.get("status") not in ("na fila", "enviando")
            and os.path.exists(os.path.join(config.CLIPS_DIR, c["id"], "corte.mp4")))


def _marca(it):
    clipper.update_clip(it["cid"], agenda={"id": it["id"], "quando": it["quando"]})


def _desmarca(cid):
    clipper.update_clip(cid, agenda=None)


# ------------------------------------------------------------------ horários
def _slots(dias=DIAS_VISTA):
    """Horários da configuração de hoje até `dias` à frente (só os que ainda dá para marcar)."""
    cfg = _dados["config"]
    tz = datetime.now().astimezone().tzinfo
    hoje = datetime.now(tz).date()
    limite = _agora() + MARGEM
    out = []
    for i in range(dias + 1):
        d = hoje + timedelta(days=i)
        if d.weekday() not in cfg["dias"]:
            continue
        for h in sorted(cfg["horarios"]):
            hh, mm = map(int, h.split(":"))
            t = datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz)
            if t >= limite:
                out.append(t)
    return out


def _ocupados(ignorar=None):
    ts = [_dt(i["quando"]) for i in _dados["itens"] if i["id"] != ignorar and i["status"] != "publicado"]
    na_agenda = {i["cid"] for i in _dados["itens"]}
    for c in clipper.list_clips():  # agendados à mão pelo Publicar
        y = c.get("youtube") or {}
        if y.get("publicar_em") and c["id"] not in na_agenda:
            ts.append(_dt(y["publicar_em"]))
    return ts


def _livre(t, ocupados):
    return all(abs(t - o) >= JANELA for o in ocupados)


def proximo_livre(ignorar=None):
    ocup = _ocupados(ignorar)
    for t in _slots(60):
        if _livre(t, ocup):
            return t
    return None


# ------------------------------------------------------------------ ações
def _achar(iid):
    for it in _dados["itens"]:
        if it["id"] == iid:
            return it
    raise ValueError("Item da agenda não encontrado.")


def _novo_item(clip, t, origem, titulo=None, descricao=None):
    it = {
        "id": uuid.uuid4().hex[:8], "cid": clip["id"], "quando": _iso(t), "status": "planejado",
        "titulo": (titulo or _titulo(clip)).strip()[:100], "descricao": descricao if descricao is not None else _descricao(clip),
        "tags": _tags(clip), "origem": origem, "criado": _iso(_agora()), "erro": None,
    }
    _dados["itens"].append(it)
    _marca(it)
    return it


def adicionar(cid, quando=None, titulo=None, descricao=None):
    with _lock:
        clip = clipper.get_clip(cid)
        if not clip or clip.get("status") != "pronto":
            raise ValueError("Corte não encontrado ou ainda processando.")
        if any(i["cid"] == cid for i in _dados["itens"]):
            raise ValueError("Esse corte já está na agenda.")
        if not _livre_para_agenda(clip):
            raise ValueError("Esse corte já foi enviado ao YouTube.")
        if quando:
            t = _dt(quando)
            if t < _agora() + MARGEM:
                raise ValueError("Escolha um horário pelo menos 20 minutos no futuro.")
        else:
            t = proximo_livre()
            if not t:
                raise ValueError("Nenhum horário livre nos próximos 60 dias. Confira os horários e dias da agenda.")
        it = _novo_item(clip, t, "manual", titulo, descricao)
        _save()
        return dict(it)


def remover(iid):
    with _lock:
        it = _achar(iid)
        if it["status"] == "enviando":
            raise ValueError("Esse vídeo está subindo agora. Espere terminar e remova depois.")
        if it["status"] == "agendado" and it.get("video_id"):
            youtube.desagendar(it["cid"], it["video_id"])  # continua no canal, como privado
        _dados["itens"].remove(it)
        _desmarca(it["cid"])
        _save()


def mover(iid, quando=None):
    with _lock:
        it = _achar(iid)
        if quando:
            t = _dt(quando)
            if t < _agora() + MARGEM:
                raise ValueError("Escolha um horário pelo menos 20 minutos no futuro.")
        else:
            t = proximo_livre(ignorar=iid)
            if not t:
                raise ValueError("Nenhum horário livre nos próximos 60 dias.")
        if it["status"] == "agendado" and it.get("video_id"):
            youtube.reagendar(it["cid"], it["video_id"], _iso(t))
        elif it["status"] in EDITAVEIS:
            it.update(status="planejado", erro=None, remarcado=False)
        else:
            raise ValueError("Esse item não pode mais mudar de horário.")
        it["quando"] = _iso(t)
        _marca(it)
        _save()
        return dict(it)


def editar(iid, titulo=None, descricao=None):
    with _lock:
        it = _achar(iid)
        if it["status"] not in EDITAVEIS:
            raise ValueError("O vídeo já subiu. Edite o título pelo YouTube Studio.")
        if titulo is not None:
            titulo = titulo.strip()
            if not titulo or len(titulo) > 100:
                raise ValueError("O título precisa ter de 1 a 100 caracteres.")
            it["titulo"] = titulo
        if descricao is not None:
            it["descricao"] = descricao
        _save()
        return dict(it)


def set_config(novo):
    with _lock:
        cfg = _dados["config"]
        if "horarios" in novo:
            hs = []
            for h in novo["horarios"]:
                m = re.fullmatch(r"\s*(\d{1,2})[:h](\d{2})?\s*", str(h))
                if not m or int(m[1]) > 23 or int(m[2] or 0) > 59:
                    raise ValueError(f"Horário inválido: {h}")
                hs.append(f"{int(m[1]):02d}:{int(m[2] or 0):02d}")
            if not hs:
                raise ValueError("Coloque pelo menos um horário.")
            cfg["horarios"] = sorted(set(hs))
        if "dias" in novo:
            dias = sorted({int(d) for d in novo["dias"] if 0 <= int(d) <= 6})
            if not dias:
                raise ValueError("Marque pelo menos um dia da semana.")
            cfg["dias"] = dias
        if "auto" in novo:
            cfg["auto"] = bool(novo["auto"])
        if "dias_auto" in novo:
            cfg["dias_auto"] = max(1, min(14, int(novo["dias_auto"])))
        if "enviar_antes_horas" in novo:
            cfg["enviar_antes_horas"] = max(1, min(48, int(novo["enviar_antes_horas"])))
        _save()
        return dict(cfg)


# ------------------------------------------------------------------ ciclo automático
def _preencher():
    """Coloca os ⭐ escolhidos nos horários livres dos próximos dias, sem repetir streamer em sequência."""
    cfg = _dados["config"]
    limite = _agora() + timedelta(days=cfg["dias_auto"])
    usados = {i["cid"] for i in _dados["itens"]}
    cands = [c for c in clipper.list_clips()
             if c.get("escolhido") is True and c["id"] not in usados and _livre_para_agenda(c)]
    if not cands:
        return False
    cands.sort(key=lambda c: str(c.get("criado", "")), reverse=True)  # mais recentes primeiro
    mudou = False
    for t in _slots(cfg["dias_auto"] + 1):
        if t > limite or not cands:
            break
        if not _livre(t, _ocupados()):
            continue
        antes = [i for i in _dados["itens"] if _dt(i["quando"]) < t]
        anterior = ""
        if antes:
            c_ant = clipper.get_clip(max(antes, key=lambda i: i["quando"])["cid"]) or {}
            anterior = (c_ant.get("canal") or "").lower()
        escolha = next((c for c in cands if (c.get("canal") or "").lower() != anterior), cands[0])
        cands.remove(escolha)
        _novo_item(escolha, t, "auto")
        mudou = True
    return mudou


def _ciclo():
    with _lock:
        agora = _agora()
        cfg = _dados["config"]
        mudou = False

        for it in list(_dados["itens"]):
            clip = clipper.get_clip(it["cid"])
            y = (clip or {}).get("youtube") or {}
            if not clip:  # corte apagado
                if it["status"] != "enviando":
                    _dados["itens"].remove(it)
                    mudou = True
                continue
            if it["status"] in EDITAVEIS and y.get("id"):  # foi enviado à mão pelo Publicar
                _dados["itens"].remove(it)
                _desmarca(it["cid"])
                mudou = True
                continue
            if it["status"] == "enviando" and y.get("pedido_em") == it.get("pedido_em"):
                if y.get("status") == "enviado":
                    it.update(status="travado" if y.get("aviso") else "agendado", aviso=y.get("aviso"),
                              video_id=y.get("id"), url=y.get("url"), studio=y.get("studio"))
                    mudou = True
                elif y.get("status") == "erro":
                    if y.get("motivo") in COTA:
                        it.update(status="aguardando_cota", erro=y.get("erro"))
                        _dados["pausa_ate"] = _iso(_reset_cota())
                    else:
                        it.update(status="erro", erro=y.get("erro"))
                    mudou = True
            elif it["status"] == "agendado" and _dt(it["quando"]) <= agora:
                it["status"] = "publicado"
                mudou = True

        # a cota voltou
        if _dados.get("pausa_ate") and _dt(_dados["pausa_ate"]) <= agora:
            _dados["pausa_ate"] = None
            for it in _dados["itens"]:
                if it["status"] == "aguardando_cota":
                    it.update(status="planejado", erro=None)
            mudou = True

        # horário passou sem o vídeo ter subido: vai para o próximo horário livre
        for it in _dados["itens"]:
            if it["status"] in ("planejado", "aguardando_cota") and _dt(it["quando"]) < agora + MARGEM:
                t = proximo_livre(ignorar=it["id"])
                if t:
                    it.update(quando=_iso(t), remarcado=True)
                    _marca(it)
                    mudou = True

        if cfg["auto"]:
            mudou |= _preencher()

        # sobe um vídeo por vez, quando faltar pouco para o horário dele
        conectado = os.path.exists(youtube.TOKEN)
        if conectado and not _dados.get("pausa_ate") and not any(i["status"] == "enviando" for i in _dados["itens"]):
            janela = timedelta(hours=cfg["enviar_antes_horas"])
            prontos = sorted((i for i in _dados["itens"] if i["status"] == "planejado" and _dt(i["quando"]) - agora <= janela),
                             key=lambda i: i["quando"])
            if prontos:
                it = prontos[0]
                try:
                    pedido = youtube.enviar(it["cid"], it["titulo"], it["descricao"], "private", it["quando"], it["tags"])
                    it.update(status="enviando", pedido_em=pedido["pedido_em"], erro=None)
                except ValueError as e:
                    it.update(status="erro", erro=str(e))
                mudou = True

        # limpa publicados antigos
        corte = agora - timedelta(days=30)
        antes = len(_dados["itens"])
        _dados["itens"] = [i for i in _dados["itens"] if not (i["status"] == "publicado" and _dt(i["quando"]) < corte)]
        mudou |= len(_dados["itens"]) != antes

        if mudou:
            _save()


def _loop():
    while True:
        try:
            _ciclo()
        except Exception:  # noqa
            traceback.print_exc()
        time.sleep(20)


# ------------------------------------------------------------------ estado para o painel
def _sugestao():
    """Horários que deram mais views no canal (painel do canal), se já houver dados."""
    try:
        from . import canal
        return canal.sugestao_horarios()
    except Exception:  # noqa
        return None


def estado():
    with _lock:
        clips = {c["id"]: c for c in clipper.list_clips()}
        itens = []
        for it in sorted(_dados["itens"], key=lambda i: i["quando"]):
            c = clips.get(it["cid"]) or {}
            y = c.get("youtube") or {}
            itens.append({**it, "canal": c.get("canal"), "capa": c.get("capa"), "duracao": c.get("duracao"),
                          "progresso": y.get("progresso") if it["status"] == "enviando" else None})
        na_agenda = {i["cid"] for i in _dados["itens"]}
        agora = _agora()
        manuais = []
        for c in clips.values():
            y = c.get("youtube") or {}
            if c["id"] not in na_agenda and y.get("publicar_em") and _dt(y["publicar_em"]) > agora:
                manuais.append({"cid": c["id"], "titulo": y.get("titulo"), "quando": y["publicar_em"], "canal": c.get("canal"),
                                "capa": c.get("capa"), "url": y.get("url"), "studio": y.get("studio"), "aviso": y.get("aviso")})
        ocup = _ocupados()
        slots = [_iso(t) for t in _slots(DIAS_VISTA) if _livre(t, ocup)]
        cands = [c for c in clips.values() if c["id"] not in na_agenda and _livre_para_agenda(c)]
        cands.sort(key=lambda c: (c.get("escolhido") is True, str(c.get("criado", ""))), reverse=True)
        return {
            "config": dict(_dados["config"]), "itens": itens, "manuais": sorted(manuais, key=lambda m: m["quando"]),
            "livres": slots, "pausa_ate": _dados.get("pausa_ate"), "conectado": os.path.exists(youtube.TOKEN),
            "sugestao": _sugestao(),
            "candidatos": [{"id": c["id"], "titulo": _titulo(c), "canal": c.get("canal"), "capa": c.get("capa"),
                            "escolhido": c.get("escolhido") is True, "duracao": c.get("duracao"), "criado": c.get("criado")}
                           for c in cands[:80]],
        }


_carregar()
threading.Thread(target=_loop, daemon=True).start()
