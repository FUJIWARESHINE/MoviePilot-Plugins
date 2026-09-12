"""媒体库缺失明细订阅（V3）合同与行为回归测试。

覆盖三块内容：
1. V3 合同适配：稳定 SDK 导入、统一媒体身份、API 声明。
2. 存量数据迁移的幂等性。
3. 详情页渲染（缺失季集明细下拉 / 电影合集视图）与扫描规则（无上映日期排除）。
"""

import json
import sys
import threading
import types
from pathlib import Path

import pytest


def _plugin_module():
    """返回已加载的插件模块。"""
    return sys.modules["app.plugins.mediamissingsubscribe_me"]


def _private(instance, name: str):
    """按 Python 名称改写规则取出私有方法。"""
    return getattr(instance, f"_MediaMissingSubscribe_me__{name}")


def _find(node, component: str, out=None):
    """递归查找页面配置中指定 component 的节点。"""
    if out is None:
        out = []
    if isinstance(node, dict):
        if node.get("component") == component:
            out.append(node)
        for value in node.values():
            _find(value, component, out)
    elif isinstance(node, list):
        for value in node:
            _find(value, component, out)
    return out


# ================================================================
# 1. V3 合同适配
# ================================================================

def test_source_uses_stable_v3_imports(plugin_class):
    """插件不得再使用 V2 的旧导入路径，必须走 app.sdk / app.db.oper。"""
    source = Path(sys.modules["app.plugins.mediamissingsubscribe_me"].__file__).read_text(
        encoding="utf-8"
    )
    for legacy in (
        "from app.core.config",
        "from app.core.event",
        "from app.log import",
        "from app.helper.mediaserver",
        "from app.db.subscribe_oper",
        "from app.utils",
    ):
        assert legacy not in source, f"仍存在 V2 旧导入：{legacy}"

    for stable in (
        "from app.sdk.config import settings",
        "from app.sdk.events import eventmanager, Event",
        "from app.sdk.logging import logger",
        "from app.sdk.services import MediaServerHelper",
        "from app.db.oper.subscribe import SubscribeOper",
        "from app.schemas.types import EventType, MediaSource, MediaType",
    ):
        assert stable in source, f"缺少 V3 稳定导入：{stable}"


def test_thread_event_is_not_shadowed_by_host_event(plugin_class):
    """回归：threading.Event 曾被宿主 Event 遮蔽，导致插件在导入期崩溃。

    宿主 Event 的 ``event_type`` 是必填位置参数，插件类体里的 ``_event = Event()``
    一旦解析到宿主对象就会抛 TypeError；异常发生在模块导入阶段，宿主 loader 会
    直接放弃加载整个插件，表现为「安装成功但插件列表里看不到」。此处同时锁定两件
    事：``_event`` 必须来自 threading，且宿主 Event 确实强制要求 event_type——即
    conftest 的 stub 没有被重新放宽，否则同类问题会再次逃过测试。
    """
    host_event = sys.modules["app.sdk.events"].Event

    assert isinstance(plugin_class._event, threading.Event)
    assert not isinstance(plugin_class._event, host_event)

    with pytest.raises(TypeError):
        host_event()


def test_api_declares_bear_auth_and_response_model(plugin_instance):
    """详情页动作端点必须声明 auth=bear 与 response_model。"""
    apis = plugin_instance.get_api()
    assert len(apis) == 13
    for entry in apis:
        assert entry["methods"] == ["GET"]
        assert entry["path"].startswith("/")
        assert entry["auth"] == "bear"
        assert entry["response_model"] is not None


def test_apikey_is_optional_compat_parameter(plugin_instance):
    """apikey 未传时放行（走 bearer），显式传入时必须是宿主 API_TOKEN。"""
    check = _private(plugin_instance, "check_apikey")
    assert check(None) is True
    assert check("test-token") is True
    assert check("wrong-token") is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (123, "123"),
        ("123", "123"),
        (" 45 ", "45"),
        (0, None),
        ("0", None),
        ("", None),
        (None, None),
    ],
)
def test_tmdb_media_id_normalization(raw, expected):
    """media_id 必须是规范的非空字符串，空白与 "0" 都不是有效身份。"""
    assert _plugin_module().tmdb_media_id(raw) == expected


# ================================================================
# 2. 存量数据迁移
# ================================================================

