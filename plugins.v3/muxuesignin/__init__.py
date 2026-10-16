"""
慕雪自动签到插件 - 自动获取 Cookie

支持慕雪阁 (pt.muxuege.org)、Depth Studio (dstudio.me)、AGSVPT (agsvpt.com)、
PTzone (ptzone.xyz) 四个 NexusPHP 站点的自动签到。
Cookie 从 MoviePilot「站点管理」中读取，不在插件内另行配置。

站点之间的签到入口存在差异，因此每个站点在注册表中单独声明签到方式：
- `sign_method` 为 `get` 时，GET 一次 `attendance.php` 即完成签到（NexusPHP 默认行为）；
- `sign_method` 为 `post` 时，GET 可能只展示签到页，需要 POST 表单才会签到。

但“签到方式”只是兜底策略：只要 GET 响应里已经带有签到结果（例如 PTzone 由服务端
直接渲染「簽到成功 / 這是您的第 N 次簽到」，页面本身没有 `<form>`），就直接采信，
不会再盲 POST，以免把成功覆盖成「您今天已經簽到過了，請勿重複刷新」。
"""
import html
import re
import threading
import time
import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.db.oper.site import SiteOper
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.schemas.types import EventType
from app.sdk.config import settings
from app.sdk.events import eventmanager, Event
from app.sdk.logging import logger
from app.utils.http import RequestUtils


# ---------------------------------------------------------------- 站点注册表

# 每个站点独立维护一份历史，避免互相覆盖
SIGN_METHOD_GET = "get"
SIGN_METHOD_POST = "post"

SITES: List[Dict[str, Any]] = [
    {
        "key": "muxuege",
        "name": "慕雪阁",
        "base_url": "https://pt.muxuege.org",
        "domain": "muxuege.org",
        "sign_method": SIGN_METHOD_GET,
        "config_enable": "enable_muxuege",
        "config_domain": "domain_muxuege",
        "config_alias": "慕雪阁 Cookie",
    },
    {
        "key": "dstudio",
        "name": "Depth Studio",
        "base_url": "https://dstudio.me",
        "domain": "dstudio.me",
        "sign_method": SIGN_METHOD_GET,
        "config_enable": "enable_dstudio",
        "config_domain": "domain_dstudio",
        "config_alias": "Depth Studio Cookie",
    },
    {
        "key": "agsvpt",
        "name": "AGSVPT",
        "base_url": "https://www.agsvpt.com",
        "domain": "agsvpt.com",
        # 站点签到按钮所在表单没有 name 属性，GET 只返回签到请求表单，必须 POST 提交
        "sign_method": SIGN_METHOD_POST,
        "config_enable": "enable_agsvpt",
        "config_domain": "domain_agsvpt",
        "config_alias": "AGSVPT Cookie",
    },
    {
        "key": "ptzone",
        "name": "PTzone",
        "base_url": "https://ptzone.xyz",
        "domain": "ptzone.xyz",
        # GET attendance.php 仅展示签到页，签到由 POST 提交执行
        "sign_method": SIGN_METHOD_POST,
        "config_enable": "enable_ptzone",
        "config_domain": "domain_ptzone",
        "config_alias": "PTzone Cookie",
    },
]

# 结果状态码（写入 history）
STATUS_SIGNED = "signed"            # 签到成功
STATUS_ALREADY = "already"          # 已签到
STATUS_LOGIN_EXPIRED = "login_expired"  # Cookie 失效
STATUS_CAPTCHA = "captcha"          # 站点要求验证码
STATUS_FAILED = "failed"            # 其他失败
STATUS_NETWORK = "network"          # 网络错误

STATUS_TEXT: Dict[str, str] = {
    STATUS_SIGNED: "签到成功",
    STATUS_ALREADY: "今日已签到",
    STATUS_LOGIN_EXPIRED: "Cookie 失效",
    STATUS_CAPTCHA: "需要验证码",
    STATUS_FAILED: "签到失败",
    STATUS_NETWORK: "网络错误",
}


