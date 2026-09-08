"""为 V2 插件测试准备宿主依赖并加载插件源码。

在 MoviePilot V2 宿主虚拟环境中运行时直接使用真实的 ``app`` 包；否则注入按 V2
真实签名构造的最小 stub，使测试不依赖宿主安装、公网或数据库。

stub 中的 ``SubscribeOper.exists(tmdbid=...)``、``SubscribeChain.add(tmdbid=...)``
和 ``MediaChain.recognize_media(tmdbid=...)`` 都严格按 V2 旧签名定义，仍使用
V3 的 ``media_source`` / ``media_id`` 写法会直接抛 ``TypeError``，从而把两代合同
差异固化成可回归的断言，避免改动从 V3 同步过来时被误带成新签名。

模块名与 V3 测试区分开（``collectionmissing_v2``），使两代插件可以在同一仓库中
共存而不互相覆盖。
"""

import importlib.util
import sys
import types
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import pytest

# repo/tests/v2/collectionmissing/conftest.py -> repo
REPO_ROOT = Path(__file__).resolve().parents[3]
PLUGIN_INIT = REPO_ROOT / "plugins.v2/collectionmissing/__init__.py"

MODULE_NAME = "app.plugins.collectionmissing_v2"

# 本代注入的 stub 类（V2 旧签名）。V3 测试与 V2 测试共用部分模块路径
# （如 app.chain.subscribe），同一会话内后加载的 conftest 会覆盖 sys.modules，
# 因此这里持有本代 stub 的引用，避免 fixture 取到另一代的实现。
_STUB_CLASSES: dict[str, Any] = {}


def _module(name: str) -> types.ModuleType:
    """注册并返回占位模块，供 stub 组装成 app.* 包结构。"""
    module = types.ModuleType(name)
    sys.modules[name] = module
    return module