def test_history_identity_migration_is_idempotent(plugin_instance, media_source):
    """剧集与电影合集的存量记录都要补齐统一身份，且可重复执行。"""
    instance = plugin_instance
    instance.save_data("history", {"details": {
        "tv-key": {
            "exist_status": "存在缺失",
            "tv_no_exist_info": {"title": "某剧", "tmdbid": 123},
        },
        "tv-key-no-id": {
            "exist_status": "存在缺失",
            "tv_no_exist_info": {"title": "无身份剧"},
        },
    }})
    instance.save_data("movie_history", {"details": {
        "m-key": {"title": "某电影", "tmdb_id": 646},
        "m-key-no-id": {"title": "无身份电影", "tmdb_id": "0"},
    }})

    migrate = _private(instance, "migrate_history_identity")
    assert migrate() == 2

    tv_info = instance.get_data("history")["details"]["tv-key"]["tv_no_exist_info"]
    assert tv_info["media_id"] == "123"
    assert tv_info["media_source"] == media_source.TMDB.value
    assert "media_source" not in instance.get_data("history")["details"]["tv-key-no-id"][
        "tv_no_exist_info"
    ]

    movie = instance.get_data("movie_history")["details"]["m-key"]
    assert movie["media_id"] == "646"
    assert movie["media_source"] == media_source.TMDB.value
    assert "media_id" not in instance.get_data("movie_history")["details"]["m-key-no-id"]

    # 幂等：再次迁移不应产生变更
    assert migrate() == 0


# ================================================================
# 3. 订阅链路身份合同
# ================================================================

def test_movie_subscribe_uses_unified_identity(plugin_instance, media_source):
    """电影订阅必须传 media_source + media_id，不得再传 tmdbid。"""
    instance = plugin_instance
    instance.save_data("movie_history", {"details": {
        "emby:645:646": {
            "server": "emby",
            "collection_id": 645,
            "collection_name": "测试合集",
            "tmdb_id": 646,
            "title": "007：无暇赴死",
            "year": "2021",
            "status": "pending",
        }
    }})

    resp = instance.movie_subscribe("emby:645:646", "test-token")
    assert resp.success is True

    call = instance._subChain.calls[-1]
    assert call["media_source"] == media_source.TMDB
    assert call["media_id"] == "646"
    assert call["mtype"] == sys.modules["app.schemas.types"].MediaType.MOVIE

    record = instance.get_data("movie_history")["details"]["emby:645:646"]
    assert record["media_source"] == media_source.TMDB.value
    assert record["media_id"] == "646"
    assert record["status"] == "subscribed"


def test_tv_subscribe_uses_unified_identity_with_season(plugin_instance, media_source):
    """剧集订阅按季添加，必须传 media_source + media_id + season。"""
    instance = plugin_instance
    unique = "emby_lib-1_item-1_某剧"
    instance.save_data("history", {"details": {
        unique: {
            "exist_status": "存在缺失",
            "tv_no_exist_info": {
                "title": "某剧",
                "year": "2020",
                "tmdbid": 123,
                "path": "/media/lib/tv/某剧 (2020)",
                "season_episode_no_exist_info": {
                    "3": {
                        "season": 3,
                        "episode_no_exist": [5],
                        "episode_total": 10,
                        "episode_total_unfiltered": 12,
                    }
                },
            },
            "skip": False,
            "ignored_seasons": [],
        }
    }})

    resp = instance.add_subscribe_history(unique, "test-token")
    assert resp.success is True

    call = instance._subChain.calls[-1]
    assert call["media_source"] == media_source.TMDB
    assert call["media_id"] == "123"
    assert call["season"] == 3
    assert call["mtype"] == sys.modules["app.schemas.types"].MediaType.TV


# ================================================================
# 4. 媒体服务器访问：链路与原生接口回退
# ================================================================

class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeInstance:
    """最小 Emby / Jellyfin 适配器替身，按 URL 关键字返回固定响应。"""

    def __init__(self, routes=None, libraries=None):
        self.user = "user-1"
        self.queries: list[str] = []
        self._routes = routes or []
        self._libraries = libraries if libraries is not None else [
            types.SimpleNamespace(id="lib-1", name="剧集库")
        ]

    def is_inactive(self):
        return False

    def get_librarys(self):
        return self._libraries

    def get_data(self, url=None, **kwargs):
        self.queries.append(url or "")
        for token, payload in self._routes:
            if token in (url or ""):
                return _FakeResponse(payload)
        return _FakeResponse({"Items": []})


def _service(instance):
    return types.SimpleNamespace(type="emby", instance=instance)


def test_tv_scan_reads_unified_identity_from_chain(plugin_instance, media_source):
    """V3 条目用 media_source/media_id 表达身份，插件必须据此识别 TMDB ID。"""
    instance = plugin_instance
    instance._whitelist_media_servers = []
    instance._whitelist_librarys = []
    instance._only_season_exist = True
    instance._only_aired = True
    instance._auto_skip_finished = False
    instance._include_s00_season = False
    instance._no_exist_action = _plugin_module().NoExistAction.ONLY_HISTORY.value
    instance._clearflag = False
    instance._msHelper = sys.modules["app.sdk.services"].MediaServerHelper(
        {"emby": _service(_FakeInstance())}
    )

    _private(instance, "get_mediaserver_tv_info")()

    details = instance.get_data("history")["details"]
    assert len(details) == 1
    tv_info = next(iter(details.values()))["tv_no_exist_info"]
    assert tv_info["tmdbid"] == 123
    assert tv_info["media_id"] == "123"
    assert tv_info["media_source"] == media_source.TMDB.value
    assert tv_info["title"] == "某剧"


