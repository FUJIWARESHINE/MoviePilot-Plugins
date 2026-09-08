"""Emby 电影合集缺失订阅（CollectionMissing）V2 实现的回归测试。

用例聚焦从 V3 v2.1.x 同步过来的改动：
- 合集展开状态端点（/toggle_group、/set_all_groups）与记录清空联动；
- 合集补齐进度字段的组内对齐；
- 详情页重构：统计卡、折叠面板、进度条、海报卡与底部操作条；
- 扫描期跳过无上映日期电影（疑似废案），与「跳过未上映」配置解耦。

同时用 V2 旧签名（tmdbid）固化两代合同差异，防止 V3 写法被误同步过来。
"""

import sys
import types
from datetime import datetime, timedelta

import pytest

pytestmark = pytest.mark.v2

APIKEY = "test-token"


class FakeResponse:
    """模拟 requests 响应对象，只需提供 .json()。"""

    def __init__(self, payload: dict):
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def _plugin():
    """取 conftest 中加载的 V2 插件模块。"""
    return sys.modules.get("app.plugins.collectionmissing_v2")


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


def _make_movie(tmdb_id, title, release_date, vote=7.0):
    """构造一个 TMDB 片单条目（只需 __process_boxset 用到的字段）。"""
    return types.SimpleNamespace(
        tmdb_id=tmdb_id,
        title=title,
        year=str(release_date)[:4] if release_date else "",
        release_date=release_date,
        vote_average=vote,
        poster_path="/p.jpg",
        overview="overview",
    )


class _FakeInstance:
    """最小 Emby/Jellyfin 实例：只需 user、is_inactive 与 get_data。"""

    def __init__(self, owned_tmdb_ids):
        self.user = "u1"
        self._owned = owned_tmdb_ids

    def is_inactive(self):
        return False

    def get_data(self, url):
        return FakeResponse({
            "Items": [{"ProviderIds": {"Tmdb": str(i)}} for i in self._owned]
        })


def _make_service(owned_tmdb_ids):
    return types.SimpleNamespace(type="emby", instance=_FakeInstance(owned_tmdb_ids))


class TestGroupExpandApi:
    """合集展开状态端点：翻转、批量、鉴权、随记录清空。"""

    GROUP_A = "我的Emby:645"
    GROUP_B = "我的Emby:999"

    def _seed(self, instance):
        instance._store = {
            "history": {
                "details": {
                    "我的Emby:645:1": _make_record(tmdb_id=1, collection_id=645),
                    "我的Emby:999:2": _make_record(tmdb_id=2, collection_id=999),
                }
            }
        }

    def test_toggle_group_flips_state(self, plugin_instance):
        self._seed(plugin_instance)
        assert plugin_instance.toggle_group(self.GROUP_A, APIKEY).success is True
        assert plugin_instance.get_data("expanded_groups") == {self.GROUP_A: True}

        assert plugin_instance.toggle_group(self.GROUP_A, APIKEY).success is True
        assert plugin_instance.get_data("expanded_groups") == {self.GROUP_A: False}

    def test_toggle_group_rejects_wrong_apikey(self, plugin_instance):
        self._seed(plugin_instance)
        assert plugin_instance.toggle_group(self.GROUP_A, "wrong").success is False

    def test_toggle_group_rejects_empty_key(self, plugin_instance):
        self._seed(plugin_instance)
        resp = plugin_instance.toggle_group("", APIKEY)
        assert resp.success is False
        assert "合集标识" in resp.message

    def test_set_all_groups_only_touches_existing_collections(self, plugin_instance):
        """批量展开只覆盖当前记录里存在的合集，不留下已消失合集的垃圾状态。"""
        self._seed(plugin_instance)
        plugin_instance.save_data("expanded_groups", {"已删除的服务器:1": True})

        resp = plugin_instance.set_all_groups("true", APIKEY)
        assert resp.success is True
        assert plugin_instance.get_data("expanded_groups") == {
            self.GROUP_A: True,
            self.GROUP_B: True,
        }

    def test_set_all_groups_collapses(self, plugin_instance):
        self._seed(plugin_instance)
        plugin_instance.set_all_groups("true", APIKEY)
        assert plugin_instance.set_all_groups("false", APIKEY).success is True
        assert plugin_instance.get_data("expanded_groups") == {
            self.GROUP_A: False,
            self.GROUP_B: False,
        }

    def test_set_all_groups_rejects_wrong_apikey(self, plugin_instance):
        self._seed(plugin_instance)
        assert plugin_instance.set_all_groups("true", "wrong").success is False

    def test_clear_all_records_resets_expand_state(self, plugin_instance):
        self._seed(plugin_instance)
        plugin_instance.set_all_groups("true", APIKEY)
        assert plugin_instance.clear_records("all", APIKEY).success is True
        assert plugin_instance.get_data("expanded_groups") == {}