class FakeResponse:
    """模拟 requests 响应对象，只需提供 .json()。"""

    def __init__(self, payload: dict):
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def _install_stubs() -> None:
    """注入最小 app.* 宿主依赖（V2 旧路径与旧签名）。"""

    @dataclass
    class Response:
        """与 V2 app.schemas.Response 等价的最小统一响应模型。"""

        success: bool
        message: str = ""
        data: Optional[Any] = None

    app = _module("app")

    app_schemas = _module("app.schemas")
    app_schemas.Response = Response

    class NotificationType(str, Enum):
        SiteMessage = "站点"

    app_schemas.NotificationType = NotificationType
    app_schemas.__all__ = ["Response", "NotificationType"]

    app_schemas_types = _module("app.schemas.types")

    class MediaType(str, Enum):
        MOVIE = "电影"
        TV = "电视剧"

    class EventType(str, Enum):
        PluginAction = "plugin_action"

    app_schemas_types.MediaType = MediaType
    app_schemas_types.EventType = EventType

    _module("app.chain")
    app_chain_media = _module("app.chain.media")

    class MediaChain:
        """媒体识别链：V2 只认 tmdbid 关键字。"""

        def __init__(self):
            self.last_call: dict[str, Any] = {}

        def recognize_media(self, meta=None, mtype=None, tmdbid=None, **kwargs):
            if "media_source" in kwargs or "media_id" in kwargs:
                raise TypeError("V2 不支持 media_source / media_id，必须使用 tmdbid")
            self.last_call = {"mtype": mtype, "tmdbid": tmdbid}
            return None

    app_chain_media.MediaChain = MediaChain

    app_chain_subscribe = _module("app.chain.subscribe")

    class SubscribeChain:
        """订阅链：V2 使用 tmdbid。"""

        def __init__(self):
            self.last_call: dict[str, Any] = {}

        def add(self, title, year, mtype=None, tmdbid=None, username=None,
                message=True, exist_ok=False, **kwargs):
            if "media_source" in kwargs or "media_id" in kwargs:
                raise TypeError("V2 不支持 media_source / media_id，必须使用 tmdbid")
            self.last_call = {
                "title": title,
                "year": year,
                "mtype": mtype,
                "tmdbid": tmdbid,
                "username": username,
            }
            return 1001, ""

    app_chain_subscribe.SubscribeChain = SubscribeChain

    app_chain_tmdb = _module("app.chain.tmdb")

    class TmdbChain:
        def __init__(self):
            self.collection_result = None

        def tmdb_collection(self, collection_id: int):
            return self.collection_result

    app_chain_tmdb.TmdbChain = TmdbChain

    app_db_subscribe_oper = _module("app.db.subscribe_oper")

    class SubscribeOper:
        """订阅数据操作：V2 使用 tmdbid 关键字。"""

        def __init__(self):
            self.calls: list[int] = []

        def exists(self, tmdbid=None, **kwargs):
            if "media_source" in kwargs or "media_id" in kwargs:
                raise TypeError("V2 不支持 media_source / media_id，必须使用 tmdbid")
            self.calls.append(tmdbid)
            return False

    app_db_subscribe_oper.SubscribeOper = SubscribeOper

    _STUB_CLASSES.update({
        "MediaChain": MediaChain,
        "SubscribeChain": SubscribeChain,
        "TmdbChain": TmdbChain,
        "SubscribeOper": SubscribeOper,
    })

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

    _module("app.core")
    app_core_config = _module("app.core.config")

    class _Settings:
        TZ = "Asia/Shanghai"
        API_TOKEN = "test-token"

        def MP_DOMAIN(self, url: str = None):
            return url

    app_core_config.settings = _Settings()

    app_core_event = _module("app.core.event")

    class Event:
        def __init__(self, etype=None):
            self.event_data: dict = {}

    class EventManager:
        def register(self, etype):
            def decorator(func):
                return func

            return decorator

    app_core_event.Event = Event
    app_core_event.EventManager = EventManager
    app_core_event.eventmanager = EventManager()

    app_log = _module("app.log")

    class _Logger:
        def info(self, *args, **kwargs):
            pass

        def debug(self, *args, **kwargs):
            pass

        def warning(self, *args, **kwargs):
            pass

        def error(self, *args, **kwargs):
            pass

    app_log.logger = _Logger()

    app_helper = _module("app.helper")
    app_helper_mediaserver = _module("app.helper.mediaserver")

    class MediaServerHelper:
        def get_services(self, name_filters=None):
            return {}

    app_helper_mediaserver.MediaServerHelper = MediaServerHelper


def _host_available() -> bool:
    """判断是否运行在已安装 MoviePilot 宿主的环境中。"""
    try:
        return importlib.util.find_spec("app.plugins") is not None
    except (ImportError, ValueError):
        return False


def _load_plugin_module():
    """加载 V2 插件源码。"""
    if not _host_available():
        _install_stubs()
    spec = importlib.util.spec_from_file_location(MODULE_NAME, PLUGIN_INIT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


plugin = _load_plugin_module()


@pytest.fixture()
def plugin_class():
    """返回插件主类。"""
    return plugin.CollectionMissing


def _chain_class(name: str, module_path: str, attr: str):
    """取本代注入的 stub 类；在真实宿主环境中回退到宿主实现。"""
    if name in _STUB_CLASSES:
        return _STUB_CLASSES[name]
    return getattr(importlib.import_module(module_path), attr)


@pytest.fixture()
def plugin_instance(plugin_class):
    """返回一个已装配 stub 链路的插件实例。"""
    instance = plugin_class.__new__(plugin_class)
    instance._subscribe_oper = _chain_class(
        "SubscribeOper", "app.db.subscribe_oper", "SubscribeOper"
    )()
    instance._subscribe_chain = _chain_class(
        "SubscribeChain", "app.chain.subscribe", "SubscribeChain"
    )()
    instance._media_chain = _chain_class(
        "MediaChain", "app.chain.media", "MediaChain"
    )()
    instance._tmdb_chain = _chain_class(
        "TmdbChain", "app.chain.tmdb", "TmdbChain"
    )()
    instance._skip_unreleased = True
    instance._min_vote = 0.0
    instance._store = {}
    return instance