def test_tv_scan_falls_back_to_native_api(plugin_instance):
    """宿主未提供 MediaServerChain 枚举能力时，回退原生接口仍能取到条目与季集。"""
    instance = plugin_instance
    instance._msChain_ready = False
    instance._msChain = types.SimpleNamespace()

    fake = _FakeInstance(routes=[
        ("IncludeItemTypes=Series", {"Items": [{
            "Id": "item-1",
            "Type": "Series",
            "Name": "某剧",
            "OriginalTitle": "Some Show",
            "ProductionYear": 2020,
            "Path": "/media/lib/tv/某剧 (2020)",
            "ParentId": "lib-1",
            "ProviderIds": {"Tmdb": "123"},
        }]}),
        ("IncludeItemTypes=Episode", {"Items": [
            {"ParentIndexNumber": 3, "IndexNumber": 1},
            {"ParentIndexNumber": 3, "IndexNumber": 2},
            {"ParentIndexNumber": 3, "IndexNumber": 5},
            {"ParentIndexNumber": 0, "IndexNumber": 9},
        ]}),
    ])
    service = _service(fake)

    libraries = _private(instance, "get_server_libraries")("emby", service)
    assert [(getattr(lib, "id"), getattr(lib, "name")) for lib in libraries] == [
        ("lib-1", "剧集库")
    ]

    items = _private(instance, "get_server_library_items")("emby", service, "lib-1")
    assert len(items) == 1
    assert items[0]["item_id"] == "item-1"
    assert items[0]["tmdbid"] == 123
    assert items[0]["item_type"] == "Series"

    seasoninfo = _private(instance, "get_server_seasoninfo")("emby", service, "item-1")
    assert seasoninfo == {3: [1, 2, 5], 0: [9]}


def test_tv_scan_falls_back_when_chain_raises(plugin_instance):
    """链路调用异常时不应中断扫描，应回退原生接口。"""
    instance = plugin_instance

    class _BrokenChain:
        def librarys(self, server):
            raise RuntimeError("chain broken")

        def items(self, server, library_id):
            raise RuntimeError("chain broken")

        def episodes(self, server, item_id):
            raise RuntimeError("chain broken")

    instance._msChain_ready = True
    instance._msChain = _BrokenChain()

    fake = _FakeInstance(routes=[
        ("IncludeItemTypes=Episode", {"Items": [{"ParentIndexNumber": 1, "IndexNumber": 1}]}),
    ])
    service = _service(fake)

    libraries = _private(instance, "get_server_libraries")("emby", service)
    assert len(libraries) == 1
    seasoninfo = _private(instance, "get_server_seasoninfo")("emby", service, "item-1")
    assert seasoninfo == {1: [1]}


# ================================================================
# 5. 电影合集扫描规则
# ================================================================

def _movie(tmdb_id, title, release_date, year=2021):
    return types.SimpleNamespace(
        tmdb_id=tmdb_id,
        title=title,
        year=year,
        poster_path="/poster.jpg",
        release_date=release_date,
        vote_average=7.0,
        overview="",
    )


def _movie_scan_fixture(instance, movies):
    """装配一次电影合集扫描所需的最小环境。"""
    instance._enable_movie = True
    instance._movie_notify = False
    instance._movie_libraries = []
    instance._whitelist_media_servers = []
    instance._tmdbChain.tmdb_collection = lambda collection_id: movies

    fake = _FakeInstance(routes=[
        ("IncludeItemTypes=BoxSet", {"Items": [{
            "Id": "bs-1",
            "Name": "测试合集",
            "ProviderIds": {"Tmdb": "645"},
        }]}),
        ("IncludeItemTypes=Movie", {"Items": [{"ProviderIds": {"Tmdb": "646"}}]}),
    ])
    instance._msHelper = sys.modules["app.sdk.services"].MediaServerHelper(
        {"emby": _service(fake)}
    )
    return fake


MOVIES = [
    _movie(646, "已在库电影", "2019-01-01"),
    _movie(647, "无上映日期电影", ""),
    _movie(648, "有上映日期电影", "2021-09-29"),
    _movie(649, "未来上映电影", "2099-01-01"),
]