class TestCollectionStats:
    """合集补齐进度字段：组内对齐、不跨组、可重复执行。"""

    def _backfill(self, instance):
        return instance._CollectionMissing__backfill_collection_stats()

    def test_propagates_within_group(self, plugin_instance):
        lacking = _make_record(tmdb_id=1)
        having = _make_record(tmdb_id=2, collection_total=26, collection_owned=24)
        plugin_instance._store["history"] = {"details": {"k1": lacking, "k2": having}}

        assert self._backfill(plugin_instance) == 1
        assert lacking["collection_total"] == 26
        assert lacking["collection_owned"] == 24
        # 幂等：再次执行不再改动
        assert self._backfill(plugin_instance) == 0

    def test_skips_group_without_stats(self, plugin_instance):
        record = _make_record(tmdb_id=1)
        plugin_instance._store["history"] = {"details": {"k1": record}}

        assert self._backfill(plugin_instance) == 0
        assert "collection_total" not in record
        assert "collection_owned" not in record

    def test_does_not_cross_groups(self, plugin_instance):
        record = _make_record(tmdb_id=1)
        other = _make_record(
            tmdb_id=2, collection_id=999, collection_total=26, collection_owned=24
        )
        plugin_instance._store["history"] = {"details": {"k1": record, "k2": other}}

        assert self._backfill(plugin_instance) == 0
        assert "collection_total" not in record

    def test_empty_details(self, plugin_instance):
        plugin_instance._store["history"] = {"details": {}}
        assert self._backfill(plugin_instance) == 0


class TestScanSkipRules:
    """扫描过滤规则：无上映日期（废案）与未上映分别处理。"""

    BOXSET = {"Id": "100", "Name": "测试合集", "ProviderIds": {"Tmdb": "645"}}

    def _process(self, instance, movies, owned=(), skip_unreleased=True):
        instance._skip_unreleased = skip_unreleased
        instance._tmdb_chain.collection_result = movies
        details = {}
        instance._CollectionMissing__process_boxset(
            "我的Emby", _make_service(list(owned)), "u1", self.BOXSET, details
        )
        return details

    def test_skips_movie_without_release_date(self, plugin_instance):
        """无上映日期视为废案，即使关闭「跳过未上映」也不入库。"""
        movies = [_make_movie(1, "无日期电影", None)]
        details = self._process(plugin_instance, movies, skip_unreleased=False)
        assert details == {}

    def test_unreleased_respects_config(self, plugin_instance):
        """未上映电影是否入库由 _skip_unreleased 决定。"""
        future = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
        movies = [_make_movie(1, "未来电影", future)]

        assert self._process(plugin_instance, movies, skip_unreleased=True) == {}
        details = self._process(plugin_instance, movies, skip_unreleased=False)
        assert "我的Emby:645:1" in details

    def test_missing_movies_recorded_with_collection_stats(self, plugin_instance):
        """缺失电影入库，并写入合集补齐进度（总数 / 已收数）。"""
        movies = [
            _make_movie(1, "已有电影", "2010-01-01"),
            _make_movie(2, "缺失电影A", "2012-01-01"),
            _make_movie(3, "缺失电影B", "2014-01-01"),
        ]
        details = self._process(plugin_instance, movies, owned=[1])

        assert set(details) == {"我的Emby:645:2", "我的Emby:645:3"}
        for record in details.values():
            assert record["collection_total"] == 3
            assert record["collection_owned"] == 1


