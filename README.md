# Emby 电影合集缺集订阅

扫描 Emby / Jellyfin 媒体库中的**电影合集（BoxSet / Collection）**，与 TMDB 合集全量片单做差集，把**缺失的电影列出**，之后逐部或按合集批量决定是否订阅。

> 同时支持 MoviePilot **V2（v2.15.0+）** 与 **V3（v3.0.0+）**。
> - V2 版本：`v1.2.0`（`plugins.v2/`）
> - V3 版本：`v2.0.0`（`plugins.v3/`，按官方迁移规范完成 V3 独立适配）


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

## V3 适配说明（v1.2.0 / v2.0.0）

对照 [官方 V3 插件迁移文档](https://github.com/jxxghp/MoviePilot-Plugins/blob/main/docs/V3_Plugin_Adaptation.md) 完成：

- 新增 `plugins.v3/collectionmissing/`（V3 专用实现，版本 `2.0.0`），`package.v3.json` 声明 `system_version: ">=3.0.0"`；`package.v2.json` 同名条目声明 `"v3": false`，避免 V3 宿主回退加载旧合同实现
- V2 实现保留在 `plugins.v2/`（版本 `1.2.0`），行为不变，V2 宿主继续正常使用
- V3 实现导入全部迁移到稳定 SDK：`app.sdk.config` / `app.sdk.events` / `app.sdk.logging` / `app.sdk.services`
- 识别与订阅链路统一按 `media_source` + `media_id` 成对身份调用（`MediaChain.recognize_media`、`SubscribeChain.add`、`SubscribeOper.exists`），不再使用 `tmdbid` 参数
- 记录数据同时保存统一身份字段（`media_source` / `media_id`），`tmdb_id` 作为 TMDB 合集维度的单源辅助字段保留；插件初始化时对存量 v1.x 记录执行幂等的统一身份迁移
- 插件 API 注册 `auth: "bear"` 并补全 `response_model` 声明；`apikey` 作为兼容参数保留（可选，显式传入时校验）

## 安装

### 方式一：远程仓库安装（推荐）

1. 进入 MoviePilot → 插件 → 仓库管理 → 添加仓库
2. 填入本仓库地址：
   ```
   https://github.com/FUJIWARESHINE/MoviePilot-Plugins
   ```
3. 在市场中找到「Emby 电影合集缺集订阅」安装

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

---

## 慕雪自动签到

自动签到慕雪阁与 Depth Studio两个站点的 PT 签到插件。Cookie 直接从 MoviePilot「站点管理」读取，不在插件内另行配置。

> 适用于 MoviePilot **v2.15.0+**。


### 特性

- 双站支持，每个站点独立开关与独立历史
- **GET 探测优先**：NexusPHP 在关闭签到验证码时，GET 一次 `attendance.php` 即自动签到，避免无脑 POST
- **验证码识别**：检测到 `imagehash` / `imagestring` 表单时直接报错「请手动签到」，不会盲提交
- 单站签到失败隔离：一个站挂了不影响另一个站
- 详情页按站点显示最近一次结果（成功、Cookie 失效、验证码、网络错误等），历史合并按时间倒序展示
- 远程命令 `/muxue_sign` 立即签到全部启用站点

### 安装

1. MoviePilot → 插件 → 仓库管理 → 添加仓库 `https://github.com/FUJIWARESHINE/MoviePilot-Plugins`
2. 在市场中找到「慕雪自动签到」安装
3. 启用插件前请确认「站点管理」中已配置好两个站点的 Cookie

### 配置

| 项 | 说明 |
|---|---|
| 启用插件 | 总开关 |
| 发送通知 | 完成后通过系统通知渠道推送结果 |
| 立即运行一次 | 保存后立刻跑一次签到 |
| 执行周期 | 定时任务 cron 表达式，默认 `0 9 * * *` |
| 清空签到记录 | 保存后立即清空所有站点的签到历史 |
| 签到 慕雪阁 | 该站点独立开关 |
| 慕雪阁 站点管理匹配域名 | 一般保持 `` 即可 |
| 签到 Depth Studio | 该站点独立开关 |
| Depth Studio 站点管理匹配域名 | 一般保持 `` 即可 |

### 注意

- `dstudio.me` 在国内部分网络环境存在 DNS 污染，请确认部署 MoviePilot 的环境能直连该站，否则单站会持续报「网络错误」（不影响慕雪阁）
- 开启签到验证码的站点无法自动签到，请打开浏览器手动签到一次；之后只要验证码开关状态不变，本插件即可继续正常工作

### 致谢

- [bfjy2024/MoviePilot-Plugins](https://github.com/bfjy2024/MoviePilot-Plugins) — `hongdoubaosignin` 提供基础架构
- [xiaomlove/nexusphp](https://github.com/xiaomlove/nexusphp) — 提供 `attendance.php` 与中文语言包源码比对
