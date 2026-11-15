"""Google Agenda → WhatsApp: evento "Enviar no WhatsApp: <número>" vira mensagem.

Max marca na Agenda um evento com o título

    Enviar no WhatsApp: 5584999990000

e escreve a mensagem na descrição. O dia e a hora do evento são a hora do envio.

O servidor lê a Agenda de hora em hora (EVOLUTION_CALENDAR_INTERVAL_MIN) e põe
cada evento desses na fila de mensagens agendadas que já existe (scheduler.py), que
olha a cada 30 s. Ler de hora em hora e enviar no minuto certo são, assim, coisas
separadas: a leitura só precisa enxergar o evento antes da hora dele.

Cada evento fica ligado à mensagem agendada que gerou (tabela `calendar_links`):

- evento novo → agenda a mensagem;
- evento editado (número, texto ou horário) antes do envio → troca a agendada;
- evento apagado, ou título que deixou de começar com "Enviar no WhatsApp" → cancela;
- evento já enviado não sai de novo, a não ser que mude para um horário futuro.

Evento criado em cima da hora, que a leitura só viu depois do horário dele, sai na
hora em que foi visto, com até uma leitura de atraso.

Leitura pela API do Google Calendar com refresh token OAuth do próprio Max, no
escopo `calendar.readonly` (só lê). O token sai de
`scripts/google_oauth_setup.py --calendar`.
"""

from __future__ import annotations

import hashlib
import html
import re
import sys
import threading
from datetime import datetime, time, timedelta, timezone
from typing import Any, Callable
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

TOKEN_URL = "https://oauth2.googleapis.com/token"
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/{cal}/events"
SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
MARCADOR = "[agenda google]"
MAX_TEXT = 4000

# "Enviar no WhatsApp: 5584...", aceitando "pelo/por/via", "Whats App", "zap" e sem dois-pontos
_TITULO = re.compile(
    r"^\s*enviar\s+(?:no|pelo|por|via|pro|para\s+o)\s+(?:whats\s*-?\s*app|zap)\s*[:\-–—]?\s*(.*?)\s*$",
    re.IGNORECASE,
)
_SO_NUMERO = re.compile(r"^\+?[\d\s().\-]+$")
_QUEBRA = re.compile(r"<\s*(?:br\s*/?|/p|/div|/h\d)\s*>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")


class CalendarError(Exception):
    """Erro ao falar com o Google Agenda ou ao interpretar um evento."""


def _log(message: str, level: str = "INFO") -> None:
    print(f"[{level}] Agenda Google: {message}", file=sys.stderr)


# ---------------------------------------------------------------- interpretação

def parse_title(titulo: str) -> str | None:
    """Devolve o destinatário escrito no título, ou None se o evento não é de envio.

    Levanta CalendarError quando o título é de envio mas o número não presta.
    """
    m = _TITULO.match(titulo or "")
    if not m:
        return None
    alvo = m.group(1).strip()
    if not alvo:
        raise CalendarError("título sem número depois de 'Enviar no WhatsApp:'")
    if "@" in alvo:  # jid de grupo ou de conversa, como veio
        return alvo
    if not _SO_NUMERO.match(alvo):
        raise CalendarError(f"'{alvo}' não é um número de telefone")
    digitos = re.sub(r"\D", "", alvo)
    if len(digitos) in (10, 11):  # DDD + número, sem o 55 do Brasil
        digitos = "55" + digitos
    if not 12 <= len(digitos) <= 15:
        raise CalendarError(f"número '{alvo}' com {len(digitos)} dígitos; use DDI + DDD + número")
    return digitos


def description_text(descricao: str) -> str:
    """A descrição do evento vem em HTML quando tem formatação. Vira texto do WhatsApp."""
    texto = descricao or ""
    texto = _QUEBRA.sub("\n", texto)
    texto = re.sub(r"<\s*li[^>]*>", "\n• ", texto, flags=re.IGNORECASE)
    texto = re.sub(r"<\s*(?:b|strong)\s*>(.*?)<\s*/\s*(?:b|strong)\s*>", r"*\1*", texto, flags=re.IGNORECASE | re.DOTALL)
    texto = re.sub(r"<\s*(?:i|em)\s*>(.*?)<\s*/\s*(?:i|em)\s*>", r"_\1_", texto, flags=re.IGNORECASE | re.DOTALL)
    texto = _TAG.sub("", texto)
    texto = html.unescape(texto).replace("\xa0", " ")
    linhas = [linha.rstrip() for linha in texto.replace("\r\n", "\n").split("\n")]
    texto = "\n".join(linhas)
    texto = re.sub(r"\n{3,}", "\n\n", texto)
    return texto.strip()


