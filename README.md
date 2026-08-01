# KiraAI 插件商店搜索插件

## 简介

KiraAI 插件商店搜索插件，提供插件商店搜索、下载安装、卸载删除功能。

## 功能

- **搜索插件**：按关键词搜索插件商店，支持按名称、描述、作者、标签筛选
- **下载安装**：从 GitHub 仓库或直链下载安装插件，自动热加载激活
- **卸载删除**：两步安全确认删除，内置插件保护
- **重新扫描**：重新加载所有已安装插件
- **斜杠命令**：支持 `/store search / install / delete / rescan / help`

## 配置

| 配置项 | 说明 |
|--------|------|
| store_url | 商店数据源URL |
| github_proxy | GitHub 下载加速代理（留空自动测速） |
| search_fields | 搜索匹配字段 |
| default_sort | 排序方式 |
| allow_uninstall | 允许卸载插件 |
| slash_whitelist | 斜杠命令白名单 |
| command_prefix | 命令前缀（默认 /store） |

## 作者

Orion & Nymph
