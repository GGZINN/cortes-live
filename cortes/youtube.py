"""Integração com o YouTube: login do canal (OAuth), envio de vídeos e agendamento.

Precisa de uma credencial OAuth do tipo "App para computador" criada no Google Cloud,
salva em dados/client_secret.json (o painel faz isso). O login fica em
dados/youtube_token.json, só neste computador.

Agendar = enviar como privado com `publishAt`: o próprio YouTube publica na hora marcada,
mesmo com o PC desligado.
"""
import json
import os
import queue
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

from . import clipper, config

CLIENT_SECRET = os.path.join(config.DATA_DIR, "client_secret.json")
TOKEN = os.path.join(config.DATA_DIR, "youtube_token.json")
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/yt-analytics.readonly",  # painel do canal (fase 5)
]
PRIVACIDADES = ("public", "unlisted", "private")
CATEGORIAS = {"20": "Jogos", "24": "Entretenimento", "23": "Comédia", "22": "Pessoas e blogs", "17": "Esportes"}

_lock = threading.Lock()
_login = {"status": "parado", "erro": None, "url": None}  # parado / aguardando / erro
_canal = None  # cache das informações do canal conectado
_erro_canal = None


# ------------------------------------------------------------------ erros em português
def _motivo(e):
    """Extrai o 'reason' de um HttpError da API do Google."""
    try:
        err = json.loads(e.content.decode() if isinstance(e.content, bytes) else e.content)["error"]
        for x in err.get("errors") or []:
            if x.get("reason"):
                return x["reason"], err.get("message", "")
        for x in err.get("details") or []:
            if x.get("reason"):
                return x["reason"], err.get("message", "")
        return err.get("status", ""), err.get("message", "")
    except Exception:
        return "", str(e)


def msg_erro(e):
    nome = type(e).__name__
    if nome == "RefreshError":
        return ("O login do YouTube expirou ou foi revogado. Conecte de novo. Dica: deixe o app do Google "
                "Cloud como 'Em produção' para o login não expirar a cada 7 dias.")
    if nome == "HttpError":
        motivo, texto = _motivo(e)
        mapa = {
            "quotaExceeded": "A cota diária da API do YouTube acabou. Ela volta à meia-noite do horário do "
                             "Pacífico (4h ou 5h da manhã em Brasília).",
            "uploadLimitExceeded": "O canal atingiu o limite de envios do YouTube por hoje. Tente amanhã.",
            "accessNotConfigured": "A YouTube Data API v3 não está ativada no seu projeto do Google Cloud.",
            "SERVICE_DISABLED": "A YouTube Data API v3 não está ativada no seu projeto do Google Cloud.",
            "insufficientPermissions": "Sem permissão. Desconecte e conecte o YouTube de novo.",
            "youtubeSignupRequired": "Essa conta Google ainda não tem canal no YouTube.",
            "invalidPublishAt": "Data de agendamento inválida (precisa ser no futuro).",
            "invalidTitle": "Título inválido (vazio, longo demais ou com < >).",
            "invalidDescription": "Descrição inválida (longa demais ou com < >).",
            "invalidTags": "Tags inválidas (passaram do limite de 500 caracteres).",
            "forbidden": "O YouTube recusou o pedido (forbidden). " + texto,
        }
        return mapa.get(motivo) or f"YouTube respondeu: {texto or motivo}"[:300]
    if nome in ("AccessDeniedError", "AccessDenied"):
        return ("Acesso negado. Se o app está em 'Teste' no Google Cloud, adicione seu e-mail como "
                "usuário de teste. E na tela do Google, permita o acesso.")
    return str(e)[:300] or nome


# ------------------------------------------------------------------ credencial e login
def configurado():
    return os.path.exists(CLIENT_SECRET)


def salvar_client_secret(texto):
    try:
        dados = json.loads(texto)
    except ValueError:
        raise ValueError("O arquivo não é um JSON válido.")
    if "installed" not in dados:
        if "web" in dados:
            raise ValueError("Essa credencial é do tipo 'Aplicativo da Web'. Crie uma do tipo 'App para computador'.")
        raise ValueError("Não parece uma credencial OAuth do Google (falta a parte 'installed').")
    with open(CLIENT_SECRET, "w", encoding="utf-8") as f:
        json.dump(dados, f)


