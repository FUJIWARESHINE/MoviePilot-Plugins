import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import schemas
from app.chain.media import MediaChain
from app.chain.subscribe import SubscribeChain
from app.chain.tmdb import TmdbChain
from app.db.oper.subscribe import SubscribeOper
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.schemas.types import EventType, MediaSource, MediaType
from app.sdk.config import settings
from app.sdk.events import eventmanager, Event
from app.sdk.logging import logger
from app.sdk.services import MediaServerHelper

# 支持扫描的媒体服务器类型
SUPPORTED_SERVERS: Tuple[str, ...] = ("emby", "jellyfin")

# URL 前缀：Emby 需要 emby/，Jellyfin 不需要
SERVER_URL_PREFIX: Dict[str, str] = {
    "emby": "emby/",
    "jellyfin": "",
}

# 记录状态
STATUS_PENDING = "pending"
STATUS_SUBSCRIBED = "subscribed"
STATUS_IGNORED = "ignored"
STATUS_FAILED = "failed"

STATUS_TEXT: Dict[str, str] = {
    STATUS_PENDING: "待处理",
    STATUS_SUBSCRIBED: "已订阅",
    STATUS_IGNORED: "已忽略",
    STATUS_FAILED: "订阅失败",
}

# 状态 → Vuetify 主题色，供统计卡、状态 Chip、海报角标复用，保证亮暗主题自适应
STATUS_COLOR: Dict[str, str] = {
    STATUS_PENDING: "primary",
    STATUS_SUBSCRIBED: "success",
    STATUS_IGNORED: "grey",
    STATUS_FAILED: "error",
}

# 单个合集默认渲染的海报数量，超出部分折叠，避免长合集把首屏一次性撑爆
GROUP_PAGE_SIZE = 12

DEFAULT_POSTER = "/assets/no-image-CweBJ8Ee.jpeg"
POSTER_BASE = "https://image.tmdb.org/t/p/w500"

# 本插件按 TMDB 合集维度工作，所有记录都属于 TMDB 这一内置来源
RECORD_MEDIA_SOURCE = MediaSource.TMDB

# 插件动作端点的输出模型：显式选择宿主统一 envelope，只回传成功状态与展示
# 文案，业务数据恒为空。get_api() 注册的路由不会被宿主隐式包装，因此必须自己
# 声明与返回结构一致的 response_model。
PluginActionResponse = schemas.Response[dict]


def collection_group_key(server: Any, collection_id: Any) -> str:
    """合集的分组键：只到合集维度，与记录键 server:collection_id:tmdb_id 区分开。"""
    return f"{server}:{collection_id}"


def tmdb_media_id(tmdb_id: Any) -> Optional[str]:
    """把任意来源的 TMDB ID 规范为 V3 媒体身份要求的非空字符串。

    V3 的 media_source 与 media_id 是不可拆分的身份对，空白和字符串 "0" 都不是
    有效身份；因此这里统一归一化，避免无效 ID 进入订阅链路或插件数据。
    """
    if tmdb_id is None:
        return None
    media_id = str(tmdb_id).strip()
    if not media_id or media_id == "0":
        return None
    return media_id


def resolve_record_identity(record: dict) -> Optional[str]:
    """读取记录的规范媒体 ID，兼容尚未写入统一身份字段的存量数据。

    迁移顺序遵循官方要求：先验证统一字段，无效时再回退到历史单源字段 tmdb_id，
    并保证可重复执行。
    """
    media_id = tmdb_media_id(record.get("media_id"))
    if media_id:
        return media_id
    return tmdb_media_id(record.get("tmdb_id"))


def ensure_record_identity(record: dict) -> Optional[str]:
    """为记录补齐 V3 统一身份字段，成功返回规范媒体 ID，失败返回 None。

    只有取得完整有效身份后才写入新字段，绝不先删旧字段；tmdb_id 作为 TMDB 合集
    维度的单源辅助字段保留，用于拼接合集记录键。
    """
    media_id = resolve_record_identity(record)
    if not media_id:
        return None
    record["media_source"] = RECORD_MEDIA_SOURCE.value
    record["media_id"] = media_id
    return media_id