class TestPageRendering:
    """详情页渲染：统计卡、进度条降级、折叠分页、海报卡与操作条。"""

    def _seed_one(self, instance, **record_overrides):
        instance._store["history"] = {
            "last_scan": "2026-09-01 10:00:00",
            "details": {"我的Emby:645:206647": _make_record(**record_overrides)},
        }

    def test_top_section_cards_and_toolbar(self, plugin_instance):
        self._seed_one(plugin_instance)
        raw = repr(plugin_instance.get_page())
        for label in ("待处理", "已订阅", "已忽略", "订阅失败"):
            assert label in raw
        assert "set_filter" in raw
        assert "set_all_groups" in raw
        assert "全部展开" in raw
        assert "清空待处理" in raw
        assert "上次扫描" in raw

    def test_poster_card_structure(self, plugin_instance):
        """海报卡：2:3 海报、评分角标、状态角标、片名链接，不再渲染简介。"""
        self._seed_one(plugin_instance, vote_average=7.4, overview="很长很长的简介" * 30)
        raw = repr(plugin_instance.get_page())
        assert "VImg" in raw
        assert "aspect-ratio" in raw
        assert "★ 7.4" in raw
        assert "position-absolute" in raw
        assert "#/media?mediaid=tmdb:206647" in raw
        assert "很长很长的简介" not in raw

    def test_action_bar_uses_explicit_hex_and_icon_delete(self, plugin_instance):
        """底部操作条用显式 hex 底色，删除为圆形图标按钮（V3 v2.1.3/2.1.4 同步）。"""
        self._seed_one(plugin_instance)
        raw = repr(plugin_instance.get_page())
        assert "#f5f5f5" in raw
        assert "mdi-delete-outline" in raw
        assert "rounded-b" not in raw  # utility class 已收回到 inline style

    def test_progress_bar_shown_when_stats_present(self, plugin_instance):
        self._seed_one(plugin_instance, collection_total=26, collection_owned=24)
        raw = repr(plugin_instance.get_page())
        assert "已收 24/26" in raw
        assert "width: 92%" in raw

    def test_progress_bar_hidden_when_stats_missing(self, plugin_instance):
        self._seed_one(plugin_instance)
        raw = repr(plugin_instance.get_page())
        assert "已收" not in raw

    def test_group_collapses_beyond_page_size(self, plugin_instance):
        page_size = _plugin().GROUP_PAGE_SIZE
        details = {
            f"我的Emby:645:{i}": _make_record(tmdb_id=i, title=f"电影 {i}")
            for i in range(1, page_size + 3)
        }
        plugin_instance._store["history"] = {"details": details}

        raw = repr(plugin_instance.get_page())
        assert "VExpansionPanels" in raw
        assert "toggle_group" in raw
        assert "展开全部（还有 2 部）" in raw
        assert raw.count("aspect-ratio") == page_size

    def test_group_show_all_renders_everything(self, plugin_instance):
        page_size = _plugin().GROUP_PAGE_SIZE
        details = {
            f"我的Emby:645:{i}": _make_record(tmdb_id=i, title=f"电影 {i}")
            for i in range(1, page_size + 3)
        }
        plugin_instance._store["history"] = {"details": details}
        plugin_instance._store["expanded_groups"] = {"我的Emby:645": True}

        raw = repr(plugin_instance.get_page())
        assert raw.count("aspect-ratio") == page_size + 2
        assert "收起" in raw

    def test_open_indices_from_expand_state(self, plugin_instance):
        plugin_instance._store["history"] = {
            "details": {
                "我的Emby:645:1": _make_record(tmdb_id=1, collection_id=645, collection_name="A 合集"),
                "我的Emby:999:2": _make_record(tmdb_id=2, collection_id=999, collection_name="B 合集"),
            }
        }
        plugin_instance._store["expanded_groups"] = {"我的Emby:999": True}

        raw = repr(plugin_instance.get_page())
        # 按名称排序 A(0) B(1)，B 合集完整展开 → modelValue [1]
        assert "'modelValue': [1]" in raw

    def test_page_actions_carry_apikey(self, plugin_instance):
        """V2 页面按钮事件仍以 apikey 鉴权。"""
        self._seed_one(plugin_instance)
        raw = repr(plugin_instance.get_page())
        assert "apikey" in raw
        assert "plugin/CollectionMissing/" in raw


class TestV2Contract:
    """V2 合同：订阅与判重仍使用 tmdbid，端点无需 bearer 声明。"""

    def test_subscribe_uses_tmdbid(self, plugin_instance):
        record = _make_record()
        ok, msg = plugin_instance._CollectionMissing__subscribe_movie(record)
        assert ok is True
        assert plugin_instance._subscribe_oper.calls == [206647]
        last = plugin_instance._subscribe_chain.last_call
        assert last["tmdbid"] == 206647
        assert "media_source" not in last

    def test_api_declaration_has_no_bear(self, plugin_instance):
        """V2 的 get_api 保持旧契约：不带 auth/response_model。"""
        decls = {d["path"]: d for d in plugin_instance.get_api()}
        assert "/toggle_group" in decls
        assert "/set_all_groups" in decls
        for decl in decls.values():
            assert "auth" not in decl
            assert "response_model" not in decl