def test_movie_scan_excludes_movies_without_release_date(plugin_instance):
    """需求：合集中没有上映日期的电影必须被排除，不进入清单。"""
    instance = plugin_instance
    instance._movie_skip_unreleased = False
    _movie_scan_fixture(instance, MOVIES)

    _private(instance, "get_mediaserver_movie_info")()

    details = instance.get_data("movie_history")["details"]
    tmdb_ids = {record["tmdb_id"] for record in details.values()}

    assert 647 not in tmdb_ids, "无上映日期的电影不应被收录"
    assert 646 not in tmdb_ids, "已在库的电影不应被收录"
    assert {648, 649} <= tmdb_ids

    record = details["emby:645:648"]
    assert record["status"] == "pending"
    assert record["release_date"] == "2021-09-29"
    assert record["collection_name"] == "测试合集"
    assert record["media_source"] == "themoviedb"
    assert record["media_id"] == "648"


def test_movie_scan_can_skip_unreleased_movies(plugin_instance):
    """可选开关：开启后跳过上映日期晚于今天的电影。"""
    instance = plugin_instance
    instance._movie_skip_unreleased = True
    _movie_scan_fixture(instance, MOVIES)

    _private(instance, "get_mediaserver_movie_info")()

    details = instance.get_data("movie_history")["details"]
    assert set(details.keys()) == {"emby:645:648"}


# ================================================================
# 6. 详情页渲染
# ================================================================

def test_tv_view_renders_missing_season_episode_detail(plugin_instance):
    """剧集卡片必须提供缺失明细下拉，逐季列出缺失的季与集。"""
    instance = plugin_instance
    instance._current_view = _plugin_module().PageViewType.TV.value
    instance.save_data("history", {"details": {
        "emby_lib-1_item-1_某剧": {
            "exist_status": "存在缺失",
            "tv_no_exist_info": {
                "title": "某剧",
                "year": "2020",
                "tmdbid": 123,
                "poster_path": "",
                "vote_average": 7.7,
                "last_air_date": "2024-01-01",
                "status_cn": "播出中",
                "season_episode_no_exist_info": {
                    "3": {"season": 3, "episode_no_exist": [5, 2, 7],
                          "episode_total": 10, "episode_total_unfiltered": 12},
                    "1": {"season": 1, "episode_no_exist": [],
                          "episode_total": 12, "episode_total_unfiltered": 12},
                },
            },
            "last_check": "09-12 10:00",
            "last_check_full": "2026-09-12 10:00:00",
            "last_status_change": "2026-09-12 10:00:00",
            "skip": False,
            "ignored_seasons": [],
        }
    }})

    page = instance.get_page()
    raw = json.dumps(page, ensure_ascii=False)

    assert "缺失明细" in raw
    assert "第 3 季：缺失 E2、E5、E7" in raw
    assert "第 1 季：整季缺失" in raw

    panels = _find(page, "VExpansionPanels")
    assert len(panels) == 1
    # 默认收起，不改变卡片原有排版
    assert "modelValue" not in panels[0].get("props", {})
    assert _find(page, "VBtnToggle"), "卡片操作按钮不应丢失"


def test_movie_view_groups_records_by_collection(plugin_instance):
    """电影合集视图按合集分组，并提供视图切换与批量按钮。"""
    instance = plugin_instance
    instance._current_view = _plugin_module().PageViewType.MOVIE.value
    instance.save_data("movie_history", {
        "last_scan": "2026-09-12 10:00:00",
        "details": {
            "emby:645:646": {
                "server": "emby",
                "collection_id": 645,
                "collection_name": "测试合集",
                "tmdb_id": 646,
                "title": "007：无暇赴死",
                "year": "2021",
                "poster_path": "",
                "release_date": "2021-09-29",
                "vote_average": 7.4,
                "status": "pending",
                "last_check": "2026-09-12 10:00:00",
            }
        },
    })

    raw = json.dumps(instance.get_page(), ensure_ascii=False)

    assert "电影合集缺失" in raw, "缺少视图切换按钮"
    assert "测试合集 · 缺失 1 部 · emby" in raw, "缺少按合集分组的标题"
    assert "007：无暇赴死" in raw
    assert "上映日期: 2021-09-29" in raw
    assert "订阅本合集全部" in raw


def test_page_shows_empty_state_without_records(plugin_instance):
    """没有任何记录时给出明确的引导文案。"""
    instance = plugin_instance
    instance.save_data("history", "")
    instance.save_data("movie_history", "")
    raw = json.dumps(instance.get_page(), ensure_ascii=False)
    assert "暂无检查记录" in raw


def test_plugin_metadata_is_v3(plugin_class):
    """V3 专用副本必须是大版本跃迁后的版本号与独立配置前缀。"""
    assert plugin_class.plugin_version == "2.0.4"
    assert plugin_class.plugin_config_prefix == "mediamissingsubscribe_me_"
    assert plugin_class.plugin_name == "媒体库缺失明细订阅"
    assert getattr(plugin_class, "_plugin_id", None) == "MediaMissingSubscribe_me"
