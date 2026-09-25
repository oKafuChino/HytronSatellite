import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.bot import Store, flatten_nodes, watch, unwatch, help_command
from tests.test_bot import FIXTURE


class BulkCommandsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.directory.name) / "test.db"))
        self.update = SimpleNamespace(
            effective_user=SimpleNamespace(id=123),
            effective_chat=SimpleNamespace(id=123, type="private"),
            message=SimpleNamespace(reply_text=AsyncMock()),
        )
        self.context = SimpleNamespace(args=["ALL"], application=SimpleNamespace(
            bot_data={"owner_id": 123, "store": self.store}))
        node = flatten_nodes(FIXTURE)["node-1"]
        self.inventory = {node.id: node, "node-2": replace(node, id="node-2", name="Second")}

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    async def test_add_all_preserves_existing_baseline(self):
        old = replace(self.inventory["node-1"], has_capacity=False)
        self.store.add_watch(old)
        self.store.save_snapshot(old)
        with patch("src.bot.fetch_inventory", return_value=self.inventory):
            await watch(self.update, self.context)
            await watch(self.update, self.context)
        self.assertEqual(len(self.store.watchlist()), 2)
        self.assertEqual(self.store.get_snapshot(old.id)["state"], old.status)
        self.assertIsNotNone(self.store.get_snapshot("node-2"))

    async def test_remove_all_cascades_and_is_repeatable(self):
        for node in self.inventory.values():
            self.store.add_watch(node)
            self.store.save_snapshot(node)
        with patch("src.bot.fetch_inventory") as fetch:
            await unwatch(self.update, self.context)
            await unwatch(self.update, self.context)
            fetch.assert_not_called()
        self.assertEqual(self.store.watchlist(), [])
        for node_id in self.inventory:
            self.assertIsNone(self.store.get_snapshot(node_id))

    async def test_unauthorized_commands_are_ignored(self):
        self.update.effective_user.id = 456
        with patch("src.bot.fetch_inventory") as fetch:
            for command in (watch, unwatch, help_command):
                await command(self.update, self.context)
            fetch.assert_not_called()
        self.update.message.reply_text.assert_not_awaited()

    async def test_help_includes_bulk_commands(self):
        await help_command(self.update, self.context)
        text = self.update.message.reply_text.await_args.args[0]
        self.assertIn("/watch all", text)
        self.assertIn("/unwatch all", text)
