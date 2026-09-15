import importlib.util
import io
import json
import os
import unittest
from contextlib import closing
from copy import deepcopy
from http.client import IncompleteRead
from unittest.mock import MagicMock, call, patch
from urllib.error import HTTPError, URLError

import bridge


ENV = {
    "SIMPLEX_CHAT_ID": "#7",
    "TELEGRAM_CHAT_ID": "-1001234567890",
    "TELEGRAM_BOT_TOKEN": "123456:test-token",
}
CONFIG = bridge.Config("#7", -1001234567890, ENV["TELEGRAM_BOT_TOKEN"])
GROUP_ITEM = {
    "chatInfo": {"type": "group", "groupInfo": {"groupId": 7}},
    "chatItem": {
        "chatDir": {
            "type": "groupRcv",
            "groupMember": {"localDisplayName": "Alice"},
        },
        "meta": {"itemId": 42},
        "content": {
            "type": "rcvMsgContent",
            "msgContent": {"type": "text", "text": "hello 👋\nnext line"},
        },
    },
}


def event(*items):
    return {"resp": {"type": "newChatItems", "chatItems": list(items)}}


def http_response(status=200, body=None, raw=None):
    if raw is None:
        raw = json.dumps({"ok": True} if body is None else body).encode()
    response = io.BytesIO(raw)
    if status >= 400:
        return HTTPError("https://api.telegram.org/botSECRET/sendMessage", status,
                         "error", {}, response)
    response.status = status
    return response


class ConfigTests(unittest.TestCase):
    def test_environment_and_defaults(self):
        with patch.dict(os.environ, ENV, clear=True):
            self.assertEqual(bridge.Config.from_env(), CONFIG)
        self.assertNotIn(CONFIG.telegram_token, repr(CONFIG))

    def test_missing_and_invalid_values(self):
        cases = {
            "SIMPLEX_CHAT_ID": ["", "group", "#0", "#-7", "@alice", "#７"],
            "TELEGRAM_CHAT_ID": ["", "0", "@channel", "1.2"],
            "TELEGRAM_BOT_TOKEN": ["", "bad", "123:token/extra"],
            "SIMPLEX_WS_URL": ["", "http://localhost", "ws://", "ws://[",
                               "ws://localhost:bad", "ws://localhost:0",
                               "ws://bad host:5225", "ws://local\nhost:5225"],
        }
        for name, values in cases.items():
            for value in values:
                with self.subTest(name=name, value=value):
                    with patch.dict(os.environ, ENV | {name: value}, clear=True):
                        with self.assertRaisesRegex(ValueError, name):
                            bridge.Config.from_env()

    def test_direct_chat_and_custom_url(self):
        settings = ENV | {"SIMPLEX_CHAT_ID": "@2", "TELEGRAM_CHAT_ID": "123",
                          "SIMPLEX_WS_URL": "wss://localhost:5225/bridge"}
        with patch.dict(os.environ, settings, clear=True):
            config = bridge.Config.from_env()
        self.assertEqual(config.simplex_chat, "@2")
        self.assertEqual(config.telegram_chat, 123)
        self.assertEqual(config.simplex_url, settings["SIMPLEX_WS_URL"])