def _parse_dt(valor: str) -> datetime:
    if valor.endswith("Z"):
        valor = valor[:-1] + "+00:00"
    return datetime.fromisoformat(valor)


def event_start(evento: dict[str, Any], tz: str, allday_time: time) -> datetime:
    """Início do evento como instante com fuso. Evento de dia inteiro sai em `allday_time`."""
    inicio = evento.get("start") or {}
    if inicio.get("dateTime"):
        dt = _parse_dt(inicio["dateTime"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(inicio.get("timeZone") or tz))
        return dt
    if inicio.get("date"):
        dia = datetime.fromisoformat(inicio["date"]).date()
        return datetime.combine(dia, allday_time, tzinfo=ZoneInfo(inicio.get("timeZone") or tz))
    raise CalendarError("evento sem horário de início")


def fingerprint(chat: str, text: str, start: datetime) -> str:
    base = f"{chat}\n{start.astimezone(timezone.utc).isoformat()}\n{text}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------- cliente

class CalendarClient:
    """Cliente mínimo da API do Google Calendar v3, sem as bibliotecas do Google."""

    def __init__(self, client_id: str, client_secret: str, refresh_token: str,
                 calendar_id: str = "primary", timeout: int = 30):
        self.client_id = (client_id or "").strip()
        self.client_secret = (client_secret or "").strip()
        self.refresh_token = (refresh_token or "").strip()
        self.calendar_id = (calendar_id or "primary").strip() or "primary"
        self.timeout = timeout
        self._token: str | None = None
        self._expires: datetime | None = None

    @property
    def available(self) -> bool:
        return bool(self.client_id and self.client_secret and self.refresh_token)

    def _access_token(self) -> str:
        if self._token and self._expires and datetime.now() < self._expires:
            return self._token
        try:
            r = requests.post(TOKEN_URL, data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
            }, timeout=self.timeout)
        except requests.exceptions.RequestException as e:
            raise CalendarError(f"falha de conexão ao renovar o token: {e}")
        if r.status_code >= 300:
            try:
                detalhe = r.json().get("error_description") or r.json().get("error") or ""
            except ValueError:
                detalhe = r.text[:200]
            raise CalendarError(
                f"não consegui renovar o acesso à Agenda (HTTP {r.status_code}): {detalhe}. "
                "Gere outro token com scripts/google_oauth_setup.py --calendar."
            )
        payload = r.json()
        self._token = payload["access_token"]
        self._expires = datetime.now() + timedelta(seconds=int(payload.get("expires_in", 3600)) - 60)
        return self._token

    def list_events(self, time_min: datetime, time_max: datetime) -> list[dict[str, Any]]:
        """Eventos que começam entre os dois instantes, com recorrências já expandidas."""
        url = EVENTS_URL.format(cal=quote(self.calendar_id, safe=""))
        params: dict[str, Any] = {
            "timeMin": time_min.astimezone(timezone.utc).isoformat(),
            "timeMax": time_max.astimezone(timezone.utc).isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": 250,
            "fields": "items(id,status,summary,description,start,updated),nextPageToken",
        }
        eventos: list[dict[str, Any]] = []
        for _ in range(20):  # teto de páginas: 5000 eventos numa janela de horas não é agenda, é bug
            try:
                r = requests.get(url, params=params, timeout=self.timeout,
                                 headers={"Authorization": f"Bearer {self._access_token()}"})
            except requests.exceptions.RequestException as e:
                raise CalendarError(f"falha de conexão com a Agenda: {e}")
            if r.status_code >= 300:
                try:
                    erro = r.json().get("error", {})
                    detalhe = erro.get("message") or ""
                except ValueError:
                    detalhe = r.text[:200]
                raise CalendarError(f"Agenda respondeu HTTP {r.status_code}: {detalhe[:300]}")
            dados = r.json()
            eventos.extend(dados.get("items") or [])
            proxima = dados.get("nextPageToken")
            if not proxima:
                break
            params["pageToken"] = proxima
        return eventos


