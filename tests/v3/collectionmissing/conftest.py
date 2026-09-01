"""为插件测试准备宿主依赖并加载插件源码。

在 MoviePilot V3 宿主虚拟环境中运行时直接使用真实的 ``app`` 包；否则注入按 V3
真实签名构造的最小 stub，使测试不依赖宿主安装、公网或数据库。

stub 中的 ``SubscribeOper.exists``、``SubscribeChain.add`` 和
``MediaChain.recognize_media`` 都严格按 V3 签名定义，仍使用旧 ``tmdbid=`` 写法
的调用会直接抛 ``TypeError``，从而把合同适配固化成可回归的断言。
"""

import importlib.util
import sys
import types
from enum import Enum
from pathlib import Path
from typing import Any, Generic, Optional, TypeVar

import pytest

# repo/tests/v3/collectionmissing/conftest.py -> repo
REPO_ROOT = Path(__file__).resolve().parents[3]
PLUGIN_INIT = REPO_ROOT / "plugins.v3/collectionmissing/__init__.py"

DataT = TypeVar("DataT")


def _module(name: str) -> types.ModuleType:
    """注册并返回占位模块，供 stub 组装成 app.* 包结构。"""
    module = types.ModuleType(name)
    sys.modules[name] = module
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
            self.last_call: dict[str, Any] = {}

        def recognize_media(self, meta=None, mtype=None, media_source=None, media_id=None,
                            episode_group=None, cache=True, share_meta=None, music_type=None):
            if media_source is None or media_id is None:
                raise TypeError("V3 要求 media_source 与 media_id 成对提供")
            assert isinstance(media_id, str), f"media_id 必须是规范字符串，收到 {type(media_id)}"
            self.last_call = {
                "mtype": mtype,
                "media_source": media_source,
                "media_id": media_id,
            }
            return None

    app_chain_media.MediaChain = MediaChain

    app_chain_subscribe = _module("app.chain.subscribe")

    class SubscribeChain:
        """订阅链：不再接受 tmdbid，改为 media_source 与 media_id。"""

        def __init__(self):
            self.last_call: dict[str, Any] = {}

        def add(self, title, year, mtype=None, episode_group=None, season=None, channel=None,
                source=None, userid=None, username=None, message=True, exist_ok=False,
                media_source=None, media_id=None, **kwargs):
            assert "tmdbid" not in kwargs, "V3 不再支持 tmdbid，必须传 media_source + media_id"
            if media_source is None or media_id is None:
                raise TypeError("V3 要求 media_source 与 media_id 成对提供")
            assert isinstance(media_id, str), f"media_id 必须是规范字符串，收到 {type(media_id)}"
            self.last_call = {
                "title": title,
                "year": year,
                "mtype": mtype,
                "media_source": media_source,
                "media_id": media_id,
                "username": username,
            }
            return 1001, ""

    app_chain_subscribe.SubscribeChain = SubscribeChain

    app_chain_tmdb = _module("app.chain.tmdb")

    class TmdbChain:
        def tmdb_collection(self, collection_id: int):
            return None

    app_chain_tmdb.TmdbChain = TmdbChain

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
            return url

    app_sdk_config.settings = _Settings()

    app_sdk_events = _module("app.sdk.events")

    class Event:
        def __init__(self, etype=None):
            self.event_data: dict = {}

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
        def get_services(self, name_filters=None):
            return {}

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
        "app.plugins.collectionmissing", PLUGIN_INIT
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["app.plugins.collectionmissing"] = module
    spec.loader.exec_module(module)
    return module


plugin = _load_plugin_module()


@pytest.fixture()
def plugin_class():
    """返回插件主类。"""
    return plugin.CollectionMissing


@pytest.fixture()
def plugin_instance(plugin_class):
    """返回一个已装配 stub 链路的插件实例，供订阅与迁移用例使用。"""
    instance = plugin_class.__new__(plugin_class)
    instance._subscribe_oper = sys.modules["app.db.oper.subscribe"].SubscribeOper()
    instance._subscribe_chain = sys.modules["app.chain.subscribe"].SubscribeChain()
    instance._media_chain = sys.modules["app.chain.media"].MediaChain()
    instance._store = {}
    return instance
