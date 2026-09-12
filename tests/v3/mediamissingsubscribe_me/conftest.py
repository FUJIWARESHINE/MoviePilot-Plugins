"""为 V3 插件测试准备宿主依赖并加载插件源码。

在 MoviePilot V3 宿主虚拟环境中运行时直接使用真实的 ``app`` 包；否则注入按 V3
真实签名构造的最小 stub，使测试不依赖宿主安装、公网或数据库。

stub 中的 ``SubscribeOper.exists``、``SubscribeChain.add`` 和
``MediaChain.recognize_media`` 都严格按 V3 签名定义：仍使用旧 ``tmdbid=`` 写法的
调用会直接抛 ``TypeError``，从而把「统一媒体身份」的适配固化成可回归的断言。
"""

import importlib.util
import sys
import types
from enum import Enum
from pathlib import Path
from typing import Any, Generic, Optional, TypeVar

import pytest

# tests/v3/mediamissingsubscribe_me/conftest.py -> 项目根
REPO_ROOT = Path(__file__).resolve().parents[3]
PLUGIN_INIT = REPO_ROOT / "plugins.v3/mediamissingsubscribe_me/__init__.py"

DataT = TypeVar("DataT")


# 本 conftest 装配的 app.* stub 模块，用于在用例前重新激活（见 _reactivate_host_stubs）
_OWNED_MODULES: list[tuple[str, types.ModuleType]] = []


def _module(name: str) -> types.ModuleType:
    """注册并返回占位模块，供 stub 组装成 app.* 包结构。"""
    module = types.ModuleType(name)
    sys.modules[name] = module
    _OWNED_MODULES.append((name, module))
    return module


def _build_response_model():
    """构造与 V3 app.schemas.Response 一致的泛型统一响应模型。"""
    from pydantic import BaseModel, ConfigDict

    class Response(BaseModel, Generic[DataT]):
        model_config = ConfigDict(extra="forbid")
        success: bool
        message: str = ""
        data: Optional[DataT] = None

    return Response


