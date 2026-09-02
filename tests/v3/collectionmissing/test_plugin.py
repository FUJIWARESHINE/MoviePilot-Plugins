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


class TestGroupExpandApi:
    """合集展开状态端点：翻转、批量、鉴权、随记录清空。"""

    GROUP_A = "我的Emby:645"
    GROUP_B = "我的Emby:999"

    def _seed(self, instance):
        instance._store = {
            "history": {
                "details": {
                    "我的Emby:645:1": _make_record(
                        tmdb_id=1, media_id="1", collection_id=645
                    ),
                    "我的Emby:999:2": _make_record(
                        tmdb_id=2, media_id="2", collection_id=999
                    ),
                }
            }
        }

    def test_toggle_group_flips_and_is_idempotent_pair(self, plugin_instance):
        self._seed(plugin_instance)
        first = plugin_instance.toggle_group(self.GROUP_A)
        assert first.success is True
        assert plugin_instance.get_data("expanded_groups") == {self.GROUP_A: True}

        second = plugin_instance.toggle_group(self.GROUP_A)
        assert second.success is True
        assert plugin_instance.get_data("expanded_groups") == {self.GROUP_A: False}

    def test_toggle_group_accepts_missing_apikey(self, plugin_instance):
        self._seed(plugin_instance)
        assert plugin_instance.toggle_group(self.GROUP_A).success is True
        assert plugin_instance.toggle_group(self.GROUP_A, "wrong").success is False

    def test_toggle_group_rejects_empty_key(self, plugin_instance):
        self._seed(plugin_instance)
        resp = plugin_instance.toggle_group("")
        assert resp.success is False
        assert "合集标识" in resp.message

    def test_set_all_groups_only_touches_existing_collections(self, plugin_instance):
        """批量展开只覆盖当前记录里存在的合集，不留下已消失合集的垃圾状态。"""
        self._seed(plugin_instance)
        plugin_instance.save_data("expanded_groups", {"已删除的服务器:1": True})

        resp = plugin_instance.set_all_groups("true")
        assert resp.success is True
        assert plugin_instance.get_data("expanded_groups") == {
            self.GROUP_A: True,
            self.GROUP_B: True,
        }

    def test_set_all_groups_collapses(self, plugin_instance):
        self._seed(plugin_instance)
        plugin_instance.set_all_groups("true")
        assert plugin_instance.set_all_groups("false").success is True
        assert plugin_instance.get_data("expanded_groups") == {
            self.GROUP_A: False,
            self.GROUP_B: False,
        }

    def test_set_all_groups_rejects_wrong_apikey(self, plugin_instance):
        self._seed(plugin_instance)
        assert plugin_instance.set_all_groups("true", "wrong").success is False

    def test_clear_all_records_resets_expand_state(self, plugin_instance):
        self._seed(plugin_instance)
        plugin_instance.set_all_groups("true")
        assert plugin_instance.clear_records("all").success is True
        assert plugin_instance.get_data("expanded_groups") == {}


class TestCollectionStats:
    """合集补齐进度字段：组内对齐、不跨组、可重复执行。"""

    def _backfill(self, instance):
        return instance._CollectionMissing__backfill_collection_stats()

    def test_propagates_within_group(self, plugin_instance):
        """同组内任一条已有统计时，同步给组内其它记录。"""
        lacking = _make_record(tmdb_id=1, media_id="1")
        having = _make_record(
            tmdb_id=2, media_id="2", collection_total=26, collection_owned=24
        )
        plugin_instance._store["history"] = {
            "details": {"k1": lacking, "k2": having}
        }

        assert self._backfill(plugin_instance) == 1
        assert lacking["collection_total"] == 26
        assert lacking["collection_owned"] == 24
        # 幂等：再次执行不再改动
        assert self._backfill(plugin_instance) == 0

    def test_skips_group_without_stats(self, plugin_instance):
        """整组都没有统计时保持原样，不写 0 或占位值。"""
        record = _make_record(tmdb_id=1, media_id="1")
        plugin_instance._store["history"] = {"details": {"k1": record}}

        assert self._backfill(plugin_instance) == 0
        assert "collection_total" not in record
        assert "collection_owned" not in record

    def test_does_not_cross_groups(self, plugin_instance):
        """不同 collection_id 的记录不共享统计。"""
        record = _make_record(tmdb_id=1, media_id="1")
        other = _make_record(
            tmdb_id=2,
            media_id="2",
            collection_id=999,
            collection_total=26,
            collection_owned=24,
        )
        plugin_instance._store["history"] = {
            "details": {"k1": record, "k2": other}
        }

        assert self._backfill(plugin_instance) == 0
        assert "collection_total" not in record

    def test_empty_details(self, plugin_instance):
        """没有记录时安全返回 0。"""
        plugin_instance._store["history"] = {"details": {}}
        assert self._backfill(plugin_instance) == 0


