import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import failover_engine
import menu


class V2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config_path = root / "config.json"
        self.cache_path = root / "dns_cache.json"
        self.state_dir = root / "state"
        self.cfg = {
            "global": {
                "cf_api_token": "token",
                "tg_bot_token": "tg",
                "tg_admin_id": 123,
                "tg_group_id": "",
            },
            "lines": {
                "1": {
                    "name": "测试线路",
                    "region": "jp",
                    "main_ip": "203.0.113.10",
                    "backup_ip": "198.51.100.10",
                    "domains": ["edge.example.com"],
                }
            },
        }
        self.config_path.write_text(json.dumps(self.cfg), encoding="utf-8")
        self.old = (failover_engine.CONFIG_PATH, failover_engine.CACHE_PATH, failover_engine.STATE_DIR)
        failover_engine.CONFIG_PATH = self.config_path
        failover_engine.CACHE_PATH = self.cache_path
        failover_engine.STATE_DIR = self.state_dir

    def tearDown(self):
        failover_engine.CONFIG_PATH, failover_engine.CACHE_PATH, failover_engine.STATE_DIR = self.old
        self.temp.cleanup()

    def test_engine_reads_line_and_status(self):
        self.assertEqual(failover_engine.get_line(failover_engine.load_config(), "1")["name"], "测试线路")
        self.assertEqual(failover_engine.get_state("1"), "MAIN")
        failover_engine.set_state("1", "BACKUP")
        self.assertEqual(failover_engine.get_state("1"), "BACKUP")

    def test_dns_update_uses_cached_record_and_puts_dns(self):
        failover_engine.CACHE_PATH.write_text(
            json.dumps({"edge.example.com": {"zone_id": "zone", "rec_id": "record", "name": "edge.example.com"}}),
            encoding="utf-8",
        )
        request = mock.Mock()
        request.read.return_value = b'{"success": true}'
        request.__enter__ = mock.Mock(return_value=request)
        request.__exit__ = mock.Mock(return_value=False)
        with mock.patch("failover_engine.urllib.request.urlopen", return_value=request) as opener:
            count = failover_engine.update_dns(
                self.cfg,
                "1",
                self.cfg["lines"]["1"],
                "198.51.100.10",
                "BACKUP",
            )
        self.assertEqual(count, 1)
        self.assertGreaterEqual(opener.call_count, 1)
        sent = opener.call_args_list[0].args[0]
        self.assertEqual(sent.method, "PUT")
        self.assertIn("record", sent.full_url)

    def test_menu_save_config_is_valid_json(self):
        root = Path(self.temp.name) / "menu"
        menu.CONFIG_PATH = root / "config.json"
        menu.ROOT = root
        menu.save_config(self.cfg)
        self.assertEqual(json.loads(menu.CONFIG_PATH.read_text(encoding="utf-8"))["lines"]["1"]["name"], "测试线路")

    def test_switch_does_not_write_state_on_partial_dns_update(self):
        with mock.patch("failover_engine.update_dns", return_value=0):
            with self.assertRaises(failover_engine.EngineError):
                failover_engine.switch_once(self.cfg, "1", self.cfg["lines"]["1"], "BACKUP")
        self.assertEqual(failover_engine.get_state("1"), "MAIN")


if __name__ == "__main__":
    unittest.main()
