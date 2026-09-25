import json
import tempfile
import unittest
from pathlib import Path

from src.bot import Store, flatten_nodes, find_nodes_by_name, normalize_node_name


FIXTURE = [
    {
        "region": "HK",
        "datacenters": [
            {
                "location": "Hong Kong",
                "location_code": "HKG",
                "nodes": [
                    {
                        "id": "node-1",
                        "name": "Test Node",
                        "location": "Hong Kong",
                        "location_code": "HKG",
                        "has_capacity": True,
                        "needs_sync": False,
                        "available": {"cpu": 4, "ram_mb": 1024, "disk_gb": 20, "ipv4": 1, "vm_slots": 2},
                        "templates": [{"os_name": "Ubuntu 24.04 LTS", "is_orderable": True}],
                    }
                ],
            }
        ],
    }
]


class BotTests(unittest.TestCase):
    def test_flatten_nodes(self):
        nodes = flatten_nodes(FIXTURE)
        self.assertEqual(list(nodes), ["node-1"])
        self.assertEqual(nodes["node-1"].status, "有货")
        self.assertEqual(nodes["node-1"].orderable_templates, ("Ubuntu 24.04 LTS",))

    def test_node_name_matching(self):
        nodes = flatten_nodes(FIXTURE)
        self.assertEqual(normalize_node_name("  test   NODE "), "test node")
        self.assertEqual(find_nodes_by_name(nodes, "  TEST NODE ")[0].id, "node-1")

    def test_store_watch_and_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(str(Path(directory) / "test.db"))
            node = flatten_nodes(FIXTURE)["node-1"]
            self.assertTrue(store.add_watch(node))
            self.assertFalse(store.add_watch(node))
            store.save_snapshot(node)
            self.assertEqual(store.get_snapshot(node.id)["state"], "有货")
            self.assertTrue(store.remove_watch(node.id))
            self.assertEqual(store.watchlist(), [])
            store.close()


if __name__ == "__main__":
    unittest.main()
