import ast
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cf_manager


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.paths = cf_manager.Paths()
        self.paths.bin_dir = root / "usr" / "local" / "bin"
        self.paths.systemd_dir = root / "etc" / "systemd" / "system"
        self.paths.tg_bot = root / "root" / "tg_bot.py"
        self.paths.config_file = root / "etc" / "cf_manager" / "lines.json"
        self.paths.runtime_dir = root / "tmp"

    def tearDown(self):
        self.temp.cleanup()

    def config(self):
        return cf_manager.LineConfig(
            line_id=6,
            alias="测试线路",
            region="eu",
            main_ip="203.0.113.10",
            backup_ip="198.51.100.20",
            domains=[cf_manager.DomainRecord("zone", "record", "edge.example.com")],
        )

    def test_generated_scripts_compile(self):
        config = self.config()
        credentials = cf_manager.Credentials("cf", "tg", "group", "private")
        ast.parse(cf_manager.render_failover_script(config, credentials, self.paths))
        ast.parse(cf_manager.render_manual_script(config, credentials))

    def test_tg_injection_keeps_valid_python_and_backup(self):
        self.paths.tg_bot.parent.mkdir(parents=True)
        self.paths.tg_bot.write_text(
            Path("examples/tg_bot.example.py").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        commands = cf_manager.inject_tg_bot(self.paths.tg_bot, self.config(), 6)
        ast.parse(self.paths.tg_bot.read_text(encoding="utf-8"))
        self.assertTrue(self.paths.tg_bot.with_suffix(".py.bak").exists())
        self.assertIn("switch_6", commands[0])
        self.assertIn("([1-6])", self.paths.tg_bot.read_text(encoding="utf-8"))

    def test_config_store_round_trip(self):
        config = self.config()
        store = cf_manager.ConfigStore(self.paths.config_file)
        store.save({config.line_id: config})
        loaded = store.load()
        self.assertEqual(loaded[6].domains[0].record_id, "record")

    def test_remove_tg_line_supports_legacy_unmarked_entries(self):
        source = '''\
SYSTEMD_SERVICES = {
    "容灾集群 6 号": "cf-failover-6",
}
COMMAND_MAPPING = {
    "run_cf6": "/usr/local/bin/cf6.py",
    "cf_status6": "/usr/local/bin/cf_failover_6.py --status",
}
COMMAND_RE = re.compile(r"^(switch|backup)_([1-6])$")
'''
        self.paths.tg_bot.parent.mkdir(parents=True)
        self.paths.tg_bot.write_text(source, encoding="utf-8")
        cf_manager.remove_tg_bot_line(self.paths.tg_bot, 6, 5)
        result = self.paths.tg_bot.read_text(encoding="utf-8")
        ast.parse(result)
        self.assertNotIn('"run_cf6"', result)
        self.assertNotIn('"cf_status6"', result)
        self.assertNotIn('"容灾集群 6 号"', result)
        self.assertIn("([1-5])", result)

    def test_install_rolls_back_files_when_tg_injection_fails(self):
        self.paths.tg_bot.parent.mkdir(parents=True)
        original_bot = Path("examples/tg_bot.example.py").read_text(encoding="utf-8")
        self.paths.tg_bot.write_text(original_bot, encoding="utf-8")
        store = cf_manager.ConfigStore(self.paths.config_file)
        runner = cf_manager.CommandRunner(dry_run=True)
        with mock.patch.object(cf_manager, "inject_tg_bot", side_effect=cf_manager.ManagerError("injected failure")):
            with self.assertRaises(cf_manager.ManagerError):
                cf_manager.install_line(
                    self.config(), cf_manager.Credentials("cf", "tg"), self.paths, runner, store
                )
        self.assertFalse(self.paths.failover_path(6).exists())
        self.assertFalse(self.paths.manual_path(6).exists())
        self.assertFalse(self.paths.service_path(6).exists())
        self.assertFalse(self.paths.config_file.exists())
        self.assertEqual(self.paths.tg_bot.read_text(encoding="utf-8"), original_bot)
        self.assertFalse(self.paths.tg_bot.with_suffix(".py.bak").exists())

    def test_backup_uses_stable_paths_and_unique_names(self):
        self.paths.bin_dir.mkdir(parents=True)
        self.paths.systemd_dir.mkdir(parents=True)
        self.paths.tg_bot.parent.mkdir(parents=True)
        self.paths.config_file.parent.mkdir(parents=True)
        self.paths.failover_path(6).write_text("# generated", encoding="utf-8")
        self.paths.service_path(6).write_text("[Service]", encoding="utf-8")
        self.paths.tg_bot.write_text("# bot", encoding="utf-8")
        self.paths.config_file.write_text(json.dumps({"lines": []}), encoding="utf-8")
        store = cf_manager.ConfigStore(self.paths.config_file)
        first = cf_manager.backup_cluster(self.paths, store)
        second = cf_manager.backup_cluster(self.paths, store)
        self.assertNotEqual(first, second)
        with tarfile.open(first, "r:gz") as archive:
            names = set(archive.getnames())
        self.assertIn("usr/local/bin/cf_failover_6.py", names)
        self.assertIn("etc/systemd/system/cf-failover-6.service", names)
        self.assertIn("root/tg_bot.py", names)
        self.assertIn("etc/cf_manager/lines.json", names)
        self.assertIn("restore_cf_cluster.sh", names)

    def test_cloudflare_zone_listing_paginates(self):
        client = object.__new__(cf_manager.CloudflareClient)
        calls = []

        def request(method, path, query=None):
            calls.append(query["page"])
            page = query["page"]
            return {
                "result": [{"id": str(page), "name": f"zone{page}.example"}],
                "result_info": {"total_pages": 2},
            }

        client._request = request
        zones = client.list_zones()
        self.assertEqual(calls, [1, 2])
        self.assertEqual([zone["id"] for zone in zones], ["1", "2"])

    def test_command_runner_reports_checked_failures(self):
        runner = cf_manager.CommandRunner()
        with mock.patch("cf_manager.subprocess.run", return_value=mock.Mock(returncode=1, stdout="", stderr="failed")):
            with self.assertRaisesRegex(cf_manager.ManagerError, "failed"):
                runner.run(["systemctl", "daemon-reload"], check=True)

    def test_domain_add_and_remove_helpers(self):
        existing = [cf_manager.DomainRecord("z", "r1", "one.example.com")]
        with mock.patch("builtins.input", side_effect=["z2", "r2", "two.example.com"]):
            updated = cf_manager.add_domain_interactively(existing)
        self.assertEqual([item.name for item in updated], ["one.example.com", "two.example.com"])
        with mock.patch("builtins.input", return_value="1"), mock.patch(
            "cf_manager.confirm", return_value=True
        ):
            remaining = cf_manager.remove_domain_interactively(updated)
        self.assertEqual([item.name for item in remaining], ["two.example.com"])

    def test_rewrite_line_updates_ip_and_domains(self):
        config = self.config()
        store = cf_manager.ConfigStore(self.paths.config_file)
        store.save({6: config})
        self.paths.bin_dir.mkdir(parents=True)
        self.paths.systemd_dir.mkdir(parents=True)
        self.paths.tg_bot.parent.mkdir(parents=True)
        self.paths.tg_bot.write_text(
            Path("examples/tg_bot.example.py").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        updated = cf_manager.dataclasses.replace(
            config,
            main_ip="203.0.113.99",
            domains=[
                config.domains[0],
                cf_manager.DomainRecord("zone2", "record2", "api.example.com"),
            ],
        )
        cf_manager.rewrite_line(
            updated,
            cf_manager.Credentials("cf", "tg"),
            self.paths,
            store,
            cf_manager.CommandRunner(dry_run=True),
            6,
        )
        self.assertIn("203.0.113.99", self.paths.failover_path(6).read_text(encoding="utf-8"))
        self.assertIn("api.example.com", self.paths.manual_path(6).read_text(encoding="utf-8"))
        self.assertEqual(store.load()[6].main_ip, "203.0.113.99")


if __name__ == "__main__":
    unittest.main()