class MuxueSignin(_PluginBase):
    """慕雪自动签到插件"""

    # 插件基本信息
    plugin_name = "慕雪自动签到"
    plugin_desc = "自动签到慕雪阁、Depth Studio、AGSVPT、PTzone 站点，Cookie 从站点管理读取"
    plugin_icon = "MuxueSignin.png"
    plugin_version = "2.0.0"
    plugin_author = "FUJIWARESHINE"
    plugin_config_prefix = "muxuesignin_"
    plugin_order = 21
    auth_level = 2

    # 常量
    MAX_HISTORY = 100
    REQUEST_TIMEOUT = 30
    # 这些站点都有 Cloudflare 前置，偶发连接重置/超时，失败后做有限次重试
    REQUEST_RETRIES = 3
    RETRY_INTERVAL = 5
    USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/131.0.0.0 Safari/537.36")

    # 私有属性（仅作类级默认值；可变对象一律在 __init__ 里按实例重建，
    # 否则分身实例之间会共享同一份 dict / Lock）
    _enabled: bool = False
    _onlyonce: bool = False
    _cron: str = ""
    _notify: bool = True
    _site_enable: Dict[str, bool] = {}
    _site_domain: Dict[str, str] = {}
    _clear: bool = False
    _scheduler: Optional[BackgroundScheduler] = None
    _lock: threading.Lock = threading.Lock()

    # -------------------------------------------------------------- 生命周期

    def __init__(self):
        super().__init__()
        self._enabled = False
        self._onlyonce = False
        self._cron = ""
        self._notify = True
        self._clear = False
        self._scheduler = None
        self._lock = threading.Lock()
        # 站点开关 / 域名以注册表默认值为准，保证即使配置缺失也有合法值
        self._site_enable = {site["key"]: True for site in SITES}
        self._site_domain = {site["key"]: site["domain"] for site in SITES}

    def init_plugin(self, config: dict = None):
        """
        生效配置。

        MoviePilot 的 PluginManager.start() 是先调用本方法、成功后才把实例放进
        running_plugins，因此这里抛出的任何异常都会让插件不被加载，前端打开配置页
        时表现为「配置加载失败，请稍后重试」。故整体做异常兜底，绝不向外抛。
        """
        try:
            self.__do_init_plugin(config)
        except Exception as e:
            logger.error(f"慕雪自动签到：初始化失败，已回退默认配置：{e}")
            logger.error(traceback.format_exc())

    def __do_init_plugin(self, config: dict = None):
        self.stop_service()

        if config:
            self._enabled = bool(config.get("enabled", False))
            self._onlyonce = bool(config.get("onlyonce", False))
            self._cron = (config.get("cron") or "").strip()
            self._notify = bool(config.get("notify", True))
            self._clear = bool(config.get("clear", False))
            for site in SITES:
                self._site_enable[site["key"]] = bool(config.get(site["config_enable"], True))
                raw_domain = config.get(site["config_domain"]) or site["domain"]
                self._site_domain[site["key"]] = str(raw_domain).strip() or site["domain"]

        # 站点启用同步到站点数据，便于详情页读取
        for site in SITES:
            self.save_data(f"enable_{site['key']}", self._site_enable.get(site["key"], True))

        if self._clear:
            self._clear_all_history()
            self._clear = False
            self.update_config({
                "enabled": self._enabled,
                "onlyonce": False,
                "cron": self._cron,
                "notify": self._notify,
                "clear": False,
                **{s["config_enable"]: self._site_enable[s["key"]] for s in SITES},
                **{s["config_domain"]: self._site_domain[s["key"]] for s in SITES},
            })
            logger.info("慕雪自动签到：已清空全部签到记录")

        if self._onlyonce:
            self._onlyonce = False
            self.update_config({
                "enabled": self._enabled,
                "onlyonce": False,
                "cron": self._cron,
                "notify": self._notify,
                "clear": self._clear,
                **{s["config_enable"]: self._site_enable[s["key"]] for s in SITES},
                **{s["config_domain"]: self._site_domain[s["key"]] for s in SITES},
            })
            logger.info("慕雪自动签到：收到立即运行请求")
            threading.Thread(target=self.__sign_all, daemon=True).start()

        logger.info(f"慕雪自动签到插件初始化完成，启用状态: {self._enabled}")

    def get_state(self) -> bool:
        return self._enabled

    # -------------------------------------------------------------- 调度

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            try:
                return [{
                    "id": "MuxueSignin",
                    "name": "慕雪自动签到",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.__sign_all,
                    "kwargs": {},
                }]
            except Exception as e:
                logger.error(f"慕雪自动签到：cron 表达式无效: {e}")
        return []

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error(f"慕雪自动签到：停止调度失败: {e}")

    # -------------------------------------------------------------- 命令 / API

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [{
            "cmd": "/muxue_sign",
            "event": EventType.PluginAction,
            "desc": "执行慕雪自动签到",
            "category": "站点",
            "data": {"action": "muxue_signin_run"},
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/sign",
            "endpoint": self.__sign_api,
            "methods": ["POST"],
            "auth": "bear",
            "summary": "执行签到（可选 ?site=muxuege|dstudio|agsvpt|ptzone）",
        }, {
            "path": "/history",
            "endpoint": self.__history_api,
            "methods": ["GET"],
            "auth": "bear",
            "summary": "获取签到历史",
        }]

    # -------------------------------------------------------------- 表单

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装配置表单。

        MoviePilot 后端在这里捕获异常后直接返回空对象，前端就会显示
        「配置加载失败，请稍后重试」，因此这里再兜一层，保证永远返回合法结构。
        """
        try:
            return self.__build_form()
        except Exception as e:
            logger.error(f"慕雪自动签到：生成配置表单失败：{e}")
            logger.error(traceback.format_exc())
            return ([{
                "component": "VAlert",
                "props": {
                    "type": "error",
                    "variant": "tonal",
                    "text": f"配置表单生成失败：{e}",
                }
            }], self.__default_config())

    @staticmethod
    def __default_config() -> Dict[str, Any]:
        return {
            "enabled": False,
            "notify": True,
            "onlyonce": False,
            "cron": "0 9 * * *",
            "clear": False,
            **{s["config_enable"]: True for s in SITES},
            **{s["config_domain"]: s["domain"] for s in SITES},
        }

    def __build_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        site_rows = []
        for site in SITES:
            site_rows.append({
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 3},
                        "content": [{
                            "component": "VSwitch",
                            "props": {
                                "model": site["config_enable"],
                                "label": f"签到 {site['name']}",
                                "color": "primary",
                            }
                        }]
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 9},
                        "content": [{
                            "component": "VTextField",
                            "props": {
                                "model": site["config_domain"],
                                "label": f"{site['name']} 站点管理匹配域名",
                                "placeholder": site["domain"],
                                "prepend-inner-icon": "mdi-web",
                            }
                        }]
                    },
                ]
            })

        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "enabled", "label": "启用插件", "color": "success"}
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "notify", "label": "发送通知", "color": "info"}
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "onlyonce", "label": "立即运行一次", "color": "warning"}
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "clear", "label": "清空签到记录", "color": "error"}
                                }]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VCronField",
                                    "props": {
                                        "model": "cron",
                                        "label": "执行周期",
                                        "placeholder": "0 9 * * *"
                                    }
                                }]
                            }
                        ]
                    },
                    {"component": "VDivider", "props": {"class": "my-2"}},
                    *site_rows,
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{
                                "component": "VAlert",
                                "props": {
                                    "type": "info",
                                    "variant": "tonal",
                                    "text": (
                                        "📌 使用说明：\n"
                                        "1. 插件从 MoviePilot「站点管理」中按域名读取 Cookie，"
                                        "请确保已为慕雪阁（muxuege.org）、Depth Studio（dstudio.me）、"
                                        "AGSVPT（agsvpt.com）、PTzone（ptzone.xyz）分别配置好站点；\n"
                                        "2. 插件先 GET 一次 attendance.php：只要页面已返回签到结果就直接采信，"
                                        "拿不到结果时才补一次表单提交，不会用多余的 POST 覆盖成功状态；\n"
                                        "3. 开启签到验证码的站点需要手动签到一次；\n"
                                        "4. AGSVPT 在国内网络直连可能不稳定，如持续报网络错误，"
                                        "请在站点管理里为该站勾选「使用代理」；\n"
                                        "5. 单站签到失败不会影响其他站点的签到。"
                                    )
                                }
                            }]
                        }]
                    }
                ]
            }
        ], self.__default_config()

    # -------------------------------------------------------------- 详情页

    def get_page(self) -> List[dict]:
        try:
            return self.__build_page()
        except Exception as e:
            logger.error(f"慕雪自动签到：生成数据页面失败：{e}")
            logger.error(traceback.format_exc())
            return [{
                "component": "VAlert",
                "props": {
                    "type": "error",
                    "variant": "tonal",
                    "text": f"数据页面生成失败：{e}",
                }
            }]

    def __build_page(self) -> List[dict]:
        merged: List[Dict[str, Any]] = []
        for site in SITES:
            history = self.get_data(f"history_{site['key']}")
            if not isinstance(history, list):
                continue
            for record in history:
                if not isinstance(record, dict):
                    continue
                merged.append({**record, "site_key": site["key"], "site_name": site["name"]})

        last_run = self.get_data("lastrun")

        # 站点状态卡片
        cards = []
        for site in SITES:
            history = self.get_data(f"history_{site['key']}")
            history = history if isinstance(history, list) else []
            enabled = self._site_enable.get(site["key"], True)
            last = history[0] if history and isinstance(history[0], dict) else None
            last_status = STATUS_TEXT.get(last.get("status"), "-") if last else "尚未运行"
            last_time = last.get("time", "-") if last else "-"
            last_msg = (last.get("message") or "-") if last else "-"
            card_color = "primary"
            if last:
                if last.get("status") in (STATUS_SIGNED, STATUS_ALREADY):
                    card_color = "success"
                elif last.get("status") in (STATUS_LOGIN_EXPIRED, STATUS_NETWORK):
                    card_color = "warning"
                elif last.get("status") in (STATUS_FAILED, STATUS_CAPTCHA):
                    card_color = "error"

            cards.append({
                "component": "VCol",
                "props": {"cols": 12, "md": 6},
                "content": [{
                    "component": "VCard",
                    "props": {"variant": "tonal", "color": card_color},
                    "content": [
                        {"component": "VCardTitle",
                         "props": {"class": "d-flex align-center"},
                         "text": f"{'✅' if enabled else '⏸️'} {site['name']}"},
                        {"component": "VCardText", "content": [
                            {
                                "component": "div",
                                "props": {"class": "text-body-2"},
                                "text": f"域名：{self._site_domain.get(site['key'], site['domain'])}"
                            },
                            {
                                "component": "div",
                                "props": {"class": "text-body-2"},
                                "text": f"最近结果：{last_status}"
                            },
                            {
                                "component": "div",
                                "props": {"class": "text-body-2"},
                                "text": f"时间：{last_time}"
                            },
                            {
                                "component": "div",
                                "props": {"class": "text-body-2"},
                                "text": f"详情：{last_msg}"
                            }
                        ]},
                    ]
                }]
            })

        # 历史表
        if not merged:
            history_block = [{
                "component": "div",
                "props": {
                    "class": "text-center pa-8",
                    "style": "font-size: 1.1rem; color: #94a3b8;",
                },
                "text": "📭 暂无签到记录",
            }]
        else:
            merged.sort(key=lambda r: r.get("time") or "", reverse=True)
            rows = []
            for record in merged[:50]:
                status = record.get("status")
                badge_color = {
                    STATUS_SIGNED: "success",
                    STATUS_ALREADY: "success",
                    STATUS_FAILED: "error",
                    STATUS_CAPTCHA: "warning",
                    STATUS_LOGIN_EXPIRED: "warning",
                    STATUS_NETWORK: "warning",
                }.get(status, "grey")
                rows.append({
                    "component": "tr",
                    "props": {"style": "border-bottom: 1px solid #f0f0f0;"},
                    "content": [
                        {"component": "td",
                         "props": {"class": "text-caption py-2 px-3"},
                         "text": record.get("site_name", "-")},
                        {"component": "td",
                         "props": {"class": "text-caption py-2 px-3"},
                         "text": record.get("time", "-")},
                        {"component": "td",
                         "props": {"class": f"text-caption py-2 px-3 font-weight-medium text-{badge_color}"},
                         "text": STATUS_TEXT.get(status, status or "-")},
                        {"component": "td",
                         "props": {"class": "text-caption py-2 px-3"},
                         "text": record.get("message", "-")},
                    ]
                })
            history_block = [{
                "component": "VCard",
                "props": {"variant": "tonal"},
                "content": [
                    {"component": "VCardTitle",
                     "props": {"class": "d-flex align-center justify-space-between"},
                         "content": [
                             {"component": "span", "text": "📊 签到历史"},
                             {"component": "span",
                              "props": {"class": "text-caption text-medium-emphasis"},
                              "text": f"上次执行：{last_run or '-'} 共 {len(merged)} 条"},
                         ]},
                    {"component": "VCardText", "content": [{
                        "component": "VSimpleTable",
                        "props": {"density": "compact"},
                        "content": [
                            {"component": "thead", "content": [{
                                "component": "tr",
                                "props": {"style": "border-bottom: 2px solid #e0e0e0;"},
                                "content": [
                                    {"component": "th",
                                     "props": {"class": "text-left text-caption font-weight-medium py-1 px-3"},
                                     "text": "站点"},
                                    {"component": "th",
                                     "props": {"class": "text-left text-caption font-weight-medium py-1 px-3"},
                                     "text": "时间"},
                                    {"component": "th",
                                     "props": {"class": "text-left text-caption font-weight-medium py-1 px-3"},
                                     "text": "状态"},
                                    {"component": "th",
                                     "props": {"class": "text-left text-caption font-weight-medium py-1 px-3"},
                                     "text": "消息"},
                                ]
                            }]},
                            {"component": "tbody", "content": rows},
                        ]
                    }]}
                ]
            }]

        return [{
            "component": "VContainer",
            "props": {"fluid": True},
            "content": [
                {"component": "VRow", "content": cards},
                *history_block,
            ]
        }]

    # -------------------------------------------------------------- 内部：核心流程

    def __sign_api(self, site: Optional[str] = None) -> Dict[str, Any]:
        if self._lock.locked():
            return {"success": False, "message": "已有签到任务正在运行"}
        threading.Thread(target=self.__sign_all,
                         kwargs={"site_filter": site},
                         daemon=True).start()
        return {"success": True, "message": "签到任务已启动"}

    def __history_api(self) -> Dict[str, Any]:
        merged: List[Dict[str, Any]] = []
        for site in SITES:
            history = self.get_data(f"history_{site['key']}")
            if not isinstance(history, list):
                continue
            for record in history:
                if not isinstance(record, dict):
                    continue
                merged.append({**record, "site_key": site["key"], "site_name": site["name"]})
        merged.sort(key=lambda r: r.get("time") or "", reverse=True)
        return {"success": True, "data": merged[:50]}

    @eventmanager.register(EventType.PluginAction)
    def handle_plugin_action(self, event: Event):
        if not event:
            return
        event_data = event.event_data
        if not event_data or event_data.get("action") != "muxue_signin_run":
            return
        if self._lock.locked():
            self.post_message(
                mtype=NotificationType.SiteMessage,
                title="【慕雪自动签到】",
                text="已有签到任务正在运行，请等待完成"
            )
            return
        threading.Thread(target=self.__sign_all, daemon=True).start()

    def __sign_all(self, site_filter: Optional[str] = None) -> None:
        if not self._lock.acquire(blocking=False):
            logger.warning("慕雪自动签到：已有任务运行，跳过本次")
            return

        try:
            logger.info("慕雪自动签到：开始本轮签到")
            summary = []
            statuses = []
            for site in SITES:
                if site_filter and site["key"] != site_filter:
                    continue
                if not self._site_enable.get(site["key"], True):
                    logger.info(f"慕雪自动签到：{site['name']} 已禁用，跳过")
                    continue

                result = self.__sign_one(site)
                statuses.append(result.get("status"))
                summary.append(f"{site['name']}：{STATUS_TEXT.get(result['status'], result['status'])}"
                               + (f" | {result['message']}" if result.get("message") else ""))

            self.save_data("lastrun", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            if self._notify and summary:
                ok = all(s in (STATUS_SIGNED, STATUS_ALREADY) for s in statuses)
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title=f"【慕雪自动签到】{'签到完成' if ok else '部分签到失败'}",
                    text="\n".join(summary),
                )
        except Exception as e:
            logger.error(f"慕雪自动签到：本轮异常: {e}")
            if self._notify:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="【慕雪自动签到】异常",
                    text=str(e)[:200],
                )
        finally:
            self._lock.release()

    def __sign_one(self, site: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行一次签到。

        签到入口存在站点差异，注册表里的 `sign_method` 只用于决定“是否需要额外 POST”：
        - `get`：GET 一次 `attendance.php` 即完成签到，直接按 GET 结果判定；
        - `post`：GET 可能只返回签到页或表单，需要 POST 才能签到时才补一次提交。

        无论哪种方式，只要 GET 响应本身已经给出了签到结果（例如 PTzone 的
        「簽到成功 / 這是您的第 N 次簽到」是服务端渲染在 `attendance.php` 上的，
        页面并没有 `<form>`，签到由页面脚本触发），就直接采信，不再盲目 POST——
        盲目 POST 会拿到「您今天已經簽到過了，請勿重複刷新」的骨架页，反而把成功判成失败。
        """
        key = site["key"]
        name = site["name"]
        base = site["base_url"]
        domain = self._site_domain.get(key, site["domain"])
        sign_method = str(site.get("sign_method") or SIGN_METHOD_GET).lower()
        signin_url = f"{base}/attendance.php"
        index_url = f"{base}/index.php"

        # 1. 取 Cookie
        cookie = self.__get_site_cookie(domain, name)
        if not cookie:
            return self.__record(key, STATUS_LOGIN_EXPIRED, f"未找到 {name}（{domain}）的 Cookie，请在站点管理中配置")

        headers = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
            "referer": index_url,
            "user-agent": self.USER_AGENT,
        }
        proxies = self.__site_proxies(domain)

        # 2. GET 探测（这些站点都有 Cloudflare 前置，偶发连接被重置/超时，
        #    单次失败不代表真的不可达，因此做有限次重试）
        res = None
        last_error = ""
        for attempt in range(self.REQUEST_RETRIES + 1):
            try:
                res = RequestUtils(
                    cookies=cookie,
                    ua=self.USER_AGENT,
                    proxies=proxies,
                    timeout=self.REQUEST_TIMEOUT,
                ).get_res(url=signin_url, headers=headers)
            except Exception as e:
                last_error = str(e)
                res = None
            if res is not None:
                break
            if attempt < self.REQUEST_RETRIES:
                logger.info(f"{name}：签到请求无响应，{self.RETRY_INTERVAL} 秒后重试"
                            f"（{attempt + 1}/{self.REQUEST_RETRIES}）")
                time.sleep(self.RETRY_INTERVAL)

        if not res:
            detail = f"：{last_error[:80]}" if last_error else ""
            logger.error(f"{name}：签到请求异常{detail}")
            return self.__record(key, STATUS_NETWORK, f"请求异常{detail}" if last_error else "签到请求无响应")
        if res.status_code != 200:
            return self.__record(key, STATUS_NETWORK, f"签到页返回 {res.status_code}")

        html_text = res.text

        # 登录跳转/未登录
        if self.__looks_like_login(html_text, res.url):
            return self.__record(key, STATUS_LOGIN_EXPIRED, "Cookie 已失效或被重定向到登录页")

        # 需要验证码
        if self.__looks_like_captcha(html_text):
            return self.__record(key, STATUS_CAPTCHA, "该站点当前要求验证码，请打开浏览器手动签到")

        # GET 已给出签到结果：直接采信（覆盖所有站点，含需要 POST 的站点）
        has_result = self.__check_already_signed(html_text, SIGN_METHOD_GET)
        # POST 站点只有在拿到签到表单时，才需要补一次提交
        form_data, action_url = self.__extract_form(html_text, signin_url)

        if has_result:
            msg, already = self.__extract_reward(html_text)
            status = self.__resolve_signed_status(msg, already, html_text)
            return self.__record(key, status, msg)

        if sign_method == SIGN_METHOD_POST and not form_data:
            # 既没有签到结果，也没有可提交的表单：说明签到由页面脚本触发且 GET 未生效
            logger.warning(f"{name}：签到页未包含签到结果与表单，无法自动签到")
            return self.__record(key, STATUS_FAILED, "签到页无可提交表单（可能依赖页面脚本），请手动签到确认")

        # 3. 提交表单（GET 未确认签到的场景）
        post_result = self.__submit_signin(action_url, form_data, cookie, base, signin_url,
                                           sign_method, proxies)
        return self.__record(key, post_result["status"], post_result["message"])

    # -------------------------------------------------------------- 内部：HTTP

    def __submit_signin(self, action_url: str, form_data: Dict[str, str],
                        cookie: str, base: str, referer: str,
                        sign_method: str = SIGN_METHOD_GET,
                        proxies: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """
        提交签到表单并解析结果。

        `form_data` 为空时按签到方式补默认参数：NexusPHP 原生签到页通常是
        `action=signin`，而需要 POST 的站点直接提交 `attendance.php` 空表单。
        `proxies` 由调用方按站点「使用代理」开关决定，缺省回退系统代理。
        """
        headers = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
            "content-type": "application/x-www-form-urlencoded",
            "origin": base,
            "referer": referer,
            "user-agent": self.USER_AGENT,
        }
        payload = dict(form_data)
        if not payload:
            payload = {"action": "signin"} if sign_method != SIGN_METHOD_POST else {}

        # 提交同样做有限次重试：GET 成功不代表 POST 不会被 Cloudflare 抖动打断
        res = None
        last_error = ""
        for attempt in range(self.REQUEST_RETRIES + 1):
            try:
                res = RequestUtils(
                    cookies=cookie,
                    ua=self.USER_AGENT,
                    proxies=proxies if proxies is not None else settings.PROXY,
                    timeout=self.REQUEST_TIMEOUT,
                ).post_res(url=action_url, headers=headers, data=payload, allow_redirects=True)
            except Exception as e:
                last_error = str(e)
                res = None
            if res is not None:
                break
            if attempt < self.REQUEST_RETRIES:
                time.sleep(self.RETRY_INTERVAL)

        if res is None and last_error:
            logger.error(f"提交签到异常: {last_error}")
            return {"status": STATUS_NETWORK, "message": f"提交异常：{last_error[:80]}"}

        if not res:
            return {"status": STATUS_NETWORK, "message": "签到请求无响应"}
        if res.status_code != 200:
            return {"status": STATUS_NETWORK, "message": f"签到返回 {res.status_code}"}

        body = res.text

        if self.__looks_like_login(body, res.url):
            return {"status": STATUS_LOGIN_EXPIRED, "message": "Cookie 已失效"}

        if self.__looks_like_captcha(body):
            return {"status": STATUS_CAPTCHA, "message": "该站点要求验证码，请手动签到"}

        err = self.__extract_error(body)
        if err:
            return {"status": STATUS_FAILED, "message": err}

        if self.__check_already_signed(body, sign_method):
            msg, already = self.__extract_reward(body)
            status = self.__resolve_signed_status(msg, already, body)
            return {"status": status, "message": msg}

        return {"status": STATUS_FAILED,
                "message": f"未知响应：{self.__snippet(body)[:120]}"}

    # -------------------------------------------------------------- 内部：解析

    @staticmethod
    def __looks_like_login(body: str, final_url: str) -> bool:
        """
        判断当前响应是否其实是登录页。

        首选依据是最终 URL 落在登录页；但部分站点（NexusPHP 系列）会把登录表单渲染在
        原路径上、或使用繁体文案，因此再叠加两层兜底：
        - 页面里存在以 takelogin/login 为 action 的表单，且包含密码输入框；
        - 文案里出现简繁体的「未登录/未登錄/请先登录/請先登入」等提示。
        """
        if final_url and ("login.php" in final_url.lower() or "/login" in final_url.lower()):
            return True
        lowered = body.lower()
        has_login_form = bool(re.search(r"<form[^>]+action\s*=\s*[\"']?[^\"'>]*take?login", lowered)) \
            or bool(re.search(r"<form[^>]+action\s*=\s*[\"']?[^\"'>]*login", lowered))
        has_password = 'type="password"' in lowered or "type='password'" in lowered \
            or 'name="password"' in lowered or 'name="passwd"' in lowered
        if has_login_form and has_password:
            return True
        keywords = ("未登录", "未登錄", "您还没有登录", "您還沒有登錄", "该页面必须在登录后才能访问",
                    "該頁面必須在登錄後才能訪問", "请先登录", "請先登入", "请先登录", "需要登录", "登錄後",
                    "登录后才能", "登錄後才能")
        return any(kw in body for kw in keywords)

    @staticmethod
    def __check_already_signed(body: str, sign_method: str = SIGN_METHOD_GET) -> bool:
        """
        判断页面是否给出了可确认的签到结果。

        关键词同时覆盖简体与繁体（PTzone 等站点使用「簽到成功」），否则繁体站点会被
        判为“没有结果”而进入多余的 POST 分支。

        `这是您的第 N 次签到`、`已连续签到 N 天`、`今日签到排名` 等是 NexusPHP 的累计数据，
        站点首页与用户面板都会展示，只对 GET 一次即完成签到的站点作为证据；对需要 POST
        的站点（AGSVPT、PTzone）不足以确认本次签到，避免误报成功。
        """
        strong = ("签到成功", "簽到成功", "本次签到获得", "本次簽到獲得", "签到已得",
                  "簽到已得", "已签到", "已簽到", "已经签到", "已經簽到",
                  "本次签到您获得", "本次簽到您獲得")
        text = re.sub(r"\s+", " ", html.unescape(body))
        if any(kw in text for kw in strong):
            return True
        if sign_method == SIGN_METHOD_POST:
            return False
        weak = ("这是您的第", "這是您的第", "已连续签到", "已連續簽到",
                "今日签到排名", "今日簽到排名", "您今天已经签到过了", "您今天已經簽到過了",
                "You have already attend")
        return any(kw in text for kw in weak)

    @staticmethod
    def __status_from_message(message: str) -> str:
        """按提取到的文案区分「今日已签到」与「本次签到成功」（兼容繁体站点文案）。"""
        text = message or ""
        if any(kw in text for kw in ("已签到", "已簽到", "已经签到", "已經簽到",
                                     "重复签到", "重複簽到", "无需", "無需", "already")):
            return STATUS_ALREADY
        return STATUS_SIGNED

    @classmethod
    def __resolve_signed_status(cls, message: str, already_flag: bool, body: str) -> str:
        """
        统一裁决「已签到」与「签到成功」两种状态。

        只要页面出现当日已完成的确认文案（`这是您的第 N 次签到`、`已连续签到 N 天`、
        `本次签到获得 X`、`今日已完成`），就说明该账号今天已经签到过，记为已签到；
        只有拿到明确的本次动作证据、且页面不含上述确认文案时，才记为签到成功。
        """
        if already_flag or cls.__status_from_message(message) == STATUS_ALREADY:
            return STATUS_ALREADY
        if cls.__is_signed_result(body):
            return STATUS_ALREADY
        return STATUS_SIGNED

    @staticmethod
    def __is_signed_result(body: str) -> bool:
        """
        判断页面是否为「当日签到已完成」的确认页。

        NexusPHP 在各站点上的表现并不一致：有些站点（如 Depth Studio）会额外渲染
        「今日已完成 / 已签到」这类标签，有些站点（慕雪阁、AGSVPT、PTzone）则只渲染
        「签到成功 + 这是您的第 N 次签到，已连续签到 N 天，本次签到获得 X」。

        判定依据只取「已计入历史」的证据——累计签到次数、连续签到天数、今日已完成标记。
        这些字段只有在本次签到已经被服务端记账后才会渲染，因此同一账号当天再次请求
        attendance.php 时会稳定命中；而首次签到的成功页只有「本次签到获得 X」这类
        单次动作文案，不包含累计信息，仍然会正确记为签到成功。

        注意：必须先剥离标签再匹配。真实 HTML 里 `这是您的第 <b>368</b> 次签到` 这类
        结构会让直接对 HTML 取正则失效，只剩纯文本才可靠。
        """
        text = re.sub(r"<[^>]+>", " ", html.unescape(body))
        text = re.sub(r"\s+", " ", text)
        return bool(
            re.search(r"(这是您的第|這是您的第)\s*[\d,]+\s*次(签到|簽到)", text)
            or re.search(r"(已)?(连续|連續)签到\s*[\d,]+\s*天", text)
            or re.search(r"(今日|今天)\s*(已完成|都已经完成|已經完成)", text)
            or re.search(r"您(今天|今日)(已经|已經)(签到|簽到)", text)
        )

    @staticmethod
    def __looks_like_captcha(body: str) -> bool:
        if re.search(r'name=["\']imagehash["\']', body, re.I):
            return True
        if re.search(r'name=["\']imagestring["\']', body, re.I):
            return True
        if re.search(r'<img[^>]*class=["\'][^"\']*captcha[^"\']*["\']', body, re.I):
            return True
        return "验证码" in body and "签到" in body and re.search(r'<form[^>]*method=["\']post["\']', body, re.I) is not None

    def __extract_form(self, html_text: str, default_url: str) -> Tuple[Dict[str, str], str]:
        """
        解析签到表单的提交地址与字段。

        NexusPHP 各站模板差异较大：属性可能使用单引号或无引号，签到按钮可能是
        `<input type="submit" value="立即签到">` 这种没有 name 属性的元素。
        因此先按 name 提取隐藏字段，取不到时再回退按 value 提取按钮字段（例如
        `signin`/`submit`/`ok`），避免 AGSVPT 一类站点提取为空表单而无法签到。
        """
        form_match = re.search(r'<form[^>]*\baction\s*=\s*("([^"]*)"|\'([^\']*)\'|([^\s>]+))[^>]*>(.*?)</form>',
                               html_text, re.S | re.I)
        action_url = default_url
        form_body = html_text
        if form_match:
            raw_action = (form_match.group(2) or form_match.group(3) or form_match.group(4) or "").strip()
            if raw_action:
                action_url = urljoin(default_url, raw_action)
            form_body = form_match.group(5)

        def _attr(tag: str, name: str) -> str:
            """按单引号、双引号或无引号形式读取一个属性值。"""
            m = re.search(rf'\b{name}\s*=\s*("([^"]*)"|\'([^\']*)\'|([^\s>]+))', tag, re.I)
            if not m:
                return ""
            return html.unescape(m.group(2) or m.group(3) or m.group(4) or "")

        skip_names = ("submit", "button", "imagehash", "imagestring", "action")
        form_data: Dict[str, str] = {}
        for m in re.finditer(r'<input\b[^>]*>', form_body, re.I):
            tag = m.group(0)
            input_type = _attr(tag, "type").strip().lower()
            if input_type in ("password", "file", "button", "image", "reset"):
                continue
            name = _attr(tag, "name")
            if name:
                if name in skip_names:
                    continue
                form_data[name] = _attr(tag, "value")
                continue
            # 无 name 的提交按钮：NexusPHP 的签到就是提交这个按钮
            if input_type == "submit":
                value = _attr(tag, "value")
                if value:
                    form_data["signin"] = value

        return form_data, action_url

    def __extract_reward(self, html_text: str) -> Tuple[str, bool]:
        """
        从签到结果页提取奖励与累计数据。

        各站文案差异较大（「获得 100 个魔力值」「本次签到获得 5 个积分」
        「这是您的第 123 次签到」「已连续签到 7 天」「今日签到排名：12/3456」等），
        因此用一组宽松的正则分别捕获，全部失败时退化为「今日已签到」。

        返回值第二个元素表示是否找到“今天已经签过”的证据，调用方据此决定把状态记为
        `signed` 还是 `already`。这一区分很关键：NexusPHP 的累计数据（连续天数、
        累计次数、今日排名、补签卡、今日已得魔力值）在首页、用户面板以及重复签到时的
        提示页里都会展示，只有「本次签到获得」「签到成功」才是本次签到动作的产物。
        """
        try:
            text = re.sub(r"<[^>]+>", " ", html.unescape(html_text))
            text = re.sub(r"\s+", " ", text).strip()
            parts: List[str] = []

            # “今天已经签过”的显式文案（含繁体，PTzone 用「您今天已經簽到過了」）
            # 仅保留真正的「重复签到」显式文案：`已签到`、`连续 N 天` 等既可能出现在
            # 重复签到页，也可能出现在首签成功页，交由 __is_signed_result 统一裁决，
            # 避免在首签成功时被误标为已签到。
            already_keywords = ("您今天已经签到过了", "您今天已經簽到過了", "今天已经签到过",
                                "今天已經簽到過", "您已经签到过了", "您已經簽到過了",
                                "已经签到过了", "已經簽到過了", "重复签到", "重複簽到",
                                "无需重复", "無需重複", "请勿重复", "請勿重複",
                                "You have already attend")
            already = any(kw in text for kw in already_keywords)

            # 魔力值 / 积分 / 冰晶 / 金币等奖励（覆盖简繁文案与各站自有货币名）：
            # 简体「本次签到获得 1000 个魔力值」/ 繁体「本次簽到獲得 1000 個魔力值」
            # AGSVPT 使用「冰晶」作为积分单位，同样需要识别
            reward = self.__match_number(
                text,
                r"(本次签到(?:您)?获得|本次簽到(?:您)?獲得|签到成功(?:，|,| )?(?:获得|已得)"
                r"|簽到成功(?:，|,| )?(?:獲得|已得)|本次签到已得|本次簽到已得)"
                r"\s*([\d,\.]+)\s*(?:个|個|枚|點|点)?\s*"
                r"(魔力值|魔力|积分|積分|金币|金幣|冰晶|威望|奖励|獎勵|茉莉|梨子|憨豆|猫粮|貓糧)",
            )
            if reward:
                parts.append(f"获得 {reward[1]} {reward[2]}")
            else:
                # 兜底：只展示“获得 N 单位”的页面（如重复签到提示页），不计入本次签到证据
                fallback = self.__match_number(
                    text,
                    r"(?:今日已得|今日獲得|今日获得|已获得|已獲得|获得|獲得)\s*([\d,\.]+)\s*"
                    r"(?:个|個|枚|點|点)?\s*"
                    r"(魔力值|魔力|积分|積分|金币|金幣|冰晶|威望|奖励|獎勵|茉莉|梨子|憨豆|猫粮|貓糧)",
                )
                if fallback:
                    parts.append(f"获得 {fallback[0]} {fallback[1]}")

            # 连续签到天数（简繁）
            days = self.__match_number(text, r"(?:已)?连续签到\s*([\d,]+)\s*天") \
                or self.__match_number(text, r"(?:已)?連續簽到\s*([\d,]+)\s*天")
            if days:
                parts.append(f"连续 {days[0]} 天")

            # 累计签到次数（简繁）
            total = self.__match_number(text, r"这是您的第\s*([\d,]+)\s*次签到") \
                or self.__match_number(text, r"這是您的第\s*([\d,]+)\s*次簽到")
            if total:
                parts.append(f"累计 {total[0]} 次")

            # 今日签到排名（属于“今日已签到”的描述，不代表本次签到动作）
            rank = self.__match_number(text, r"今日签到排名[：: ]*\s*([\d,]+)\s*(?:/|名|位)") \
                or self.__match_number(text, r"今日簽到排名[：: ]*\s*([\d,]+)\s*(?:/|名|位)")
            if rank:
                parts.append(f"今日排名 {rank[0]}")

            # 补签卡
            card = self.__match_number(text, r"补签卡\s*([\d,]+)\s*张") \
                or self.__match_number(text, r"補簽卡\s*([\d,]+)\s*張")
            if card:
                parts.append(f"补签卡 {card[0]} 张")

            return (" | ".join(parts) if parts else "今日已签到"), already
        except Exception as e:
            logger.debug(f"提取奖励异常: {e}")
            return "今日已签到", True

    @staticmethod
    def __match_number(text: str, pattern: str) -> Tuple[str, ...]:
        """返回正则各捕获组（去掉数字中的千分位逗号），未匹配到则返回空元组。"""
        m = re.search(pattern, text)
        if not m:
            return ()
        return tuple((g or "").replace(",", "") for g in m.groups())

    @staticmethod
    def __extract_error(html_text: str) -> str:
        patterns = [
            r'<div[^>]*class="[^"]*(?:error|danger|alert)[^"]*"[^>]*>(.*?)</div>',
            r'<span[^>]*class="[^"]*error[^"]*"[^>]*>(.*?)</span>',
            r'<p[^>]*class="[^"]*error[^"]*"[^>]*>(.*?)</p>',
            r'stderr\([^)]*\)\s*[^<]*?=>\s*([^<\n]+)',
        ]
        for pat in patterns:
            m = re.search(pat, html_text, re.S | re.I)
            if m:
                text = re.sub(r"<[^>]+>", "", m.group(1)).strip()
                if text:
                    return text[:120]
        m = re.search(r"[^<>]{2,80}失败[^<>]{0,40}", html_text)
        if m:
            return m.group(0).strip()[:120]
        return ""

    @staticmethod
    def __snippet(html_text: str, limit: int = 200) -> str:
        text = re.sub(r"<[^>]+>", " ", html.unescape(html_text))
        text = re.sub(r"\s+", " ", text).strip()
        return text[:limit]

    # -------------------------------------------------------------- 内部：站点数据

    def __site_proxies(self, domain: str) -> Optional[Dict[str, str]]:
        """
        按站点管理里的「使用代理」开关决定本次请求是否走系统代理。

        AGSVPT 这类站点在国内网络直连会一直无响应，只有在站点管理里勾选「使用代理」
        并配置好 MoviePilot 系统代理才可访问；慕雪阁/Depth Studio 等直连站点则不应
        被强制走上代理。站点未配置「使用代理」时保持原行为（跟随系统代理设置）。
        """
        try:
            site = self.__lookup_site(domain)
            if site is None:
                return settings.PROXY or None
            if getattr(site, "proxy", 0):
                if not settings.PROXY:
                    logger.warning(f"站点 {domain} 勾选了「使用代理」，但系统未配置代理")
                    return None
                return settings.PROXY
            return None
        except Exception as e:
            logger.error(f"读取 {domain} 代理设置异常: {e}")
            return settings.PROXY or None

    def __get_site_cookie(self, domain: str, name: str) -> str:
        try:
            site = self.__lookup_site(domain)
            if site and site.cookie:
                actual_domain = getattr(site, "domain", domain) or domain
                logger.info(f"✅ 读取到 {name}（{actual_domain}）的 Cookie")
                return site.cookie.strip()
            logger.warning(f"⚠️ 未在站点管理中找到 {name}（{domain}）的 Cookie")
            self.__log_all_sites(name)
            return ""
        except Exception as e:
            logger.error(f"读取 {name} Cookie 异常: {e}")
            return ""

    def __lookup_site(self, domain: str) -> Optional[Any]:
        """
        按域名查找站点，先做精确匹配，失败时按常见域名变体回退。

        MoviePilot 站点管理里存的是 `domain` 字段，且该字段在首次添加站点时
        写死、后续改 URL 不会同步更新，常出现插件查 `pt.muxuege.org` 但库里是
        `muxuege.org` 的情况。这里兼容 scheme、尾斜杠、www、pt 子域等差异。
        """
        def _norm(d: Any) -> str:
            if not d:
                return ""
            s = str(d).strip().lower()
            for prefix in ("https://", "http://"):
                if s.startswith(prefix):
                    s = s[len(prefix):]
            # 去掉路径与端口
            s = s.split("/")[0].split(":")[0]
            if s.startswith("www."):
                s = s[4:]
            return s

        req = _norm(domain)
        if not req:
            return None

        # 候选域名：自身、去掉 pt 子域、加上 pt 子域
        candidates = {req}
        parts = req.split(".")
        if len(parts) >= 3 and parts[0] not in ("www", ""):
            candidates.add(".".join(parts[1:]))
        if len(parts) == 2:
            candidates.add(f"pt.{req}")

        # 1. 按候选 domain 精确查
        for cand in candidates:
            if not cand:
                continue
            site = SiteOper().get_by_domain(cand)
            if site:
                return site

        # 2. 兜底：遍历所有站点，按 domain/url 模糊匹配
        try:
            all_sites = SiteOper().list() or []
        except Exception:
            all_sites = []
        for site in all_sites:
            if not site:
                continue
            site_domain = _norm(site.domain)
            site_url = _norm(site.url)
            if site_domain in candidates:
                return site
            if site_url and req in site_url:
                return site
            # 子域/父域关系（要求差异至少包含一个点，避免 .com 误匹配）
            for cand in candidates:
                if site_domain and site_domain != cand:
                    if site_domain.endswith(cand):
                        extra = site_domain[:-len(cand)].rstrip(".")
                        if extra and "." in extra:
                            return site
                    if cand.endswith(site_domain):
                        extra = cand[:-len(site_domain)].rstrip(".")
                        if extra and "." in extra:
                            return site
        return None

    def __log_all_sites(self, name: str):
        """失败时打印站点管理里所有站点的域名/URL，方便排查"""
        try:
            sites = SiteOper().list() or []
            lines = []
            for site in sites:
                has_cookie = "有" if (getattr(site, "cookie", None) or "").strip() else "无"
                lines.append(
                    f"  - 名称={site.name} | domain={site.domain} | url={site.url} | cookie={has_cookie}"
                )
            if lines:
                logger.warning(
                    f"慕雪自动签到：未匹配到 {name}，当前「站点管理」中的站点如下（请核对 domain 列）：\n"
                    + "\n".join(lines)
                )
        except Exception:
            pass

    def __record(self, site_key: str, status: str, message: str) -> Dict[str, Any]:
        record = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": status,
            "message": message,
        }
        history = self.get_data(f"history_{site_key}") or []
        if not isinstance(history, list):
            history = []
        history.insert(0, record)
        if len(history) > self.MAX_HISTORY:
            history = history[:self.MAX_HISTORY]
        self.save_data(f"history_{site_key}", history)
        return record

    def _clear_all_history(self):
        for site in SITES:
            self.save_data(f"history_{site['key']}", [])
        self.save_data("lastrun", None)