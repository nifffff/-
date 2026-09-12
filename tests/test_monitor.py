import base64
import io
import json
import tempfile
import unittest
from pathlib import Path

from monitor import (
    Account,
    CocofaHttpClient,
    assert_same_origin,
    atomic_write_json,
    diff_snapshots,
    load_config,
    make_accounts,
    captcha_to_png,
)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP error")


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return FakeResponse(self.payload)


class MonitorTests(unittest.TestCase):
    def test_account_normalization(self):
        account = Account.from_api(
            {
                "remark": " 账号 A ",
                "nickname": " 测试 用户 ",
                "status": 1,
                "expireTime": "2026/09/28 15:12",
                "coinBalance": "53,138",
                "cashBalance": " 10.50元 ",
            }
        )
        self.assertEqual(account.key, "账号 A")
        self.assertEqual(account.nickname, "测试 用户")
        self.assertEqual(account.coins, "53138")
        self.assertEqual(account.balance, "10.50")

    def test_all_change_types(self):
        previous = {
            "kept": {
                "remark": "保留",
                "nickname": "K",
                "status": "运行中",
                "expires_at": "2026/09/28 15:12",
                "coins": "100",
                "balance": "1",
            },
            "removed": {"remark": "删除", "status": "已停止", "expires_at": "", "coins": "0", "balance": "0"},
        }
        current = {
            "kept": {
                "remark": "保留",
                "nickname": "K",
                "status": "已停止",
                "expires_at": "2026/10/01 00:00",
                "coins": "120",
                "balance": "2",
            },
            "added": {"remark": "新增", "status": "运行中", "expires_at": "", "coins": "5", "balance": "0"},
        }
        changes = diff_snapshots(previous, current)
        self.assertEqual(
            [change["type"] for change in changes],
            ["account_added", "account_removed", "field_changed", "field_changed", "field_changed", "field_changed"],
        )
        self.assertEqual(
            [change.get("field") for change in changes if change["type"] == "field_changed"],
            ["status", "expires_at", "coins", "balance"],
        )

    def test_duplicate_keys_are_disambiguated(self):
        accounts = make_accounts([{"remark": "same"}, {"remark": "same"}])
        self.assertEqual(set(accounts), {"same", "same#2"})

    def test_atomic_snapshot_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "snapshot.json"
            atomic_write_json(path, {"accounts": {"a": {"status": "运行中"}}})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["accounts"]["a"]["status"], "运行中")

    def test_cross_origin_endpoint_is_rejected(self):
        assert_same_origin("http://example.test:15000/", "http://example.test:15000/api/auth/accounts")
        with self.assertRaises(ValueError):
            assert_same_origin("http://example.test:15000/", "https://evil.test/api/auth/login")

    def test_accounts_poll_is_get_only(self):
        client = object.__new__(CocofaHttpClient)
        client.config = {"accounts_endpoint": "/api/auth/accounts"}
        client.base_url = "http://example.test:15000/"
        client.timeout = 15
        client.session = FakeSession(
            {"success": True, "accounts": [{"id": 7, "remark": "A", "status": 1, "coinBalance": 9, "cashBalance": 2}]}
        )
        accounts = client.fetch_accounts()
        self.assertEqual(list(accounts), ["7"])
        self.assertEqual(client.session.calls[0][0], "GET")
        self.assertEqual(client.session.calls[0][1], "http://example.test:15000/api/auth/accounts")

    def test_example_config_uses_five_second_interval(self):
        config = load_config(Path(__file__).resolve().parents[1] / "config.example.json")
        self.assertEqual(config["poll_interval_seconds"], 5)

    def test_svg_captcha_renders_without_system_cairo(self):
        from PIL import Image

        svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 40"><rect width="120" height="40" fill="#f0f0f0"/><path fill="#cc0000" d="M10 5 L30 5 L30 35 L10 35 Z"/><path stroke="#00aa00" fill="none" d="M0 20 Q60 0 120 20"/></svg>'
        data_uri = "data:image/svg+xml;base64," + base64.b64encode(svg).decode("ascii")
        png = captcha_to_png(data_uri)
        image = Image.open(io.BytesIO(png))
        self.assertEqual(image.format, "PNG")
        self.assertEqual(image.size, (240, 80))


if __name__ == "__main__":
    unittest.main()
