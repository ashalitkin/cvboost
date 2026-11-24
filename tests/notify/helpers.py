from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

from cvboost.http import HttpClient
from cvboost.notify.telegram import TelegramClient
from tests.conftest import PUBLIC_RESOLVER

TOKEN = "123456789:" + ("AAfakeTOKEN_-" * 3)[:35]  # real-looking shape, never a real token
OWNER = 777000111
assert len(TOKEN.split(":")[1]) == 35


class FakeTelegram:
    """Records (method, payload); answers like the Bot API. Queue canned replies per method."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.updates: list[dict[str, Any]] = []
        self.replies: dict[str, list[Any]] = {}  # method -> [httpx.Response | Exception | callable]
        self._mid = 100
        self.urls: list[str] = []
        self.stop_after: tuple[Any, int] | None = None  # (Event, polls): ends a bot loop
        self.polls = 0

    def queue(self, method: str, *replies: Any) -> None:
        self.replies.setdefault(method, []).extend(replies)

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.urls.append(str(req.url))
        method = req.url.path.rsplit("/", 1)[-1]
        payload = json.loads(req.content or b"{}")
        self.calls.append((method, payload))
        if method == "getUpdates":
            self.polls += 1
            if self.stop_after and self.polls >= self.stop_after[1]:
                self.stop_after[0].set()
        if self.replies.get(method):
            r = self.replies[method].pop(0)
            if isinstance(r, Exception):
                raise r
            return r(req) if callable(r) else r
        if method == "sendMessage":
            self._mid += 1
            return ok({"message_id": self._mid, "chat": {"id": payload["chat_id"]}})
        if method == "getUpdates":
            ups, self.updates = self.updates, []
            return ok(ups)
        if method == "getMe":
            return ok({"id": 1, "is_bot": True, "username": "cvboost_test_bot"})
        return ok(True)

    def of(self, method: str) -> list[dict[str, Any]]:
        return [p for m, p in self.calls if m == method]

    def client(self) -> TelegramClient:
        http = HttpClient(
            httpx.MockTransport(self.handler), sleep=lambda s: None, resolver=PUBLIC_RESOLVER
        )
        return TelegramClient(TOKEN, http)


def ok(result: Any) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


def err(status: int, description: str, **params: Any) -> httpx.Response:
    body: dict[str, Any] = {"ok": False, "error_code": status, "description": description}
    if params:
        body["parameters"] = params
    return httpx.Response(status, json=body)


def callback(
    data: str,
    *,
    message_id: int,
    update_id: int = 1,
    user: int = OWNER,
    chat: int = OWNER,
    cb_id: str = "cbq1",
) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": cb_id,
            "from": {"id": user, "is_bot": False, "first_name": "x"},
            "message": {"message_id": message_id, "chat": {"id": chat, "type": "private"}},
            "data": data,
        },
    }


def last(items: list, default: Callable | None = None):
    return items[-1] if items else default
