"""
慕雪自动签到插件 - 自动获取 Cookie

支持慕雪阁 (pt.muxuege.org) 与 Depth Studio (dstudio.me) 两个 NexusPHP 站点的自动签到。
Cookie 从 MoviePilot「站点管理」中读取，不在插件内另行配置。
"""
import html
import re
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.event import eventmanager, Event
from app.db.site_oper import SiteOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType
from app.utils.http import RequestUtils


# ---------------------------------------------------------------- 站点注册表

# 每个站点独立维护一份历史，避免互相覆盖
SITES: List[Dict[str, Any]] = [
    {
        "key": "muxuege",
        "name": "慕雪阁",
        "base_url": "https://pt.muxuege.org",
        "domain": "pt.muxuege.org",
        "config_enable": "enable_muxuege",
        "config_domain": "domain_muxuege",
        "config_alias": "慕雪阁 Cookie",
    },
    {
        "key": "dstudio",
        "name": "Depth Studio",
        "base_url": "https://dstudio.me",
        "domain": "dstudio.me",
        "config_enable": "enable_dstudio",
        "config_domain": "domain_dstudio",
        "config_alias": "Depth Studio Cookie",
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
    plugin_desc = "自动签到慕雪阁、Depth Studio 站点，Cookie 从站点管理读取"
    plugin_icon = "MuxueSignin.png"
    plugin_version = "1.0.0"
    plugin_author = "FUJIWARESHINE"
    plugin_config_prefix = "muxuesignin_"
    plugin_order = 21
    auth_level = 2

    # 常量
    MAX_HISTORY = 100
    REQUEST_TIMEOUT = 30
    USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/131.0.0.0 Safari/537.36")

    # 私有属性
    _enabled: bool = False
    _onlyonce: bool = False
    _cron: str = ""
    _notify: bool = False
    _site_enable: Dict[str, bool] = {}
    _site_domain: Dict[str, str] = {}
    _clear: bool = False
    _scheduler: Optional[BackgroundScheduler] = None
    _lock: threading.Lock = threading.Lock()

    # -------------------------------------------------------------- 生命周期

    def init_plugin(self, config: dict = None):
        self.stop_service()

        if config:
            self._enabled = config.get("enabled", False)
            self._onlyonce = config.get("onlyonce", False)
            self._cron = config.get("cron", "") or ""
            self._notify = config.get("notify", True)
            self._clear = config.get("clear", False)
            for site in SITES:
                self._site_enable[site["key"]] = bool(config.get(site["config_enable"], True))
                self._site_domain[site["key"]] = (
                    config.get(site["config_domain"]) or site["domain"]
                ).strip() or site["domain"]

        # 站点启用同步到站点数据，便于详情页读取
        for site in SITES:
            self.save_data(f"enable_{site['key']}", self._site_enable[site["key"]])

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
            "summary": "执行签到（可选 ?site=muxuege|dstudio）",
        }, {
            "path": "/history",
            "endpoint": self.__history_api,
            "methods": ["GET"],
            "auth": "bear",
            "summary": "获取签到历史",
        }]

    # -------------------------------------------------------------- 表单

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
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
                                        "请确保已为慕雪阁（pt.muxuege.org）与 Depth Studio（dstudio.me）"
                                        "分别配置好站点；\n"
                                        "2. NexusPHP 在关闭签到验证码时，GET 一次 attendance.php 即自动签到；"
                                        "开启验证码的站点需要手动签到一次；\n"
                                        "3. 单站签到失败不会影响其他站点的签到。"
                                    )
                                }
                            }]
                        }]
                    }
                ]
            }
        ], {
            "enabled": False,
            "notify": True,
            "onlyonce": False,
            "cron": "0 9 * * *",
            "clear": False,
            **{s["config_enable"]: True for s in SITES},
            **{s["config_domain"]: s["domain"] for s in SITES},
        }

    # -------------------------------------------------------------- 详情页

    def get_page(self) -> List[dict]:
        merged: List[Dict[str, Any]] = []
        for site in SITES:
            for record in (self.get_data(f"history_{site['key']}") or []):
                merged.append({**record, "site_key": site["key"], "site_name": site["name"]})

        last_run = self.get_data("lastrun")

        # 站点状态卡片
        cards = []
        for site in SITES:
            history = self.get_data(f"history_{site['key']}") or []
            enabled = self._site_enable.get(site["key"], True)
            last = history[0] if history else None
            last_status = STATUS_TEXT.get(last["status"], "-") if last else "尚未运行"
            last_time = last.get("time", "-") if last else "-"
            last_msg = (last.get("message") or "-") if last else "-"
            card_color = "primary"
            if last:
                if last["status"] == STATUS_SIGNED or last["status"] == STATUS_ALREADY:
                    card_color = "success"
                elif last["status"] in (STATUS_LOGIN_EXPIRED, STATUS_NETWORK):
                    card_color = "warning"
                elif last["status"] in (STATUS_FAILED, STATUS_CAPTCHA):
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
            for record in (self.get_data(f"history_{site['key']}") or []):
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
            for site in SITES:
                if site_filter and site["key"] != site_filter:
                    continue
                if not self._site_enable.get(site["key"], True):
                    logger.info(f"慕雪自动签到：{site['name']} 已禁用，跳过")
                    continue

                result = self.__sign_one(site)
                summary.append(f"{site['name']}：{STATUS_TEXT.get(result['status'], result['status'])}"
                               + (f" | {result['message']}" if result.get("message") else ""))

            self.save_data("lastrun", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            if self._notify and summary:
                ok = all(": 签到成功" in s or ": 今日已签到" in s for s in summary)
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
        key = site["key"]
        name = site["name"]
        base = site["base_url"]
        domain = self._site_domain.get(key, site["domain"])
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

        # 2. GET 探测
        try:
            res = RequestUtils(
                cookies=cookie,
                ua=settings.USER_AGENT,
                proxies=settings.PROXY,
                timeout=self.REQUEST_TIMEOUT,
            ).get_res(url=signin_url, headers=headers)
        except Exception as e:
            logger.error(f"{name}：签到请求异常: {e}")
            return self.__record(key, STATUS_NETWORK, f"请求异常：{str(e)[:80]}")

        if not res:
            return self.__record(key, STATUS_NETWORK, "签到请求无响应")
        if res.status_code != 200:
            return self.__record(key, STATUS_NETWORK, f"签到页返回 {res.status_code}")

        html_text = res.text

        # 登录跳转/未登录
        if self.__looks_like_login(html_text, res.url):
            return self.__record(key, STATUS_LOGIN_EXPIRED, "Cookie 已失效或被重定向到登录页")

        # 已签到/成功（GET 一次就自动签到的场景）
        if self.__check_already_signed(html_text):
            reward = self.__extract_reward(html_text)
            msg = reward if reward else "今日已签到"
            return self.__record(key, STATUS_ALREADY if "已签到" in msg else STATUS_SIGNED, msg)

        # 需要验证码
        if self.__looks_like_captcha(html_text):
            return self.__record(key, STATUS_CAPTCHA, "该站点当前要求验证码，请打开浏览器手动签到")

        # 3. 提交表单（部分站点需要 POST 才能签到）
        form_data, action_url = self.__extract_form(html_text, signin_url)
        post_result = self.__submit_signin(action_url, form_data, cookie, base, signin_url)
        if post_result["status"] in (STATUS_SIGNED, STATUS_ALREADY):
            return self.__record(key, post_result["status"], post_result["message"])
        return self.__record(key, post_result["status"], post_result["message"])

    # -------------------------------------------------------------- 内部：HTTP

    def __submit_signin(self, action_url: str, form_data: Dict[str, str],
                        cookie: str, base: str, referer: str) -> Dict[str, Any]:
        headers = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
            "content-type": "application/x-www-form-urlencoded",
            "origin": base,
            "referer": referer,
            "user-agent": self.USER_AGENT,
        }
        payload = dict(form_data)
        payload.setdefault("action", "signin")

        try:
            res = RequestUtils(
                cookies=cookie,
                ua=settings.USER_AGENT,
                proxies=settings.PROXY,
                timeout=self.REQUEST_TIMEOUT,
            ).post_res(url=action_url, headers=headers, data=payload, allow_redirects=True)
        except Exception as e:
            logger.error(f"提交签到异常: {e}")
            return {"status": STATUS_NETWORK, "message": f"提交异常：{str(e)[:80]}"}

        if not res:
            return {"status": STATUS_NETWORK, "message": "签到请求无响应"}
        if res.status_code != 200:
            return {"status": STATUS_NETWORK, "message": f"签到返回 {res.status_code}"}

        body = res.text

        if self.__looks_like_login(body, res.url):
            return {"status": STATUS_LOGIN_EXPIRED, "message": "Cookie 已失效"}

        if self.__check_already_signed(body):
            reward = self.__extract_reward(body)
            status = STATUS_ALREADY if "已签到" in (reward or "") else STATUS_SIGNED
            return {"status": status, "message": reward or "签到成功"}

        if self.__looks_like_captcha(body):
            return {"status": STATUS_CAPTCHA, "message": "该站点要求验证码，请手动签到"}

        err = self.__extract_error(body)
        if err:
            return {"status": STATUS_FAILED, "message": err}

        if "签到" in body and ("成功" in body or "完成" in body):
            reward = self.__extract_reward(body)
            return {"status": STATUS_SIGNED, "message": reward or "签到成功"}

        return {"status": STATUS_FAILED,
                "message": f"未知响应：{self.__snippet(body)[:120]}"}

    # -------------------------------------------------------------- 内部：解析

    @staticmethod
    def __looks_like_login(body: str, final_url: str) -> bool:
        if final_url and "login.php" in final_url:
            return True
        keywords = ("未登录", "您还没有登录", "该页面必须在登录后才能访问",
                    "请先登录", "需要登录", "登录后")
        return any(kw in body for kw in keywords)

    @staticmethod
    def __check_already_signed(body: str) -> bool:
        keywords = ("签到成功", "本次签到获得", "这是您的第", "已连续签到",
                    "今日签到排名", "您今天已经签到过了")
        return any(kw in body for kw in keywords)

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
        form_match = re.search(r'<form[^>]*action="([^"]*)"[^>]*>(.*?)</form>',
                               html_text, re.S | re.I)
        action_url = default_url
        form_body = html_text
        if form_match:
            raw_action = form_match.group(1).strip()
            if raw_action:
                if raw_action.startswith("/"):
                    action_url = urljoin(default_url, raw_action)
                elif not raw_action.startswith("http"):
                    action_url = urljoin(default_url, raw_action)
                else:
                    action_url = raw_action
            form_body = form_match.group(2)

        form_data: Dict[str, str] = {}
        for m in re.finditer(
            r'<input[^>]*\bname="([^"]+)"[^>]*\bvalue="([^"]*)"', form_body, re.I):
            name = m.group(1)
            if name in ("submit", "button", "imagehash", "imagestring"):
                continue
            form_data[name] = html.unescape(m.group(2))

        # 兜底：NexusPHP 原生表单可能无 hidden 字段，按钮即为提交
        if not form_data:
            form_data["action"] = "signin"

        return form_data, action_url

    def __extract_reward(self, html_text: str) -> str:
        try:
            text = re.sub(r"<[^>]+>", " ", html.unescape(html_text))
            text = re.sub(r"\s+", " ", text).strip()
            parts: List[str] = []

            m = re.search(r"本次签到获得\s*([\d,]+)\s*个魔力值", text)
            if m:
                parts.append(f"获得 {m.group(1).replace(',', '')} 魔力值")
            else:
                m = re.search(r"获得\s*([\d,]+)\s*个?魔力", text)
                if m:
                    parts.append(f"获得 {m.group(1).replace(',', '')} 魔力值")

            m = re.search(r"已连续签到\s*(\d+)\s*天", text)
            if m:
                parts.append(f"连续 {m.group(1)} 天")
            m = re.search(r"这是您的第\s*(\d+)\s*次签到", text)
            if m:
                parts.append(f"累计 {m.group(1)} 天")
            m = re.search(r"今日签到排名[：:]\s*(\d+)\s*/\s*(\d+)", text)
            if m:
                parts.append(f"排名 {m.group(1)}/{m.group(2)}")
            m = re.search(r"补签卡\s*(\d+)\s*张", text)
            if m:
                parts.append(f"补签卡 {m.group(1)} 张")

            return " | ".join(parts) if parts else "今日已签到"
        except Exception as e:
            logger.debug(f"提取奖励异常: {e}")
            return "今日已签到"

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

    def __get_site_cookie(self, domain: str, name: str) -> str:
        try:
            site = SiteOper().get_by_domain(domain)
            if site and site.cookie:
                logger.info(f"✅ 读取到 {name}（{domain}）的 Cookie")
                return site.cookie.strip()
            logger.warning(f"⚠️ 未在站点管理中找到 {name}（{domain}）的 Cookie")
            return ""
        except Exception as e:
            logger.error(f"读取 {name} Cookie 异常: {e}")
            return ""

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