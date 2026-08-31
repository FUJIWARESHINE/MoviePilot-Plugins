# 电影合集查缺

扫描 Emby / Jellyfin 媒体库中的**电影合集（BoxSet / Collection）**，与 TMDB 合集全量片单做差集，把**缺失的电影列出来给你看**，由你逐部或按合集批量决定是否订阅。

> 适用于 MoviePilot **v2.15.0+**。

## 与 `embymissingsubscribe` 的区别

`embymissingsubscribe` 扫描到合集缺失电影后会**直接**添加到 MoviePilot 订阅；本插件改成"扫描只入清单"，**由你手动在详情页选择是否订阅**。

## 功能

- 定时或手动扫描选中的 Emby / Jellyfin 媒体库
- 自动解析 BoxSet 对应的 TMDB 合集 ID（ProviderIds 优先，子项电影回退）
- 与 TMDB 合集全量片单做差集，识别缺失
- 可选：跳过未上映电影、TMDB 评分下限过滤
- 详情页按合集分组，每部电影一张卡片（海报 / 评分 / 上映日期 / 状态 / 检查时间）
- 单部操作：订阅 / 忽略 / 恢复 / 删除
- 合集批量操作：一键订阅 / 忽略本合集全部待处理
- 页面筛选：待处理 / 已订阅 / 已忽略 / 全部
- 发现新增缺失时支持系统通知
- 远程命令 `/collection_missing` 立即扫描

## 重扫规则

| 记录状态 | 重扫时行为 |
|---|---|
| 待处理 | 若对应电影已入库 → 自动移除该记录；否则只刷新检查时间 |
| 已订阅 | 保持原样，仅刷新检查时间 |
| 已忽略 | 保持原样，仅刷新检查时间，可手动恢复 |

## 安装

### 方式一：远程仓库安装（推荐）

1. 进入 MoviePilot → 插件 → 仓库管理 → 添加仓库
2. 填入本仓库地址：
   ```
   https://github.com/FUJIWARESHINE/MoviePilot-Plugins
   ```
3. 在市场中找到「电影合集查缺」安装

### 方式二：上传 zip

在 [Release](https://github.com/FUJIWARESHINE/MoviePilot-Plugins/releases) 下载 zip，到 MoviePilot 插件页上传安装。

## 配置

| 项 | 说明 |
|---|---|
| 启用插件 | 总开关 |
| 发现缺失时通知 | 启用后，扫描发现新缺失时通过系统通知渠道推送 |
| 立即运行一次 | 保存配置后立即触发一次扫描 |
| 跳过未上映电影 | 过滤掉 TMDB release_date 在未来日期的电影 |
| TMDB 评分下限 | 低于此评分的电影不收录；0 = 不限制 |
| 清空检查记录 | 保存后立即清空所有检查记录 |
| 执行周期 | 定时任务 cron 表达式，默认 `0 8 * * *` |
| 媒体服务器 | 选择要扫描的 Emby / Jellyfin |
| 媒体库 | 限定到具体媒体库，不选则扫描全部 |

## 使用

1. 启用插件并选好媒体服务器（建议先勾「立即运行一次」）
2. 进入插件详情页查看缺失电影
3. 对每部电影选择：订阅 / 忽略 / 删除，或点击合集标题旁的「订阅本合集全部」
4. 后续扫描只更新检查时间，不会覆盖你的决定

## 支持范围

- ✅ Emby
- ✅ Jellyfin
- ❌ Plex（API 签名与 BoxSet 命名不同，一期不支持）

## 致谢

- [baranwang/MoviePilot-Plugins](https://github.com/baranwang/MoviePilot-Plugins) — `embymissingsubscribe` 提供合集扫描链路
- [andyxu8023/MoviePilot-Plugins](https://github.com/andyxu8023/MoviePilot-Plugins) — `getmissingepisodes` 提供详情页交互机制

## 许可

仅供个人使用。
