# 远程Hosts 插件 (MoviePilot V2)

## 功能

| 功能 | 说明 |
| ---- | ---- |
| 自动拉取 | 从配置的远程 URL 拉取 hosts 规则,解析 `IP + 域名` 格式 |
| 定时更新 | 自带 BackgroundScheduler,按 cron 表达式定时执行 |
| 手动更新 | 配置页 "立即更新" 按钮,POST 触发,实时返回结果 |
| 安全标识 | 写入的段以 `# HostsUrlPlugin` 开头,可与"自定义Hosts"插件共存 |
| 状态回显 | 配置页展示上次更新时间、规则数、错误信息 |

## 安装

把整个 `HostsUrl` 目录复制到 MoviePilot 的 `app/plugins/` 下,重启 MoviePilot。

```
MoviePilot/
  app/
    plugins/
      HostsUrl/
        __init__.py
        requirements.txt
```

## 配置项

| 字段 | 说明 | 默认值 |
| ---- | ---- | ---- |
| `enabled` | 总开关 | `false` |
| `source_url` | 远程 hosts URL(支持 `https://raw.githubusercontent.com/...` 等) | `https://raw.githubusercontent.com/938134/check_hosts/main/hosts` |
| `cron` | 5 段 cron 表达式 | `0 3 * * *` (每天凌晨 3 点) |
| `timeout` | HTTP 请求超时(秒) | `30` |
| `run_on_init` | 启用 / 重载后是否立即执行一次 | `false` |
| `clear_before_write` | 写入前清空本插件旧段(永远会清,本字段保留) | `true` |

> 配置完成后点 "保存",定时任务立即生效;再点 "立即更新" 立刻拉取一次。

## 远程文件格式

每行一条,IP 在前,域名在后,中间空格分隔,支持一行多域名:

```
# 自定义 GitHub hosts
140.82.112.4   github.com
185.199.108.133 raw.githubusercontent.com
```

## 手动 API

```
POST /api/v1/plugin/HostsUrl/refresh
```

返回:

```json
{"success": true, "message": "更新成功,共写入 100 条规则"}
```

## 注意事项

- **容器运行写的是容器内 hosts,不是宿主机**。要给宿主机用,需要在宿主机直接跑拉取脚本。
- Windows 下需要管理员权限才能写入 `C:\Windows\System32\drivers\etc\hosts`。
- 远程 URL 不可用时,不会清空旧 hosts,只会在配置页和消息通知里报错。
