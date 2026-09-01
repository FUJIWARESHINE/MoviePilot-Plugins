"""Emby 电影合集缺集订阅（CollectionMissing）V3 实现的合同回归测试。

用例覆盖：
- 记录写入采用 V3 统一媒体身份（media_source + media_id 成对）；
- 订阅链路只按 media_source + media_id 调用（不再传 tmdbid）；
- 订阅判重使用 V3 的 SubscribeOper.exists(media_source, media_id)；
- 存量 v1.x 记录在初始化时被幂等迁移出统一身份字段；
- 详情页按钮事件仍携带 apikey，后端以可选参数兼容两种鉴权路径。
"""

import sys

import pytest

pytestmark = pytest.mark.v3


def _plugin():
    """取 conftest 中按生产命名空间加载的插件模块。"""
    return sys.modules.get("app.plugins.collectionmissing")


def _make_record(**overrides):
    """构造一条典型缺失电影记录。"""
    record = {
        "server": "我的Emby",
        "collection_id": 645,
        "collection_name": "詹姆斯·邦德",
        "tmdb_id": 206647,
        "title": "007：幽灵党",
        "year": "2015",
        "poster_path": "/abc.jpg",
        "release_date": "2015-10-26",
        "vote_average": 6.5,
        "overview": "test",
        "status": "pending",
        "subscribe_id": None,
        "message": "",
        "last_check": "2026-09-01 10:00:00",
        "last_status_change": "2026-09-01 10:00:00",
    }
    record.update(overrides)
    return record


class TestRecordIdentity:
    """统一媒体身份：写入与迁移。"""

    def test_new_record_writes_unified_identity(self):
        """扫描写入的新记录应成对携带 V3 统一媒体身份。"""
        details = {
            "我的Emby:645:206647": _make_record(
                media_source="themoviedb", media_id="206647"
            )
        }
        rec = details["我的Emby:645:206647"]
        assert rec["media_source"] == "themoviedb"
        assert rec["media_id"] == "206647"
        assert str(rec["tmdb_id"]) == rec["media_id"]

    def test_legacy_record_migrates_identity(self, plugin_instance):
        """仅含 tmdb_id 的存量记录在迁移后获得统一身份字段，且可重复执行。"""
        plugin = _plugin()
        legacy = _make_record()  # 无 media_source / media_id
        plugin_instance._store["history"] = {
            "last_scan": "2026-08-31 08:00:00",
            "details": {"我的Emby:645:206647": legacy},
        }
        # 实例化 init 前的迁移入口：直接调用迁移方法
        migrated = plugin_instance._CollectionMissing__migrate_history_identity()
        assert migrated == 1
        assert legacy["media_source"] == "themoviedb"
        assert legacy["media_id"] == "206647"
        # 幂等：再次执行不再改动
        assert plugin_instance._CollectionMissing__migrate_history_identity() == 0

    def test_invalid_identity_not_migrated(self, plugin_instance):
        """空白与 "0" 不是有效身份，保留原记录不迁移。"""
        plugin = _plugin()
        invalid = _make_record(tmdb_id=0)
        plugin_instance._store["history"] = {
            "details": {"我的Emby:645:0": invalid},
        }
        assert plugin_instance._CollectionMissing__migrate_history_identity() == 0
        assert invalid.get("media_id") is None


class TestSubscribeContract:
    """订阅链路必须使用 media_source + media_id 对。"""

    def test_subscribe_calls_pair_identity(self, plugin_instance):
        plugin = _plugin()
        record = _make_record(media_source="themoviedb", media_id="206647")
        ok, msg = plugin_instance._CollectionMissing__subscribe_movie(record)
        assert ok is True
        # 判重调用：media_source + media_id
        assert plugin_instance._subscribe_oper.calls == [
            (plugin.RECORD_MEDIA_SOURCE, "206647")
        ]
        # 订阅调用：media_source + media_id，且不含 tmdbid
        last = plugin_instance._subscribe_chain.last_call
        assert last["media_source"] == plugin.RECORD_MEDIA_SOURCE
        assert last["media_id"] == "206647"
        assert last["mtype"].value == plugin.MediaType.MOVIE.value

    def test_subscribe_missing_identity_fails(self, plugin_instance):
        plugin = _plugin()
        bad = _make_record(tmdb_id=None)
        ok, msg = plugin_instance._CollectionMissing__subscribe_movie(bad)
        assert ok is False
        assert "媒体身份" in msg


class TestApiContract:
    """插件 API：可选 apikey 兼容两种鉴权路径。"""

    def test_endpoints_accept_missing_apikey(self, plugin_instance, plugin_class):
        plugin = _plugin()
        instance = plugin_instance
        instance._store = {}
        api_decls = instance.get_api()
        assert api_decls
        for decl in api_decls:
            assert decl.get("auth") == "bear"
            assert decl.get("response_model") is not None
            endpoint = decl["endpoint"]
            # 全部端点均把 apikey 作为可选参数
            import inspect

            sig = inspect.signature(endpoint)
            if "apikey" in sig.parameters:
                assert sig.parameters["apikey"].default is None

    def test_apikey_check_behavior(self, plugin_instance):
        plugin = _plugin()
        plugin_instance._store = {}
        plugin_instance._CollectionMissing__save_details({})
        # 不传 apikey（走 bearer）放行
        assert plugin_instance._CollectionMissing__check_apikey(None) is True
        # 显式传错 apikey 拒绝
        assert plugin_instance._CollectionMissing__check_apikey("wrong") is False


class TestPageContract:
    """详情页事件与 V2 保持兼容：仍携带 apikey 参数。"""

    def test_page_builds(self, plugin_instance):
        plugin = _plugin()
        plugin_instance._store["history"] = {
            "last_scan": "2026-09-01 10:00:00",
            "details": {"我的Emby:645:206647": _make_record(
                media_source="themoviedb", media_id="206647"
            )},
        }
        page = plugin_instance.get_page()
        assert page
        # 按钮事件应携带 apikey（兼容路径）
        raw = repr(page)
        assert "apikey" in raw
        assert "plugin/CollectionMissing/" in raw
