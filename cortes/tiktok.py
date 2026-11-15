"""Integração com o TikTok: login da conta (OAuth com PKCE) e envio dos cortes pela Content Posting API.

Precisa de um app criado em developers.tiktok.com (Client key e Client secret nas configurações), com:
- Login Kit, plataforma Desktop, Redirect URI = REDIRECT;
- Content Posting API (escopo video.upload; para postar direto, também Direct Post / video.publish).

Dois modos:
- "rascunho" (video.upload): o vídeo vai para a caixa de entrada do TikTok da conta; você abre o app, cola a
  legenda e publica. Funciona do jeito normal mesmo antes da auditoria do TikTok. Máximo de 5 rascunhos
  pendentes por dia (limite do TikTok).
- "direto" (video.publish): o vídeo é publicado com a legenda. Enquanto o app não passar na auditoria do
  TikTok, só dá para publicar como privado (só você vê).

O login fica em dados/tiktok_token.json, só neste computador. A API do TikTok não agenda: o envio é na hora.
"""
import hashlib
import json
import os
import queue
import secrets
import threading
import time
import traceback
from urllib.parse import urlencode

from . import clipper, config

TOKEN = os.path.join(config.DATA_DIR, "tiktok_token.json")
REDIRECT = "http://localhost:8765/api/tiktok/callback/"
AUTH = "https://www.tiktok.com/v2/auth/authorize/"
API = "https://open.tiktokapis.com/v2"
MODOS = ("rascunho", "direto")
PRIVACIDADES = {"PUBLIC_TO_EVERYONE": "Público", "MUTUAL_FOLLOW_FRIENDS": "Amigos", "FOLLOWER_OF_CREATOR": "Seguidores",
                "SELF_ONLY": "Só eu (privado)"}
UM_PEDACO_MAX = 64 * 2 ** 20   # até 64 MB vai num PUT só
PEDACO = 10 * 2 ** 20
ESPERA_MAX_S = 15 * 60         # quanto tempo acompanha o processamento no TikTok

_lock = threading.Lock()
_login = {"status": "parado", "erro": None, "url": None, "state": None, "verifier": None, "escopo": None}
_conta = None


# ------------------------------------------------------------------ erros em português
ERROS = {
    "spam_risk_too_many_pending_share": "O TikTok só aceita 5 rascunhos pendentes por dia. Publique (ou apague) os "
                                        "rascunhos na caixa de entrada do app e tente de novo mais tarde.",
    "spam_risk_too_many_posts": "Limite de postagens do dia pela API atingido. Tente amanhã.",
    "spam_risk_user_banned_from_posting": "O TikTok bloqueou esta conta de fazer postagens novas.",
    "access_token_invalid": "O login do TikTok expirou. Conecte a conta de novo.",
    "scope_not_authorized": "A conta não deu a permissão necessária. Conecte de novo e marque todas as permissões.",
    "unaudited_client_can_only_post_to_private_accounts": "Antes da auditoria do TikTok, o modo direto só publica "
                                                          "como privado. Escolha “Só eu (privado)” ou use o modo rascunho.",
    "privacy_level_option_mismatch": "Essa visibilidade não está disponível para esta conta agora.",
    "rate_limit_exceeded": "Muitos pedidos ao TikTok em pouco tempo. Espere um minuto e tente de novo.",
    "file_format_check_failed": "O TikTok recusou o arquivo de vídeo.",
    "duration_check_failed": "O TikTok recusou a duração do vídeo.",
    "frame_rate_check_failed": "O TikTok recusou a taxa de quadros do vídeo.",
    "picture_size_check_failed": "O TikTok recusou o tamanho da imagem do vídeo.",
    "video_pull_failed": "O TikTok não conseguiu receber o vídeo.",
    "internal": "Erro interno do TikTok. Tente de novo mais tarde.",
}


class ErroTikTok(Exception):
    def __init__(self, codigo, msg=""):
        self.codigo = codigo
        super().__init__(ERROS.get(codigo) or f"TikTok: {codigo} {msg}".strip())


# ------------------------------------------------------------------ credencial e token
def credencial():
    return config.get("tiktok_client_key").strip(), config.get("tiktok_client_secret").strip()


