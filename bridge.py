"""Forward received SimpleX text messages to a Telegram chat."""

import json
import logging
import os
import re
import signal
import time
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass, field
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Config:
    simplex_chat: str
    telegram_chat: int
    telegram_token: str = field(repr=False)
    simplex_url: str = "ws://127.0.0.1:5225"

    @classmethod
    def from_env(cls) -> "Config":
        def required(name: str) -> str:
            value = os.environ.get(name, "").strip()
            if not value:
                raise ValueError(f"Set {name}")
            return value

        source = required("SIMPLEX_CHAT_ID")
        if not re.fullmatch(r"[@#][1-9][0-9]*", source):
            raise ValueError("SIMPLEX_CHAT_ID must be #<groupId> or @<contactId>")
        target = required("TELEGRAM_CHAT_ID")
        if not re.fullmatch(r"-?[1-9][0-9]*", target):
            raise ValueError("TELEGRAM_CHAT_ID must be a nonzero integer")
        token = required("TELEGRAM_BOT_TOKEN")
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
            raise ValueError("TELEGRAM_BOT_TOKEN must be a BotFather token")
        url = os.environ.get("SIMPLEX_WS_URL", "ws://127.0.0.1:5225").strip()
        try:
            parsed = urlsplit(url)
            valid = parsed.scheme in {"ws", "wss"} and parsed.hostname
            valid = valid and not parsed.fragment and parsed.port != 0
            valid = valid and not any(char.isspace() for char in url)
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("SIMPLEX_WS_URL must be a valid ws:// or wss:// URL")
        return cls(source, int(target), token, url)


def message_text(entry: dict, source: str) -> str | None:
    """Select received text from the configured contact or main group chat."""
    try:
        info = entry["chatInfo"]
        item = entry["chatItem"]
        direction = item["chatDir"]
        if info["type"] == "direct" and direction["type"] == "directRcv":
            sender = info["contact"]
            chat = f"@{sender['contactId']}"
        elif info["type"] == "group" and direction["type"] == "groupRcv":
            if info.get("groupChatScope") is not None:
                return None
            sender = direction["groupMember"]
            chat = f"#{info['groupInfo']['groupId']}"
        else:
            return None
        if chat != source or item.get("meta", {}).get("itemDeleted") is not None:
            return None
        content = item["content"]
        if content["type"] != "rcvMsgContent":
            return None
        message = content["msgContent"]
        if message["type"] not in {"text", "link"}:
            return None
        body = message["text"]
        if not isinstance(body, str) or not body.strip():
            return None
        name = " ".join(sender["localDisplayName"].split()) or "SimpleX"
        return f"{name}: {body}"
    except (KeyError, TypeError, AttributeError):
        return None


def event_messages(event: dict, source: str) -> Iterator[str]:
    if not isinstance(event, dict) or event.get("corrId") is not None:
        return
    response = event.get("resp")
    if not isinstance(response, dict) or response.get("type") != "newChatItems":
        return
    entries = response.get("chatItems")
    if isinstance(entries, list):
        for entry in entries:
            text = message_text(entry, source)
            if text is not None:
                yield text


def telegram_chunks(text: str) -> Iterator[str]:
    """Keep each chunk within 4096 UTF-16 units without splitting a character."""
    start = size = 0
    for index, char in enumerate(text):
        width = 2 if ord(char) > 0xFFFF else 1
        if size + width > 4096:
            yield text[start:index]
            start, size = index, 0
        size += width
    if start < len(text):
        yield text[start:]


def send_telegram(config: Config, text: str) -> None:
    payload = json.dumps({
        "chat_id": config.telegram_chat,
        "text": text,
        "link_preview_options": {"is_disabled": True},
    }).encode("utf-8")
    request = Request(
        f"https://api.telegram.org/bot{config.telegram_token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    backoff = 1
    while True:
        delay = backoff
        try:
            try:
                response = urlopen(request, timeout=30)
            except HTTPError as error:
                response = error
            with response:
                status = response.status
                try:
                    result = json.load(response)
                except (ValueError, UnicodeError):
                    result = {}
            if not isinstance(result, dict):
                result = {}
            if status == 200 and result.get("ok") is True:
                return
            code = result.get("error_code", status)
            if code == 429:
                parameters = result.get("parameters")
                retry_after = (
                    parameters.get("retry_after") if isinstance(parameters, dict) else None
                )
                delay = max(1, retry_after) if isinstance(retry_after, int) else backoff
            elif not isinstance(code, int) or not 500 <= code < 600:
                raise RuntimeError(
                    f"Telegram rejected sendMessage (HTTP {status}); "
                    "check the bot token, chat ID, and posting permissions"
                )
        except (URLError, OSError, HTTPException):
            pass
        log.warning("Telegram send failed; retrying in %s seconds", delay)
        time.sleep(delay)
        backoff = min(backoff * 2, 60)


def simplex_messages(config: Config) -> Iterator[str]:
    from websockets.exceptions import WebSocketException
    from websockets.sync.client import connect

    delay = 1
    while True:
        try:
            with connect(config.simplex_url, proxy=None, close_timeout=5) as websocket:
                log.info("Connected to SimpleX")
                for raw in websocket:
                    delay = 1
                    try:
                        event = json.loads(raw)
                    except (ValueError, UnicodeError):
                        log.warning("Ignoring invalid SimpleX JSON")
                        continue
                    yield from event_messages(event, config.simplex_chat)
        except (OSError, WebSocketException) as error:
            log.warning("SimpleX connection failed (%s)", type(error).__name__)
        log.warning("Reconnecting to SimpleX in %s seconds", delay)
        time.sleep(delay)
        delay = min(delay * 2, 60)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        config = Config.from_env()
        with closing(simplex_messages(config)) as messages:
            for text in messages:
                for chunk in telegram_chunks(text):
                    if chunk.strip():
                        send_telegram(config, chunk)
    except KeyboardInterrupt:
        log.info("Stopped")
    except ModuleNotFoundError:
        log.error("Install the dependency: python3 -m pip install -r requirements.txt")
        return 1
    except (ValueError, RuntimeError) as error:
        log.error("%s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