class MessageTests(unittest.TestCase):
    def test_group_batch_preserves_order_and_text(self):
        second = deepcopy(GROUP_ITEM)
        second["chatItem"]["chatDir"]["groupMember"]["localDisplayName"] = "Bob"
        self.assertEqual(list(bridge.event_messages(event(GROUP_ITEM, second), "#7")),
                         ["Alice: hello 👋\nnext line", "Bob: hello 👋\nnext line"])

    def test_contact_routing(self):
        item = deepcopy(GROUP_ITEM)
        item["chatInfo"] = {"type": "direct", "contact": {
            "contactId": 7, "localDisplayName": "Bob"}}
        item["chatItem"]["chatDir"] = {"type": "directRcv"}
        self.assertEqual(bridge.message_text(item, "@7"), "Bob: hello 👋\nnext line")
        self.assertIsNone(bridge.message_text(item, "#7"))
        self.assertIsNone(bridge.message_text(item, "@8"))

    def test_ignores_unrelated_and_private_group_chats(self):
        self.assertIsNone(bridge.message_text(GROUP_ITEM, "#8"))
        item = deepcopy(GROUP_ITEM)
        item["chatInfo"]["groupChatScope"] = {"type": "memberSupport"}
        self.assertIsNone(bridge.message_text(item, "#7"))

    def test_ignores_sent_deleted_and_non_text_items(self):
        for path, value in [
            (("chatDir", "type"), "groupSnd"),
            (("content", "type"), "sndMsgContent"),
            (("content", "type"), "rcvGroupEvent"),
            (("meta", "itemDeleted"), {"type": "deleted"}),
            (("content", "msgContent", "type"), "image"),
            (("content", "msgContent", "type"), "file"),
            (("content", "msgContent", "type"), "voice"),
            (("content", "msgContent", "text"), "  \n"),
        ]:
            with self.subTest(path=path, value=value):
                item = deepcopy(GROUP_ITEM)
                target = item["chatItem"]
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                self.assertIsNone(bridge.message_text(item, "#7"))

    def test_link_text_and_sender_newlines(self):
        item = deepcopy(GROUP_ITEM)
        item["chatItem"]["content"]["msgContent"] = {
            "type": "link", "text": "https://example.com", "preview": {}}
        item["chatItem"]["chatDir"]["groupMember"]["localDisplayName"] = "Alice\nSmith"
        self.assertEqual(bridge.message_text(item, "#7"),
                         "Alice Smith: https://example.com")

    def test_unknown_events_and_malformed_items(self):
        for data in [None, [], {}, {"resp": None}, {"resp": {}},
                     {"resp": {"type": "chatItemUpdated", "chatItems": [GROUP_ITEM]}},
                     event(GROUP_ITEM) | {"corrId": "command"},
                     {"resp": {"type": "newChatItems", "chatItems": None}}]:
            with self.subTest(data=data):
                self.assertEqual(list(bridge.event_messages(data, "#7")), [])
        data = event(None, {}, {"chatInfo": []}, GROUP_ITEM)
        self.assertEqual(list(bridge.event_messages(data, "#7")),
                         ["Alice: hello 👋\nnext line"])

    def test_unicode_chunk_boundaries(self):
        for text in ["", "x" * 4096, "x" * 4097, "x" * 4095 + "👋end",
                     "👋" * 4096, "e\u0301 🇬🇧 漢字\n" * 2000]:
            with self.subTest(length=len(text)):
                parts = list(bridge.telegram_chunks(text))
                self.assertEqual("".join(parts), text)
                self.assertTrue(all(0 < len(part.encode("utf-16-le")) <= 8192
                                    for part in parts))


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.urlopen = self.enterContext(patch("bridge.urlopen"))
        self.sleep = self.enterContext(patch("bridge.time.sleep"))
        self.enterContext(patch.object(bridge.log, "warning"))

    def test_plain_text_request_and_timeout(self):
        response = http_response()
        self.urlopen.return_value = response
        bridge.send_telegram(CONFIG, "<b>hello</b> 👋")
        request = self.urlopen.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(json.loads(request.data), {
            "chat_id": CONFIG.telegram_chat, "text": "<b>hello</b> 👋",
            "link_preview_options": {"is_disabled": True}})
        self.assertEqual(self.urlopen.call_args.kwargs, {"timeout": 30})
        self.assertTrue(response.closed)
        self.sleep.assert_not_called()

    def test_honors_telegram_retry_after(self):
        self.urlopen.side_effect = [
            http_response(429, {"ok": False, "error_code": 429,
                                "parameters": {"retry_after": 73}}),
            http_response(),
        ]
        bridge.send_telegram(CONFIG, "hello")
        self.sleep.assert_called_once_with(73)
        self.assertEqual(self.urlopen.call_count, 2)

    def test_missing_rate_limit_parameters(self):
        self.urlopen.side_effect = [
            http_response(429, {"ok": False, "parameters": None}), http_response()]
        bridge.send_telegram(CONFIG, "hello")
        self.sleep.assert_called_once_with(1)

    def test_retries_transient_errors_with_capped_backoff(self):
        self.urlopen.side_effect = [
            URLError("connection refused"), TimeoutError(), IncompleteRead(b""),
            http_response(502, raw=b"<html>Bad Gateway</html>"),
            *[http_response(503, {"ok": False}) for _ in range(4)],
            http_response(),
        ]
        bridge.send_telegram(CONFIG, "hello")
        self.assertEqual(self.sleep.call_args_list,
                         [call(delay) for delay in [1, 2, 4, 8, 16, 32, 60, 60]])

    def test_permanent_errors_stop_without_exposing_response(self):
        for status in [400, 401, 403, 404]:
            with self.subTest(status=status):
                self.urlopen.side_effect = http_response(status, {
                    "ok": False, "error_code": status,
                    "description": CONFIG.telegram_token,
                })
                with self.assertRaises(RuntimeError) as raised:
                    bridge.send_telegram(CONFIG, "hello")
                self.assertIn(str(status), str(raised.exception))
                self.assertNotIn(CONFIG.telegram_token, str(raised.exception))
        self.sleep.assert_not_called()

    def test_invalid_success_body_is_not_accepted(self):
        for raw in [b"not json", b"[]", b'{}', b'{"ok": false}']:
            with self.subTest(raw=raw):
                self.urlopen.return_value = http_response(raw=raw)
                with self.assertRaises(RuntimeError):
                    bridge.send_telegram(CONFIG, "hello")
        self.sleep.assert_not_called()


