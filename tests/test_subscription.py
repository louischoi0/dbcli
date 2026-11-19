import tempfile
import unittest
from pathlib import Path

from dbcli import subscription as sub
from dbcli.config import load

BASE = '''# my config
[ui]
panel = "history"

[connections.local]
type = "sqlite"
path = "a.db"
'''


class SubscriptionTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "config.toml"

    def tearDown(self):
        self.dir.cleanup()

    def save(self, text):
        sub._write(self.path, text)
        return load(self.path)

    def test_adds_an_ai_section_at_the_end(self):
        text = sub.set_value(BASE, "api_key", "sk-ant-abc")
        self.assertTrue(text.startswith(BASE))
        self.assertTrue(text.endswith('\n[ai]\napi_key = "sk-ant-abc"\n'))
        cfg = self.save(text)
        self.assertEqual(cfg.ai.api_key, "sk-ant-abc")
        self.assertEqual([p.name for p in cfg.profiles], ["local"])  # connections untouched
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), "0o600")

    def test_replaces_in_place_and_keeps_comments(self):
        start = BASE.replace("[connections.local]", '[ai]\nmodel = "claude-opus-5-5"   # mine\napi_key = "old"\n\n# local db\n[connections.local]')
        text = sub.set_value(start, "api_key", "sk-ant-new")
        self.assertIn('model = "claude-opus-5-5"   # mine\napi_key = "sk-ant-new"\n\n# local db\n', text)
        text = sub.set_value(text, "effort", "high")  # a new setting goes after the last one, not under the comment
        self.assertIn('api_key = "sk-ant-new"\neffort = "high"\n\n# local db\n', text)
        cfg = self.save(text)
        self.assertEqual((cfg.ai.api_key, cfg.ai.effort), ("sk-ant-new", "high"))

    def test_unset(self):
        text = sub.set_value(BASE, "api_key", "sk-ant-abc")
        text, removed = sub.unset_value(text, "api_key")
        self.assertTrue(removed)
        self.assertIsNone(self.save(text).ai.api_key)
        self.assertEqual(sub.unset_value(text, "api_key"), (text, False))

    def test_new_file(self):
        self.assertIn("[ai]\napi_key", sub.set_value("", "api_key", "k"))

    def test_mask_hides_the_key(self):
        self.assertEqual(sub.mask("sk-ant-api03-abcdefghijklmnop"), "sk-ant-…mnop")
        self.assertEqual(sub.mask("short"), "………")


    def test_subscriptions_add_activate_delete(self):
        self.path.write_text(BASE)
        sub.add(self.path, "work", "sk-ant-work-0000000001", activate=True)
        sub.add(self.path, "home", "sk-ant-home-0000000002", model="claude-sonnet-5-5")
        self.assertEqual(sub.names(self.path), ["work", "home"])
        ai = load(self.path).ai
        self.assertEqual((ai.subscription, ai.effective().api_key, ai.effective().model),
                         ("work", "sk-ant-work-0000000001", "claude-opus-5-5"))
        sub.activate(self.path, "home")
        eff = load(self.path).ai.effective()
        self.assertEqual((eff.api_key, eff.model), ("sk-ant-home-0000000002", "claude-sonnet-5-5"))
        self.assertTrue(sub.delete(self.path, "home"))  # it was the active one
        ai = load(self.path).ai
        self.assertIsNone(ai.subscription)
        self.assertEqual(list(ai.subscriptions), ["work"])
        self.assertEqual([p.name for p in load(self.path).profiles], ["local"])

    def test_no_active_subscription_means_no_key(self):
        self.path.write_text(BASE)
        sub.add(self.path, "work", "sk-ant-work-0000000001")
        ai = load(self.path).ai
        self.assertIsNone(ai.active)
        self.assertIsNone(ai.effective().api_key)  # listed, but none picked yet

    def test_activate_unknown_and_duplicates(self):
        self.path.write_text(BASE)
        sub.add(self.path, "work", "k1")
        with self.assertRaises(sub.ConnectionError_):
            sub.activate(self.path, "nope")
        with self.assertRaises(sub.ConnectionError_):
            sub.add(self.path, "work", "k2")


if __name__ == "__main__":
    unittest.main()
