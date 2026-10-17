import json
import tempfile
import unittest
import copy
import urllib.parse
from pathlib import Path
from unittest import mock

import failover_engine
import menu
import tg_bot


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
            json.dumps({"edge.example.com": [{"zone_id": "zone", "rec_id": "record", "name": "edge.example.com"}]}),
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

    def test_domain_resolver_caches_every_a_record(self):
        zone = {"result": [{"id": "zone"}]}
        records = {
            "result": [
                {"id": "one", "type": "A", "name": "edge.example.com"},
                {"id": "two", "type": "A", "name": "edge.example.com"},
            ]
        }
        with mock.patch("failover_engine._request_json", side_effect=[{"result": []}, zone, records]):
            resolved = failover_engine.resolve_domain_ids("token", ["edge.example.com"])
        self.assertEqual({item["rec_id"] for item in resolved}, {"one", "two"})
        cached = json.loads(failover_engine.CACHE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(len(cached["edge.example.com"]), 2)

    def test_dns_cache_save_merges_concurrent_line_entries(self):
        failover_engine.CACHE_PATH.write_text(
            json.dumps({"one.example.com": [{"zone_id": "z1", "rec_id": "r1", "name": "one.example.com"}]}),
            encoding="utf-8",
        )
        failover_engine._save_cache({
            "two.example.com": [{"zone_id": "z2", "rec_id": "r2", "name": "two.example.com"}]
        })
        cached = json.loads(failover_engine.CACHE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(set(cached), {"one.example.com", "two.example.com"})

    def test_force_refresh_removes_cache_when_domain_lookup_fails(self):
        failover_engine.CACHE_PATH.write_text(
            json.dumps({"edge.example.com": [{"zone_id": "stale", "rec_id": "old", "name": "edge.example.com"}]}),
            encoding="utf-8",
        )
        with mock.patch("failover_engine._request_json", return_value={"result": []}):
            resolved = failover_engine.resolve_domain_ids("token", ["edge.example.com"], force_refresh=True)
        self.assertEqual(resolved, [])
        self.assertEqual(json.loads(failover_engine.CACHE_PATH.read_text(encoding="utf-8")), {})

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

    def test_failed_cached_dns_record_is_refreshed_and_retried(self):
        cfg = copy.deepcopy(self.cfg)
        record = {"zone_id": "zone", "rec_id": "old", "name": "edge.example.com"}
        with (
            mock.patch("failover_engine.resolve_domain_ids", side_effect=[[record], [dict(record, rec_id="new")]]) as resolve,
            mock.patch("failover_engine._cloudflare_update", side_effect=[False, True]),
            mock.patch("failover_engine.notify"),
        ):
            count = failover_engine.update_dns(cfg, "1", cfg["lines"]["1"], "198.51.100.10", "BACKUP")
        self.assertEqual(count, 1)
        resolve.assert_any_call("token", ["edge.example.com"], force_refresh=True)

    def test_partial_dns_update_rolls_records_back_to_current_state(self):
        cfg = copy.deepcopy(self.cfg)
        records = [
            {"zone_id": "zone", "rec_id": "one", "name": "edge.example.com"},
            {"zone_id": "zone", "rec_id": "two", "name": "edge.example.com"},
        ]
        with (
            mock.patch("failover_engine.resolve_domain_ids", side_effect=[records, records]),
            mock.patch("failover_engine._cloudflare_update", side_effect=[True, False, True, False, True, True]) as update,
            mock.patch("failover_engine.notify"),
        ):
            count = failover_engine.update_dns(cfg, "1", cfg["lines"]["1"], "198.51.100.10", "BACKUP")
        self.assertEqual(count, 0)
        rollback_calls = update.call_args_list[-2:]
        self.assertEqual({call.args[1] for call in rollback_calls}, {"203.0.113.10"})

    def test_partial_dns_rollback_uses_refreshed_record_ids(self):
        cfg = copy.deepcopy(self.cfg)
        old = {"zone_id": "zone", "rec_id": "old", "name": "edge.example.com"}
        fresh = {"zone_id": "zone", "rec_id": "fresh", "name": "edge.example.com"}
        with (
            mock.patch("failover_engine.resolve_domain_ids", side_effect=[[old], [fresh]]),
            mock.patch("failover_engine._cloudflare_update", side_effect=[False, False, True]) as update,
            mock.patch("failover_engine.notify"),
        ):
            count = failover_engine.update_dns(cfg, "1", cfg["lines"]["1"], "198.51.100.10", "BACKUP")
        self.assertEqual(count, 0)
        self.assertEqual(update.call_args_list[-1].args[0]["rec_id"], "fresh")

    def test_partial_dns_failure_rolls_back_other_successful_domains(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["lines"]["1"]["domains"] = ["one.example.com", "two.example.com"]
        records = [
            {"zone_id": "zone", "rec_id": "one", "name": "one.example.com"},
            {"zone_id": "zone", "rec_id": "two", "name": "two.example.com"},
        ]
        refreshed = [records[1]]
        with (
            mock.patch("failover_engine.resolve_domain_ids", side_effect=[records, refreshed]),
            mock.patch("failover_engine._cloudflare_update", side_effect=[True, False, False, True, True]) as update,
            mock.patch("failover_engine.notify") as notify,
        ):
            count = failover_engine.update_dns(cfg, "1", cfg["lines"]["1"], "198.51.100.10", "BACKUP")
        self.assertEqual(count, 0)
        rollback_calls = update.call_args_list[-2:]
        self.assertEqual({call.args[1] for call in rollback_calls}, {"203.0.113.10"})
        self.assertEqual(notify.call_args.args[4:6], (0, 2))

    def test_check_host_uses_repeated_region_nodes_and_tcp_result_format(self):
        nodes = {
            "jp1.node.check-host.net": {"location": ["jp", "Japan", "Tokyo"]},
            "hk1.node.check-host.net": {"location": ["hk", "Hong Kong", "Hong Kong"]},
            "sg1.node.check-host.net": {"location": ["sg", "Singapore", "Singapore"]},
            "de1.node.check-host.net": {"location": ["de", "Germany", "Nuremberg"]},
        }
        node_names = sorted(host for host, info in nodes.items() if info["location"][0] in {"jp", "hk", "sg"})
        result = {host: [[0, 0.3, "connection refused"]] for host in node_names}
        initial = {"request_id": "request", "nodes": {host: [] for host in node_names}}
        with (
            mock.patch("failover_engine._request_json", side_effect=[{"nodes": nodes}, initial, result]) as request,
            mock.patch("failover_engine.time.sleep"),
        ):
            healthy_external = failover_engine.check_external_latency("203.0.113.10", 22, "jp", 150)
        self.assertFalse(healthy_external)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(request.call_args_list[1].args[0].full_url).query)
        self.assertEqual(set(query["node"]), set(node_names))
        self.assertNotIn("max_nodes", query)

    def test_check_host_normal_tcp_latency_blocks_switch(self):
        node = "jp1.node.check-host.net"
        other_node = "hk1.node.check-host.net"
        response = {node: [{"address": "203.0.113.10", "time": 0.05}], other_node: None}
        with (
            mock.patch(
                "failover_engine._request_json",
                side_effect=[
                    {"nodes": {
                        node: {"location": ["jp", "Japan", "Tokyo"]},
                        other_node: {"location": ["hk", "Hong Kong", "Hong Kong"]},
                    }},
                    {"request_id": "request", "nodes": {node: [], other_node: []}},
                    response,
                ],
            ),
            mock.patch("failover_engine.time.sleep"),
        ):
            healthy_external = failover_engine.check_external_latency("203.0.113.10", 22, "jp", 150)
        self.assertTrue(healthy_external)

    def test_group_notification_text_matches_switch_direction_without_latency(self):
        cfg = copy.deepcopy(self.cfg)
        line = cfg["lines"]["1"]
        with mock.patch("failover_engine.send_telegram") as send:
            failover_engine.notify(cfg, line, "198.51.100.10", "BACKUP", 1, 1, [50.5])
            backup_group = send.call_args_list[0].args[2]
            failover_engine.notify(cfg, line, "203.0.113.10", "MAIN", 1, 1, [50.5])
            main_group = send.call_args_list[2].args[2]
        self.assertIn("已切换备用节点", backup_group)
        self.assertIn("主节点已恢复", main_group)
        self.assertNotIn("ms", backup_group)
        self.assertNotIn("ms", main_group)

    def test_check_host_unknown_request_result_fails_closed(self):
        with mock.patch(
            "failover_engine._request_json",
            side_effect=[
                {"nodes": {
                    "jp1.node.check-host.net": {"location": ["jp", "Japan", "Tokyo"]},
                    "hk1.node.check-host.net": {"location": ["hk", "Hong Kong", "Hong Kong"]},
                }},
                {"nodes": {}},
            ],
        ):
            self.assertTrue(failover_engine.check_external_latency("203.0.113.10", 22, "jp", 150))

    def test_global_menu_can_delete_cloudflare_token_and_stop_lines(self):
        config = copy.deepcopy(self.cfg)
        with (
            mock.patch("builtins.input", side_effect=["2", "DELETE", "0"]),
            mock.patch("menu.ask_secret", side_effect=["token", "tg"]),
            mock.patch("menu.save_config"),
            mock.patch("menu.run") as run,
        ):
            menu.configure_global(config)
        self.assertEqual(config["global"]["cf_api_token"], "")
        run.assert_any_call(["systemctl", "disable", "--now", "cf-failover@1.service"], check=False)

    def test_global_menu_can_delete_telegram_token_and_stop_bot(self):
        config = copy.deepcopy(self.cfg)
        with (
            mock.patch("builtins.input", side_effect=["4", "DELETE", "0"]),
            mock.patch("menu.ask_secret", side_effect=["token", "tg"]),
            mock.patch("menu.save_config"),
            mock.patch("menu.run") as run,
        ):
            menu.configure_global(config)
        self.assertEqual(config["global"]["tg_bot_token"], "")
        run.assert_any_call(["systemctl", "disable", "--now", "tg-bot.service"], check=False)

    def test_placeholder_credentials_can_be_cleared(self):
        config = copy.deepcopy(self.cfg)
        config["global"]["cf_api_token"] = "YOUR_CF_API_TOKEN"
        with (
            mock.patch("builtins.input", side_effect=["2", "0"]),
            mock.patch("menu.save_config"),
            mock.patch("menu.run"),
        ):
            menu.configure_global(config)
        self.assertEqual(config["global"]["cf_api_token"], "")

    def test_deleting_line_ip_disables_monitor_until_ip_is_restored(self):
        config = copy.deepcopy(self.cfg)
        with (
            mock.patch("builtins.input", return_value="DELETE"),
            mock.patch("menu.save_config"),
            mock.patch("menu.run") as run,
        ):
            menu.delete_line_ip(config, "1", "main_ip")
        self.assertEqual(config["lines"]["1"]["main_ip"], "")
        run.assert_any_call(["systemctl", "disable", "--now", "cf-failover@1.service"], check=False)

    def test_line_editor_changes_main_ip_and_restarts_service(self):
        config = copy.deepcopy(self.cfg)
        with (
            mock.patch("builtins.input", side_effect=["1", "2", "203.0.113.20", "0"]),
            mock.patch("menu.save_config"),
            mock.patch("menu.run") as run,
        ):
            menu.edit_line(config)
        self.assertEqual(config["lines"]["1"]["main_ip"], "203.0.113.20")
        run.assert_any_call(["systemctl", "restart", "cf-failover@1.service"], check=True)

    def test_line_editor_can_remove_last_domain_and_stops_service(self):
        config = copy.deepcopy(self.cfg)
        with (
            mock.patch("builtins.input", side_effect=["1", "5", "1", "DELETE", "0"]),
            mock.patch("menu.save_config"),
            mock.patch("menu.run") as run,
        ):
            menu.edit_line(config)
        self.assertEqual(config["lines"]["1"]["domains"], [])
        run.assert_any_call(["systemctl", "disable", "--now", "cf-failover@1.service"], check=False)

    def test_setting_secret_does_not_echo_token_and_restarts_lines(self):
        config = copy.deepcopy(self.cfg)
        with (
            mock.patch("builtins.input", side_effect=["1", "0"]),
            mock.patch("menu.ask_secret", return_value="new-cloudflare-token"),
            mock.patch("menu._verify_global_secret"),
            mock.patch("menu.save_config"),
            mock.patch("menu.run") as run,
        ):
            menu.configure_global(config)
        self.assertEqual(config["global"]["cf_api_token"], "new-cloudflare-token")
        run.assert_any_call(["systemctl", "restart", "cf-failover@1.service"], check=True)

    def test_token_menu_verifies_cloudflare_and_telegram_before_saving(self):
        config = copy.deepcopy(self.cfg)
        responses = [
            {"success": True, "result": {"status": "active"}},
            {"ok": True},
        ]

        def open_request(request, timeout):
            response = mock.MagicMock()
            response.read.return_value = json.dumps(responses.pop(0)).encode()
            response.__enter__.return_value = response
            return response

        with (
            mock.patch("builtins.input", side_effect=["1", "3", "0"]),
            mock.patch("menu.ask_secret", side_effect=["new-cf-token", "new-tg-token"]),
            mock.patch("menu.urllib.request.urlopen", side_effect=open_request) as opener,
            mock.patch("menu.save_config"),
            mock.patch("menu.run"),
        ):
            menu.configure_global(config)
        self.assertEqual(config["global"]["cf_api_token"], "new-cf-token")
        self.assertEqual(config["global"]["tg_bot_token"], "new-tg-token")
        self.assertEqual(opener.call_count, 2)
        self.assertIn("api.cloudflare.com", opener.call_args_list[0].args[0].full_url)
        self.assertIn("/botnew-tg-token/getMe", opener.call_args_list[1].args[0].full_url)

    def test_invalid_cloudflare_token_is_not_saved(self):
        config = copy.deepcopy(self.cfg)
        response = mock.MagicMock()
        response.read.return_value = json.dumps({"success": False, "result": {"status": "invalid"}}).encode()
        response.__enter__.return_value = response
        with (
            mock.patch("menu.urllib.request.urlopen", return_value=response),
            mock.patch("menu.ask_secret", return_value="bad-token"),
            mock.patch("menu.save_config") as save,
        ):
            with self.assertRaises(menu.MenuError):
                menu.set_global_secret(config, "cf_api_token", "Cloudflare API Token")
        self.assertEqual(config["global"]["cf_api_token"], "token")
        save.assert_not_called()

    def test_secret_prompt_never_prints_existing_token(self):
        with mock.patch("getpass.getpass", return_value="") as getpass_prompt:
            self.assertEqual(menu.ask_secret("Token", "secret-value"), "secret-value")
        self.assertNotIn("secret-value", getpass_prompt.call_args.args[0])

    def test_group_telegram_command_with_bot_mention_is_parsed(self):
        self.assertEqual(tg_bot.parse_dynamic_command("/switch_12@my_bot"), ("switch", "12"))
        self.assertIsNone(tg_bot.parse_dynamic_command(""))

    def test_invalid_ipv4_line_config_is_rejected(self):
        config = copy.deepcopy(self.cfg)
        config["lines"]["1"]["main_ip"] = "203.0.113.999"
        with self.assertRaises(failover_engine.EngineError):
            failover_engine.get_line(config, "1")

    def test_invalid_domain_is_not_restartable_from_telegram(self):
        config = copy.deepcopy(self.cfg)
        config["lines"]["1"]["domains"] = ["bad..example.com"]
        self.assertFalse(tg_bot.line_ready(config, config["lines"]["1"]))
        self.assertFalse(tg_bot.line_ready(config, []))

    def test_invalid_region_is_rejected_by_engine(self):
        config = copy.deepcopy(self.cfg)
        config["lines"]["1"]["region"] = "unknown"
        with self.assertRaises(failover_engine.EngineError):
            failover_engine.get_line(config, "1")

    def test_restart_all_keeps_incomplete_ip_lines_stopped(self):
        config = copy.deepcopy(self.cfg)
        config["lines"]["1"]["backup_ip"] = ""
        with mock.patch("menu.run") as run:
            menu._restart_all_services(config)
        run.assert_any_call(["systemctl", "disable", "--now", "cf-failover@1.service"], check=False)
        self.assertNotIn(mock.call(["systemctl", "restart", "cf-failover@1.service"], check=True), run.call_args_list)


if __name__ == "__main__":
    unittest.main()
