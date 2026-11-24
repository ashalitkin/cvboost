"""Thin Telegram Bot API client over `HttpClient.post_json` (no SDK).

The bot token is part of every request URL. It lives only in `self._token`, is never part of
`repr(client)`, and no exception that leaves this module carries the URL: every failure becomes a
`TelegramError` built from method, error class, HTTP status and Telegram's own `description`,
raised outside the `except` block so no traceback chain points back to the URL.
"""

from __future__ import annotations

from typing import Any

from cvboost.http import FetchError, HttpClient
from cvboost.notify.redact import redact

HOST = "api.telegram.org"
ALLOWED_HOSTS = frozenset({HOST})
SEND_DELAY_S = 1.1  # Telegram: avoid more than ~1 message per second in one chat
NOT_MODIFIED = "message is not modified"
MAX_DESCRIPTION = 200
UPDATES_LIMIT = 5
UPDATES_MAX_BYTES = 2 * 1024 * 1024


class TelegramError(Exception):
    def __init__(
        self,
        method: str,
        *,
        kind: str,
        status: int | None = None,
        description: str | None = None,
        retry_after: int | None = None,
        token: str | None = None,
    ):
        desc = redact(description or "", token)[:MAX_DESCRIPTION]  # redact first, then cut
        parts = [f"telegram {method}: {kind}"]
        if status is not None:
            parts.append(f"HTTP {status}")
        if desc:
            parts.append(desc)
        super().__init__(" ".join(parts))
        self.method = method
        self.kind = kind
        self.status = status
        self.description = desc
        self.retry_after = retry_after


class TelegramClient:
    def __init__(self, token: str, http: HttpClient):
        if not token:
            raise ValueError("empty Telegram token")
        self._token = token
        self._http = http

    def __repr__(self) -> str:
        return "TelegramClient(token=<hidden>)"

    def _call(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        timeout_s: float = 20.0,
        min_delay_s: float = 0.0,
        max_bytes: int | None = None,
    ) -> Any:
        url = f"https://{HOST}/bot{self._token}/{method}"
        failure: tuple[str, int | None] | None = None
        resp = None
        try:
            resp = self._http.post_json(
                url,
                payload=payload,
                allowed_hosts=ALLOWED_HOSTS,
                min_delay_s=min_delay_s,
                timeout_s=timeout_s,
                **({"max_bytes": max_bytes} if max_bytes else {}),
            )
        except FetchError as exc:
            failure = (type(exc).__name__, exc.status)
        except Exception as exc:  # transport errors: their text contains the URL
            failure = (type(exc).__name__, None)
        if failure is not None:  # raised here, outside `except`: no __context__ with the URL
            raise TelegramError(method, kind=failure[0], status=failure[1], token=self._token)
        assert resp is not None
        try:
            data = resp.json()
        except ValueError:
            data = None
        if resp.status_code == 200 and isinstance(data, dict) and data.get("ok") is True:
            return data.get("result")
        body = data if isinstance(data, dict) else {}
        params = body.get("parameters")
        retry_after = params.get("retry_after") if isinstance(params, dict) else None
        raise TelegramError(
            method,
            kind="ApiError",
            status=resp.status_code,
            description=str(body.get("description") or ""),
            retry_after=retry_after if isinstance(retry_after, int) else None,
            token=self._token,
        )

    def get_me(self) -> dict[str, Any]:
        return self._call("getMe", {}, timeout_s=15)

    def get_updates(self, offset: int | None, timeout_s: int = 25) -> list[dict[str, Any]]:
        """Small batches and a 2 MB cap: only the owner's button presses are wanted. If a batch is
        over the cap, ask for one update at a time."""
        for limit in (UPDATES_LIMIT, 1):
            payload: dict[str, Any] = {
                "timeout": timeout_s,
                "limit": limit,
                "allowed_updates": ["callback_query"],
            }
            if offset is not None:
                payload["offset"] = offset
            try:
                return (
                    self._call(
                        "getUpdates", payload, timeout_s=timeout_s + 10, max_bytes=UPDATES_MAX_BYTES
                    )
                    or []
                )
            except TelegramError as exc:
                if exc.kind != "ResponseTooLarge" or limit == 1:
                    raise
        return []  # unreachable

    def recent_chats(self) -> tuple[list[dict[str, Any]], int]:
        """Who wrote to the bot, from ONE getUpdates call (setup helper, no long poll, nothing is
        acknowledged). Returns ([{id, type}] of private chats, each once; number of other chats
        ignored). Names and titles are attacker-controlled text and are never returned. A running
        worker polls the same queue: stop it first (Telegram answers 409)."""
        updates = self._call(
            "getUpdates",
            {"timeout": 0, "limit": 100, "allowed_updates": ["message", "my_chat_member"]},
            timeout_s=15,
            max_bytes=UPDATES_MAX_BYTES,
        )
        private: dict[int, dict[str, Any]] = {}
        other: set[int] = set()
        for u in updates or []:
            if not isinstance(u, dict):
                continue
            msg = u.get("message") or u.get("my_chat_member")
            chat = msg.get("chat") if isinstance(msg, dict) else None
            if not isinstance(chat, dict) or not isinstance(chat.get("id"), int):
                continue
            if chat.get("type") == "private":
                private.setdefault(chat["id"], {"id": chat["id"], "type": "private"})
            else:
                other.add(chat["id"])
        return list(private.values()), len(other)

    def send_message(
        self, chat_id: int, text: str, keyboard: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Plain text (no parse_mode), link previews off. Returns the sent Message."""
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "link_preview_options": {"is_disabled": True},
        }
        if keyboard is not None:
            payload["reply_markup"] = keyboard
        return self._call("sendMessage", payload, min_delay_s=SEND_DELAY_S)

    def edit_markup(self, chat_id: int, message_id: int, keyboard: dict[str, Any]) -> None:
        try:
            self._call(
                "editMessageReplyMarkup",
                {"chat_id": chat_id, "message_id": message_id, "reply_markup": keyboard},
            )
        except TelegramError as exc:
            if exc.status == 400 and NOT_MODIFIED in exc.description.lower():
                return  # pressing a button twice: the markup is already what we wanted
            raise

    def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text[:200]  # Bot API: 0-200 characters
        self._call("answerCallbackQuery", payload)
