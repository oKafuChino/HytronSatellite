"""Private HyVPS inventory Telegram bot."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import signal
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes

API_URL = "https://hyvps.hytron.io/api/v1/nodes/catalog/grouped"
MAX_MESSAGE_LENGTH = 3900
logger = logging.getLogger("hytron_bot")


@dataclass(frozen=True)
class Node:
    id: str
    name: str
    location: str
    location_code: str
    has_capacity: bool
    needs_sync: bool
    available: dict[str, Any]
    orderable_templates: tuple[str, ...]

    @property
    def status(self) -> str:
        if self.needs_sync:
            return "待同步"
        return "有货" if self.has_capacity else "无货"


class InventoryError(RuntimeError):
    pass


def flatten_nodes(payload: Any) -> dict[str, Node]:
    if not isinstance(payload, list):
        raise InventoryError("inventory response is not a list")
    result: dict[str, Node] = {}
    for region in payload:
        if not isinstance(region, dict):
            continue
        for datacenter in region.get("datacenters", []):
            if not isinstance(datacenter, dict):
                continue
            for raw in datacenter.get("nodes", []):
                if not isinstance(raw, dict) or not raw.get("id"):
                    continue
                available = raw.get("available") or {}
                templates = raw.get("templates") or []
                orderable = tuple(
                    str(item.get("os_name", ""))
                    for item in templates
                    if isinstance(item, dict) and item.get("is_orderable")
                )
                result[str(raw["id"])] = Node(
                    id=str(raw["id"]),
                    name=str(raw.get("name", raw["id"])),
                    location=str(raw.get("location", "")),
                    location_code=str(raw.get("location_code", "")),
                    has_capacity=bool(raw.get("has_capacity", False)),
                    needs_sync=bool(raw.get("needs_sync", False)),
                    available={str(k): v for k, v in available.items()},
                    orderable_templates=orderable,
                )
    if not result:
        raise InventoryError("inventory response contains no nodes")
    return result


def fetch_inventory(url: str = API_URL, timeout: int = 20) -> dict[str, Node]:
    request = Request(url, headers={"User-Agent": "HytronSatellite/1.0"})
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except HTTPError as exc:
        raise InventoryError(f"HTTP {exc.code}: {exc.reason}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise InventoryError(f"{type(exc).__name__}: {exc}") from exc
    return flatten_nodes(payload)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS watchlist (
                node_id TEXT PRIMARY KEY,
                node_name TEXT NOT NULL,
                added_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS snapshots (
                node_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                checked_at TEXT NOT NULL,
                FOREIGN KEY(node_id) REFERENCES watchlist(node_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(snapshots)")}
        if "payload_json" not in columns:
            self.conn.execute("ALTER TABLE snapshots ADD COLUMN payload_json TEXT NOT NULL DEFAULT '{}'")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def watchlist(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM watchlist ORDER BY node_name"))

    def get_watch(self, node_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM watchlist WHERE node_id = ?", (node_id,)).fetchone()

    def add_watch(self, node: Node) -> bool:
        cursor = self.conn.execute(
            "INSERT OR IGNORE INTO watchlist(node_id, node_name, added_at) VALUES (?, ?, ?)",
            (node.id, node.name, utc_now()),
        )
        self.conn.commit()
        return cursor.rowcount == 1

    def remove_watch(self, node_id: str) -> bool:
        cursor = self.conn.execute("DELETE FROM watchlist WHERE node_id = ?", (node_id,))
        self.conn.commit()
        return cursor.rowcount == 1

    def remove_all_watches(self) -> int:
        cursor = self.conn.execute("DELETE FROM watchlist")
        self.conn.commit()
        return cursor.rowcount

    def get_snapshot(self, node_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM snapshots WHERE node_id = ?", (node_id,)).fetchone()

    def save_snapshot(self, node: Node) -> None:
        payload = json.dumps(node_payload(node), ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        self.conn.execute(
            """INSERT INTO snapshots(node_id, state, payload_hash, payload_json, checked_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(node_id) DO UPDATE SET state=excluded.state,
               payload_hash=excluded.payload_hash, payload_json=excluded.payload_json,
               checked_at=excluded.checked_at""",
            (node.id, node.status, digest, payload, utc_now()),
        )
        self.conn.commit()

    def set_value(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    def get_value(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default


RESOURCE_LABELS = {
    "cpu": "CPU",
    "ram_mb": "内存(MB)",
    "disk_gb": "磁盘(GB)",
    "ips": "IP总量",
    "ipv4": "IPv4",
    "ipv6": "IPv6",
    "vm_slots": "VM名额",
}


def node_payload(node: Node) -> dict[str, Any]:
    return {"state": node.status, "available": node.available}


def snapshot_payload(snapshot: sqlite3.Row) -> dict[str, Any]:
    raw = snapshot["payload_json"] if "payload_json" in snapshot.keys() else "{}"
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def format_resource_changes(previous: sqlite3.Row, current: Node) -> str:
    old_payload = snapshot_payload(previous)
    old_available = old_payload.get("available", {})
    changes: list[str] = []
    for key, label in RESOURCE_LABELS.items():
        old_value = old_available.get(key, "未知")
        new_value = current.available.get(key, "未知")
        if old_value == new_value:
            continue
        delta = ""
        if isinstance(old_value, (int, float)) and isinstance(new_value, (int, float)):
            difference = new_value - old_value
            delta = f" ({difference:+g})"
        changes.append(f"{label}: {old_value} → {new_value}{delta}")
    old_state = old_payload.get("state", previous["state"])
    if old_state != current.status:
        changes.append(f"状态: {old_state} → {current.status}")
    return "\n".join(changes) or "资源配置发生变化，但没有可展示的字段差异。"


def normalize_node_name(name: str) -> str:
    return " ".join(name.split()).casefold()


def find_nodes_by_name(inventory: dict[str, Node], name: str) -> list[Node]:
    target = normalize_node_name(name)
    return [node for node in inventory.values() if normalize_node_name(node.name) == target]


def split_message(lines: list[str], limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    messages: list[str] = []
    current: list[str] = []
    current_length = 0
    for line in lines:
        line_length = len(line) + (1 if current else 0)
        if current and current_length + line_length > limit:
            messages.append("\n".join(current))
            current = []
            current_length = 0
        current.append(line)
        current_length += len(line) + (1 if len(current) > 1 else 0)
    if current:
        messages.append("\n".join(current))
    return messages


def format_node(node: Node) -> str:
    available = node.available
    return (
        f"<b>{escape(node.name)}</b>\n"
        f"位置: {escape(node.location)} ({escape(node.location_code)})\n"
        f"状态: <b>{node.status}</b>\n"
        f"余量: CPU {available.get('cpu', '?')} | RAM {available.get('ram_mb', '?')} MB | "
        f"磁盘 {available.get('disk_gb', '?')} GB | IPv4 {available.get('ipv4', '?')} | "
        f"VM {available.get('vm_slots', '?')}"
    )


def is_owner(update: Update, owner_id: int) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    return bool(user and chat and user.id == owner_id and chat.type == ChatType.PRIVATE)


async def reject_unless_owner(update: Update, owner_id: int) -> bool:
    if is_owner(update, owner_id):
        return True
    logger.warning("ignored unauthorized update user=%s chat=%s", update.effective_user and update.effective_user.id, update.effective_chat and update.effective_chat.id)
    return False


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await reject_unless_owner(update, context.application.bot_data["owner_id"]):
        return
    await update.message.reply_text(
        "HyVPS 库存 Bot 使用说明\n\n"
        "/nodes 查看所有节点\n"
        "/get <节点名称> 查询指定节点当前库存\n"
        "/watch <节点名称> 加入单个节点白名单\n"
        "/watch all 监控所有节点\n"
        "/unwatch <节点名称> 移除单个节点\n"
        "/unwatch all 移除所有节点\n"
        "/watchlist 查看白名单\n"
        "/status 查看运行状态\n"
        "/pause 暂停通知\n"
        "/resume 恢复通知"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await help_command(update, context)


async def nodes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await reject_unless_owner(update, context.application.bot_data["owner_id"]):
        return
    try:
        inventory = await asyncio.to_thread(fetch_inventory, os.getenv("INVENTORY_API_URL", API_URL))
    except InventoryError as exc:
        await update.message.reply_text(f"库存接口请求失败：{escape(str(exc))}")
        return
    lines = [f"节点数：{len(inventory)}", "使用 /watch &lt;节点名称&gt; 添加监控："]
    for node in sorted(inventory.values(), key=lambda item: (item.location_code, item.name)):
        lines.append(f"{node.status} | {escape(node.location_code)} | {escape(node.name)}")
    for message in split_message(lines):
        await update.message.reply_text(message, parse_mode="HTML")


async def get_node(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await reject_unless_owner(update, context.application.bot_data["owner_id"]):
        return
    node_name = " ".join(context.args).strip()
    if not node_name:
        await update.message.reply_text("用法：/get <节点名称>，节点名称可从 /nodes 获取。")
        return
    try:
        inventory = await asyncio.to_thread(fetch_inventory, os.getenv("INVENTORY_API_URL", API_URL))
    except InventoryError as exc:
        await update.message.reply_text(f"库存接口请求失败：{escape(str(exc))}")
        return
    matches = find_nodes_by_name(inventory, node_name)
    if not matches:
        await update.message.reply_text("找不到这个节点，请确认名称与 /nodes 显示的名称一致。")
        return
    if len(matches) > 1:
        names = "\n".join(f"- {escape(node.name)} ({escape(node.location_code)})" for node in matches)
        await update.message.reply_text(f"节点名称不唯一，请提供更准确的名称：\n{names}", parse_mode="HTML")
        return
    await update.message.reply_text(format_node(matches[0]), parse_mode="HTML")


async def watch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await reject_unless_owner(update, context.application.bot_data["owner_id"]):
        return
    node_name = " ".join(context.args).strip()
    if not node_name:
        await update.message.reply_text("用法：/watch <节点名称>，也可以使用 /watch all 监控全部节点。")
        return
    try:
        inventory = await asyncio.to_thread(fetch_inventory, os.getenv("INVENTORY_API_URL", API_URL))
    except InventoryError as exc:
        await update.message.reply_text(f"库存接口请求失败：{escape(str(exc))}")
        return
    store: Store = context.application.bot_data["store"]
    if normalize_node_name(node_name) == "all":
        for node in inventory.values():
            if store.add_watch(node):
                store.save_snapshot(node)
        await update.message.reply_text(f"已将全部 {len(inventory)} 个节点加入白名单，并建立初始基线。")
        return
    matches = find_nodes_by_name(inventory, node_name)
    if not matches:
        await update.message.reply_text("找不到这个节点，请确认名称与 /nodes 显示的名称一致。")
        return
    if len(matches) > 1:
        names = "\n".join(f"- {escape(node.name)} ({escape(node.location_code)})" for node in matches)
        await update.message.reply_text(f"节点名称不唯一，请提供更准确的名称：\n{names}", parse_mode="HTML")
        return
    node = matches[0]
    added = store.add_watch(node)
    store.save_snapshot(node)
    await update.message.reply_text(
        ("已加入白名单并建立初始基线。" if added else "该节点已经在白名单中。") + "\n\n" + format_node(node),
        parse_mode="HTML",
    )


async def unwatch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await reject_unless_owner(update, context.application.bot_data["owner_id"]):
        return
    node_name = " ".join(context.args).strip()
    if not node_name:
        await update.message.reply_text("用法：/unwatch <节点名称>，也可以使用 /unwatch all 移除全部节点。")
        return
    store: Store = context.application.bot_data["store"]
    if normalize_node_name(node_name) == "all":
        removed = store.remove_all_watches()
        await update.message.reply_text(f"已移除全部节点，共 {removed} 个。")
        return
    matches = [row for row in store.watchlist() if normalize_node_name(row["node_name"]) == normalize_node_name(node_name)]
    if len(matches) != 1:
        await update.message.reply_text("该节点不在白名单中。" if not matches else "节点名称不唯一，请使用完整节点名称。")
        return
    await update.message.reply_text("已移除。" if store.remove_watch(matches[0]["node_id"]) else "该节点不在白名单中。")


async def watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await reject_unless_owner(update, context.application.bot_data["owner_id"]):
        return
    store: Store = context.application.bot_data["store"]
    rows = store.watchlist()
    if not rows:
        await update.message.reply_text("白名单为空，当前不会主动推送库存变化。")
        return
    for message in split_message([escape(row["node_name"]) for row in rows]):
        await update.message.reply_text(message, parse_mode="HTML")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await reject_unless_owner(update, context.application.bot_data["owner_id"]):
        return
    store: Store = context.application.bot_data["store"]
    paused = store.get_value("paused", "0") == "1"
    last_check = store.get_value("last_check", "从未")
    await update.message.reply_text(f"监控状态：{'暂停' if paused else '运行中'}\n白名单节点：{len(store.watchlist())}\n最近检查：{last_check}")


async def set_paused(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await reject_unless_owner(update, context.application.bot_data["owner_id"]):
        return
    store: Store = context.application.bot_data["store"]
    paused = update.message.text.split("@", 1)[0] == "/pause"
    store.set_value("paused", "1" if paused else "0")
    if not paused:
        store.set_value("rebaseline", "1")
    await update.message.reply_text("已暂停库存通知。" if paused else "已恢复库存通知；下一次检查将重新建立比较基线。")


async def monitor_loop(application: Application) -> None:
    store: Store = application.bot_data["store"]
    owner_id: int = application.bot_data["owner_id"]
    interval = application.bot_data["poll_interval"]
    while True:
        try:
            if store.watchlist() and store.get_value("paused", "0") != "1":
                inventory = await asyncio.to_thread(fetch_inventory, os.getenv("INVENTORY_API_URL", API_URL))
                for row in store.watchlist():
                    node = inventory.get(row["node_id"])
                    if not node:
                        continue
                    previous = store.get_snapshot(node.id)
                    payload = json.dumps(node_payload(node), ensure_ascii=False, sort_keys=True)
                    digest = hashlib.sha256(payload.encode()).hexdigest()
                    changed = previous and previous["payload_hash"] != digest
                    store.save_snapshot(node)
                    if changed and store.get_value("rebaseline", "0") != "1":
                        await application.bot.send_message(
                            chat_id=owner_id,
                            text=(
                                f"库存配置变化通知\n\n{format_node(node)}\n\n"
                                f"资源变动：\n{format_resource_changes(previous, node)}"
                            ),
                            parse_mode="HTML",
                        )
                store.set_value("last_check", utc_now())
                if store.get_value("rebaseline", "0") == "1":
                    store.set_value("rebaseline", "0")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("monitor loop failed")
        await asyncio.sleep(interval)


async def post_init(application: Application) -> None:
    application.bot_data["monitor_task"] = asyncio.create_task(monitor_loop(application))


async def post_shutdown(application: Application) -> None:
    task = application.bot_data.get("monitor_task")
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    application.bot_data["store"].close()


def build_application() -> Application:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    owner_id = int(os.environ["OWNER_TELEGRAM_ID"])
    db_path = os.getenv("DATABASE_PATH", "/data/hytron.db")
    poll_interval = max(15, int(os.getenv("POLL_INTERVAL_SECONDS", "60")))
    store = Store(db_path)
    application = (
        ApplicationBuilder()
        .token(token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.bot_data.update(owner_id=owner_id, store=store, poll_interval=poll_interval)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("nodes", nodes))
    application.add_handler(CommandHandler("get", get_node))
    application.add_handler(CommandHandler("watch", watch))
    application.add_handler(CommandHandler("unwatch", unwatch))
    application.add_handler(CommandHandler("watchlist", watchlist))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler(["pause", "resume"], set_paused))
    return application


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    application = build_application()
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
