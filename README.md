# sms-code-copy

监听 Mac 上收到的短信，命中关键词（默认：`验证码` / `动态密码` / `验证密码`）后自动提取短信验证码并复制到剪贴板，可选弹出系统通知。从此收到验证码后直接 ⌘V 粘贴即可。

> 适用场景：iPhone 的短信通过「短信转发」同步到 Mac 的「信息」App 后，本工具在后台轮询新短信，自动把验证码放进剪贴板。

## 特性

- **自动复制**：新短信命中关键词后自动提取验证码写入剪贴板
- **关键词可配置**：`config.json` 中自由增删关键词，修改后自动热重载，无需重启
- **开机自启可配置**：通过 `install.sh` / `uninstall.sh` 一键安装 / 卸载 launchd 后台服务
- **智能提取**：优先取关键词附近的数字串（支持 `123 456`、`123-456` 分隔格式），自动排除手机号、日期等干扰
- **系统通知**：复制成功后可弹 macOS 通知（可开关、可带提示音）
- **只读安全**：对短信数据库只读，绝不写入；仅使用 Python 标准库，零依赖
- **断点续扫**：记录已处理位置（`state.json`），重启后不重复复制；数据库被系统重建时自动重置游标

## 工作原理

1. 以 SQLite 只读模式（`mode=ro`）轮询 `~/Library/Messages/chat.db`（macOS「信息」数据库）；
2. 查询新增的收件短信（`is_from_me = 0`），命中关键词后提取验证码；
3. 通过 `pbcopy` 写入剪贴板，可选经 `osascript` 弹出通知。

## 环境要求

- macOS（自带 `python3` 即可，无需安装第三方依赖）
- iPhone 短信已转发到 Mac：iPhone 上「设置 → 信息 → 短信转发 → 勾选本 Mac」
- **完全磁盘访问权限**（读取短信数据库必需，见下文）

## 快速开始

```bash
# 1. 克隆并进入目录
git clone https://github.com/CangWeiohh/sms-code-copy.git
cd sms-code-copy

# 2. 生成配置文件（首次运行脚本也会自动生成）
cp config.example.json config.json
# 按需修改 keywords / poll_interval 等配置项

# 3. 测试关键词匹配与验证码提取（使用虚构内容，不影响游标）
python3 sms_code_copy.py --test '【示例服务】您的验证码为654321，10分钟内有效。'

# 4. 前台运行
python3 sms_code_copy.py
```

### 授予「完全磁盘访问权限」

macOS 会阻止普通进程读取 `~/Library/Messages/chat.db`，需要授权：

**系统设置 → 隐私与安全性 → 完全磁盘访问权限**，勾选以下二选一：

1. 运行脚本的终端 App（终端 / iTerm2 等）——适合前台手动运行；
2. Python 解释器的真实路径——适合 launchd 后台运行，路径查询：

```bash
python3 -c 'import sys; print(sys.executable)'
```

授权后需要重启终端（或重新加载 launchd 服务）才能生效。

## 开机自启（后台运行）

```bash
./install.sh      # 安装并立即启动；开机自动运行，崩溃自动拉起
./uninstall.sh    # 卸载自启并停止服务
```

`install.sh` 会：

- 生成 `~/Library/LaunchAgents/com.cangwei.sms-code-copy.plist`（路径自动填入，无需手改）；
- 首次安装时从 `config.example.json` 复制出 `config.json`；
- 通过 `launchctl bootstrap` 加载服务（`RunAtLoad` + `KeepAlive`）。

常用命令：

```bash
launchctl list | grep sms-code-copy                       # 查看运行状态
launchctl bootout gui/$(id -u)/com.cangwei.sms-code-copy  # 停止服务（保留自启配置）
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.cangwei.sms-code-copy.plist  # 重新启动
tail -f logs/sms-code-copy.log                            # 查看运行日志
```

> 注意：服务运行后修改 `config.json` 会自动热重载（关键词、轮询间隔等），只有 `db_path` 需要重启服务。