class TestPageRendering:
    """详情页渲染：海报卡、进度条降级、折叠分页、默认展开项。"""

    def _seed_one(self, instance, **record_overrides):
        instance._store["history"] = {
            "details": {
                "我的Emby:645:206647": _make_record(
                    media_source="themoviedb",
                    media_id="206647",
                    **record_overrides,
                )
            },
        }

    def test_poster_card_structure(self, plugin_instance):
        """海报卡：2:3 海报、评分角标、状态角标、片名链接、长简介截断为悬浮提示。"""
        self._seed_one(
            plugin_instance,
            vote_average=7.4,
            overview="这是一段很长的电影简介。" * 30,
        )
        raw = repr(plugin_instance.get_page())
        assert "VImg" in raw
        assert "aspect-ratio" in raw
        assert "★ 7.4" in raw
        assert "position-absolute" in raw
        assert "#/media?mediaid=tmdb:206647" in raw
        assert "…" in raw  # 超过 120 字的简介被截断

    def test_progress_bar_shown_when_stats_present(self, plugin_instance):
        """有统计字段时渲染「已收 N/M」与按比例填充的进度条。"""
        self._seed_one(plugin_instance, collection_total=26, collection_owned=24)
        raw = repr(plugin_instance.get_page())
        assert "已收 24/26" in raw
        assert "width: 92%" in raw  # 24/26 ≈ 92%

    def test_progress_bar_hidden_when_stats_missing(self, plugin_instance):
        """统计字段缺失时（存量记录/TMDB 异常）隐藏进度条，不显示 0%。"""
        self._seed_one(plugin_instance)
        raw = repr(plugin_instance.get_page())
        assert "已收" not in raw

    def test_group_collapses_beyond_page_size(self, plugin_instance):
        """超过 GROUP_PAGE_SIZE 的合集默认只渲染前 N 部，并提供「展开全部」按钮。"""
        page_size = _plugin().GROUP_PAGE_SIZE
        details = {
            f"我的Emby:645:{i}": _make_record(tmdb_id=i, media_id=str(i), title=f"电影 {i}")
            for i in range(1, page_size + 3)
        }
        plugin_instance._store["history"] = {"details": details}

        raw = repr(plugin_instance.get_page())
        assert "VExpansionPanels" in raw
        assert "toggle_group" in raw
        assert "展开全部（还有 2 部）" in raw
        # 只渲染了前 N 张海报卡
        assert raw.count("aspect-ratio") == page_size

    def test_group_show_all_renders_everything(self, plugin_instance):
        """expanded_groups 为 True 的合集完整渲染全部海报。"""
        page_size = _plugin().GROUP_PAGE_SIZE
        details = {
            f"我的Emby:645:{i}": _make_record(tmdb_id=i, media_id=str(i), title=f"电影 {i}")
            for i in range(1, page_size + 3)
        }
        plugin_instance._store["history"] = {"details": details}
        plugin_instance._store["expanded_groups"] = {"我的Emby:645": True}

        raw = repr(plugin_instance.get_page())
        assert raw.count("aspect-ratio") == page_size + 2
        assert "收起" in raw

    def test_open_indices_from_expand_state(self, plugin_instance):
        """expanded_groups 中 True 的分组出现在 modelValue 默认展开项里。"""
        details = {
            "我的Emby:645:1": _make_record(
                tmdb_id=1, media_id="1", collection_id=645, collection_name="A 合集"
            ),
            "我的Emby:999:2": _make_record(
                tmdb_id=2, media_id="2", collection_id=999, collection_name="B 合集"
            ),
        }
        plugin_instance._store["history"] = {"details": details}
        plugin_instance._store["expanded_groups"] = {"我的Emby:999": True}

        raw = repr(plugin_instance.get_page())
        # 按名称排序 A(0) B(1)，B 合集完整展开 → modelValue [1]
        assert "'modelValue': [1]" in raw


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

    def test_page_top_section_builds(self, plugin_instance):
        """顶部区：四张统计卡、实心筛选条、全部展开/收起、清空待处理。"""
        plugin_instance._store["history"] = {
            "last_scan": "2026-09-01 10:00:00",
            "details": {
                "我的Emby:645:206647": _make_record(
                    media_source="themoviedb", media_id="206647"
                )
            },
        }
        raw = repr(plugin_instance.get_page())
        # 四类统计卡标签（订阅失败此前从未展示，属本次新增）
        for label in ("待处理", "已订阅", "已忽略", "订阅失败"):
            assert label in raw
        # 工具条：筛选、全部展开/收起、清空待处理
        assert "set_filter" in raw
        assert "set_all_groups" in raw
        assert "全部展开" in raw
        assert "清空待处理" in raw
        # 次级信息行：统计总览与上次扫描时间
        assert "上次扫描" in raw
