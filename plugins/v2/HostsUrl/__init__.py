"""
MoviePilot V2 插件 - 远程Hosts
==============================
功能:
  1. 从配置的远程 URL 拉取 hosts 规则(替代手动粘贴)
  2. 定时自动更新(可配置 cron 表达式)
  3. 手动点击按钮立即更新(API 触发)
  4. 与系统 hosts 集成(使用 # HostsUrlPlugin 段标记,可与其它自定义hosts共存)
  5. 错误信息、更新时间、规则数等实时回显到配置页

部署: 将本目录整体复制到 MoviePilot 的 app/plugins/ 目录下,重启后即可在插件市场看到 "远程Hosts"。
"""
import re
import time
from typing import List, Tuple, Dict, Any

import requests
import urllib3
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from python_hosts import Hosts, HostsEntry

from app.core.event import eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType
from app.utils.ip import IpUtils
from app.utils.system import SystemUtils

# 关闭 urllib3 的 SSL 不安全警告(允许 verify=False)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class HostsUrl(_PluginBase):
    # ===== 插件元信息 =====
    plugin_name = "远程Hosts"
    plugin_desc = "定时从远程URL拉取hosts并写入系统,支持手动立即更新。"
    plugin_icon = "hosts.png"
    plugin_version = "1.0.0"
    plugin_author = "you"
    author_url = "https://github.com/"
    plugin_config_prefix = "hostsurl_"
    plugin_order = 10
    auth_level = 1

    # ===== 私有属性 =====
    _enabled: bool = False
    _source_url: str = ""
    _cron: str = ""
    _run_on_init: bool = False
    _timeout: int = 30
    _scheduler: BackgroundScheduler = None

    # ===== 初始化 =====
    def init_plugin(self, config: dict = None):
        # 任何重载都先关掉旧调度器
        self.__stop_scheduler()

        if config:
            self._enabled = bool(config.get("enabled"))
            self._source_url = (config.get("source_url")
                                or "https://raw.githubusercontent.com/938134/check_hosts/main/hosts").strip()
            self._cron = (config.get("cron") or "0 3 * * *").strip()
            self._run_on_init = bool(config.get("run_on_init"))
            try:
                self._timeout = int(config.get("timeout") or 30)
            except (TypeError, ValueError):
                self._timeout = 30

        if not self._enabled:
            logger.info("远程Hosts插件未启用,跳过初始化")
            return

        if not self._source_url:
            logger.error("远程Hosts插件未配置 source_url,跳过")
            return

        if not self._cron:
            logger.error("远程Hosts插件未配置 cron,跳过")
            return

        # 启动调度器
        self.__start_scheduler()

        # 是否在启动后立即执行一次
        if self._run_on_init and self._scheduler:
            self._scheduler.add_job(
                self.__update_hosts_job,
                "date",
                run_date=time.strftime("%Y-%m-%d %H:%M:%S"),
                id="hostsurl_run_on_init",
            )

    def get_state(self) -> bool:
        return self._enabled

    # ===== 暴露给前端的命令(本插件用 API 触发) =====
    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    # ===== 暴露给前端的 API =====
    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/refresh",
                "endpoint": self.__api_refresh,
                "methods": ["GET", "POST"],
                "summary": "立即更新hosts",
                "description": "从配置的URL拉取最新hosts并写入系统",
            }
        ]

    def get_page(self) -> List[dict]:
        pass

    # ===== 配置页面 =====
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                'component': 'VForm',
                'content': [
                    # 第一行:开关
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'run_on_init',
                                            'label': '启用时立即执行一次',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'clear_before_write',
                                            'label': '写入前先清空旧规则',
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                    # 第二行:URL + 超时
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 8},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'source_url',
                                            'label': '远程hosts URL',
                                            'placeholder': 'https://raw.githubusercontent.com/xxx/hosts',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'timeout',
                                            'label': '请求超时(秒)',
                                            'type': 'number',
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                    # 第三行:cron + 立即更新按钮
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 8},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '定时执行 (5段cron)',
                                            'placeholder': '0 3 * * *  (例:每天凌晨3点)',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VBtn',
                                        'props': {
                                            'color': 'primary',
                                            'block': True,
                                            'text': '立即更新',
                                            'api': 'post',
                                            'apiUrl': 'plugin/HostsUrl/refresh',
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                    # 第四行:状态回显
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'last_update',
                                            'label': '上次更新时间',
                                            'readonly': True,
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'last_count',
                                            'label': '上次规则数',
                                            'readonly': True,
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'last_error',
                                            'label': '最近一次错误',
                                            'readonly': True,
                                            'rows': 2,
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # 底部说明
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '从远程URL拉取 IP+host 格式的规则,自动写入系统hosts(注:容器运行则更新容器hosts!非宿主机!)。'
                                                    '远程文件每行格式: <ip> <host1> <host2> ... '
                                                    '插件标识注释为 # HostsUrlPlugin,可与自定义hosts插件共存。'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "source_url": "https://raw.githubusercontent.com/938134/check_hosts/main/hosts",
            "cron": "0 3 * * *",
            "timeout": 30,
            "run_on_init": False,
            "clear_before_write": True,
            "last_update": "",
            "last_count": 0,
            "last_error": "",
        }

    # ===== 调度器管理 =====
    def __start_scheduler(self):
        try:
            # 先校验 cron
            CronTrigger.from_crontab(self._cron)
        except Exception as err:
            msg = f"无效的 cron 表达式 '{self._cron}': {err}"
            logger.error(msg)
            self.systemmessage.put(msg, title="远程Hosts")
            return

        try:
            self._scheduler = BackgroundScheduler(timezone='Asia/Shanghai')
            self._scheduler.add_job(
                self.__update_hosts_job,
                CronTrigger.from_crontab(self._cron),
                id="hostsurl_periodic",
                replace_existing=True,
            )
            self._scheduler.start()
            logger.info(f"远程Hosts定时任务已启动: cron='{self._cron}', url='{self._source_url}'")
        except Exception as err:
            logger.error(f"启动远程Hosts定时任务失败: {err}")
            self.systemmessage.put(f"启动远程Hosts定时任务失败: {err}", title="远程Hosts")

    def __stop_scheduler(self):
        if self._scheduler:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception as e:
                logger.debug(f"停止远程Hosts调度器时异常: {e}")
            self._scheduler = None

    def __update_hosts_job(self):
        """定时任务执行体"""
        try:
            count = self.__do_update()
            msg = f"远程Hosts定时更新成功,共 {count} 条规则"
            logger.info(msg)
            self.systemmessage.put(msg, title="远程Hosts")
        except Exception as err:
            logger.error(f"远程Hosts定时更新失败: {err}")
            self.systemmessage.put(f"远程Hosts定时更新失败: {err}", title="远程Hosts")
            self.update_config({
                "last_error": str(err),
                "last_update": time.strftime("%Y-%m-%d %H:%M:%S"),
            })

    # ===== 核心流程 =====
    def __do_update(self) -> int:
        """
        拉取并写入系统 hosts,返回成功写入的规则数。
        出错抛异常(供上层捕获并记录)。
        """
        if not self._source_url:
            raise Exception("source_url 未配置")

        rules = self.__fetch_rules(self._source_url)
        if not rules:
            raise Exception(f"未从 {self._source_url} 解析到任何有效规则")

        formatted = [f"{ip} {' '.join(names)}" if isinstance(names, list) else f"{ip} {names}"
                     for ip, names in rules]

        # 写系统 hosts(永远覆盖本插件的旧段)
        err_flag, err_hosts = self.__write_to_system(formatted)

        now = time.strftime("%Y-%m-%d %H:%M:%S")
        update_payload = {
            "last_update": now,
            "last_count": len(rules),
        }
        if err_hosts:
            update_payload["last_error"] = f"{len(err_hosts)} 条规则格式错误,已跳过"
        else:
            update_payload["last_error"] = ""
        self.update_config(update_payload)
        return len(rules)

    def __fetch_rules(self, url: str) -> List[Tuple[str, List[str]]]:
        """
        拉取远程 hosts 并解析为 [(ip, [host, host, ...]), ...]
        一行多个 host 也支持(同 IP 多域名)。
        """
        session = requests.Session()
        retry = Retry(
            total=5,
            backoff_factor=1,
            status_forcelist=[500, 502, 503, 504],
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)

        try:
            resp = session.get(url, timeout=self._timeout, verify=False)
            resp.raise_for_status()
            text = resp.text
        except Exception as e:
            raise Exception(f"请求失败: {e}")

        # 匹配: <ip> <host1> <host2> ...
        rule_pattern = re.compile(
            r"^\s*(\d{1,3}(?:\.\d{1,3}){3})\s+([a-zA-Z0-9\.\-]+(?:\s+[a-zA-Z0-9\.\-]+)*)",
            re.MULTILINE,
        )

        result: List[Tuple[str, List[str]]] = []
        for ip, hosts_str in rule_pattern.findall(text):
            hosts = hosts_str.split()
            # 过滤空、去重保序
            seen = set()
            uniq_hosts = []
            for h in hosts:
                if h and h not in seen:
                    seen.add(h)
                    uniq_hosts.append(h)
            if uniq_hosts:
                result.append((ip, uniq_hosts))
        return result

    @staticmethod
    def __read_system_hosts():
        if SystemUtils.is_windows():
            hosts_path = r"c:\windows\system32\drivers\etc\hosts"
        else:
            hosts_path = '/etc/hosts'
        return Hosts(path=hosts_path)

    def __write_to_system(self, hosts: List[str]):
        """
        写入系统 hosts。
        先把属于本插件的旧段(# HostsUrlPlugin 起)整体剥掉,再追加新段。
        返回 (err_flag, err_hosts_lines)。
        """
        system_hosts = self.__read_system_hosts()

        # 过滤掉插件添加的旧段
        orgin_entries = []
        for entry in system_hosts.entries:
            if entry.entry_type == "comment" and entry.comment == "# HostsUrlPlugin":
                break
            orgin_entries.append(entry)
        system_hosts.entries = orgin_entries

        new_entries = []
        err_hosts: List[str] = []
        for host in hosts:
            host = host.strip()
            if not host:
                continue
            host_arr = host.split()
            if len(host_arr) < 2:
                err_hosts.append(host + "\n")
                continue
            try:
                host_entry = HostsEntry(
                    entry_type='ipv4' if IpUtils.is_ipv4(host_arr[0]) else 'ipv6',
                    address=host_arr[0],
                    names=host_arr[1:],
                )
                new_entries.append(host_entry)
            except Exception as err:
                err_hosts.append(host + "\n")
                logger.error(f"[HOST] 格式转换错误: {err}")

        if not new_entries:
            return bool(err_hosts), err_hosts

        try:
            # 添加分段标识 + 注释行(更新时间)
            system_hosts.add([
                HostsEntry(entry_type='comment', comment="# HostsUrlPlugin"),
                HostsEntry(
                    entry_type='comment',
                    comment=f"# Updated at: {time.strftime('%Y-%m-%d %H:%M:%S')}",
                ),
            ])
            system_hosts.add(new_entries)
            system_hosts.write()
            logger.info(f"远程Hosts写入系统成功: {len(new_entries)} 条")
        except Exception as err:
            raise Exception(f"写入系统hosts失败: {err} (容器运行请确认有权限)")

        return bool(err_hosts), err_hosts

    # ===== API 端点 =====
    def __api_refresh(self) -> Dict[str, Any]:
        """
        手动触发更新。MoviePilot 前端通过 VBtn(api=post, apiUrl=plugin/HostsUrl/refresh) 调用。
        """
        if not self._source_url:
            return {"success": False, "message": "未配置 source_url,请先在配置页填写远程URL"}

        try:
            count = self.__do_update()
            return {"success": True, "message": f"更新成功,共写入 {count} 条规则"}
        except Exception as err:
            logger.error(f"手动更新hosts失败: {err}")
            self.update_config({"last_error": str(err)})
            return {"success": False, "message": f"更新失败: {err}"}

    # ===== 生命周期 =====
    def stop_service(self):
        """插件卸载时清理"""
        self.__stop_scheduler()

    @eventmanager.register(EventType.PluginReload)
    def reload(self, event):
        """响应插件重载事件"""
        plugin_id = event.event_data.get("plugin_id")
        if not plugin_id:
            return
        if plugin_id != self.__class__.__name__:
            return
        return self.init_plugin(self.get_config())
