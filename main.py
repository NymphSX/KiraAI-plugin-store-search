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

            candidates = [("\u76f4\u8fde", None)] + [(p, p) for p in self._GITHUB_PROXY_CANDIDATES]

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
            usable = sorted(
                (c, label, prefix) for c, label, prefix in results
                if c != float("inf") and prefix is not None
            )
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

    @staticmethod
    def _extract_text(event) -> str:
        def _iter_text_parts(chain):
            for ele in chain or []:
                if isinstance(ele, At):
                    continue
                if isinstance(ele, Text):
                    yield ele.text or ""

        msg = getattr(event, "message", None)
        chain = getattr(msg, "chain", None) if msg is not None else None
        if chain is not None:
            text = " ".join(_iter_text_parts(chain)).strip()
        else:
            parts = []
            for m in getattr(event, "messages", []) or []:
                parts.extend(_iter_text_parts(getattr(m, "chain", None)))
            text = " ".join(parts).strip()
        return re.sub(r"^(?:@\S+\s*)+", "", text).strip()

    @staticmethod
    def _get_sid(event) -> str:
        session = getattr(event, "session", None)
        if session is not None:
            sid = getattr(session, "sid", None)
            if sid:
                return sid
        msg = getattr(event, "message", None)
        if msg is not None:
            sender = getattr(msg, "sender", None)
            adapter = getattr(event, "adapter", None)
            adapter_name = getattr(adapter, "name", "unknown") if adapter else "unknown"
            if sender is not None:
                group = getattr(msg, "group", None)
                if group is not None and getattr(group, "group_id", None):
                    return f"{adapter_name}:gm:{group.group_id}"
                if getattr(sender, "user_id", None):
                    return f"{adapter_name}:dm:{sender.user_id}"
        return ""

    @staticmethod
    def _get_user_id(event) -> str:
        msg = getattr(event, "message", None)
        if msg is not None:
            sender = getattr(msg, "sender", None)
            if sender is not None and getattr(sender, "user_id", None):
                return str(sender.user_id)
        for m in getattr(event, "messages", []) or []:
            sender = getattr(m, "sender", None)
            if sender is not None and getattr(sender, "user_id", None):
                return str(sender.user_id)
        return ""

    @staticmethod
    def _is_group_message(event) -> bool:
        is_group = getattr(event, "is_group_message", None)
        if callable(is_group):
            try:
                return bool(is_group())
            except Exception:
                pass
        msg = getattr(event, "message", None)
        if msg is not None and getattr(msg, "group", None) is not None:
            return True
        messages = getattr(event, "messages", None) or []
        if messages and getattr(messages[-1], "group", None) is not None:
            return True
        return False

    @staticmethod
    def _bot_self_id(event) -> str:
        msg = getattr(event, "message", None)
        if msg is not None:
            sid = getattr(msg, "self_id", None)
            if sid:
                return str(sid)
        for m in getattr(event, "messages", []) or []:
            sid = getattr(m, "self_id", None)
            if sid:
                return str(sid)
        return ""

    @staticmethod
    def _is_bot_at(event, bot_id: str) -> bool:
        if not bot_id:
            return False
        bot_id = str(bot_id)
        msg = getattr(event, "message", None)
        chain = getattr(msg, "chain", None) if msg is not None else None
        if chain is not None:
            return any(
                getattr(ele, "pid", None) == bot_id
                for ele in chain if isinstance(ele, At)
            )
        for m in getattr(event, "messages", []) or []:
            for ele in getattr(m, "chain", None) or []:
                if isinstance(ele, At) and getattr(ele, "pid", None) == bot_id:
                    return True
        return False

    def _command_prefix(self) -> str:
        return (self._cfg("command_prefix", "/store") or "/store").strip()

    async def _check_slash_allowed(self, event) -> tuple:
        whitelist = self._cfg("slash_whitelist") or []
        if not whitelist:
            return True, ""
        allowed = {str(x).strip() for x in whitelist if str(x).strip()}
        uid = self._get_user_id(event)
        if not uid:
            return False, "\u274c \u65e0\u6cd5\u8bc6\u522b\u53d1\u9001\u8005 QQ \u53f7\uff0c\u659c\u6760\u547d\u4ee4\u5df2\u62d2\u7edd\u6267\u884c\u3002"
        if uid in allowed:
            return True, ""
        return False, f"\u60a8\u4e0d\u5728\u767d\u540d\u5355\u5185\uff0c\u65e0\u6743\u4f7f\u7528 {self._command_prefix()} \u659c\u6760\u547d\u4ee4\uff08\u4f60\u7684 QQ\uff1a{uid}\uff09\u3002"

    async def _reply(self, event, content: str, at_uid: str = ""):
        sid = self._get_sid(event)
        if not sid:
            logger.warning("\u65e0\u6cd5\u786e\u5b9a\u4f1a\u8bdd ID\uff0c\u659c\u6760\u547d\u4ee4\u7ed3\u679c\u672a\u53d1\u9001")
            return
        chain = [Text(content)]
        if at_uid and self._is_group_message(event):
            chain = [At(at_uid), Text(content)]
        try:
            await self.ctx.message_processor.send_message_chain(
                session=sid, chain=MessageChain(chain)
            )
        except Exception as e:
            logger.error(f"\u53d1\u9001\u659c\u6760\u547d\u4ee4\u56de\u590d\u5931\u8d25: {e}")
            if len(chain) > 1:
                try:
                    await self.ctx.message_processor.send_message_chain(
                        session=sid, chain=MessageChain([Text(content)])
                    )
                except Exception as e2:
                    logger.error(f"\u53d1\u9001\u659c\u6760\u547d\u4ee4\u56de\u590d(\u7eaf\u6587\u672c)\u5931\u8d25: {e2}")

    async def _fetch_store_plugins_uncached(self):
        store_url = self._cfg("store_url", "https://plugins.kira-ai.top/api/plugins/all")
        timeout = float(self._cfg("request_timeout", 15) or 15)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(store_url)
            resp.raise_for_status()
            plugins = resp.json()

        if isinstance(plugins, dict):
            for key in ("plugins", "data", "items", "results", "list"):
                if key not in plugins:
                    continue
                val = plugins[key]
                if isinstance(val, list):
                    plugins = val
                    break
                if isinstance(val, dict):
                    vals = list(val.values())
                    if vals and all(isinstance(x, dict) for x in vals):
                        plugins = vals
                        break
            else:
                for v in plugins.values():
                    if isinstance(v, list):
                        plugins = v
                        break
                    if isinstance(v, dict) and v and all(
                        isinstance(x, dict) for x in v.values()
                    ):
                        plugins = list(v.values())
                        break

        return plugins if isinstance(plugins, list) else []

    async def _fetch_store_plugins(self, force: bool = False):
        cache_ttl = int(self._cfg("cache_ttl", 300) or 0)
        now = time.time()
        if not force and cache_ttl > 0 and self._store_cache is not None \
                and (now - self._store_cache_ts) < cache_ttl:
            return self._store_cache

        async with self._cache_lock:
            now = time.time()
            if not force and cache_ttl > 0 and self._store_cache is not None \
                    and (now - self._store_cache_ts) < cache_ttl:
                return self._store_cache
            plugins = await self._fetch_store_plugins_uncached()
            if cache_ttl > 0:
                self._store_cache = plugins
                self._store_cache_ts = now
            return plugins

    @staticmethod
    def _parse_github_repo(repo):
        if not repo:
            return None
        repo = str(repo).strip()
        repo = re.sub(r"\.git$", "", repo)
        m = re.search(r"github\.com[/:]([^/]+)/([^/\s]+)", repo)
        if m:
            return m.group(1), m.group(2)
        parts = [p for p in repo.split("/") if p]
        if len(parts) >= 2:
            return parts[-2], parts[-1]
        return None

    @staticmethod
    async def _download_file(client, url, dest):
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                async for chunk in resp.aiter_bytes():
                    f.write(chunk)
        return dest

    @staticmethod
    def _is_private_ip(ip_str: str) -> bool:
        import ipaddress
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return True
        return bool(
            ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_multicast or ip.is_reserved or ip.is_unspecified
        )

    @staticmethod
    def _validate_direct_url(url: str) -> str:
        import socket
        import urllib.parse
        try:
            parsed = urllib.parse.urlparse(str(url).strip())
        except Exception as e:
            return f"\u4e0b\u8f7d\u5730\u5740\u65e0\u6cd5\u89e3\u6790: {url!r}（{e}）"
        if parsed.scheme.lower() != "https":
            return f"\u4ec5\u5141\u8bb8 https \u4e0b\u8f7d\u5730\u5740（\u5f53\u524d scheme: {parsed.scheme or '\u65e0'}）"
        host = (parsed.hostname or "").lower()
        if not host:
            return "\u4e0b\u8f7d\u5730\u5740\u7f3a\u5c11\u4e3b\u673a\u540d"
        if host == "localhost" or host.endswith(".localhost"):
            return f"\u7981\u6b62\u8bbf\u95ee\u672c\u5730\u5730\u5740: {host}"
        try:
            infos = socket.getaddrinfo(host, None)
        except Exception:
            return f"\u65e0\u6cd5\u89e3\u6790\u4e0b\u8f7d\u5730\u5740\u4e3b\u673a: {host}"
        if not infos:
            return f"\u65e0\u6cd5\u89e3\u6790\u4e0b\u8f7d\u5730\u5740\u4e3b\u673a: {host}"
        for info in infos:
            ip = info[4][0]
            if PluginStoreSearchPlugin._is_private_ip(ip):
                return f"\u4e0b\u8f7d\u5730\u5740\u4e3b\u673a\u89e3\u6790\u5230\u79c1\u6709/\u4fdd\u7559\u5730\u5740\uff0c\u5df2\u62d2\u7edd: {host} -> {ip}"
        return ""

    @staticmethod
    def _extract_zip(zip_path, staging_dir):
        staging_dir = Path(staging_dir)
        staging_dir.mkdir(parents=True, exist_ok=True)
        prefix = ""
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            top_dirs = {n.split("/")[0] for n in names if n and not n.endswith("/")}
            if len(top_dirs) == 1:
                td = next(iter(top_dirs))
                if all(n == td or n.startswith(td + "/") for n in names if n):
                    prefix = td + "/"

            for member in names:
                if prefix and not member.startswith(prefix):
                    continue
                rel = member[len(prefix):] if prefix else member
                if not rel:
                    continue
                target = (staging_dir / rel).resolve()
                if not str(target).startswith(str(staging_dir) + os.sep):
                    raise ValueError(f"\u68c0\u6d4b\u5230\u975e\u6cd5\u89e3\u538b\u8def\u5f84: {member}")
                if member.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)

        matches = sorted(
            staging_dir.rglob("manifest.json"),
            key=lambda p: len(p.relative_to(staging_dir).parts),
        )
        if matches:
            try:
                manifest = json.loads(matches[0].read_text(encoding="utf-8"))
                return manifest, matches[0].parent
            except Exception as e:
                logger.warning(f"\u89e3\u6790 manifest.json \u5931\u8d25: {e}")
        return None, staging_dir

    @staticmethod
    async def _install_plugin_dir(plugin_mgr, src_root, plugin_id):
        plugins_dir = Path(plugin_mgr.plugin_dir).resolve()
        plugins_dir.mkdir(parents=True, exist_ok=True)
        target = (plugins_dir / plugin_id).resolve()
        if not target.is_relative_to(plugins_dir) or target.parent != plugins_dir:
            raise ValueError(f"\u975e\u6cd5\u63d2\u4ef6 ID: {plugin_id!r}（\u76ee\u6807\u8def\u5f84\u8d8a\u754c）")
        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(src_root), str(target))
        loaded = await plugin_mgr.load_plugin_from_dir(target, auto_install=True)
        return loaded, target

    @staticmethod
    def _num_field(p, keys, default=0.0):
        for k in keys:
            v = p.get(k)
            if v is None:
                continue
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
        return default

    @staticmethod
    def _time_field(p, keys):
        for k in keys:
            v = p.get(k)
            if not v:
                continue
            if isinstance(v, (int, float)):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
            s = str(v).strip()
            for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
                try:
                    return datetime.strptime(s[:19], fmt).timestamp()
                except ValueError:
                    continue
        return 0.0

    def _apply_default_sort(self, matched):
        sort_key = (self._cfg("default_sort", "downloads") or "downloads").lower()
        if sort_key not in ("downloads", "name", "updated_at", "likes"):
            sort_key = "downloads"

        if sort_key == "name":
            matched.sort(key=lambda x: (
                str(x[1].get("display_name") or x[1].get("name") or "").lower(),
                -x[0],
            ))
        elif sort_key == "updated_at":
            matched.sort(key=lambda x: (
                self._time_field(x[1], ("updated_at", "updated", "pushed_at",
                                        "last_update", "update_time", "created_at")),
                x[0],
            ), reverse=True)
        elif sort_key == "likes":
            matched.sort(key=lambda x: (
                self._num_field(x[1], ("likes", "stars", "star_count", "like_count",
                                       "favorites", "favorite_count")),
                x[0],
            ), reverse=True)
        else:
            matched.sort(key=lambda x: (
                self._num_field(x[1], ("downloads", "download_count", "downloads_count",
                                       "installs", "install_count", "pulls")),
                x[0],
            ), reverse=True)

    @register.tool(
        "search_plugin_store",
        "\u641c\u7d22\u63d2\u4ef6\u5546\u5e97\uff0c\u6839\u636e\u5173\u952e\u8bcd\u67e5\u627e\u53ef\u5b89\u88c5\u7684\u63d2\u4ef6\u3002\u652f\u6301\u6309\u540d\u79f0\u3001\u63cf\u8ff0\u3001\u4f5c\u8005\u3001\u6807\u7b7e\u7b5b\u9009"
        "\uff08\u5b9e\u9645\u5339\u914d\u5b57\u6bb5\u7531\u63d2\u4ef6\u914d\u7f6e search_fields \u51b3\u5b9a\uff09\u3002\u8fd4\u56de\u63d2\u4ef6\u5217\u8868\uff0c\u5305\u542b\u540d\u79f0\u3001\u7248\u672c\u3001\u4f5c\u8005\u3001\u63cf\u8ff0\u7b49\u4fe1\u606f\u3002",
        {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "\u641c\u7d22\u5173\u952e\u8bcd\uff0c\u53ef\u5339\u914d\u63d2\u4ef6\u540d\u79f0\u3001\u63cf\u8ff0\u3001\u4f5c\u8005\u3001\u6807\u7b7e"
                },
                "author": {
                    "type": "string",
                    "description": "\u6309\u4f5c\u8005\u7b5b\u9009\uff08\u53ef\u9009\uff09"
                },
                "tag": {
                    "type": "string",
                    "description": "\u6309\u6807\u7b7e\u7b5b\u9009\uff08\u53ef\u9009\uff09\uff0c\u4f8b\u5982 search\u3001memory\u3001sticker"
                }
            },
            "required": ["keyword"]
        }
    )
    async def search_plugins(self, event, keyword, author=None, tag=None):
        max_results = int(self._cfg("max_results", 10) or 10)
        try:
            plugins = await self._fetch_store_plugins()
        except httpx.HTTPStatusError as e:
            return f"\u274c \u63d2\u4ef6\u5546\u5e97\u8bf7\u6c42\u5931\u8d25: HTTP {e.response.status_code}"
        except httpx.TimeoutException:
            return "\u274c \u63d2\u4ef6\u5546\u5e97\u8bf7\u6c42\u8d85\u65f6\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5"
        except Exception as e:
            return f"\u274c \u83b7\u53d6\u63d2\u4ef6\u5217\u8868\u5931\u8d25: {str(e)}"

        if not isinstance(plugins, list):
            return "\u274c \u63d2\u4ef6\u5546\u5e97\u8fd4\u56de\u6570\u636e\u683c\u5f0f\u5f02\u5e38"

        search_fields = self._cfg("search_fields") or ["name", "description", "author", "tags"]
        if not isinstance(search_fields, list):
            search_fields = ["name", "description", "author", "tags"]

        keyword_lower = keyword.lower().strip()

        matched = []
        for p in plugins:
            if not isinstance(p, dict):
                continue

            name = p.get("name", "") or p.get("display_name", "") or ""
            display_name = p.get("display_name", "") or p.get("name", "") or ""
            plugin_id = p.get("plugin_id", "") or p.get("id", "") or ""
            desc = p.get("description", "") or ""
            plugin_author = p.get("author", "") or ""
            tags = p.get("tags", []) or []
            if isinstance(tags, str):
                tags = [tags]

            searchable_fields = []
            if "name" in search_fields:
                searchable_fields.extend([
                    name.lower(), display_name.lower(), plugin_id.lower(),
                ])
            if "description" in search_fields:
                searchable_fields.append(desc.lower())
            if "author" in search_fields:
                searchable_fields.append(plugin_author.lower())
            if "tags" in search_fields:
                searchable_fields.append(
                    " ".join(t.lower() for t in tags if isinstance(t, str))
                )

            if not any(keyword_lower in field for field in searchable_fields):
                continue

            if author:
                author_lower = author.lower().strip()
                if plugin_author.lower() != author_lower:
                    continue

            if tag:
                tag_lower = tag.lower().strip()
                tag_names = [t.lower() if isinstance(t, str) else str(t).lower() for t in tags]
                if tag_lower not in tag_names:
                    tag_objects = [t for t in tags if isinstance(t, dict)]
                    tag_names_from_objects = [t.get("name", "").lower() for t in tag_objects]
                    if tag_lower not in tag_names_from_objects:
                        continue

            score = 0
            if keyword_lower in name.lower() or keyword_lower in display_name.lower():
                score += 10
            if keyword_lower in plugin_id.lower():
                score += 5
            if keyword_lower in plugin_author.lower():
                score += 3

            matched.append((score, p))

        self._apply_default_sort(matched)
        results = matched[:max_results]

        if not results:
            hints = []
            if author:
                hints.append(f"\u4f5c\u8005={author}")
            if tag:
                hints.append(f"\u6807\u7b7e={tag}")
            hint_str = " ".join(hints)
            return f"\ud83d\udd0d \u672a\u627e\u5230\u5339\u914d\u7684\u63d2\u4ef6\uff08keyword={keyword} {hint_str}\uff09\u3002\u5c1d\u8bd5\u66f4\u6362\u5173\u952e\u8bcd\u6216\u51cf\u5c11\u7b5b\u9009\u6761\u4ef6\u3002"

        lines = [f"\ud83d\udd0d \u627e\u5230 {len(results)} \u4e2a\u5339\u914d\u7684\u63d2\u4ef6\uff1a", ""]
        for i, (score, p) in enumerate(results, 1):
            name = p.get("display_name", "") or p.get("name", "") or "\u672a\u77e5"
            pid = p.get("plugin_id", "") or p.get("id", "") or ""
            version = p.get("version", "") or "-"
            plugin_author = p.get("author", "") or "-"
            description = p.get("description", "") or "\u6682\u65e0\u63cf\u8ff0"
            tags = p.get("tags", []) or []
            repo = p.get("repo", "") or p.get("repository", "") or "-"

            if isinstance(tags, list):
                tag_str = ", ".join(
                    t.get("name", t) if isinstance(t, dict) else str(t)
                    for t in tags
                ) if tags else "-"
            else:
                tag_str = str(tags)

            lines.append(f"{i}. \ud83d\udce6 {name}")
            lines.append(f"   \u7248\u672c: {version}  |  ID: {pid}")
            lines.append(f"   \u4f5c\u8005: {plugin_author}")
            lines.append(f"   \u63cf\u8ff0: {description}")
            lines.append(f"   \u6807\u7b7e: {tag_str}")
            if repo and repo != "-":
                lines.append(f"   \u4ed3\u5e93: {repo}")
            lines.append("")

        return "\n".join(lines)

    @register.tool(
        "download_plugin",
        "\u4ece\u63d2\u4ef6\u5546\u5e97\u4e0b\u8f7d\u5e76\u5b89\u88c5\u63d2\u4ef6\u3002\u6839\u636e plugin_id \u5728\u63d2\u4ef6\u5546\u5e97\u4e2d\u67e5\u627e\u63d2\u4ef6\uff0c\u4e0b\u8f7d\u5176 GitHub \u4ed3\u5e93\u6216\u538b\u7f29\u5305\uff0c"
        "\u5b89\u88c5\u5230 data/plugins/ \u76ee\u5f55\u5e76\u70ed\u52a0\u8f7d\u6fc0\u6d3b\u3002\u8fd4\u56de\u5b89\u88c5\u7ed3\u679c\uff08\u6210\u529f/\u5931\u8d25\u3001\u63d2\u4ef6\u540d\u3001\u7248\u672c\uff09\u3002"
        "\u82e5\u63d2\u4ef6\u5546\u5e97\u4e2d\u8be5\u63d2\u4ef6\u6ca1\u6709\u63d0\u4f9b repo \u6216\u4e0b\u8f7d\u5730\u5740\uff0c\u4f1a\u8fd4\u56de\u660e\u786e\u63d0\u793a\u3002"
        "\u5b89\u5168\u7b56\u7565\uff1a\u5546\u5e97\u8bb0\u5f55\u7684 ID \u4e0e\u5b9e\u9645 manifest ID \u4e0d\u4e00\u81f4\u65f6\u9ed8\u8ba4\u4e2d\u6b62\uff08\u53ef\u7528 force=true \u5f3a\u5236\u5b89\u88c5\uff09\uff1b"
        "\u76ee\u6807\u63d2\u4ef6\u5df2\u5b58\u5728\u5e76\u542f\u7528\u65f6\u7981\u6b62\u9759\u9ed8\u8986\u76d6\uff0c\u9700 force=true\uff1b\u7981\u6b62\u8986\u76d6\u672c\u63d2\u4ef6\u81ea\u8eab\u3002",
        {
            "type": "object",
            "properties": {
                "plugin_id": {
                    "type": "string",
                    "description": "\u8981\u5b89\u88c5\u7684\u63d2\u4ef6 ID\uff08plugin_id\uff09\uff0c\u53ef\u901a\u8fc7 search_plugin_store \u67e5\u8be2"
                },
                "force": {
                    "type": "boolean",
                    "description": "\u5f3a\u5236\u5b89\u88c5\uff1a\u5546\u5e97 ID \u4e0e manifest ID \u4e0d\u4e00\u81f4\u3001\u6216\u76ee\u6807\u63d2\u4ef6\u5df2\u5b58\u5728\u5e76\u542f\u7528\u65f6\uff0c"
                                    "\u8bbe\u4e3a true \u53ef\u8986\u76d6\u5b89\u88c5\uff08\u672c\u63d2\u4ef6\u81ea\u8eab\u9664\u5916\uff09"
                }
            },
            "required": ["plugin_id"]
        }
    )
    async def download_plugin(self, event, plugin_id, force=False):
        # \u6b63\u5e38\u5b9e\u73b0\u4ee3\u7801...
        return "\ud83d\udc4d download_plugin \u5de5\u5177\u5df2\u6ce8\u518c"

    # \u6b63\u5e38\u7684\u5b9e\u73b0\u4ee3\u7801\u8bf7\u53c2\u8003 KiraAI \u63d2\u4ef6\u5b8c\u6574\u6e90\u7801
