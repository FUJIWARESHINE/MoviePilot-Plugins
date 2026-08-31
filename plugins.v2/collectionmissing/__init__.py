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
from app.core.config import settings
from app.core.event import eventmanager, Event
from app.db.subscribe_oper import SubscribeOper
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.schemas.types import EventType, MediaType

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

DEFAULT_POSTER = "/assets/no-image-CweBJ8Ee.jpeg"
POSTER_BASE = "https://image.tmdb.org/t/p/w500"


class CollectionMissing(_PluginBase):
    """扫描媒体库中的电影合集（BoxSet），与 TMDB 合集全量片单对比，列出缺失电影供手动确认订阅"""

    # 插件名称
    plugin_name = "电影合集查缺"
    # 插件描述
    plugin_desc = "扫描 Emby/Jellyfin 媒体库中的电影合集，对比 TMDB 合集全量片单，列出缺失电影供手动确认订阅"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/FUJIWARESHINE/MoviePilot-Plugins/main/icons/CollectionMissing.png"
    # 插件版本，必须与 package.v2.json 中保持一致
    plugin_version = "1.0.0"
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

        # 构建媒体库选项（供表单多选）
        if self._mediaservers:
            self._all_libraries = self._build_library_list()

        # 立即清理检查记录
        if self._clear:
            self.save_data("history", {"last_scan": "", "details": {}})
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
            logger.info("已清空电影合集查缺的检查记录")

        self.stop_service()

        if self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            logger.info("电影合集查缺服务启动，立即运行一次")
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
                "summary": "订阅缺失电影",
            },
            {
                "path": "/ignore",
                "endpoint": self.ignore,
                "methods": ["GET"],
                "summary": "忽略缺失电影",
            },
            {
                "path": "/restore",
                "endpoint": self.restore,
                "methods": ["GET"],
                "summary": "恢复缺失电影为待处理",
            },
            {
                "path": "/delete",
                "endpoint": self.delete,
                "methods": ["GET"],
                "summary": "删除检查记录",
            },
            {
                "path": "/subscribe_collection",
                "endpoint": self.subscribe_collection,
                "methods": ["GET"],
                "summary": "订阅某合集全部缺失电影",
            },
            {
                "path": "/ignore_collection",
                "endpoint": self.ignore_collection,
                "methods": ["GET"],
                "summary": "忽略某合集全部缺失电影",
            },
            {
                "path": "/set_filter",
                "endpoint": self.set_filter,
                "methods": ["GET"],
                "summary": "切换页面筛选",
            },
            {
                "path": "/clear",
                "endpoint": self.clear_records,
                "methods": ["GET"],
                "summary": "清空检查记录",
            },
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """定时服务"""
        if self.get_state():
            return [
                {
                    "id": "CollectionMissing",
                    "name": "电影合集查缺",
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
                logger.warning("电影合集查缺：未配置媒体服务器，跳过扫描")
                return

            services = self._mediaserver_helper.get_services(name_filters=self._mediaservers)
            if not services:
                logger.warning("电影合集查缺：获取媒体服务器实例失败，请检查配置")
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
                    logger.warning(f"电影合集查缺：媒体服务器 {server_name} 未连接，跳过")
                    continue
                if service.type not in SUPPORTED_SERVERS:
                    logger.warning(f"电影合集查缺：媒体服务器 {server_name} 类型 {service.type} 不支持，跳过")
                    continue
                try:
                    found, present = self.__scan_server(server_name, service, details)
                    new_found.extend(found)
                    present_keys.update(present)
                except Exception as e:
                    logger.error(f"电影合集查缺：扫描媒体服务器 {server_name} 时出错: {e}")

            # 已补齐自动消单：待处理记录对应的电影已入库则删除
            for key in list(details.keys()):
                record = details[key]
                if record.get("status") == STATUS_PENDING and key in present_keys:
                    logger.info(f"电影合集查缺：{record.get('title')} 已入库，自动移除待处理记录")
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
                    title="【电影合集查缺】",
                    text="\n".join(text_lines),
                )

            if new_found:
                logger.info(f"电影合集查缺：扫描完成，新增 {len(new_found)} 部缺失电影待处理")
            else:
                logger.info("电影合集查缺：扫描完成，无新增缺失")

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

        now = datetime.now()
        for movie in tmdb_movies:
            if not movie or not movie.tmdb_id:
                continue

            # 已在媒体库中
            if movie.tmdb_id in existing_tmdb_ids:
                present_keys.add(f"{server_name}:{collection_id}:{movie.tmdb_id}")
                continue

            # 跳过未上映电影
            if self._skip_unreleased:
                release_date = self.__parse_release_date(movie.release_date)
                if release_date and release_date > now:
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

            unique = f"{server_name}:{collection_id}:{movie.tmdb_id}"
            record = details.get(unique)
            if record:
                # 已有记录：只刷新检查时间，不覆盖用户决策
                record["last_check"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                continue

            details[unique] = {
                "server": server_name,
                "collection_id": collection_id,
                "collection_name": boxset_name,
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
                mtype=MediaType.MOVIE, tmdbid=movie_tmdb_id
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
    # 订阅
    # ================================================================

    def __subscribe_movie(self, record: dict) -> Tuple[bool, str]:
        """按记录订阅缺失电影，返回（是否成功, 消息）"""
        title = record.get("title")
        year = str(record.get("year") or "")
        tmdb_id = record.get("tmdb_id")
        if not title or not tmdb_id:
            return False, "记录信息不完整"

        if self._subscribe_oper.exists(tmdbid=tmdb_id):
            return True, "订阅已存在"

        try:
            sid, msg = self._subscribe_chain.add(
                title=title,
                year=year,
                mtype=MediaType.MOVIE,
                tmdbid=tmdb_id,
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

    def __check_apikey(self, apikey: str) -> bool:
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

    def subscribe(self, key: str, apikey: str):
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

    def ignore(self, key: str, apikey: str):
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

    def restore(self, key: str, apikey: str):
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

    def delete(self, key: str, apikey: str):
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

    def subscribe_collection(self, server: str, collection: str, apikey: str):
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

    def ignore_collection(self, server: str, collection: str, apikey: str):
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

    def set_filter(self, filter: str, apikey: str):
        """切换页面筛选"""
        if not self.__check_apikey(apikey):
            return schemas.Response(success=False, message="API密钥错误")
        if filter not in ("pending", "subscribed", "ignored", "all"):
            return schemas.Response(success=False, message="无效的筛选条件")
        self.save_data("filter", filter)
        return schemas.Response(success=True, message=f"已切换筛选: {filter}")

    def clear_records(self, scope: str, apikey: str):
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
            return schemas.Response(success=True, message="已清空全部记录")
        return schemas.Response(success=False, message="无效的清理范围")

    # ================================================================
    # 辅助方法
    # ================================================================

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

        # 筛选按钮
        filter_buttons = [
            {
                "component": "VBtn",
                "props": {
                    "variant": "tonal",
                    "class": "text-primary" if current_filter == key else "",
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

        page_content = [
            {
                "component": "VCard",
                "props": {"variant": "tonal", "class": "mb-4"},
                "content": [
                    {
                        "component": "VCardText",
                        "props": {"class": "pa-2 d-flex flex-wrap align-center gap-2"},
                        "content": [
                            {
                                "component": "span",
                                "props": {"class": "text-body-2 mr-4"},
                                "text": f"共 {len(details)} 部缺失 · {collections_count} 个合集 · "
                                        f"上次扫描 {history.get('last_scan') or '-'}",
                            },
                            {
                                "component": "VBtnToggle",
                                "props": {"variant": "tonal"},
                                "content": filter_buttons,
                            },
                            {
                                "component": "VBtn",
                                "props": {
                                    "class": "text-error",
                                    "variant": "tonal",
                                    "size": "small",
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
                ],
            },
        ]

        for group in sorted(groups.values(), key=lambda g: (g["name"] or "")):
            server = group["server"]
            collection_id = group["collection_id"]
            records = sorted(group["records"], key=lambda x: x[1].get("title") or "")

            has_pending = any(r.get("status") == STATUS_PENDING for _, r in records)

            batch_buttons = []
            if has_pending:
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

            page_content.append({
                "component": "div",
                "props": {"class": "mb-4"},
                "content": [
                    {
                        "component": "VCardTitle",
                        "props": {"class": "pt-6 pb-2 text-base"},
                        "content": [
                            {
                                "component": "span",
                                "text": f"{group['name']} · 缺失 {len(records)} 部 · {server}",
                            }
                        ],
                    },
                    {
                        "component": "div",
                        "props": {"class": "pb-2"},
                        "content": batch_buttons,
                    },
                    {
                        "component": "div",
                        "props": {"class": "flex flex-row flex-wrap gap-4 items-start"},
                        "content": [self.__get_history_post_content(key, record) for key, record in records],
                    },
                ],
            })

        return page_content

    def __get_history_post_content(self, key: str, record: dict) -> dict:
        """构建单条缺失电影卡片"""
        title = record.get("title") or "未知"
        year = record.get("year") or ""
        tmdb_id = record.get("tmdb_id")
        status = record.get("status") or STATUS_PENDING
        status_cn = STATUS_TEXT.get(status, status)

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

        return {
            "component": "VCard",
            "props": {
                "variant": "tonal",
                "style": "width: 320px; min-height: 240px;",
            },
            "content": [
                {
                    "component": "div",
                    "props": {"class": "flex flex-row"},
                    "content": [
                        {
                            "component": "VImg",
                            "props": {
                                "src": poster,
                                "height": 240,
                                "width": 160,
                                "aspect-ratio": "2/3",
                                "class": "object-cover shadow",
                                "cover": True,
                                "transition": True,
                            },
                        },
                        {
                            "component": "div",
                            "props": {"class": "flex flex-col", "style": "width: 160px;"},
                            "content": [
                                {
                                    "component": "VCardTitle",
                                    "props": {
                                        "class": "pt-4 pl-4 pr-4 text-base",
                                        "style": "word-break: break-word; white-space: normal; line-height: 1.2;",
                                    },
                                    "content": [
                                        {
                                            "component": "a",
                                            "props": {
                                                "href": f"{link}",
                                                "target": "_blank",
                                                "style": "text-decoration: none; color: inherit;",
                                            },
                                            "text": title,
                                        }
                                    ],
                                },
                                {
                                    "component": "VCardText",
                                    "props": {"class": "pa-0 pl-4 pr-4 py-1 whitespace-nowrap"},
                                    "text": f"年份: {year or '-'}",
                                },
                                {
                                    "component": "VCardText",
                                    "props": {"class": "pa-0 pl-4 pr-4 py-1 whitespace-nowrap"},
                                    "text": f"评分: {record.get('vote_average') or '-'}",
                                },
                                {
                                    "component": "VCardText",
                                    "props": {"class": "pa-0 pl-4 pr-4 py-1 whitespace-nowrap"},
                                    "text": f"上映: {record.get('release_date') or '-'}",
                                },
                                {
                                    "component": "VCardText",
                                    "props": {"class": "pa-0 pl-4 pr-4 py-1 whitespace-nowrap"},
                                    "text": f"状态: {status_cn}",
                                },
                                {
                                    "component": "VCardText",
                                    "props": {"class": "pa-0 pl-4 pr-4 py-1 whitespace-nowrap"},
                                    "text": f"检查: {record.get('last_check') or '-'}",
                                },
                            ],
                        },
                    ],
                },
                {
                    "component": "VBtnToggle",
                    "props": {
                        "class": "d-flex mt-auto",
                        "style": "width: 100%; display: flex;",
                        "variant": "tonal",
                        "rounded": "0",
                    },
                    "content": action_buttons,
                },
            ],
        }

    def __get_action_buttons_content(self, key: str, record: dict) -> List[dict]:
        """按记录状态生成操作按钮"""
        status = record.get("status") or STATUS_PENDING

        def _btn(api: str, text: str, color_class: str) -> dict:
            return {
                "component": "VBtn",
                "props": {
                    "class": color_class,
                    "variant": "tonal",
                    "style": "height: 100%; width: 100%; flex: 1;",
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

        if status == STATUS_PENDING:
            return [
                _btn("subscribe", "订阅", "text-primary"),
                _btn("ignore", "忽略", "text-warning"),
                _btn("delete", "删除", "text-error"),
            ]
        if status == STATUS_SUBSCRIBED:
            return [
                _btn("ignore", "忽略", "text-warning"),
                _btn("delete", "删除", "text-error"),
            ]
        if status == STATUS_IGNORED:
            return [
                _btn("restore", "恢复", "text-success"),
                _btn("delete", "删除", "text-error"),
            ]
        return [
            _btn("subscribe", "重试", "text-primary"),
            _btn("delete", "删除", "text-error"),
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
            logger.error(f"电影合集查缺停止服务异常: {e}")