class CollectionMissing(_PluginBase):
    """扫描媒体库中的电影合集（BoxSet），与 TMDB 合集全量片单对比，列出缺失电影供手动确认订阅"""

    # 插件名称
    plugin_name = "Emby 电影合集缺集订阅"
    # 插件描述
    plugin_desc = "扫描 Emby/Jellyfin 媒体库中的电影合集，对比 TMDB 合集全量片单，列出缺失电影供手动确认订阅"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/FUJIWARESHINE/MoviePilot-Plugins/main/icons/CollectionMissing.png"
    # 插件版本，必须与 package.v3.json 中保持一致
    plugin_version = "2.1.2"
    # 插件作者
    plugin_author = "FUJIWARESHINE"
    # 作者主页
    author_url = "https://github.com/FUJIWARESHINE"
    # 配置项前缀
    plugin_config_prefix = "collectionmissing_"
    # 加载顺序
    plugin_order = 10
    # 可使用的用户级别
    auth_level = 2

    # 配置属性
    _enabled: bool = False
    _notify: bool = True
    _onlyonce: bool = False
    _cron: str = "0 8 * * *"
    _mediaservers: List[str] = []
    _libraries: List[str] = []
    _skip_unreleased: bool = True
    _min_vote: float = 0.0
    _clear: bool = False

    # 运行时
    _lock = threading.Lock()
    _event = threading.Event()
    _scheduler: Optional[BackgroundScheduler] = None
    _all_libraries: List[dict] = []

    def init_plugin(self, config: dict = None):
        """初始化插件"""
        self._event = threading.Event()
        self._scheduler = None
        self._media_chain = MediaChain()
        self._subscribe_chain = SubscribeChain()
        self._subscribe_oper = SubscribeOper()
        self._tmdb_chain = TmdbChain()
        self._mediaserver_helper = MediaServerHelper()

        if config:
            self._enabled = bool(config.get("enabled", False))
            self._notify = bool(config.get("notify", True))
            self._onlyonce = bool(config.get("onlyonce", False))
            self._cron = str(config.get("cron") or "0 8 * * *")
            self._mediaservers = config.get("mediaservers") or []
            self._libraries = config.get("libraries") or []
            self._skip_unreleased = bool(config.get("skip_unreleased", True))
            try:
                self._min_vote = float(config.get("min_vote") or 0)
            except (TypeError, ValueError):
                self._min_vote = 0.0
            self._clear = bool(config.get("clear", False))

        # 存量数据迁移：为 v1.x 遗留的仅含 tmdb_id 的记录补齐 V3 统一身份字段
        self.__migrate_history_identity()

        # 存量数据对齐：同一合集的记录共享一份补齐进度（总片数 / 已收数）
        self.__backfill_collection_stats()

        # 构建媒体库选项（供表单多选）
        if self._mediaservers:
            self._all_libraries = self._build_library_list()

        # 立即清理检查记录
        if self._clear:
            self.save_data("history", {"last_scan": "", "details": {}})
            self.save_data("expanded_groups", {})
            self._clear = False
            self.update_config({
                "enabled": self._enabled,
                "notify": self._notify,
                "onlyonce": self._onlyonce,
                "cron": self._cron,
                "mediaservers": self._mediaservers,
                "libraries": self._libraries,
                "skip_unreleased": self._skip_unreleased,
                "min_vote": self._min_vote,
                "clear": False,
            })
            logger.info("已清空合集查缺的检查记录")

        self.stop_service()

        if self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            logger.info("合集查缺服务启动，立即运行一次")
            self._scheduler.add_job(
                func=self.__scan,
                trigger="date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
            )
            self._onlyonce = False
            self.update_config({
                "enabled": self._enabled,
                "notify": self._notify,
                "onlyonce": False,
                "cron": self._cron,
                "mediaservers": self._mediaservers,
                "libraries": self._libraries,
                "skip_unreleased": self._skip_unreleased,
                "min_vote": self._min_vote,
                "clear": False,
            })
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def get_state(self) -> bool:
        """是否启用"""
        return True if self._enabled and self._cron and self._mediaservers else False

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """远程命令"""
        return [
            {
                "cmd": "/collection_missing",
                "event": EventType.PluginAction,
                "desc": "立即扫描电影合集缺失",
                "category": "合集查缺",
                "data": {"action": "collection_missing_scan"},
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        """插件 API"""
        return [
            {
                "path": "/subscribe",
                "endpoint": self.subscribe,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "订阅缺失电影",
                "response_model": PluginActionResponse,
            },
            {
                "path": "/ignore",
                "endpoint": self.ignore,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "忽略缺失电影",
                "response_model": PluginActionResponse,
            },
            {
                "path": "/restore",
                "endpoint": self.restore,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "恢复缺失电影为待处理",
                "response_model": PluginActionResponse,
            },
            {
                "path": "/delete",
                "endpoint": self.delete,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "删除检查记录",
                "response_model": PluginActionResponse,
            },
            {
                "path": "/subscribe_collection",
                "endpoint": self.subscribe_collection,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "订阅某合集全部缺失电影",
                "response_model": PluginActionResponse,
            },
            {
                "path": "/ignore_collection",
                "endpoint": self.ignore_collection,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "忽略某合集全部缺失电影",
                "response_model": PluginActionResponse,
            },
            {
                "path": "/set_filter",
                "endpoint": self.set_filter,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "切换页面筛选",
                "response_model": PluginActionResponse,
            },
            {
                "path": "/toggle_group",
                "endpoint": self.toggle_group,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "切换单个合集的展开状态",
                "response_model": PluginActionResponse,
            },
            {
                "path": "/set_all_groups",
                "endpoint": self.set_all_groups,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "一次性展开或收起全部合集",
                "response_model": PluginActionResponse,
            },
            {
                "path": "/clear",
                "endpoint": self.clear_records,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "清空检查记录",
                "response_model": PluginActionResponse,
            },
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """定时服务"""
        if self.get_state():
            return [
                {
                    "id": "CollectionMissing",
                    "name": "Emby 电影合集缺集订阅",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.__scan,
                    "kwargs": {},
                }
            ]
        return []

    @eventmanager.register(EventType.PluginAction)
    def handle_command(self, event: Event):
        """处理远程命令"""
        if not self._enabled:
            return
        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "collection_missing_scan":
                return
        logger.info("收到远程命令，立即执行电影合集缺失扫描")
        self.__scan()

    # ================================================================
    # 核心扫描逻辑
    # ================================================================

    def __scan(self):
        """扫描所有已配置媒体服务器的合集缺失电影，只记录不订阅"""
        with self._lock:
            if not self._mediaservers:
                logger.warning("合集查缺：未配置媒体服务器，跳过扫描")
                return

            services = self._mediaserver_helper.get_services(name_filters=self._mediaservers)
            if not services:
                logger.warning("合集查缺：获取媒体服务器实例失败，请检查配置")
                return

            history: dict = self.get_data("history") or {}
            if not isinstance(history, dict):
                history = {}
            details: Dict[str, dict] = history.get("details") or {}
            if not isinstance(details, dict):
                details = {}
            new_found: List[str] = []
            # 本次扫描确认已在库中的 (server, collection_id, tmdb_id)
            present_keys: set = set()

            for server_name, service in services.items():
                if service.instance.is_inactive():
                    logger.warning(f"合集查缺：媒体服务器 {server_name} 未连接，跳过")
                    continue
                if service.type not in SUPPORTED_SERVERS:
                    logger.warning(f"合集查缺：媒体服务器 {server_name} 类型 {service.type} 不支持，跳过")
                    continue
                try:
                    found, present = self.__scan_server(server_name, service, details)
                    new_found.extend(found)
                    present_keys.update(present)
                except Exception as e:
                    logger.error(f"合集查缺：扫描媒体服务器 {server_name} 时出错: {e}")

            # 已补齐自动消单：待处理记录对应的电影已入库则删除
            for key in list(details.keys()):
                record = details[key]
                if record.get("status") == STATUS_PENDING and key in present_keys:
                    logger.info(f"合集查缺：{record.get('title')} 已入库，自动移除待处理记录")
                    del details[key]

            history["details"] = details
            history["last_scan"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.save_data("history", history)

            # 发送通知
            if self._notify and new_found:
                text_lines = [f"发现 {len(new_found)} 部缺失电影："]
                for item in new_found[:10]:
                    text_lines.append(f"· {item}")
                if len(new_found) > 10:
                    text_lines.append(f"... 等共 {len(new_found)} 部，请到插件详情页确认是否订阅")
                else:
                    text_lines.append("请到插件详情页确认是否订阅")
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="【Emby 电影合集缺集订阅】",
                    text="\n".join(text_lines),
                )

            if new_found:
                logger.info(f"合集查缺：扫描完成，新增 {len(new_found)} 部缺失电影待处理")
            else:
                logger.info("合集查缺：扫描完成，无新增缺失")

    def __scan_server(
        self, server_name: str, service, details: Dict[str, dict]
    ) -> Tuple[List[str], set]:
        """扫描单个媒体服务器，返回（新增缺失标题列表, 已在库的唯一键集合）"""
        new_found: List[str] = []
        present_keys: set = set()

        user_id = service.instance.user
        if not user_id:
            logger.warning(f"[{server_name}] 无法获取媒体服务器用户 ID，跳过")
            return new_found, present_keys

        library_ids = self._get_scan_library_ids(server_name)
        if not library_ids:
            library_ids = [None]

        for library_id in library_ids:
            try:
                boxsets = self.__fetch_boxsets(service, user_id, library_id)
                if not boxsets:
                    continue
                for boxset in boxsets:
                    found, present = self.__process_boxset(
                        server_name, service, user_id, boxset, details
                    )
                    new_found.extend(found)
                    present_keys.update(present)
            except Exception as e:
                logger.error(
                    f"[{server_name}] 扫描媒体库 {library_id} 合集时出错: {e}"
                )

        return new_found, present_keys

    def __process_boxset(
        self,
        server_name: str,
        service,
        user_id: str,
        boxset: dict,
        details: Dict[str, dict],
    ) -> Tuple[List[str], set]:
        """处理单个 BoxSet：对比 TMDB 合集全量片单，把缺失电影写入待处理清单"""
        new_found: List[str] = []
        present_keys: set = set()
        boxset_id = boxset.get("Id")
        boxset_name = boxset.get("Name", "未知合集")

        collection_id = self.__resolve_collection_id(service, user_id, boxset)
        if not collection_id:
            logger.debug(f"[{server_name}] 合集 {boxset_name} 无法获取 TMDB 合集 ID，跳过")
            return new_found, present_keys

        tmdb_movies = self._tmdb_chain.tmdb_collection(collection_id=collection_id)
        if not tmdb_movies:
            logger.debug(f"[{server_name}] TMDB 合集 {collection_id}（{boxset_name}）无电影信息")
            return new_found, present_keys

        logger.info(
            f"[{server_name}] 合集 {boxset_name}（TMDB:{collection_id}）共 {len(tmdb_movies)} 部电影"
        )

        existing_tmdb_ids = self.__get_boxset_movie_tmdb_ids(service, user_id, boxset_id)
        logger.debug(f"[{server_name}] 合集 {boxset_name} 已有 {len(existing_tmdb_ids)} 部电影")

        # 合集补齐进度：总数取 TMDB 全量片单长度；已收数取片单与媒体库已有 TMDB ID
        # 的交集，避免把不在 TMDB 片单里的库内条目也算成已收
        collection_total = len(tmdb_movies)
        collection_owned = sum(
            1 for m in tmdb_movies if m and m.tmdb_id and m.tmdb_id in existing_tmdb_ids
        )

        now = datetime.now()
        for movie in tmdb_movies:
            if not movie or not movie.tmdb_id:
                continue

            # 已在媒体库中
            if movie.tmdb_id in existing_tmdb_ids:
                present_keys.add(f"{server_name}:{collection_id}:{movie.tmdb_id}")
                continue

            # 解析上映日期：缺日期视为废案（电影公司早年立项但未上映的影片也被
            # 收录进合集），订阅了也不会有任何资源，长期占位没意义，直接跳过
            release_date = self.__parse_release_date(movie.release_date)
            if not release_date:
                logger.debug(
                    f"[{server_name}] 跳过无上映日期电影（疑似废案）: {movie.title}"
                )
                continue

            # 跳过未上映电影（受 _skip_unreleased 配置控制）
            if self._skip_unreleased and release_date > now:
                logger.debug(
                    f"[{server_name}] 跳过未上映电影: {movie.title}（{movie.release_date}）"
                )
                continue

            # TMDB 评分下限过滤
            vote = float(movie.vote_average or 0)
            if self._min_vote > 0 and vote < self._min_vote:
                logger.debug(
                    f"[{server_name}] 评分 {vote} 低于下限 {self._min_vote}，跳过: {movie.title}"
                )
                continue

            media_id = tmdb_media_id(movie.tmdb_id)
            if not media_id:
                # 没有有效 TMDB 身份的电影无法参与订阅，直接跳过
                continue

            unique = f"{server_name}:{collection_id}:{media_id}"
            record = details.get(unique)
            if record:
                # 已有记录：只刷新检查时间与合集进度，不覆盖用户决策
                record["last_check"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                record["collection_total"] = collection_total
                record["collection_owned"] = collection_owned
                continue

            details[unique] = {
                "server": server_name,
                "collection_id": collection_id,
                "collection_name": boxset_name,
                # 合集补齐进度：TMDB 全量片单长度与媒体库已收数量
                "collection_total": collection_total,
                "collection_owned": collection_owned,
                # V3 统一媒体身份；tmdb_id 作为 TMDB 合集维度的单源辅助字段保留
                "media_source": RECORD_MEDIA_SOURCE.value,
                "media_id": media_id,
                "tmdb_id": movie.tmdb_id,
                "title": movie.title or "未知",
                "year": str(movie.year or ""),
                "poster_path": movie.poster_path or "",
                "release_date": movie.release_date or "",
                "vote_average": vote,
                "overview": movie.overview or "",
                "status": STATUS_PENDING,
                "subscribe_id": None,
                "message": "",
                "last_check": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "last_status_change": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            new_found.append(f"{movie.title}（{boxset_name}）")
            logger.info(
                f"[{server_name}] 发现缺失电影: {movie.title}（{movie.year}），"
                f"合集: {boxset_name}"
            )

        return new_found, present_keys

    # ================================================================
    # 媒体服务器 API 交互
    # ================================================================

    def __fetch_boxsets(
        self, service, user_id: str, parent_id: Optional[str] = None
    ) -> List[dict]:
        """获取媒体库中的所有 BoxSet（合集）"""
        prefix = SERVER_URL_PREFIX.get(service.type, "")
        url = (
            f"[HOST]{prefix}Users/{user_id}/Items?"
            f"api_key=[APIKEY]"
            f"&IncludeItemTypes=BoxSet"
            f"&Recursive=true"
            f"&Fields=ProviderIds"
            f"&Limit=500"
        )
        if parent_id:
            url += f"&ParentId={parent_id}"

        try:
            res = service.instance.get_data(url=url)
            if not res:
                return []
            data = res.json()
            items = data.get("Items", [])
            logger.info(
                f"获取到 {len(items)} 个合集"
                + (f"（媒体库 {parent_id}）" if parent_id else "")
            )
            return items
        except Exception as e:
            logger.error(f"获取合集列表失败: {e}")
            return []

    def __get_boxset_movie_tmdb_ids(
        self, service, user_id: str, boxset_id: str
    ) -> set:
        """获取 BoxSet 中已有电影的 TMDB ID 集合"""
        prefix = SERVER_URL_PREFIX.get(service.type, "")
        url = (
            f"[HOST]{prefix}Users/{user_id}/Items?"
            f"api_key=[APIKEY]"
            f"&ParentId={boxset_id}"
            f"&IncludeItemTypes=Movie"
            f"&Fields=ProviderIds"
            f"&Recursive=true"
            f"&Limit=500"
        )
        tmdb_ids: set = set()
        try:
            res = service.instance.get_data(url=url)
            if not res:
                return tmdb_ids
            data = res.json()
            for item in data.get("Items", []):
                provider_ids = item.get("ProviderIds", {})
                tmdb_str = provider_ids.get("Tmdb") or provider_ids.get("tmdb")
                if tmdb_str:
                    try:
                        tmdb_ids.add(int(tmdb_str))
                    except (ValueError, TypeError):
                        pass
        except Exception as e:
            logger.error(f"获取合集子项失败: {e}")
        return tmdb_ids

    def __resolve_collection_id(
        self, service, user_id: str, boxset: dict
    ) -> Optional[int]:
        """获取 BoxSet 对应的 TMDB 合集 ID：
        1. 优先取 BoxSet 自身的 ProviderIds
        2. 回退到子项电影的 belongs_to_collection
        """
        provider_ids = boxset.get("ProviderIds", {})
        tmdb_str = provider_ids.get("Tmdb") or provider_ids.get("tmdb")
        if tmdb_str:
            try:
                return int(tmdb_str)
            except (ValueError, TypeError):
                pass

        boxset_id = boxset.get("Id")
        boxset_name = boxset.get("Name", "未知")
        if not boxset_id:
            return None

        prefix = SERVER_URL_PREFIX.get(service.type, "")
        url = (
            f"[HOST]{prefix}Users/{user_id}/Items?"
            f"api_key=[APIKEY]"
            f"&ParentId={boxset_id}"
            f"&IncludeItemTypes=Movie"
            f"&Fields=ProviderIds"
            f"&Limit=1"
        )
        try:
            res = service.instance.get_data(url=url)
            if not res:
                return None
            data = res.json()
            items = data.get("Items", [])
            if not items:
                return None

            movie_item = items[0]
            movie_provider_ids = movie_item.get("ProviderIds", {})
            movie_tmdb_str = (
                movie_provider_ids.get("Tmdb") or movie_provider_ids.get("tmdb")
            )
            if not movie_tmdb_str:
                return None

            movie_tmdb_id = int(movie_tmdb_str)
            mediainfo = self._media_chain.recognize_media(
                mtype=MediaType.MOVIE,
                media_source=MediaSource.TMDB,
                media_id=str(movie_tmdb_id),
            )
            if not mediainfo:
                return None

            cid = getattr(mediainfo, "collection_id", None)
            if cid:
                logger.info(
                    f"通过子项电影 {movie_item.get('Name')} 识别到合集 "
                    f"{boxset_name} 的 TMDB 合集 ID: {cid}"
                )
                return cid

            if mediainfo.tmdb_info:
                btc = mediainfo.tmdb_info.get("belongs_to_collection")
                if btc and btc.get("id"):
                    cid = btc["id"]
                    logger.info(
                        f"通过子项电影 {movie_item.get('Name')} 的 "
                        f"belongs_to_collection 识别到合集 {boxset_name} "
                        f"的 TMDB 合集 ID: {cid}"
                    )
                    return cid

        except Exception as e:
            logger.debug(f"从子项电影获取 TMDB 合集 ID 失败: {e}")

        return None

    # ================================================================
    # 存量数据迁移
    # ================================================================

    def __migrate_history_identity(self) -> int:
        """为存量记录补齐 V3 统一媒体身份字段，返回本次发生变更的记录数。

        迁移必须可重复执行：统一字段已有效时只补来源、不再改写 ID；只有从历史
        单源字段 tmdb_id 取得有效身份后才写入新字段，绝不先删除旧数据再保存。
        找不到有效回填来源时保留原记录，避免为了“清理”而丢数据。
        """
        history: dict = self.get_data("history") or {}
        details = history.get("details") if isinstance(history, dict) else None
        if not isinstance(details, dict) or not details:
            return 0

        migrated = 0
        for record in details.values():
            if not isinstance(record, dict):
                continue
            if tmdb_media_id(record.get("media_id")):
                # 统一身份已有效，仅保证来源字段与当前来源一致
                if record.get("media_source") != RECORD_MEDIA_SOURCE.value:
                    record["media_source"] = RECORD_MEDIA_SOURCE.value
                    migrated += 1
                continue
            if ensure_record_identity(record):
                migrated += 1

        if migrated:
            self.__save_details(details)
            logger.info(f"已为 {migrated} 条存量记录补齐 V3 统一媒体身份")
        return migrated

    def __backfill_collection_stats(self) -> int:
        """在同一合集内对齐补齐进度字段，返回本次实际写入的记录数。

        合集总片数与已收数只能在扫描时从 TMDB 片单与媒体库现况取得，存量记录无法
        凭空推算，因此这里只做组内传播：同一 (server, collection_id) 的记录共享同一
        份统计，任一条已有有效值时同步给组内其它记录，避免同一合集里部分记录显示出
        进度、另一部分显示不出来。

        整组都没有统计时保持原样（不写 0、不写占位值），页面按「无统计」降级隐藏进度
        条，等下一次扫描自然补齐。方法可重复执行：第二次执行时组内已一致，返回 0 且不写库。
        """
        history: dict = self.get_data("history") or {}
        details = history.get("details") if isinstance(history, dict) else None
        if not isinstance(details, dict) or not details:
            return 0

        # 先按合集聚合出组内已知的有效统计
        group_stats: Dict[tuple, Tuple[int, int]] = {}
        for record in details.values():
            if not isinstance(record, dict):
                continue
            total = record.get("collection_total")
            owned = record.get("collection_owned")
            if isinstance(total, int) and isinstance(owned, int):
                group_stats[(record.get("server"), record.get("collection_id"))] = (
                    total,
                    owned,
                )

        if not group_stats:
            return 0

        filled = 0
        for record in details.values():
            if not isinstance(record, dict):
                continue
            stats = group_stats.get((record.get("server"), record.get("collection_id")))
            if not stats:
                continue
            total, owned = stats
            if (
                record.get("collection_total") != total
                or record.get("collection_owned") != owned
            ):
                record["collection_total"] = total
                record["collection_owned"] = owned
                filled += 1

        if filled:
            self.__save_details(details)
            logger.info(f"已对齐 {filled} 条记录的合集补齐进度字段")
        return filled

    # ================================================================
    # 订阅
    # ================================================================

    def __subscribe_movie(self, record: dict) -> Tuple[bool, str]:
        """按记录订阅缺失电影，返回（是否成功, 消息）"""
        title = record.get("title")
        year = str(record.get("year") or "")
        media_id = ensure_record_identity(record)
        if not title or not media_id:
            return False, "记录信息不完整或缺少有效媒体身份"

        if self._subscribe_oper.exists(RECORD_MEDIA_SOURCE, media_id):
            return True, "订阅已存在"

        try:
            sid, msg = self._subscribe_chain.add(
                title=title,
                year=year,
                mtype=MediaType.MOVIE,
                media_source=RECORD_MEDIA_SOURCE,
                media_id=media_id,
                exist_ok=True,
                username=self.plugin_name,
                message=False,
            )
            if sid:
                return True, "订阅成功"
            logger.warning(f"添加订阅 {title} 失败: {msg}")
            return False, str(msg) or "订阅失败"
        except Exception as e:
            logger.error(f"添加订阅失败: {e}")
            return False, str(e)

    def __update_status(self, details: dict, key: str, status: str, **extra) -> bool:
        """更新记录状态"""
        record = details.get(key)
        if not record:
            return False
        record["status"] = status
        record["last_status_change"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        record.update(extra)
        return True

    # ================================================================
    # API 端点
    # ================================================================

    def __check_apikey(self, apikey: Optional[str]) -> bool:
        # V3 宿主已通过 auth="bear" 完成统一鉴权；apikey 仅作为兼容 v2 页面事件的
        # 兜底校验：显式传入时必须与宿主 API_TOKEN 一致，未传入（走 bearer）则放行。
        if apikey is None:
            return True
        return apikey == settings.API_TOKEN

    def __get_details(self) -> Optional[dict]:
        history: dict = self.get_data("history") or {}
        details = history.get("details") if isinstance(history, dict) else None
        if not isinstance(details, dict) or not details:
            return None
        return details

    def __save_details(self, details: dict):
        history: dict = self.get_data("history") or {}
        if not isinstance(history, dict):
            history = {}
        history["details"] = details
        self.save_data("history", history)

    def subscribe(self, key: str, apikey: Optional[str] = None) -> PluginActionResponse:
        """订阅单部缺失电影"""
        if not self.__check_apikey(apikey):
            return schemas.Response(success=False, message="API密钥错误")
        details = self.__get_details()
        if details is None:
            return schemas.Response(success=False, message="未找到检查记录")
        record = details.get(key)
        if not record:
            return schemas.Response(success=False, message="记录不存在")

        ok, msg = self.__subscribe_movie(record)
        if ok:
            self.__update_status(
                details, key, STATUS_SUBSCRIBED,
                subscribe_id=record.get("subscribe_id"),
                message=msg,
            )
            self.__save_details(details)
            return schemas.Response(success=True, message=f"{record.get('title')} 已订阅")
        self.__update_status(details, key, STATUS_FAILED, message=msg)
        self.__save_details(details)
        return schemas.Response(success=False, message=f"{record.get('title')} 订阅失败: {msg}")

    def ignore(self, key: str, apikey: Optional[str] = None) -> PluginActionResponse:
        """忽略单部缺失电影"""
        if not self.__check_apikey(apikey):
            return schemas.Response(success=False, message="API密钥错误")
        details = self.__get_details()
        if details is None:
            return schemas.Response(success=False, message="未找到检查记录")
        if key not in details:
            return schemas.Response(success=False, message="记录不存在")
        self.__update_status(details, key, STATUS_IGNORED)
        self.__save_details(details)
        return schemas.Response(success=True, message="已忽略")

    def restore(self, key: str, apikey: Optional[str] = None) -> PluginActionResponse:
        """恢复为待处理"""
        if not self.__check_apikey(apikey):
            return schemas.Response(success=False, message="API密钥错误")
        details = self.__get_details()
        if details is None:
            return schemas.Response(success=False, message="未找到检查记录")
        if key not in details:
            return schemas.Response(success=False, message="记录不存在")
        self.__update_status(details, key, STATUS_PENDING)
        self.__save_details(details)
        return schemas.Response(success=True, message="已恢复为待处理")

    def delete(self, key: str, apikey: Optional[str] = None) -> PluginActionResponse:
        """删除单条检查记录"""
        if not self.__check_apikey(apikey):
            return schemas.Response(success=False, message="API密钥错误")
        details = self.__get_details()
        if details is None:
            return schemas.Response(success=False, message="未找到检查记录")
        if key not in details:
            return schemas.Response(success=False, message="记录不存在")
        del details[key]
        self.__save_details(details)
        return schemas.Response(success=True, message="删除成功")

    def subscribe_collection(self, server: str, collection: str, apikey: Optional[str] = None) -> PluginActionResponse:
        """批量订阅某合集下所有待处理电影"""
        if not self.__check_apikey(apikey):
            return schemas.Response(success=False, message="API密钥错误")
        details = self.__get_details()
        if details is None:
            return schemas.Response(success=False, message="未找到检查记录")
        targets = [
            (key, record) for key, record in details.items()
            if str(record.get("server")) == server
            and str(record.get("collection_id")) == collection
            and record.get("status") == STATUS_PENDING
        ]
        if not targets:
            return schemas.Response(success=False, message="该合集没有待处理记录")

        success_count = 0
        for key, record in targets:
            ok, msg = self.__subscribe_movie(record)
            if ok:
                self.__update_status(
                    details, key, STATUS_SUBSCRIBED,
                    subscribe_id=record.get("subscribe_id"),
                    message=msg,
                )
                success_count += 1
            else:
                self.__update_status(details, key, STATUS_FAILED, message=msg)
        self.__save_details(details)
        return schemas.Response(
            success=success_count > 0,
            message=f"合集订阅完成：成功 {success_count}，失败 {len(targets) - success_count}",
        )

    def ignore_collection(self, server: str, collection: str, apikey: Optional[str] = None) -> PluginActionResponse:
        """批量忽略某合集下所有待处理电影"""
        if not self.__check_apikey(apikey):
            return schemas.Response(success=False, message="API密钥错误")
        details = self.__get_details()
        if details is None:
            return schemas.Response(success=False, message="未找到检查记录")
        count = 0
        for key, record in details.items():
            if (
                str(record.get("server")) == server
                and str(record.get("collection_id")) == collection
                and record.get("status") == STATUS_PENDING
            ):
                self.__update_status(details, key, STATUS_IGNORED)
                count += 1
        if count == 0:
            return schemas.Response(success=False, message="该合集没有待处理记录")
        self.__save_details(details)
        return schemas.Response(success=True, message=f"已忽略 {count} 部")

    def set_filter(self, filter: str, apikey: Optional[str] = None) -> PluginActionResponse:
        """切换页面筛选"""
        if not self.__check_apikey(apikey):
            return schemas.Response(success=False, message="API密钥错误")
        if filter not in ("pending", "subscribed", "ignored", "all"):
            return schemas.Response(success=False, message="无效的筛选条件")
        self.save_data("filter", filter)
        return schemas.Response(success=True, message=f"已切换筛选: {filter}")

    def clear_records(self, scope: str, apikey: Optional[str] = None) -> PluginActionResponse:
        """清空检查记录，scope=all 全部清空，scope=pending 只清待处理"""
        if not self.__check_apikey(apikey):
            return schemas.Response(success=False, message="API密钥错误")
        if scope == "pending":
            details = self.__get_details()
            if details is None:
                return schemas.Response(success=True, message="没有待处理记录")
            before = len(details)
            for key in [k for k, r in details.items() if r.get("status") == STATUS_PENDING]:
                del details[key]
            self.__save_details(details)
            return schemas.Response(success=True, message=f"已清空 {before - len(details)} 条待处理记录")
        if scope == "all":
            self.save_data("history", {"last_scan": "", "details": {}})
            # 记录清空后展开状态已无对应合集，一并清掉避免留下垃圾
            self.save_data("expanded_groups", {})
            return schemas.Response(success=True, message="已清空全部记录")
        return schemas.Response(success=False, message="无效的清理范围")

    def toggle_group(self, group: str, apikey: Optional[str] = None) -> PluginActionResponse:
        """切换单个合集的展开状态：展开后渲染该合集全部缺失电影"""
        if not self.__check_apikey(apikey):
            return schemas.Response(success=False, message="API密钥错误")
        if not group:
            return schemas.Response(success=False, message="缺少合集标识")

        expanded = self.__get_expanded_groups()
        key = str(group)
        expanded[key] = not expanded.get(key, False)
        self.save_data("expanded_groups", expanded)
        return schemas.Response(
            success=True,
            message="已展开该合集" if expanded[key] else "已收起该合集",
        )

    def set_all_groups(self, expanded: str, apikey: Optional[str] = None) -> PluginActionResponse:
        """一次性展开或收起全部合集，expanded 取 true/1/yes/on 视为展开"""
        if not self.__check_apikey(apikey):
            return schemas.Response(success=False, message="API密钥错误")

        expand = str(expanded).strip().lower() in ("true", "1", "yes", "on")
        # 以当前记录里实际存在的合集为准，避免为已消失的合集留下永久垃圾状态
        keys = {
            collection_group_key(record.get("server"), record.get("collection_id"))
            for record in (self.__get_details() or {}).values()
            if isinstance(record, dict)
        }
        self.save_data("expanded_groups", {key: expand for key in keys})
        return schemas.Response(
            success=True,
            message=f"已{'展开' if expand else '收起'} {len(keys)} 个合集",
        )

    # ================================================================
    # 辅助方法
    # ================================================================

    def __get_expanded_groups(self) -> Dict[str, bool]:
        """读取合集展开状态，始终返回字典，避免调用方各自判空。"""
        data = self.get_data("expanded_groups")
        return data if isinstance(data, dict) else {}

    @staticmethod
    def __parse_release_date(release_date: Optional[str]) -> Optional[datetime]:
        """解析上映日期，失败返回 None"""
        if not release_date:
            return None
        try:
            return datetime.strptime(str(release_date)[:10], "%Y-%m-%d")
        except (ValueError, TypeError):
            return None

    def _get_scan_library_ids(self, server_name: str) -> List[str]:
        """从用户选择的媒体库中提取属于指定服务器的媒体库 ID"""
        if not self._libraries:
            return []
        result = []
        prefix = f"{server_name}-"
        for lib_value in self._libraries:
            if lib_value.startswith(prefix):
                result.append(lib_value[len(prefix):])
        return result

    def _build_library_list(self) -> List[dict]:
        """构建媒体库选项列表（供表单多选）"""
        lib_items: List[dict] = []
        if not self._mediaservers:
            return lib_items

        services = self._mediaserver_helper.get_services(name_filters=self._mediaservers)
        if not services:
            return lib_items

        for server_name, service in services.items():
            if service.instance.is_inactive() or service.type not in SUPPORTED_SERVERS:
                continue
            try:
                libraries = service.instance.get_librarys()
                if not libraries:
                    continue
                for lib in libraries:
                    lib_id = lib.id
                    lib_name = lib.name
                    if lib_id is not None and lib_name:
                        lib_items.append({
                            "title": f"{server_name}: {lib_name}",
                            "value": f"{server_name}-{lib_id}",
                        })
            except Exception as e:
                logger.debug(f"获取媒体库列表失败: {server_name}, {e}")

        return lib_items

    # ================================================================
    # 表单
    # ================================================================

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        server_items = []
        mediaserver_helper = getattr(self, "_mediaserver_helper", None)
        if mediaserver_helper:
            for svc in mediaserver_helper.get_services().values():
                server_items.append({"title": svc.name, "value": svc.name})

        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "notify",
                                            "label": "发现缺失时通知",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "onlyonce",
                                            "label": "立即运行一次",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "skip_unreleased",
                                            "label": "跳过未上映电影",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "min_vote",
                                            "label": "TMDB 评分下限",
                                            "placeholder": "0 = 不限制",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "clear",
                                            "label": "清空检查记录",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VCronField",
                                        "props": {
                                            "model": "cron",
                                            "label": "执行周期",
                                            "placeholder": "0 8 * * *",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "multiple": True,
                                            "chips": True,
                                            "clearable": True,
                                            "model": "mediaservers",
                                            "label": "媒体服务器",
                                            "items": server_items,
                                            "hint": "选择要扫描的 Emby/Jellyfin 服务器",
                                            "persistentHint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "multiple": True,
                                            "chips": True,
                                            "clearable": True,
                                            "model": "libraries",
                                            "label": "媒体库",
                                            "items": self._all_libraries or [],
                                            "hint": "选择要扫描的媒体库，不选则扫描全部",
                                            "persistentHint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": (
                                                "扫描媒体库中的电影合集（BoxSet），"
                                                "与 TMDB 合集全量片单对比，"
                                                "把缺失的电影记录到详情页，"
                                                "由你逐部或按合集批量确认是否订阅。\n\n"
                                                "重扫不会覆盖你已做出的订阅/忽略决定；"
                                                "待处理记录对应的电影一旦入库会自动移除。\n\n"
                                                "详情页操作：订阅 / 忽略 / 恢复 / 删除，"
                                                "也可通过 /collection_missing 命令立即扫描。"
                                            ),
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "notify": True,
            "onlyonce": False,
            "cron": "0 8 * * *",
            "mediaservers": [],
            "libraries": [],
            "skip_unreleased": True,
            "min_vote": 0,
            "clear": False,
        }

    # ================================================================
    # 详情页
    # ================================================================

    def get_page(self) -> List[dict]:
        history: dict = self.get_data("history") or {}
        details = history.get("details") or {}
        if not details:
            return [
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "text": "暂无检查记录。请在配置页勾选「立即运行一次」并保存，"
                                "或等待定时任务执行；也可在消息渠道发送 /collection_missing 立即扫描。",
                    },
                }
            ]

        # 统计
        count_pending = sum(1 for r in details.values() if r.get("status") == STATUS_PENDING)
        count_subscribed = sum(1 for r in details.values() if r.get("status") == STATUS_SUBSCRIBED)
        count_ignored = sum(1 for r in details.values() if r.get("status") == STATUS_IGNORED)
        count_failed = sum(1 for r in details.values() if r.get("status") == STATUS_FAILED)
        collections_count = len({(r.get("server"), r.get("collection_id")) for r in details.values()})

        current_filter = self.get_data("filter") or STATUS_PENDING

        # 全部合集是否已完整展开：决定工具条「全部展开/收起」的文案与目标状态
        expanded_groups = self.__get_expanded_groups()
        all_groups_full = bool(expanded_groups) and all(expanded_groups.values())

        # 筛选按钮：选中项实心主色，未选中描边，一眼可辨
        filter_buttons = [
            {
                "component": "VBtn",
                "props": {
                    "variant": "flat" if current_filter == key else "outlined",
                    "color": "primary" if current_filter == key else "",
                    "size": "small",
                    "class": "mr-2",
                },
                "events": {
                    "click": {
                        "api": "plugin/CollectionMissing/set_filter",
                        "method": "get",
                        "params": {"filter": key, "apikey": settings.API_TOKEN},
                    }
                },
                "text": text,
            }
            for key, text in [
                (STATUS_PENDING, f"待处理 {count_pending}"),
                (STATUS_SUBSCRIBED, f"已订阅 {count_subscribed}"),
                (STATUS_IGNORED, f"已忽略 {count_ignored}"),
                ("all", f"全部 {len(details)}"),
            ]
        ]

        # 按合集分组
        groups: Dict[tuple, dict] = {}
        for key, record in details.items():
            if current_filter != "all" and record.get("status") != current_filter:
                continue
            gkey = (record.get("server"), record.get("collection_id"))
            group = groups.setdefault(gkey, {
                "server": record.get("server", ""),
                "collection_id": record.get("collection_id"),
                "name": record.get("collection_name") or "未知合集",
                "records": [],
            })
            group["records"].append((key, record))

        # 顶部状态统计卡：待处理 / 已订阅 / 已忽略 / 订阅失败
        stat_cards = [
            self.__get_stat_card_content(label, count, STATUS_COLOR[status])
            for status, label, count in [
                (STATUS_PENDING, "待处理", count_pending),
                (STATUS_SUBSCRIBED, "已订阅", count_subscribed),
                (STATUS_IGNORED, "已忽略", count_ignored),
                (STATUS_FAILED, "订阅失败", count_failed),
            ]
        ]

        page_content = [
            {
                "component": "VRow",
                "props": {"class": "mb-2"},
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 6, "md": 3},
                        "content": [card],
                    }
                    for card in stat_cards
                ],
            },
            # 筛选与操作工具条
            {
                "component": "VCard",
                "props": {"variant": "tonal", "class": "mb-4"},
                "content": [
                    {
                        "component": "VCardText",
                        "props": {"class": "pa-2 d-flex flex-wrap align-center gap-2"},
                        "content": [
                            *filter_buttons,
                            {"component": "div", "props": {"class": "flex-grow-1"}},
                            {
                                "component": "VBtn",
                                "props": {
                                    "variant": "tonal",
                                    "size": "small",
                                    "class": "mr-2",
                                },
                                "events": {
                                    "click": {
                                        "api": "plugin/CollectionMissing/set_all_groups",
                                        "method": "get",
                                        "params": {
                                            "expanded": "false" if all_groups_full else "true",
                                            "apikey": settings.API_TOKEN,
                                        },
                                    }
                                },
                                "text": "全部收起" if all_groups_full else "全部展开",
                            },
                            {
                                "component": "VBtn",
                                "props": {
                                    "variant": "tonal",
                                    "size": "small",
                                    "class": "text-error",
                                },
                                "events": {
                                    "click": {
                                        "api": "plugin/CollectionMissing/clear",
                                        "method": "get",
                                        "params": {
                                            "scope": "pending",
                                            "apikey": settings.API_TOKEN,
                                        },
                                    }
                                },
                                "text": "清空待处理",
                            },
                        ],
                    },
                    {
                        "component": "VCardText",
                        "props": {"class": "pa-2 pt-0 text-caption text-grey-darken-1"},
                        "text": f"共 {len(details)} 部缺失 · {collections_count} 个合集 · "
                                f"上次扫描 {history.get('last_scan') or '-'}",
                    },
                ],
            },
        ]

        # 合集分组渲染为折叠面板：面板自身的展开/收起由前端即时切换（零刷新）；
        # 服务端只负责「完整展开」状态（expanded_groups），用于驱动每组合集的
        # 默认渲染数量（前 GROUP_PAGE_SIZE 部 / 全部），并让整页重渲染后仍能
        # 通过 modelValue 恢复默认展开项。
        group_list = sorted(groups.values(), key=lambda g: (g["name"] or ""))
        open_indices = [
            i
            for i, g in enumerate(group_list)
            if expanded_groups.get(collection_group_key(g["server"], g["collection_id"]), False)
        ]

        panels = [self.__get_group_panel_content(g, expanded_groups) for g in group_list]
        if panels:
            page_content.append({
                "component": "VExpansionPanels",
                "props": {"multiple": True, "modelValue": open_indices},
                "content": panels,
            })

        return page_content

    def __get_group_panel_content(self, group: dict, expanded_groups: dict) -> dict:
        """构建单个合集的折叠面板：标题含名称/服务器/缺失Chip/补齐进度，内容为海报网格。

        每组合集默认只渲染前 GROUP_PAGE_SIZE 部，超出部分通过底部「展开全部」切换；
        expanded_groups[gkey] 为 True 表示完整展开。面板自身的展开/收起由前端即时切换，
        整页重渲染时以 expanded_groups 推导默认展开项。
        """
        server = group["server"]
        collection_id = group["collection_id"]
        records = sorted(group["records"], key=lambda x: x[1].get("title") or "")
        gkey = collection_group_key(server, collection_id)
        show_all = bool(expanded_groups.get(gkey, False))

        # 补齐进度：取组内任一条有效统计，缺失时整块隐藏，不显示 0%
        total = owned = None
        for _, record in records:
            t = record.get("collection_total")
            o = record.get("collection_owned")
            if isinstance(t, int) and isinstance(o, int):
                total, owned = t, o
                break
        pct = 0
        if isinstance(total, int) and isinstance(owned, int) and total > 0:
            pct = min(100, int(round(owned / total * 100)))

        progress_content = []
        if isinstance(total, int) and isinstance(owned, int) and total > 0:
            progress_content = [
                {
                    "component": "div",
                    "props": {
                        "class": "d-flex align-center flex-grow-1",
                        "style": "max-width: 260px; min-width: 140px;",
                    },
                    "content": [
                        {
                            "component": "span",
                            "props": {"class": "text-caption text-grey-darken-1 mr-2"},
                            "text": f"已收 {owned}/{total}",
                        },
                        {
                            "component": "div",
                            "props": {
                                "class": "flex-grow-1 bg-grey-lighten-3 rounded",
                                "style": "height: 6px;",
                            },
                            "content": [
                                {
                                    "component": "div",
                                    "props": {
                                        "class": "bg-primary rounded",
                                        "style": f"height: 100%; width: {pct}%;",
                                    },
                                }
                            ],
                        },
                    ],
                },
            ]

        # 批量操作按钮：仅当组内存在待处理记录时展示
        batch_buttons = []
        if any(r.get("status") == STATUS_PENDING for _, r in records):
            batch_buttons = [
                {
                    "component": "VBtn",
                    "props": {"class": "text-primary mr-2", "variant": "tonal", "size": "small"},
                    "events": {
                        "click": {
                            "api": "plugin/CollectionMissing/subscribe_collection",
                            "method": "get",
                            "params": {
                                "server": str(server),
                                "collection": str(collection_id),
                                "apikey": settings.API_TOKEN,
                            },
                        }
                    },
                    "text": "订阅本合集全部",
                },
                {
                    "component": "VBtn",
                    "props": {"class": "text-warning mr-2", "variant": "tonal", "size": "small"},
                    "events": {
                        "click": {
                            "api": "plugin/CollectionMissing/ignore_collection",
                            "method": "get",
                            "params": {
                                "server": str(server),
                                "collection": str(collection_id),
                                "apikey": settings.API_TOKEN,
                            },
                        }
                    },
                    "text": "忽略本合集全部",
                },
            ]

        # 每组合集渲染数量：默认前 GROUP_PAGE_SIZE 部，完整展开时渲染全部
        shown = records
        if len(records) > GROUP_PAGE_SIZE and not show_all:
            shown = records[:GROUP_PAGE_SIZE]

        # 面板内操作条：批量按钮 + 右侧展开/收起
        footer_buttons = []
        if len(records) > GROUP_PAGE_SIZE:
            footer_buttons = [
                {
                    "component": "VBtn",
                    "props": {"variant": "tonal", "size": "small", "class": "text-primary"},
                    "events": {
                        "click": {
                            "api": "plugin/CollectionMissing/toggle_group",
                            "method": "get",
                            "params": {"group": gkey, "apikey": settings.API_TOKEN},
                        }
                    },
                    "text": f"展开全部（还有 {len(records) - GROUP_PAGE_SIZE} 部）" if not show_all else "收起",
                },
            ]

        action_bar = [
            {
                "component": "div",
                "props": {"class": "d-flex flex-wrap align-center gap-2 pb-2"},
                "content": [
                    *batch_buttons,
                    {"component": "div", "props": {"class": "flex-grow-1"}},
                    *footer_buttons,
                ],
            },
        ]

        grid_content = {
            "component": "VRow",
            "props": {"class": "d-flex flex-wrap"},
            "content": [
                {
                    "component": "VCol",
                    "props": {"cols": 6, "sm": 4, "md": 3, "lg": 2},
                    "content": [self.__get_history_post_content(key, record)],
                }
                for key, record in shown
            ],
        }

        return {
            "component": "VExpansionPanel",
            "props": {"class": "mb-2"},
            "content": [
                {
                    "component": "VExpansionPanelTitle",
                    "content": [
                        {
                            "component": "div",
                            "props": {"class": "d-flex flex-wrap align-center gap-2 w-100 py-1"},
                            "content": [
                                {
                                    "component": "span",
                                    "props": {"class": "text-subtitle-1 font-weight-medium"},
                                    "text": group["name"],
                                },
                                {
                                    "component": "span",
                                    "props": {"class": "text-caption text-grey-darken-1"},
                                    "text": server,
                                },
                                {
                                    "component": "VChip",
                                    "props": {"size": "small", "variant": "tonal", "color": "primary"},
                                    "text": f"缺失 {len(records)} 部",
                                },
                                *progress_content,
                            ],
                        },
                    ],
                },
                {
                    "component": "VExpansionPanelText",
                    "content": [*action_bar, grid_content],
                },
            ],
        }

    @staticmethod
    def __get_stat_card_content(label: str, value: int, color: str) -> dict:
        """构建单张状态统计卡：大号数字 + 小号标签，数字按状态语义着色"""
        return {
            "component": "VCard",
            "props": {"variant": "tonal", "class": "text-center pa-2"},
            "content": [
                {
                    "component": "div",
                    "props": {"class": f"text-h5 font-weight-bold text-{color}"},
                    "text": f"{value}",
                },
                {
                    "component": "div",
                    "props": {"class": "text-caption text-grey-darken-1"},
                    "text": label,
                },
            ],
        }

    def __get_history_post_content(self, key: str, record: dict) -> dict:
        """构建单张缺失电影海报卡：2:3 海报 + 评分/状态角标 + 片名两行 + 悬浮简介"""
        title = record.get("title") or "未知"
        year = record.get("year") or ""
        tmdb_id = record.get("tmdb_id")
        status = record.get("status") or STATUS_PENDING
        status_cn = STATUS_TEXT.get(status, status)
        status_color = STATUS_COLOR.get(status, "grey")

        poster_path = record.get("poster_path") or ""
        if poster_path.startswith("http"):
            poster = poster_path
        elif poster_path:
            poster = f"{POSTER_BASE}{poster_path}"
        else:
            poster = DEFAULT_POSTER

        mp_domain = settings.MP_DOMAIN()
        link = f"#/media?mediaid=tmdb:{tmdb_id}&type={MediaType.MOVIE.value}"
        if mp_domain:
            if mp_domain.endswith("/"):
                link = f"{mp_domain}{link}"
            else:
                link = f"{mp_domain}/{link}"

        action_buttons = self.__get_action_buttons_content(key, record)

        # 评分角标文案：无效评分不渲染
        vote = record.get("vote_average")
        vote_text = f"★ {vote:.1f}" if isinstance(vote, (int, float)) else ""

        # 已处理视觉退让：已忽略整卡降透明，已订阅海报叠淡绿蒙层
        card_class = "d-flex flex-column"
        if status == STATUS_IGNORED:
            card_class += " opacity-60"

        poster_overlay = []
        if status == STATUS_SUBSCRIBED:
            poster_overlay.append({
                "component": "div",
                "props": {
                    "class": "bg-success",
                    "style": "position: absolute; top: 0; left: 0; right: 0; bottom: 0; "
                             "opacity: 0.12; pointer-events: none; border-radius: 4px 4px 0 0;",
                },
            })

        return {
            "component": "VCard",
            "props": {
                "variant": "tonal",
                "class": card_class,
                "style": "height: 100%;",
            },
            "content": [
                # 海报区：评分角标左上、状态角标右上
                {
                    "component": "div",
                    "props": {"class": "position-relative"},
                    "content": [
                        {
                            "component": "VImg",
                            "props": {
                                "src": poster,
                                "lazy-src": DEFAULT_POSTER,
                                "aspect-ratio": "2/3",
                                "cover": True,
                                "class": "rounded-t",
                                "transition": True,
                            },
                        },
                        *poster_overlay,
                        *([{
                            "component": "span",
                            "props": {
                                "class": "position-absolute top-0 left-0 ma-1 rounded pa-1 text-caption text-white",
                                "style": "background: rgba(0, 0, 0, 0.65);",
                            },
                            "text": vote_text,
                        }] if vote_text else []),
                        {
                            "component": "span",
                            "props": {
                                "class": f"position-absolute top-0 right-0 ma-1 bg-{status_color} rounded pa-1 text-caption text-white",
                            },
                            "text": status_cn,
                        },
                    ],
                },
                # 信息区：片名两行截断 + 年份 · 上映日期
                {
                    "component": "div",
                    "props": {"class": "pa-2 flex-grow-1"},
                    "content": [
                        {
                            "component": "a",
                            "props": {
                                "href": f"{link}",
                                "target": "_blank",
                                "class": "text-body-2 font-weight-medium text-decoration-none",
                                "style": "color: inherit; display: -webkit-box; "
                                         "-webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;",
                            },
                            "text": title,
                        },
                        {
                            "component": "div",
                            "props": {"class": "text-caption text-grey-darken-1 mt-1"},
                            "text": f"{year or '-'} · {record.get('release_date') or '-'}",
                        },
                    ],
                },
                # 底部操作按钮：等宽 flex 行，按钮通过 flex-grow-1 自动均分；
                # 容器加 flex-shrink-0 防止上方长片名/长简介把按钮区挤出可视范围
                {
                    "component": "div",
                    "props": {"class": "d-flex w-100 mt-auto flex-shrink-0"},
                    "content": action_buttons,
                },
            ],
        }

    def __get_action_buttons_content(self, key: str, record: dict) -> List[dict]:
        """按记录状态生成操作按钮

        设计要点：
        - "删除"在三个按钮里语义最弱，因此压成图标按钮（垃圾桶图标），保证
          在窄卡片宽度下订阅/忽略两个文字按钮也能完整显示不被截断；
        - 文字按钮加 `text-no-wrap` 强制单行，配合 `flex-grow-1` 自动均分；
        - 按钮容器固定 `flex-shrink-0`，不会被上方海报/信息区挤压消失。
        """
        status = record.get("status") or STATUS_PENDING

        def _text_btn(api: str, text: str, color_class: str) -> dict:
            return {
                "component": "VBtn",
                "props": {
                    "class": f"{color_class} flex-grow-1 flex-shrink-1 text-no-wrap",
                    "variant": "tonal",
                    "size": "small",
                    "density": "comfortable",
                    "style": "min-width: 0;",
                },
                "events": {
                    "click": {
                        "api": f"plugin/CollectionMissing/{api}",
                        "method": "get",
                        "params": {"key": f"{key}", "apikey": settings.API_TOKEN},
                    }
                },
                "text": text,
            }

        def _icon_btn(api: str, icon: str, color_class: str, title: str) -> dict:
            """图标按钮：用于"删除"等次要动作占位，悬浮提示靠原生 title 属性"""
            return {
                "component": "VBtn",
                "props": {
                    "class": f"{color_class} flex-shrink-0",
                    "variant": "tonal",
                    "size": "small",
                    "density": "comfortable",
                    "icon": True,
                    "title": title,
                    "style": "min-width: 0;",
                },
                "events": {
                    "click": {
                        "api": f"plugin/CollectionMissing/{api}",
                        "method": "get",
                        "params": {"key": f"{key}", "apikey": settings.API_TOKEN},
                    }
                },
                "content": [
                    {
                        "component": "VIcon",
                        "props": {"size": "small"},
                        "text": icon,
                    }
                ],
            }

        if status == STATUS_PENDING:
            return [
                _text_btn("subscribe", "订阅", "text-primary"),
                _text_btn("ignore", "忽略", "text-warning"),
                _icon_btn("delete", "mdi-delete-outline", "text-error", "删除"),
            ]
        if status == STATUS_SUBSCRIBED:
            return [
                _text_btn("ignore", "忽略", "text-warning"),
                _icon_btn("delete", "mdi-delete-outline", "text-error", "删除"),
            ]
        if status == STATUS_IGNORED:
            return [
                _text_btn("restore", "恢复", "text-success"),
                _icon_btn("delete", "mdi-delete-outline", "text-error", "删除"),
            ]
        return [
            _text_btn("subscribe", "重试", "text-primary"),
            _icon_btn("delete", "mdi-delete-outline", "text-error", "删除"),
        ]

    def stop_service(self):
        """停止插件，清理后台任务"""
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._event.set()
                    self._scheduler.shutdown()
                    self._event.clear()
                self._scheduler = None
        except Exception as e:
            logger.error(f"合集查缺停止服务异常: {e}")