## 配置说明

所有配置项（`config.json`，均可省略，省略时使用默认值）：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `keywords` | `["验证码", "动态密码", "验证密码"]` | 关键词列表，命中任一即尝试提取验证码（不区分大小写） |
| `code_min_length` | `4` | 验证码最小位数 |
| `code_max_length` | `8` | 验证码最大位数 |
| `code_regex` | `""` | 自定义验证码正则，设置后优先于位数规则；含捕获组取第 1 组。例：`"验证码[:：]\\s*([A-Z0-9]{6})"` |
| `poll_interval` | `3` | 轮询间隔（秒） |
| `db_path` | `~/Library/Messages/chat.db` | 短信数据库路径 |
| `copy_prefix` | `""` | 写入剪贴板内容的前缀（默认只复制验证码本身） |
| `copy_suffix` | `""` | 写入剪贴板内容的后缀 |
| `notification` | `true` | 复制成功后是否弹 macOS 通知 |
| `notification_sound` | `false` | 通知是否带提示音 |
| `first_run_max_age_seconds` | `0` | 首次运行回看最近 N 秒内的短信；`0` = 跳过全部历史 |
| `log_level` | `INFO` | 日志级别：`DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `log_file` | `logs/sms-code-copy.log` | 日志文件路径（相对路径基于 `config.json` 所在目录；留空不写文件） |

## 命令行参数

| 参数 | 说明 |
| --- | --- |
| `--config PATH` | 配置文件路径（默认：脚本目录下 `config.json`） |
| `--once` | 只扫描一次后退出（用于测试） |
| `--test TEXT` | 对给定文本测试关键词匹配与验证码提取，并复制结果（不影响游标） |
| `--reset-cursor` | 忽略已保存的游标，从最新短信开始监听 |
| `--debug` | 输出 DEBUG 级日志 |
| `--version` | 显示版本号 |

## 验证码提取规则

1. 关键词匹配不区分大小写，且在短信正文中任意位置命中即可；
2. 优先在**关键词后方 20 个字符内**寻找第一个验证码候选，多个命中时取离关键词最近的；
3. 验证码候选为 `code_min_length` ~ `code_max_length` 位数字，允许 `123 456`、`123-456` 等分隔写法（复制时自动去除分隔符）；
4. 前后断言保证不会把手机号、订单号等更长数字串的一部分误认为验证码；
5. 关键词附近没有候选时，退化为在全文中取离关键词最近的候选；
6. 以上规则都不满足需求时，用 `code_regex` 完全自定义。

## 常见问题

**报错 `unable to open database file` / 无法读取短信？**
未授予完全磁盘访问权限，或授权后未重启终端 / 服务。见上文「授予完全磁盘访问权限」。

**收到了验证码短信但没有复制？**
1. 用 `--test` 确认关键词与提取规则：`python3 sms_code_copy.py --test '短信原文'`；
2. 查看日志 `logs/sms-code-copy.log`，若显示「命中关键词但未提取到验证码」，调整位数配置或改用 `code_regex`；
3. 确认短信是「收到」的（自己发出的不会处理），且 iPhone 已开启短信转发。

**通知不显示？**
launchd 后台服务在图形会话中运行时通知正常；若仍不显示，可先关闭再重新加载服务，或直接依赖剪贴板 ⌘V（通知只是辅助提示）。

**重复复制了旧验证码？**
正常情况下 `state.json` 保证不重复；如游标错乱，执行 `--reset-cursor` 重新开始。

## 隐私与安全

- 本工具只在本机运行，不联网、不上传任何数据；
- 对短信数据库**只读**，绝不写入；
- 日志中会包含验证码与发送方号码，`logs/`、`config.json`、`state.json` 均已加入 `.gitignore`，不会被提交；
- 本仓库不包含任何真实短信样例，提交 issue 时也请勿粘贴真实短信内容。

## 卸载

```bash
./uninstall.sh   # 停止并移除开机自启
# 如不再使用，删除整个项目目录即可
```