def _install_stubs() -> None:
    """注入最小 app.* 宿主依赖。"""
    Response = _build_response_model()

    app = _module("app")

    app_schemas = _module("app.schemas")
    app_schemas.Response = Response

    class NotificationType(str, Enum):
        SiteMessage = "站点"

    app_schemas.NotificationType = NotificationType
    app_schemas.__all__ = ["Response", "NotificationType"]

    app_schemas_types = _module("app.schemas.types")

    class MediaSource(str, Enum):
        TMDB = "themoviedb"
        Douban = "douban"

    class MediaType(str, Enum):
        MOVIE = "电影"
        TV = "电视剧"

    class EventType(str, Enum):
        PluginAction = "plugin_action"

    app_schemas_types.MediaSource = MediaSource
    app_schemas_types.MediaType = MediaType
    app_schemas_types.EventType = EventType

    _module("app.chain")
    app_chain_media = _module("app.chain.media")

    class MediaChain:
        """媒体识别链：只接受成对的 media_source 与 media_id。"""

        def __init__(self):
            self.calls: list[dict[str, Any]] = []

        def recognize_media(self, meta=None, mtype=None, media_source=None, media_id=None,
                            episode_group=None, cache=True, share_meta=None, music_type=None):
            if media_source is None or media_id is None:
                raise TypeError("V3 要求 media_source 与 media_id 成对提供")
            assert isinstance(media_id, str), f"media_id 必须是规范字符串，收到 {type(media_id)}"
            self.calls.append({
                "mtype": mtype,
                "media_source": media_source,
                "media_id": media_id,
            })
            return None

    app_chain_media.MediaChain = MediaChain

    app_chain_subscribe = _module("app.chain.subscribe")

    class SubscribeChain:
        """订阅链：不再接受 tmdbid，改为 media_source 与 media_id。"""

        def __init__(self):
            self.calls: list[dict[str, Any]] = []

        def add(self, title, year, mtype=None, episode_group=None, season=None, channel=None,
                source=None, userid=None, username=None, message=True, exist_ok=False,
                media_source=None, media_id=None, **kwargs):
            assert "tmdbid" not in kwargs, "V3 不再支持 tmdbid，必须传 media_source + media_id"
            if media_source is None or media_id is None:
                raise TypeError("V3 要求 media_source 与 media_id 成对提供")
            assert isinstance(media_id, str), f"media_id 必须是规范字符串，收到 {type(media_id)}"
            self.calls.append({
                "title": title,
                "year": year,
                "mtype": mtype,
                "season": season,
                "media_source": media_source,
                "media_id": media_id,
                "username": username,
            })
            return 1001, ""

    app_chain_subscribe.SubscribeChain = SubscribeChain

    app_chain_tmdb = _module("app.chain.tmdb")

    class TmdbChain:
        def __init__(self):
            self.collection_calls: list[int] = []

        def tmdb_collection(self, collection_id: int):
            self.collection_calls.append(collection_id)
            return []

        def tmdb_episodes(self, tmdbid: int, season: int, episode_group=None):
            return []

    app_chain_tmdb.TmdbChain = TmdbChain

    app_chain_mediaserver = _module("app.chain.mediaserver")

    class MediaServerChain:
        """媒体服务器链：V3 中用于枚举媒体库、条目与剧集季集。"""

        def __init__(self):
            self.ready = True

        def librarys(self, server: str):
            return [{"id": "lib-1", "name": "剧集库"}]

        def items(self, server: str, library_id: str):
            return [{
                "item_id": "item-1",
                "item_type": "Series",
                "title": "某剧",
                "original_title": "Some Show",
                "year": 2020,
                "path": "/media/lib/tv/某剧 (2020)",
                "library": library_id,
                "media_source": MediaSource.TMDB,
                "media_id": "123",
            }]

        def episodes(self, server: str, item_id: str):
            return [{"season": 3, "episodes": [1, 2, 5]}]

    app_chain_mediaserver.MediaServerChain = MediaServerChain

    _module("app.db")
    _module("app.db.oper")
    app_db_oper_subscribe = _module("app.db.oper.subscribe")

    class SubscribeOper:
        """订阅数据操作：首两个参数固定为 media_source 与 media_id。"""

        def __init__(self):
            self.calls: list[tuple[Any, str]] = []

        def exists(self, media_source, media_id, season=None, episode_group=None,
                   music_type=None):
            if media_source is None or media_id is None:
                raise TypeError("V3 要求 media_source 与 media_id 成对提供")
            assert isinstance(media_id, str), f"media_id 必须是规范字符串，收到 {type(media_id)}"
            self.calls.append((media_source, media_id))
            return False

    app_db_oper_subscribe.SubscribeOper = SubscribeOper

    app_plugins = _module("app.plugins")

    class _PluginBase:
        """最小插件基类，插件数据保存在内存中。"""

        plugin_name = "stub"

        def __init__(self):
            self._store: dict[str, Any] = {}

        def update_config(self, config: dict) -> bool:
            return True

        def get_config(self) -> dict:
            return {}

        def save_data(self, key: str, value: Any) -> None:
            self._store[key] = value

        def get_data(self, key: str):
            return self._store.get(key)

        def del_data(self, key: str) -> None:
            self._store.pop(key, None)

        def get_data_path(self) -> Path:
            return REPO_ROOT

        def post_message(self, **kwargs) -> None:
            return None

        def stop_service(self) -> None:
            return None

    app_plugins._PluginBase = _PluginBase

    _module("app.sdk")
    app_sdk_config = _module("app.sdk.config")

    class _Settings:
        TZ = "Asia/Shanghai"
        API_TOKEN = "test-token"
        APP_DOMAIN = ""

        def MP_DOMAIN(self, url: str = None):
            return url or ""

    app_sdk_config.settings = _Settings()

    app_sdk_events = _module("app.sdk.events")

    class Event:
        """V3 宿主事件对象：``event_type`` 是必填位置参数。

        签名严格对齐宿主 ``app.runtime.events.Event``。此前 stub 把 event_type
        写成可选（``etype=None``），掩盖了插件里 threading.Event 被宿主 Event 遮蔽
        的问题——插件在导入期构造 ``Event()`` 会抛 TypeError，宿主随即放弃加载。
        改成必填后，这类导入期崩溃会在测试收集阶段直接暴露。
        """

        def __init__(self, event_type, data=None):
            self.event_type = event_type
            self.event_data = data or {}

    class EventManager:
        def register(self, etype):
            def decorator(func):
                return func

            return decorator

    app_sdk_events.Event = Event
    app_sdk_events.EventManager = EventManager
    app_sdk_events.eventmanager = EventManager()

    app_sdk_logging = _module("app.sdk.logging")

    class _Logger:
        def info(self, *args, **kwargs):
            pass

        def debug(self, *args, **kwargs):
            pass

        def warning(self, *args, **kwargs):
            pass

        def error(self, *args, **kwargs):
            pass

    app_sdk_logging.logger = _Logger()

    app_sdk_services = _module("app.sdk.services")

    class MediaServerHelper:
        def __init__(self, services: Optional[dict] = None):
            self._services = services or {}

        def get_services(self, name_filters=None):
            return self._services

    app_sdk_services.MediaServerHelper = MediaServerHelper


