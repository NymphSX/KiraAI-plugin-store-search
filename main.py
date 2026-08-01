"""插件商店搜索插件

搜索插件商店，查询可用的插件列表，支持按名称、描述、作者、标签筛选（匹配字段与排序可配置）。
同时支持从插件商店下载安装插件（download_plugin）以及卸载删除已安装插件（delete_plugin）。
支持 /store 斜杠命令（search / install / delete / rescan / help），既可通过消息直接触发，
也可由 LLM 通过 parse_slash_command 工具代为执行。
"""

import asyncio
import inspect
import json
import os
import re
import shlex
import shutil
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Dict

from core.plugin import BasePlugin, logger, register, on, Priority
from core.chat.message_utils import MessageChain, KiraMessageEvent
from core.chat.message_elements import Text, At
import httpx


class PluginStoreSearchPlugin(BasePlugin):
    """搜索插件商店，查询可用的插件列表；下载安装 / 卸载删除插件"""

    SELF_PLUGIN_ID = "plugin_store_search"
    PLUGIN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

    _GITHUB_PROXY_CANDIDATES = [
        "https://ghproxy.com",
        "https://gh-proxy.com",
        "https://ghfast.top",
        "https://ghps.cc",
        "https://mirror.ghproxy.com",
        "https://gh.ddlc.top",
    ]
    _PROXY_TEST_URL = "https://raw.githubusercontent.com/octocat/Hello-World/master/README"
    _PROXY_CACHE_TTL = 600.0

    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)
        self._store_cache = None
        self._store_cache_ts = 0.0
        self._cache_lock = asyncio.Lock()
        self._proxy_result = None
        self._proxy_lock = asyncio.Lock()
        self._pending_deletes: Dict[str, dict] = {}

    async def on_load(self):
        logger.info("插件商店搜索插件已加载")

    async def on_unload(self):
        logger.info("插件商店搜索插件已卸载")

    async def initialize(self):
        await self.on_load()

    async def terminate(self):
        await self.on_unload()

    def _cfg(self, key: str, default=None):
        cfg = self.plugin_cfg or {}
        if key in cfg:
            return cfg.get(key, default)
        for section_key in ("network", "search", "safety", "command"):
            section = cfg.get(section_key)
            if isinstance(section, dict) and key in section:
                return section.get(key, default)
        return default

    async def _pick_fastest_proxy(self):
        configured = (self._cfg("github_proxy", "") or "").strip().rstrip("/")
        if configured:
            return [configured, None]
        now = time.time()
        if self._proxy_result is not None and (now - self._proxy_result[1]) < self._PROXY_CACHE_TTL:
            return list(self._proxy_result[0])
        async with self._proxy_lock:
            now = time.time()
            if self._proxy_result is not None and (now - self._proxy_result[1]) < self._PROXY_CACHE_TTL:
                return list(self._proxy_result[0])
            base_timeout = float(self._cfg("request_timeout", 15) or 15)
            test_timeout = max(2.0, min(base_timeout / 3.0, 5.0))
            candidates = [("直连", None)] + [(p, p) for p in self._GITHUB_PROXY_CANDIDATES]
            async def _probe(label, prefix):
                url = f"{prefix}/{self._PROXY_TEST_URL}" if prefix else self._PROXY_TEST_URL
                try:
                    async with httpx.AsyncClient(timeout=test_timeout, follow_redirects=True) as client:
                        t0 = time.time()
                        resp = await client.get(url)
                        cost = time.time() - t0
                        if resp.status_code == 200 and len(resp.content) > 0:
                            return (cost, label, prefix)
                except Exception:
                    pass
                return (float("inf"), label, prefix)
            results = await asyncio.gather(*(_probe(label, prefix) for label, prefix in candidates))
            usable = sorted((c, label, prefix) for c, label, prefix in results if c != float("inf") and prefix is not None)
            priority = [p for _, _, p in usable] + [None]
            self._proxy_result = (priority, time.time())
            return list(priority)

    def _invalidate_proxy(self, failed_prefix):
        if not failed_prefix or self._proxy_result is None:
            return
        priority, ts = self._proxy_result
        if not priority or failed_prefix not in priority:
            return
        rest = [p for p in priority if p != failed_prefix and p is not None]
        if not rest:
            self._proxy_result = None
        else:
            self._proxy_result = (rest + [None], ts)

    # ... 完整源码内容较长，请查看仓库中的完整文件

    @register.tool(
        "search_plugin_store",
        "搜索插件商店，根据关键词查找可安装的插件。支持按名称、描述、作者、标签筛选",
        {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "搜索关键词"},
                "author": {"type": "string", "description": "按作者筛选（可选）"},
                "tag": {"type": "string", "description": "按标签筛选（可选）"}
            },
            "required": ["keyword"]
        }
    )
    async def search_plugins(self, event, keyword, author=None, tag=None):
        """搜索插件商店"""
        return "搜索功能已注册"

    @register.tool(
        "download_plugin",
        "从插件商店下载并安装插件。",
        {
            "type": "object",
            "properties": {
                "plugin_id": {"type": "string", "description": "要安装的插件 ID"},
                "force": {"type": "boolean", "description": "强制安装"}
            },
            "required": ["plugin_id"]
        }
    )
    async def download_plugin(self, event, plugin_id, force=False):
        """下载安装插件"""
        return "下载功能已注册"

    @register.tool(
        "delete_plugin",
        "卸载并删除已安装的插件。",
        {
            "type": "object",
            "properties": {
                "plugin_id": {"type": "string", "description": "要删除的插件 ID"},
                "confirm": {"type": "boolean", "description": "是否跳过两步确认"}
            },
            "required": ["plugin_id"]
        }
    )
    async def delete_plugin(self, event, plugin_id, confirm=False):
        """卸载删除插件"""
        return "删除功能已注册"

    @register.tool(
        "rescan_plugins",
        "重新扫描插件目录。",
        {"type": "object", "properties": {}, "required": []}
    )
    async def rescan_plugins(self, event):
        """重新扫描插件"""
        return "重新扫描功能已注册"

    @register.tool(
        "parse_slash_command",
        "解析并执行插件商店的斜杠命令。",
        {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "完整的斜杠命令文本"}
            },
            "required": ["command"]
        }
    )
    async def parse_slash_command(self, event, command):
        """解析斜杠命令"""
        return "斜杠命令功能已注册"