@unittest.skipUnless(importlib.util.find_spec("websockets"), "websockets is not installed")
class SimpleXTests(unittest.TestCase):
    def test_reconnect_and_ignore_invalid_json(self):
        connection = MagicMock()
        connection.__enter__.return_value = iter([
            "not json", json.dumps(event(GROUP_ITEM)),
        ])
        with patch("websockets.sync.client.connect",
                   side_effect=[OSError(), connection]) as connect:
            with patch("bridge.time.sleep") as sleep, patch.object(bridge.log, "warning"):
                with closing(bridge.simplex_messages(CONFIG)) as messages:
                    self.assertEqual(next(messages), "Alice: hello 👋\nnext line")
        sleep.assert_called_once_with(1)
        self.assertEqual(connect.call_count, 2)
        connection.__exit__.assert_called_once()

    def test_reconnect_after_normal_close(self):
        connection = MagicMock()
        connection.__enter__.return_value = iter([])
        with patch("websockets.sync.client.connect", return_value=connection) as connect:
            with patch("bridge.time.sleep", side_effect=KeyboardInterrupt):
                with patch.object(bridge.log, "warning"):
                    with self.assertRaises(KeyboardInterrupt):
                        next(bridge.simplex_messages(CONFIG))
        connect.assert_called_once()
        connection.__exit__.assert_called_once()


class MainTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, ENV, clear=True))
        self.enterContext(patch("bridge.signal.signal"))
        self.enterContext(patch.object(bridge.log, "warning"))

    def test_failed_chunk_retries_without_resending_completed_chunk(self):
        with patch("bridge.simplex_messages", return_value=(s for s in ["x" * 5000])):
            with patch("bridge.urlopen", side_effect=[http_response(),
                       http_response(503, {"ok": False}), http_response()]) as send:
                with patch("bridge.time.sleep"):
                    self.assertEqual(bridge.main(), 0)
        texts = [json.loads(args.args[0].data)["text"] for args in send.call_args_list]
        self.assertEqual(texts, ["x" * 4096, "x" * 904, "x" * 904])

    def test_shutdown_closes_source(self):
        closed = []

        def messages():
            try:
                yield "hello"
            finally:
                closed.append(True)

        with patch("bridge.simplex_messages", return_value=messages()):
            with patch("bridge.send_telegram", side_effect=KeyboardInterrupt):
                self.assertEqual(bridge.main(), 0)
        self.assertEqual(closed, [True])

    def test_missing_configuration_fails_before_connecting(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch("bridge.simplex_messages") as source:
                with self.assertLogs(bridge.log, level="ERROR"):
                    self.assertEqual(bridge.main(), 1)
        source.assert_not_called()

    def test_permanent_telegram_error_returns_failure(self):
        with patch("bridge.simplex_messages", return_value=(s for s in ["hello"])):
            with patch("bridge.send_telegram", side_effect=RuntimeError("rejected")):
                with self.assertLogs(bridge.log, level="ERROR"):
                    self.assertEqual(bridge.main(), 1)


if __name__ == "__main__":
    unittest.main()