def _credenciais():
    """Credenciais válidas (renova sozinho) ou None se não estiver conectado."""
    if not os.path.exists(TOKEN):
        return None
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    with _lock:
        creds = Credentials.from_authorized_user_file(TOKEN)
        if not creds.valid and creds.refresh_token:
            creds.refresh(Request())
            with open(TOKEN, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
        return creds if creds.valid else None


def _servico(api="youtube", versao="v3"):
    creds = _credenciais()
    if not creds:
        raise RuntimeError("YouTube não conectado.")
    from googleapiclient.discovery import build
    return build(api, versao, credentials=creds, cache_discovery=False)


def _buscar_canal():
    r = _servico().channels().list(part="snippet,statistics", mine=True).execute()
    itens = r.get("items") or []
    if not itens:
        raise RuntimeError("Essa conta Google não tem canal no YouTube.")
    c = itens[0]
    fotos = c["snippet"].get("thumbnails") or {}
    return {
        "id": c["id"],
        "titulo": c["snippet"]["title"],
        "foto": (fotos.get("default") or {}).get("url"),
        "url": f"https://www.youtube.com/channel/{c['id']}",
        "inscritos": c["statistics"].get("subscriberCount"),
        "videos": c["statistics"].get("videoCount"),
    }


def status(recarregar=False):
    global _canal, _erro_canal
    st = {"configurado": configurado(), "conectado": False, "login": dict(_login), "canal": None,
          "erro": None, "categorias": CATEGORIAS, "categoria": config.get("youtube_categoria"),
          "thumb_automatica": config.get("thumb_automatica")}
    if not os.path.exists(TOKEN):
        return st
    if _canal is None or recarregar:
        try:
            _canal, _erro_canal = _buscar_canal(), None
        except Exception as e:  # noqa
            _canal, _erro_canal = None, msg_erro(e)
    st["conectado"] = _canal is not None
    st["canal"] = _canal
    st["erro"] = _erro_canal
    return st


def conectar():
    if not configurado():
        raise RuntimeError("Envie primeiro o arquivo da credencial (client_secret.json).")
    if _login["status"] == "aguardando":
        return
    _login.update(status="aguardando", erro=None, url=None)
    threading.Thread(target=_fluxo_login, daemon=True).start()


def _fluxo_login():
    global _canal, _erro_canal
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET, SCOPES)
        original = flow.authorization_url

        def guarda_url(**kw):  # mostra o link no painel, caso o navegador não abra sozinho
            url, state = original(**kw)
            _login["url"] = url
            return url, state

        flow.authorization_url = guarda_url
        creds = flow.run_local_server(
            port=0, open_browser=True, prompt="consent", timeout_seconds=300,
            authorization_prompt_message="",
            success_message="Pronto! O canal foi conectado ao Cortes Live. Pode fechar esta aba.",
        )
        with _lock:
            with open(TOKEN, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
        _canal, _erro_canal = None, None
        status(recarregar=True)
        _login.update(status="parado", erro=None, url=None)
    except Exception as e:  # noqa
        traceback.print_exc()
        msg = msg_erro(e)
        if isinstance(e, AttributeError):  # o tempo de 5 min acabou sem resposta do Google
            msg = "O login não foi concluído a tempo (5 minutos). Tente de novo."
        _login.update(status="erro", erro=msg, url=None)


def desconectar():
    global _canal, _erro_canal
    with _lock:
        try:
            os.remove(TOKEN)
        except OSError:
            pass
    _canal, _erro_canal = None, None
    _login.update(status="parado", erro=None, url=None)


# ------------------------------------------------------------------ envio
_fila = queue.Queue()


def _limpa(texto, limite):
    return str(texto or "").replace("<", "‹").replace(">", "›").strip()[:limite]


def _tags(lista):
    """Tags sem '#', sem repetir, somando no máximo 500 caracteres (regra do YouTube)."""
    out, total, vistos = [], 0, set()
    for t in lista or []:
        t = _limpa(str(t).lstrip("#"), 60)
        if not t or t.lower() in vistos:
            continue
        custo = len(t) + 2 + (2 if " " in t else 0)
        if total + custo > 480:
            break
        out.append(t)
        vistos.add(t.lower())
        total += custo
    return out


def _yt(cid, **kw):
    clip = clipper.get_clip(cid) or {}
    yt = dict(clip.get("youtube") or {})
    yt.update(kw)
    clipper.update_clip(cid, youtube=yt)
    return yt


def enviar(cid, titulo, descricao, privacidade="public", publicar_em=None, tags=None, forcar=False):
    """Coloca o corte na fila de envio. publicar_em: data ISO (UTC) para agendar."""
    clip = clipper.get_clip(cid)
    if not clip or clip.get("status") != "pronto":
        raise ValueError("Corte não encontrado ou ainda processando.")
    if not os.path.exists(os.path.join(config.CLIPS_DIR, cid, "corte.mp4")):
        raise ValueError("O arquivo do corte não existe mais.")
    atual = clip.get("youtube") or {}
    if atual.get("status") in ("na fila", "enviando"):
        raise ValueError("Esse corte já está sendo enviado.")
    if atual.get("id") and not forcar:
        raise ValueError("Esse corte já foi enviado ao YouTube.")
    if not _credenciais():
        raise ValueError("Conecte o canal do YouTube primeiro.")
    titulo = _limpa(titulo, 100)
    if not titulo:
        raise ValueError("O título está vazio.")
    privacidade, publicar_em = validar_publicacao(privacidade, publicar_em)
    pedido = {
        "status": "na fila", "progresso": 0, "titulo": titulo, "privacidade": privacidade,
        "publicar_em": publicar_em, "pedido_em": datetime.now(timezone.utc).isoformat(),
        "erro": None, "aviso": None, "motivo": None, "id": None, "url": None,
    }
    clipper.update_clip(cid, youtube=pedido)
    _fila.put((cid, titulo, _limpa(descricao, 5000), privacidade, pedido["publicar_em"], _tags(tags)))
    return pedido


def validar_publicacao(privacidade, publicar_em):
    """Confere visibilidade e data. Devolve (privacidade, publicar_em ISO UTC ou None)."""
    if privacidade not in PRIVACIDADES:
        raise ValueError("Visibilidade inválida.")
    if not publicar_em:
        return privacidade, None
    try:
        quando = datetime.fromisoformat(publicar_em.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Data de agendamento inválida.")
    if quando.tzinfo is None:
        quando = quando.replace(tzinfo=timezone.utc)
    if quando < datetime.now(timezone.utc) + timedelta(minutes=2):
        raise ValueError("A data do agendamento precisa ser no futuro.")
    quando = quando.astimezone(timezone.utc).replace(microsecond=0)
    # regra do YouTube: agendado sobe privado e vira público na hora
    return "private", quando.isoformat().replace("+00:00", "Z")


def snippet(titulo, descricao, tags):
    s = {"title": _limpa(titulo, 100), "description": _limpa(descricao, 5000), "tags": _tags(tags),
         "categoryId": config.get("youtube_categoria") or "20"}
    idioma = config.get("idioma")
    if idioma and idioma != "auto":
        s["defaultLanguage"] = s["defaultAudioLanguage"] = {"pt": "pt-BR"}.get(idioma, idioma)
    return s


def subir(caminho, snip, status_video, progresso=lambda p: None):
    """Envia um arquivo de vídeo em partes (retoma sozinho se a conexão cair). Devolve a resposta do YouTube."""
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload
    req = _servico().videos().insert(
        part="snippet,status", body={"snippet": snip, "status": status_video}, notifySubscribers=True,
        media_body=MediaFileUpload(caminho, mimetype="video/mp4", chunksize=4 * 1024 * 1024, resumable=True),
    )
    resp, falhas = None, 0
    while resp is None:
        try:
            prog, resp = req.next_chunk()
            if prog:
                progresso(round(prog.progress(), 3))
            falhas = 0
        except HttpError as e:
            if e.resp.status in (500, 502, 503, 504) and falhas < 6:
                falhas += 1
                time.sleep(2 ** falhas)
                continue
            raise
        except (ConnectionError, TimeoutError, OSError):
            if falhas < 6:  # queda de internet: o envio continua de onde parou
                falhas += 1
                time.sleep(2 ** falhas)
                continue
            raise
    return resp


def por_thumb(video_id, caminho):
    """Põe uma imagem como thumbnail do vídeo. Erro vira RuntimeError com mensagem em português."""
    from googleapiclient.http import MediaFileUpload
    try:
        _servico().thumbnails().set(videoId=video_id, media_body=MediaFileUpload(caminho, mimetype="image/jpeg")).execute()
    except Exception as e:  # noqa
        if type(e).__name__ == "HttpError" and e.resp.status == 403:
            raise RuntimeError("O YouTube não aceitou a thumbnail: o canal precisa estar verificado por telefone "
                               "para usar thumbnail personalizada (youtube.com/verify).")
        raise RuntimeError(msg_erro(e))


def _enviar_agora(cid, titulo, descricao, privacidade, publicar_em, tags):
    caminho = os.path.join(config.CLIPS_DIR, cid, "corte.mp4")
    _yt(cid, status="enviando", progresso=0)
    resp = subir(caminho, snippet(titulo, descricao, tags), _status_video(privacidade, publicar_em),
                 lambda p: _yt(cid, progresso=p))
    vid = resp["id"]
    _yt(cid, status="enviado", progresso=1, id=vid, url=f"https://youtube.com/shorts/{vid}",
        studio=f"https://studio.youtube.com/video/{vid}/edit",
        enviado_em=datetime.now(timezone.utc).isoformat())
    _conferir(cid, resp.get("status") or {}, privacidade, publicar_em)
    if config.get("thumb_automatica"):
        try:
            definir_thumb(cid)
        except Exception:  # noqa - o vídeo subiu; a falha da thumbnail fica anotada no corte
            traceback.print_exc()


def definir_thumb(cid):
    """Envia a thumbnail escolhida do corte para o vídeo dele no YouTube (gera antes, se preciso)."""
    from . import thumbs
    clip = clipper.get_clip(cid) or {}
    vid = (clip.get("youtube") or {}).get("id")
    if not vid:
        raise ValueError("Esse corte ainda não foi enviado ao YouTube.")
    caminho = thumbs.arquivo(cid)
    if not caminho:
        thumbs.gerar_agora(cid)
        caminho = thumbs.arquivo(cid)
    try:
        por_thumb(vid, caminho)
    except RuntimeError as e:
        _yt(cid, thumb_erro=str(e), thumb_ok=False)
        raise
    _yt(cid, thumb_ok=True, thumb_erro=None)
    return clipper.get_clip(cid)


def _conferir(cid, st, pedida, publicar_em):
    """Compara a visibilidade pedida com a que o YouTube aplicou (projeto sem auditoria trava em privado)."""
    real = st.get("privacyStatus")
    aviso = None
    if st.get("uploadStatus") in ("rejected", "failed"):
        aviso = f"O YouTube recusou o vídeo ({st.get('rejectionReason') or st.get('failureReason') or 'sem motivo'})."
    elif real == "private" and (pedida != "private" or publicar_em) and not st.get("publishAt"):
        aviso = ("O YouTube deixou o vídeo privado. Isso acontece enquanto o projeto do Google Cloud não passa "
                 "na auditoria da API. Até lá, mude a visibilidade pelo YouTube Studio.")
    _yt(cid, privacidade_real=real, upload_status=st.get("uploadStatus"), aviso=aviso,
        publicar_em=st.get("publishAt") or (None if aviso else publicar_em))


def atualizar(cid):
    """Busca de novo no YouTube a situação do vídeo de um corte."""
    clip = clipper.get_clip(cid) or {}
    yt = clip.get("youtube") or {}
    if not yt.get("id"):
        raise ValueError("Esse corte não foi enviado ao YouTube.")
    r = _servico().videos().list(part="status", id=yt["id"]).execute()
    itens = r.get("items") or []
    if not itens:
        _yt(cid, aviso="O vídeo não foi encontrado no canal (foi apagado?).")
    else:
        _conferir(cid, itens[0]["status"], yt.get("privacidade"), yt.get("publicar_em"))
    return clipper.get_clip(cid)


def _status_video(privacidade, publicar_em=None):
    st = {"privacyStatus": privacidade, "selfDeclaredMadeForKids": False, "embeddable": True}
    if publicar_em:
        st["publishAt"] = publicar_em
    return st


def reagendar(cid, video_id, publicar_em):
    """Muda a data de publicação de um vídeo que já está agendado no YouTube."""
    _servico().videos().update(part="status", body={"id": video_id, "status": _status_video("private", publicar_em)}).execute()
    _yt(cid, publicar_em=publicar_em, privacidade="private")


def desagendar(cid, video_id):
    """Tira o agendamento: o vídeo continua no canal, como privado."""
    _servico().videos().update(part="status", body={"id": video_id, "status": _status_video("private")}).execute()
    _yt(cid, publicar_em=None, privacidade="private", privacidade_real="private")


def _worker():
    while True:
        cid, *args = _fila.get()
        try:
            _enviar_agora(cid, *args)
        except Exception as e:  # noqa
            traceback.print_exc()
            _yt(cid, status="erro", erro=msg_erro(e),
                motivo=_motivo(e)[0] if type(e).__name__ == "HttpError" else None)


# envios interrompidos ao fechar o programa
for _c in clipper.list_clips():
    if (_c.get("youtube") or {}).get("status") in ("na fila", "enviando"):
        _yt(_c["id"], status="erro", erro="O programa foi fechado durante o envio. Envie de novo.")

threading.Thread(target=_worker, daemon=True).start()