# ---------------------------------------------------------------- leitor

class CalendarWatcher:
    """Lê a Agenda e mantém a fila de agendadas em dia com ela. Nunca derruba o processo."""

    def __init__(
        self,
        store: Any,
        calendar: Any,
        tz: str = "America/Fortaleza",
        interval_s: float = 3600,
        lookahead_s: float = 24 * 3600,
        allday_time: str = "09:00",
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.store = store
        self.calendar = calendar
        self.tz = tz
        self.interval_s = interval_s
        self.lookahead_s = lookahead_s
        hora, minuto = (int(p) for p in (allday_time or "09:00").split(":", 1))
        self.allday_time = time(hora, minuto)
        self.clock = clock
        self.started_at = clock()
        self.last_sync: datetime | None = None
        self.last_result: dict[str, Any] | None = None
        self.last_error: str | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ leitura

    def sync(self) -> dict[str, Any]:
        """Uma leitura: agenda o que é novo, troca o que mudou, cancela o que sumiu."""
        with self._lock:
            agora = self.clock()
            # Volta uma leitura inteira (e uma folga) para pegar o evento criado em cima da hora.
            inicio = agora - timedelta(seconds=self.interval_s + 600)
            fim = agora + timedelta(seconds=self.lookahead_s)
            eventos = self.calendar.list_events(inicio, fim)
            resultado: dict[str, Any] = {
                "lidos": len(eventos), "agendados": [], "atualizados": [], "cancelados": [],
                "ignorados": [], "sem_mudanca": 0,
            }
            vistos: set[str] = set()
            for evento in eventos:
                if evento.get("status") == "cancelled":
                    continue
                titulo = evento.get("summary") or ""
                try:
                    chat = parse_title(titulo)
                except CalendarError as e:
                    resultado["ignorados"].append({"titulo": titulo, "motivo": str(e)})
                    continue
                if chat is None:
                    continue
                eid = evento.get("id") or ""
                try:
                    inicio_ev = event_start(evento, self.tz, self.allday_time)
                except CalendarError as e:
                    resultado["ignorados"].append({"titulo": titulo, "motivo": str(e)})
                    continue
                texto = description_text(evento.get("description") or "")
                if not texto:
                    resultado["ignorados"].append({"titulo": titulo, "quando": self._fmt(inicio_ev),
                                                   "motivo": "descrição vazia: a mensagem vai na descrição do evento"})
                    continue
                if len(texto) > MAX_TEXT:
                    resultado["ignorados"].append({"titulo": titulo, "quando": self._fmt(inicio_ev),
                                                   "motivo": f"mensagem com {len(texto)} caracteres; o máximo é {MAX_TEXT}"})
                    continue
                vistos.add(eid)
                self._apply(eid, chat, texto, inicio_ev, agora, resultado)

            # O que estava agendado pela Agenda, dentro da janela lida, e não apareceu: sumiu.
            for link in self.store.calendar_pending_links():
                if link["event_id"] in vistos:
                    continue
                send_at = link.get("send_at")
                if send_at is not None and inicio <= send_at <= fim:
                    if self.store.cancel_scheduled(link["scheduled_id"]):
                        resultado["cancelados"].append({"id": link["scheduled_id"], "quando": self._fmt(send_at)})
                        _log(f"#{link['scheduled_id']} cancelada: o evento sumiu da Agenda")

            self.last_sync = agora
            self.last_result = resultado
            self.last_error = None
            if resultado["agendados"] or resultado["atualizados"] or resultado["cancelados"] or resultado["ignorados"]:
                _log(
                    f"{len(eventos)} eventos lidos; {len(resultado['agendados'])} agendados, "
                    f"{len(resultado['atualizados'])} atualizados, {len(resultado['cancelados'])} cancelados, "
                    f"{len(resultado['ignorados'])} ignorados"
                )
            return resultado

    def _apply(self, eid: str, chat: str, texto: str, inicio_ev: datetime, agora: datetime,
               resultado: dict[str, Any]) -> None:
        marca = fingerprint(chat, texto, inicio_ev)
        link = self.store.calendar_link(eid)
        if link is not None and link.get("fingerprint") == marca:
            resultado["sem_mudanca"] += 1
            return

        atrasado = inicio_ev <= agora
        if link is None:
            if atrasado and not self._may_send_late(inicio_ev):
                resultado["ignorados"].append({"chat": chat, "quando": self._fmt(inicio_ev),
                                               "motivo": "horário anterior à subida do servidor; não envio atrasado"})
                return
            sid = self.store.schedule(chat, texto, max(inicio_ev, agora), "text")
            self.store.set_calendar_link(eid, sid, marca, inicio_ev)
            resultado["agendados"].append(self._item(sid, chat, texto, inicio_ev, atrasado))
            _log(f"#{sid} para {chat} em {self._fmt(inicio_ev)}" + (" (atrasado: sai agora)" if atrasado else ""))
            return

        # Evento já conhecido que mudou.
        status = link.get("status")
        if status == "pending":
            self.store.cancel_scheduled(link["scheduled_id"])
        elif status == "sent":
            # Já foi. Só sai de novo se ele foi remarcado para um horário futuro.
            mudou_hora = link.get("start_at") != inicio_ev
            if not (mudou_hora and not atrasado):
                self.store.set_calendar_link(eid, link["scheduled_id"], marca, inicio_ev)
                resultado["sem_mudanca"] += 1
                return
        elif atrasado:
            # Falhou ou foi cancelada, e o horário já passou: não reenvio por conta própria.
            self.store.set_calendar_link(eid, link["scheduled_id"], marca, inicio_ev)
            resultado["sem_mudanca"] += 1
            return
        sid = self.store.schedule(chat, texto, max(inicio_ev, agora), "text")
        self.store.set_calendar_link(eid, sid, marca, inicio_ev)
        resultado["atualizados"].append(self._item(sid, chat, texto, inicio_ev, atrasado) | {"substitui": link["scheduled_id"]})
        _log(f"#{link['scheduled_id']} → #{sid}: evento mudou ({chat}, {self._fmt(inicio_ev)})")

    def _may_send_late(self, inicio_ev: datetime) -> bool:
        # Com banco, o vínculo sobrevive ao restart: o que não tem vínculo nunca foi enviado.
        # Sem banco, depois de um restart não sei; só envio atrasado o que é desta execução.
        if getattr(self.store, "kind", "") == "postgres":
            return True
        return inicio_ev >= self.started_at

    # ------------------------------------------------------------------ laço

    def run_forever(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        _log(f"ativa: lê a Agenda '{getattr(self.calendar, 'calendar_id', '?')}' a cada "
             f"{int(self.interval_s // 60)} min, fuso {self.tz}")
        stop.wait(15)  # deixa o resto do servidor subir antes da primeira leitura
        while not stop.is_set():
            try:
                self.sync()
            except Exception as e:
                self.last_error = str(e)[:500]
                _log(f"erro na leitura: {e}", "ERROR")
            stop.wait(self.interval_s)

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.run_forever, name="agenda-google", daemon=True)
        t.start()
        return t

    # ------------------------------------------------------------------ saída

    def _fmt(self, dt: datetime | None) -> str | None:
        return dt.astimezone(ZoneInfo(self.tz)).strftime("%Y-%m-%d %H:%M") if dt else None

    def _item(self, sid: int, chat: str, texto: str, inicio: datetime, atrasado: bool) -> dict[str, Any]:
        item = {"id": sid, "chat": chat, "quando": self._fmt(inicio),
                "texto": texto if len(texto) <= 120 else texto[:117] + "..."}
        if atrasado:
            item["atrasado"] = True
        return item

    def describe(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "ativa": True,
            "agenda": getattr(self.calendar, "calendar_id", None),
            "intervalo_min": int(self.interval_s // 60),
            "janela_horas": round(self.lookahead_s / 3600, 1),
            "ultima_leitura": self._fmt(self.last_sync),
        }
        if self.last_error:
            info["ultimo_erro"] = self.last_error
        if self.last_result:
            info["ignorados"] = self.last_result.get("ignorados", [])[:10]
        return info
