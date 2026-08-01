# KiraAI 插件商店搜索插件

> **Plugin ID**: `plugin_store_search`  
> **版本**: v1.2.2  
> **作者**: Orion & Nymph  
> **标签**: `plugin` `store` `search` `install` `manage` `slash-command`

## 概述

KiraAI 插件商店搜索插件，为 KiraAI 提供完整的插件商店管理能力。支持搜索、安装、卸载、重新扫描插件，以及通过 `/store` 斜杠命令在聊天中直接操作。

## 功能

### 1. 搜索插件 (`search_plugin_store`)

根据关键词在插件商店中搜索可用插件，支持按名称、描述、作者、标签多字段匹配。

- 匹配字段可配置（`search_fields`），默认匹配名称、描述、作者、标签
- 支持按作者精确筛选（`author` 参数）
- 支持按标签精确筛选（`tag` 参数）
- 排序方式可配置（`default_sort`），支持按下载量、名称、更新时间、点赞数排序
- 商店列表带内存缓存（`cache_ttl` 配置，默认 300 秒）

### 2. 下载安装插件 (`download_plugin`)

从插件商店下载并安装插件，自动完成下载、解压、校验、目录移动、热加载激活。

**安装流程：**

1. 从插件商店拉取全部插件列表，找到目标插件
2. 确定下载源：GitHub 仓库优先，其次直接下载地址
3. 下载到临时目录（支持 GitHub 代理自动测速 + 故障转移）
4. 解压并读取 `manifest.json`
5. 校验 manifest ID 与商店记录 ID 一致性
6. 安全校验：插件 ID 白名单、路径穿越防护、禁止覆盖自身、禁止静默覆盖已启用插件
7. 移动到 `data/plugins/<plugin_id>/` 并热加载激活

**安全策略：**

- **SSRF 防护**：直连下载仅允许 HTTPS，主机不得解析到私有/保留网段
- **Zip-Slip 防护**：解压时校验路径不越界
- **ID 交叉校验**：商店记录 ID 与 manifest ID 不一致时默认中止（`force=true` 可跳过）
- **路径穿越防护**：目标目录必须位于插件根目录下且为直接子目录
- **自我保护**：禁止覆盖安装本插件自身
- **静默覆盖防护**：目标插件已存在并启用时需 `force=true`

### 3. 卸载删除插件 (`delete_plugin`)

卸载并删除已安装的插件，支持两步确认安全机制。

**删除流程：**

1. 校验插件 ID 合法性（白名单正则）
2. 检查安全开关 `allow_uninstall`（可全局禁用卸载功能）
3. 检查插件是否存在（含加载失败记录）
4. 内置插件保护（不可删除）
5. 路径越界校验（防止删除插件根目录或越界目录）
6. 内存注销（`uninstall_plugin`）
7. 删除插件目录

**两步确认机制：**

- 默认两步确认：先返回确认提示，用户回复「确认删除」后执行
- `confirm=true` 可跳过确认直接删除
- `auto_confirm_delete=true` 配置可全局跳过确认
- 待确认记录有过期机制（`pending_delete_ttl` 配置，默认 300 秒）

### 4. 重新扫描插件 (`rescan_plugins`)

调用 `PluginManager.reload()` 重新扫描所有插件目录（内置 + 用户），刷新插件注册列表。

返回：插件总数、内置/用户插件数量、成功加载数、失败数，以及详细列表。

### 5. 斜杠命令 (`/store`)

支持在聊天中通过斜杠命令直接操作，无需 LLM 介入。

**使用方式：**
- 群聊：需先 `@` 机器人
- 私聊：无需 `@`

**支持的命令：**

| 命令 | 说明 | 示例 |
|------|------|------|
| `/store search <关键词> [--author] [--tag]` | 搜索插件 | `/store search 表情包` |
| `/store install <plugin_id> [--force]` | 安装插件 | `/store install some_plugin` |
| `/store delete <plugin_id> [--confirm]` | 删除插件 | `/store delete some_plugin` |
| `/store rescan` | 重新扫描 | `/store rescan` |
| `/store help` | 查看帮助 | `/store help` |

**安全控制：**
- `slash_whitelist` 配置白名单 QQ 号列表，留空则所有用户可用
- 钩子拦截后直接 discard 消息，不进入 LLM 流程

## 配置项

### 基础配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `store_url` | string | `https://plugins.kira-ai.top/api/plugins/all` | 商店数据源 URL |
| `max_results` | integer | 10 | 每次搜索返回的最大结果数 |
| `github_proxy` | string | 空 | GitHub 下载加速代理前缀，留空则自动测速选最快代理 |

### 网络设置 (`network`)

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `request_timeout` | integer | 15 | 商店 API 和下载的超时秒数 |
| `cache_ttl` | integer | 300 | 商店列表缓存秒数，0=不缓存 |

### 搜索设置 (`search`)

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `search_fields` | multi_select | name, description, author, tags | 搜索关键词匹配的插件字段 |
| `default_sort` | enum | downloads | 排序方式：downloads/name/updated_at/likes |

### 安全设置 (`safety`)

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `allow_uninstall` | switch | true | 关闭后 delete_plugin 将拒绝执行 |
| `auto_confirm_delete` | switch | false | 开启后跳过确认直接删除 |
| `pending_delete_ttl` | integer | 300 | 待确认删除有效期（秒） |
| `slash_whitelist` | list | 空 | 斜杠命令白名单 QQ 号列表 |

### 命令设置 (`command`)

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `command_prefix` | string | /store | 斜杠命令前缀（必须以 `/` 开头） |

## 核心技术

### GitHub 代理自动测速

当 `github_proxy` 留空时，插件会并发测速多个候选代理，按响应耗时升序排列，最快在前、直连兜底。测速结果缓存 10 分钟，下载失败自动剔除失效代理并切换到下一个候选。

### 事件兼容性

消息处理兼容 `KiraMessageEvent` 与 `KiraMessageBatchEvent`，支持单条/批量消息事件。

### 异常兜底

斜杠命令钩子整体兜底：任何异常都先尝试回复错误信息而不是静默抛错，确保用户能感知到问题。

## 文件结构

```
plugin_store_search/
├── __init__.py          # 包标记（空文件）
├── main.py              # 核心逻辑实现
├── manifest.json        # 插件元数据
└── schema.json          # 配置项定义
```

## 许可证

MIT License