def configurado():
    k, s = credencial()
    return bool(k and s)


def _ler_token():
    try:
        with open(TOKEN, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _gravar_token(t):
    config.salvar_json(TOKEN, t, indent=1)


def _pedir_token(dados):
    import httpx
    k, s = credencial()
    r = httpx.post(API + "/oauth/token/", data={"client_key": k, "client_secret": s, **dados},
                   headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
    j = r.json() if r.content else {}
    if r.status_code >= 400 or j.get("error") or not j.get("access_token"):
        desc = j.get("error_description") or j.get("error") or r.text[:200]
        raise RuntimeError(f"O TikTok recusou o login: {desc}")
    agora = time.time()
    return {"access_token": j["access_token"], "refresh_token": j.get("refresh_token"),
            "expira": agora + int(j.get("expires_in") or 86400),
            "refresh_expira": agora + int(j.get("refresh_expires_in") or 365 * 86400),
            "open_id": j.get("open_id"), "escopo": j.get("scope", "")}


def _token():
    """Token válido (renova sozinho quando está para vencer)."""
    with _lock:
        t = _ler_token()
        if not t:
            raise RuntimeError("Conecte a conta do TikTok antes de enviar.")
        if time.time() < t["expira"] - 300:
            return t
        if not t.get("refresh_token") or time.time() > t.get("refresh_expira", 0):
            raise RuntimeError("O login do TikTok expirou. Conecte a conta de novo.")
        novo = _pedir_token({"grant_type": "refresh_token", "refresh_token": t["refresh_token"]})
        novo["refresh_token"] = novo["refresh_token"] or t["refresh_token"]
        _gravar_token(novo)
        return novo


def _api(caminho, corpo=None, metodo="POST", params=None):
    import httpx
    t = _token()
    r = httpx.request(metodo, API + caminho, params=params, json=corpo if metodo == "POST" else None, timeout=60,
                      headers={"Authorization": f"Bearer {t['access_token']}",
                               "Content-Type": "application/json; charset=UTF-8"})
    try:
        j = r.json()
    except ValueError:
        raise RuntimeError(f"TikTok {r.status_code}: {r.text[:200]}")
    err = j.get("error") or {}
    if err.get("code") not in (None, "", "ok"):
        raise ErroTikTok(err["code"], err.get("message", ""))
    if r.status_code >= 400:
        raise RuntimeError(f"TikTok {r.status_code}: {r.text[:200]}")
    return j.get("data") or {}


# ------------------------------------------------------------------ login
def _escopos():
    e = ["user.info.basic", "video.upload"]
    if config.get("tiktok_modo") == "direto":
        e.append("video.publish")
    return e


def conectar():
    """Abre a página de autorização do TikTok no navegador. A resposta volta em /api/tiktok/callback/."""
    if not configurado():
        raise RuntimeError("Coloque o Client key e o Client secret do app do TikTok e salve antes de conectar.")
    import webbrowser
    verifier = secrets.token_urlsafe(64)[:96]
    state = secrets.token_urlsafe(24)
    url = AUTH + "?" + urlencode({
        "client_key": credencial()[0], "response_type": "code", "scope": ",".join(_escopos()),
        "redirect_uri": REDIRECT, "state": state, "code_challenge": hashlib.sha256(verifier.encode()).hexdigest(),
        "code_challenge_method": "S256",
    })
    _login.update(status="aguardando", erro=None, url=url, state=state, verifier=verifier)
    try:
        webbrowser.open(url)
    except Exception:  # noqa - o painel mostra o link
        pass
    return url


def callback(code, state, erro=None, erro_desc=None):
    """Chamado quando o TikTok volta para o REDIRECT. Devolve (ok, mensagem)."""
    global _conta
    if erro:
        _login.update(status="erro", erro=f"O TikTok não autorizou: {erro_desc or erro}")
        return False, _login["erro"]
    if not state or state != _login.get("state") or not _login.get("verifier"):
        _login.update(status="erro", erro="Resposta do TikTok não confere com o pedido. Clique em Conectar de novo.")
        return False, _login["erro"]
    try:
        t = _pedir_token({"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
                          "code_verifier": _login["verifier"]})
        _gravar_token(t)
        _login.update(status="parado", erro=None, url=None, state=None, verifier=None)
        _conta = None
        conta()
        return True, "Conta do TikTok conectada."
    except Exception as e:  # noqa
        traceback.print_exc()
        _login.update(status="erro", erro=str(e)[:300])
        return False, _login["erro"]


def desconectar():
    global _conta
    t = _ler_token()
    if t:
        try:
            import httpx
            k, s = credencial()
            httpx.post(API + "/oauth/revoke/", data={"client_key": k, "client_secret": s, "token": t["access_token"]},
                       timeout=20)
        except Exception:  # noqa - apaga o login local de qualquer jeito
            pass
    try:
        os.remove(TOKEN)
    except OSError:
        pass
    _conta = None
    _login.update(status="parado", erro=None, url=None)


def conta(recarregar=False):
    global _conta
    if _conta and not recarregar:
        return _conta
    d = _api("/user/info/", metodo="GET", params={"fields": "open_id,display_name,avatar_url"})
    u = d.get("user") or {}
    _conta = {"nome": u.get("display_name") or "TikTok", "foto": u.get("avatar_url")}
    return _conta


def status(recarregar=False):
    t = _ler_token()
    out = {"configurado": configurado(), "conectado": False, "redirect": REDIRECT, "modo": config.get("tiktok_modo"),
           "login": {k: _login[k] for k in ("status", "erro", "url")}, "privacidades": PRIVACIDADES}
    if t:
        out["escopo"] = t.get("escopo", "")
        try:
            out["conta"] = conta(recarregar)
            out["conectado"] = True
        except Exception as e:  # noqa
            out["erro"] = str(e)[:300]
    return out


def criador():
    """Opções que o TikTok libera para esta conta agora (visibilidades, duração máxima...)."""
    d = _api("/post/publish/creator_info/query/")
    return {"privacidades": d.get("privacy_level_options") or [], "max_s": d.get("max_video_post_duration_sec"),
            "comentarios_off": d.get("comment_disabled"), "nome": d.get("creator_nickname"),
            "usuario": d.get("creator_username"), "foto": d.get("creator_avatar_url")}


# ------------------------------------------------------------------ envio
_fila = queue.Queue()


def _tt(cid, **kw):
    c = clipper.get_clip(cid) or {}
    t = dict(c.get("tiktok") or {})
    t.update(kw)
    clipper.update_clip(cid, tiktok=t)


def enviar(cid, legenda="", modo="rascunho", privacidade=None, comentarios=True):
    c = clipper.get_clip(cid)
    if not c or c.get("status") != "pronto":
        raise ValueError("corte não encontrado ou ainda não está pronto")
    if not os.path.exists(os.path.join(config.CLIPS_DIR, cid, "corte.mp4")):
        raise ValueError("o vídeo deste corte não existe mais")
    if modo not in MODOS:
        raise ValueError("modo inválido")
    if (c.get("tiktok") or {}).get("status") in ("na fila", "enviando", "processando"):
        raise ValueError("este corte já está sendo enviado ao TikTok")
    if modo == "direto" and privacidade not in PRIVACIDADES:
        raise ValueError("escolha a visibilidade do vídeo")
    if not _ler_token():
        raise ValueError("conecte a conta do TikTok antes de enviar")
    legenda = (legenda or "").strip()[:2200]
    clipper.update_clip(cid, tiktok={"status": "na fila", "modo": modo, "privacidade": privacidade,
                                     "progresso": 0, "pedido_em": time.strftime("%Y-%m-%dT%H:%M:%S")})
    _fila.put((cid, legenda, modo, privacidade, bool(comentarios)))
    return clipper.get_clip(cid)


def _subir(url, caminho, progresso):
    import httpx
    total = os.path.getsize(caminho)
    n, tam = _pedacos(total)
    with open(caminho, "rb") as f:
        for i in range(n):
            ini = i * tam
            fim = total - 1 if i == n - 1 else ini + tam - 1
            dados = f.read(fim - ini + 1)
            r = httpx.put(url, content=dados, timeout=600, headers={
                "Content-Type": "video/mp4", "Content-Length": str(len(dados)),
                "Content-Range": f"bytes {ini}-{fim}/{total}"})
            if r.status_code >= 400:
                raise RuntimeError(f"O TikTok recusou o envio do vídeo ({r.status_code}): {r.text[:200]}")
            progresso((fim + 1) / total)


def _pedacos(total):
    """(quantidade, tamanho) dos pedaços: até 64 MB vai inteiro; acima, pedaços de 10 MB e o último leva a sobra."""
    if total <= UM_PEDACO_MAX:
        return 1, total
    return max(1, total // PEDACO), PEDACO


def _enviar_agora(cid, legenda, modo, privacidade, comentarios):
    caminho = os.path.join(config.CLIPS_DIR, cid, "corte.mp4")
    total = os.path.getsize(caminho)
    n, tam = _pedacos(total)
    fonte = {"source": "FILE_UPLOAD", "video_size": total, "chunk_size": tam, "total_chunk_count": n}
    _tt(cid, status="enviando", progresso=0, erro=None)
    if modo == "rascunho":
        d = _api("/post/publish/inbox/video/init/", {"source_info": fonte})
    else:
        info = criador()
        if privacidade not in info["privacidades"]:
            raise ErroTikTok("privacy_level_option_mismatch")
        _tt(cid, usuario=info.get("usuario"))  # para montar o link do vídeo publicado
        d = _api("/post/publish/video/init/", {"source_info": fonte, "post_info": {
            "title": legenda, "privacy_level": privacidade, "disable_comment": not comentarios,
            "disable_duet": False, "disable_stitch": False, "video_cover_timestamp_ms": 1000}})
    _tt(cid, publish_id=d["publish_id"])
    _subir(d["upload_url"], caminho, lambda p: _tt(cid, progresso=round(p, 3)))
    _tt(cid, status="processando", progresso=1)
    _acompanhar(cid, d["publish_id"], modo)


def _acompanhar(cid, publish_id, modo):
    limite = time.time() + ESPERA_MAX_S
    while time.time() < limite:
        time.sleep(5)
        d = _api("/post/publish/status/fetch/", {"publish_id": publish_id})
        st = d.get("status")
        if st == "FAILED":
            raise ErroTikTok(d.get("fail_reason") or "internal")
        if st == "SEND_TO_USER_INBOX":
            _tt(cid, status="rascunho", enviado_em=time.strftime("%Y-%m-%dT%H:%M:%S"))
            return
        if st == "PUBLISH_COMPLETE":
            ids = d.get("publicaly_available_post_id") or []
            usuario = ((clipper.get_clip(cid) or {}).get("tiktok") or {}).get("usuario")
            url = f"https://www.tiktok.com/@{usuario}/video/{ids[0]}" if ids and usuario else None
            _tt(cid, status="publicado", post_id=ids[0] if ids else None, url=url,
                enviado_em=time.strftime("%Y-%m-%dT%H:%M:%S"))
            return
    _tt(cid, status="erro", erro="O TikTok demorou demais para processar. Confira no app se o vídeo chegou.")


def _worker():
    while True:
        cid, *args = _fila.get()
        try:
            _enviar_agora(cid, *args)
        except Exception as e:  # noqa
            traceback.print_exc()
            _tt(cid, status="erro", erro=str(e)[:300])


def _retomar():
    """Ao abrir o programa: envio interrompido no meio vira erro; o que já tinha subido volta a ser acompanhado."""
    for c in clipper.list_clips():
        t = c.get("tiktok") or {}
        if t.get("status") in ("na fila", "enviando"):
            _tt(c["id"], status="erro", erro="O programa foi fechado durante o envio. Envie de novo.")
        elif t.get("status") == "processando" and t.get("publish_id"):
            threading.Thread(target=_seguir, args=(c["id"], t["publish_id"], t.get("modo")), daemon=True).start()


def _seguir(cid, publish_id, modo):
    try:
        _acompanhar(cid, publish_id, modo)
    except Exception as e:  # noqa
        _tt(cid, status="erro", erro=str(e)[:300])


_retomar()
threading.Thread(target=_worker, daemon=True).start()
