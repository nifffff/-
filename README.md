# Cocofa 账号后台只读监控

这个工具直接通过自己的 HTTP 会话登录 Cocofa，在后台监控全部账号的运行状态、到期时间、金币、余额，以及账号新增和删除。监控期间不需要打开或保持网页、Chrome、Playwright 等浏览器窗口。

安全边界：程序只有登录时会向固定的同源 `/api/auth/login` 发送一次 POST；每次监控轮询只执行 `GET /api/auth/accounts`。代码没有停止、运行、查询、编辑、删除、添加账号等管理接口，也拒绝把登录端点配置到其他站点。

## macOS 安装与启动

需要 Python 3.9 或更高版本：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python monitor.py watch
```

项目已经附带可直接使用的 `config.json`。若需要恢复默认配置，可执行 `cp config.example.json config.json`。

公开的 `config.example.json` 使用占位地址；请在本地 `config.json` 中填写真实平台地址。`config.json` 已被 Git 忽略，不会上传。

## Windows 安装与启动

建议使用从 python.org 安装的 Python 3.9 或更高版本，并在安装时勾选 Tk/Tcl（标准安装默认包含）：

```bat
py -3 -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python monitor.py watch
```

登录窗口关闭后，命令窗口可以最小化，不需要保持任何网页或浏览器窗口。关闭命令窗口或按 `Ctrl+C` 会结束监控和仅存于内存的登录 Cookie。

## 启动后台监控

本地登录窗口会显示平台返回的验证码，并允许输入用户名、密码、验证码和本软件的轮询秒数。默认轮询间隔为 5 秒，界面或 `config.json` 都可以调大，最小值为 5 秒；这不会修改目标网站的任何自动刷新设置。

登录成功后窗口自动关闭，终端中的 Python 进程继续在后台轮询；不需要保留任何浏览器窗口。按 `Ctrl+C` 停止。

只检查一次可用：

```bash
python monitor.py once
```

## 凭据与会话

- 用户名、密码和验证码只存在于当前进程内，只用于向配置中的同源登录接口提交请求。
- 密码不会写入源代码、配置、终端日志、变更日志或快照；请求完成后界面变量会立即清空。
- 认证 Cookie 仅保存在内存里的 HTTP 会话中，不写磁盘。进程结束后必须重新登录。
- 验证码由用户本人识别；点击验证码图片可以刷新。
- 当前目标地址是 HTTP，传输不具备 HTTPS 加密。若平台以后支持 HTTPS，应立即把 `target_url` 改为 HTTPS。

## 输出

- `data/last_snapshot.json`：上一轮完整账号快照，原子写入。
- `logs/changes.jsonl`：每行一条带时间的变化记录。
- 控制台：中文显示账号新增、删除、运行状态、到期时间、金币和余额变化。

首次成功读取只建立基线，不会把全部现有账号误报为新增。网络失败时不覆盖旧快照，也不立即重试；程序会等待正常轮询周期后再试，避免高频请求。登录失效会停止并提示重新启动。

## 配置

- `target_url`：目标平台地址。
- `captcha_endpoint`：验证码 GET 接口，已按页面脚本确认是 `/api/auth/captcha`。
- `login_endpoint`：唯一允许的非只读请求，已确认是 `/api/auth/login`。
- `accounts_endpoint`：账号列表 GET 接口，已确认是 `/api/auth/accounts`。
- `poll_interval_seconds`：本地监控软件的刷新频率，默认 5 秒；界面可为本次运行调整，最小 5 秒。
- `request_timeout_seconds`：单次网络请求超时，默认 15 秒。
- `snapshot_file`、`change_log_file`：快照和变更日志位置。

接口路径集中在配置中，页面升级后可以统一调整。所有接口都必须与 `target_url` 同源，否则程序会拒绝启动，避免凭据被发往其他地址。

当前没有外部通知，因此不会把账号数据发送给第三方。需要微信、邮件或 Webhook 时，可在明确目标渠道后再接入；现有 JSONL 日志可以直接作为通知输入。