def _host_available() -> bool:
    """判断是否运行在已安装 MoviePilot 宿主的环境中。"""
    try:
        return importlib.util.find_spec("app.plugins") is not None
    except (ImportError, ValueError):
        return False


def _load_plugin_module():
    """按生产一致的 app.plugins.<plugin_id> 路径加载插件源码。"""
    if not _host_available():
        _install_stubs()
    spec = importlib.util.spec_from_file_location(
        "app.plugins.mediamissingsubscribe_me", PLUGIN_INIT
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["app.plugins.mediamissingsubscribe_me"] = module
    spec.loader.exec_module(module)
    return module


plugin = _load_plugin_module()


@pytest.fixture(autouse=True)
def _reactivate_host_stubs():
    """每个用例前把本测试包装配的 app.* stub 重新挂回 sys.modules。

    tests/v3 下每个插件测试包都会注入同名的 app.* stub，后导入的 conftest 会覆盖
    先导入的，使先导入的包在取 fixture 时拿到别人的 stub（collectionmissing 因此
    会拿到没有 last_call 的 SubscribeChain）。这里只重新挂回本包首次装配的同名
    模块对象，类身份保持不变，不影响插件模块里已捕获的枚举与类型引用。
    """
    if not _host_available():
        for name, module in _OWNED_MODULES:
            sys.modules[name] = module


@pytest.fixture()
def plugin_class():
    """返回插件主类。"""
    return plugin.MediaMissingSubscribe_me


@pytest.fixture()
def plugin_instance(plugin_class):
    """返回一个已装配 stub 链路的插件实例，供订阅、迁移与页面用例使用。"""
    instance = plugin_class.__new__(plugin_class)
    instance._subChain = sys.modules["app.chain.subscribe"].SubscribeChain()
    instance._subOper = sys.modules["app.db.oper.subscribe"].SubscribeOper()
    instance._mediaChain = sys.modules["app.chain.media"].MediaChain()
    instance._tmdbChain = sys.modules["app.chain.tmdb"].TmdbChain()
    instance._msChain = sys.modules["app.chain.mediaserver"].MediaServerChain()
    instance._store = {}
    return instance


@pytest.fixture()
def media_source():
    """返回 stub 中的 MediaSource 枚举。"""
    return sys.modules["app.schemas.types"].MediaSource
