# Cloudflare 多线路智能容灾管理中心

这是一个**中文、可审计**的 Linux 运维工具，用来管理多条 Cloudflare DNS 容灾线路。
仓库同时保留旧版 `cf_manager.py` 兼容入口，并提供推荐的 v2“配置与代码分离”架构：一个通用容灾引擎、一个 JSON 账本、一个中文菜单和统一的 systemd 模板。

> 重要：本项目会修改 DNS、systemd 和 Telegram 机器人，属于生产运维工具。第一次使用请先在测试目录演练，确认 IP、Zone ID、Record ID 和 Token 都正确，再放到 VPS 上运行。

## 已完成的功能

- 中文 ANSI 菜单：状态总览、添加线路、Cloudflare 记录检索、编辑、删除、服务管理、全量备份。
- 线路分项编辑：选定线路后可查看脚本名与全部绑定域名，只修改主 IP、备用 IP，手动添加/删除域名；每次保存自动重写脚本并重启对应 systemd 服务。
- Cloudflare Token：优先读取环境变量或已有 `cf_failover*.py`，也支持交互输入并调用 Token Verify API。
- Cloudflare Zone/A 记录：列出 Zone 和 A 记录，支持逗号多选，也支持现场新建 A 记录。
- 自动脚本：5 包 TCP 平均延迟、三次间隔 20 秒复核、按官方节点列表筛选的 Check-Host 区域仲裁、状态文件和锁文件。
- 通知规范：群组消息不包含具体毫秒数，私聊消息包含目标 IP 和切换细节。
- 安全注入：`tg_bot.py` 修改前生成 `.bak`，使用 AST 定位字典并在写回前再次编译；失败自动恢复。
- 备份包：包含线路脚本、systemd 单元、机器人、配置和 `restore_cf_cluster.sh`。
- 离线自测：不访问网络、不执行 systemd，验证脚本生成、语法注入和备份逻辑。

## v2 标准架构（推荐新机器使用）

新版本把“线路数据”和“业务代码”彻底分开：所有线路集中放在 `/opt/cf-failover/config.json`，只运行一个通用 `failover_engine.py`，不再为 1、2、3、4、5 号线复制五份业务脚本。

```text
/opt/cf-failover/config.json       # 唯一配置账本，不提交到 GitHub
/opt/cf-failover/failover_engine.py # 通用容灾引擎
/opt/cf-failover/menu.py            # 中文菜单
/opt/cf-failover/tg_bot.py          # 动态读取配置的 TG Bot
cf-failover@1.service              # systemd 模板，线路编号统一
```

大白话理解：以后改 IP 或域名，只改一个 JSON 账本；菜单会自动重写配置、重启对应服务，避免复制脚本和手改 Python 语法。

每条线路也可以单独设置 `port`（例如 22、80、443）；不填写时使用全局 `tcp_port`。

安装器会在 `/opt/cf-failover/.venv` 建立项目独立的 Python 环境，不会把 Telegram 依赖强行装进 Debian/Ubuntu 的系统 Python。

### 新机器一键安装

Debian/Ubuntu 新 VPS 可以直接执行（先自动安装 Git）：

```bash
sudo apt-get update -y && sudo apt-get install -y git && git clone https://github.com/jaycen-0502/cf-failover-manager.git && cd cf-failover-manager && sudo bash install.sh
```

然后编辑配置并打开菜单：

```bash
sudo nano /opt/cf-failover/config.json
sudo cf-menu
```

菜单支持查看状态、新增/修改/删除线路、强制切主/切备、重启服务和导出配置备份。安装脚本不会自动把示例 Token 或示例 IP 当成生产配置。

### Token 和生产 IP 的填入/删除

进入 `sudo cf-menu` 后：

1. 选择 `[4] 配置全局 Token 与管理员 ID`。
2. 选择 `[1]` 填入/更新 Cloudflare Token，选择 `[2]` 删除 Cloudflare Token。
3. 选择 `[3]` 填入/更新 Telegram Token，选择 `[4]` 删除 Telegram Token。
4. 删除 Cloudflare Token 会自动停用全部线路；删除 Telegram Token 会自动停用 TG Bot。删除操作都要求输入 `DELETE` 二次确认。

填入 Token 时会先向对应官方 API 验证有效性；输错或网络验证失败不会覆盖原配置，也不会显示 Token 明文。

修改线路的主备生产 IP：

1. 主菜单选择 `[2]`，输入线路编号。
2. 选择 `[2]` 填入/修改主 IP，选择 `[3]` 填入/修改备用 IP。
3. 选择 `[7]` 删除主 IP，选择 `[8]` 删除备用 IP；删除 IP 后该线路会自动停用，重新填回合法 IP 后自动恢复服务。

每次有效修改都会原子写入 `/opt/cf-failover/config.json` 并自动重启对应线路；配置不完整时只保存、不启动服务，避免空 IP 进入监控。

### 通用引擎快捷命令

```bash
sudo python3 /opt/cf-failover/failover_engine.py --line 1 --status
sudo python3 /opt/cf-failover/failover_engine.py --line 1 --switch
sudo python3 /opt/cf-failover/failover_engine.py --line 1 --backup
sudo systemctl enable --now cf-failover@1.service
sudo systemctl enable --now tg-bot.service
```

## 目录

```text
cf_manager.py       # 旧版兼容管理入口
failover_engine.py  # v2 通用容灾引擎
menu.py             # v2 中文配置菜单
tg_bot.py           # v2 动态 Telegram Bot
install.sh          # v2 Linux 一键安装
systemd/            # v2 systemd 模板
config.example.json # 脱敏配置模板
README.md           # 本说明和大白话教程
examples/           # 示例配置和最小 tg_bot.py
tests/              # 标准库 unittest 测试
LICENSE             # MIT 许可证
```

## 大白话教程：先在电脑上安全演练

你现在是在 Windows 电脑上开发，不能直接执行 Linux 的 `systemctl`。先做离线自测：

```bash
python cf_manager.py --self-test
```

看到 `cf_manager self-test: PASS` 就说明生成器和注入器的基本逻辑正常。

如需在 Windows 上模拟生产目录，不要碰真实的 `/etc`、`/root`，给程序一个临时根目录：

```powershell
$env:CF_MANAGER_ROOT = "$PWD\work\demo-root"
$env:CF_MANAGER_DRY_RUN = "1"
python .\cf_manager.py
```

## VPS 部署步骤

仓库现在是公开仓库，VPS 只需要安装 `git` 和 Python 3，不需要配置 GitHub SSH key。下面这一行会在首次运行时克隆仓库，之后运行时自动快进更新；然后安装管理程序、运行自测并打开中文菜单：

```bash
if [ -d "$HOME/cf-failover-manager/.git" ]; then git -C "$HOME/cf-failover-manager" pull --ff-only; else git clone https://github.com/jaycen-0502/cf-failover-manager.git "$HOME/cf-failover-manager"; fi && sudo install -o root -g root -m 700 "$HOME/cf-failover-manager/cf_manager.py" /usr/local/bin/cf_manager && sudo python3 /usr/local/bin/cf_manager --self-test && sudo python3 /usr/local/bin/cf_manager
```

此部署命令只更新管理工具，不会自动修改 DNS 或创建线路。

不使用 GitHub 克隆时，也可以手动上传后安装：

```bash
sudo install -o root -g root -m 700 cf_manager.py /usr/local/bin/cf_manager
sudo python3 /usr/local/bin/cf_manager --self-test
sudo python3 /usr/local/bin/cf_manager
```

### 最简单的快捷运行

如果已经把仓库克隆到默认目录，日常升级和启动只需要这一条：

```bash
bash "$HOME/cf-failover-manager/deploy.sh"
```

第一次部署可以使用下面这一条：

```bash
git clone https://github.com/jaycen-0502/cf-failover-manager.git "$HOME/cf-failover-manager" && bash "$HOME/cf-failover-manager/deploy.sh"
```

安装完成后，以后直接运行：

```bash
sudo cfm
```

`deploy.sh` 会自动拉取最新代码并调用 v2 `install.sh`，安装完成后直接打开 `cf-menu`；公开仓库不需要 GitHub 登录凭证。

主菜单中选择 `[2]`，按提示填写：

1. 线路编号和别名；
2. 区域（`jp`、`us`、`eu`）；
3. 主 IP、备用 IP、探活端口和阈值；
4. Cloudflare Token；
5. Zone 和需要纳入切换的 A 记录。

向导确认后会生成：

```text
/usr/local/bin/cf_failover_N.py
/usr/local/bin/cfN.py
/etc/systemd/system/cf-failover-N.service
/etc/cf_manager/lines.json
```

如果存在 `/root/tg_bot.py`，程序会先创建 `/root/tg_bot.py.bak`，再注入服务和命令映射。注入完成后请检查输出的 BotFather 命令，并按需重启机器人。

### 修改指定线路

主菜单选择 `[4] 线路参数调整`，再输入线路编号。编辑菜单支持：

1. 查看该线路的监控脚本、手动脚本、主备 IP 和全部绑定域名；
2. 只修改主 IP；
3. 只修改备用 IP；
4. 手动添加域名（Zone ID、Record ID、完整域名）；
5. 删除指定域名（需要输入 `DELETE` 二次确认）；
6. 完整编辑线路参数。

每次保存都会重新生成对应 `cf_failover_N.py`、`cfN.py`，更新配置和 Telegram 映射，并执行 `systemctl stop`、`daemon-reload`、`enable --now` 自动重启该线路服务。域名添加只绑定已有 Cloudflare 记录，不会误创建 DNS 记录。

## 凭证配置

推荐用环境变量，避免把 Token 明文写在 shell 历史里：

```bash
export CF_API_TOKEN='你的 Cloudflare API Token'
export TG_TOKEN='你的 Telegram Bot Token'
export TG_GROUP_CHAT_ID='群组 Chat ID'
export TG_PRIVATE_CHAT_ID='私聊 Chat ID'
python3 /root/cf_manager.py
```

Token 至少需要读取 Zone 和编辑 DNS 记录的权限。程序不会把完整 Token 打印到屏幕，只显示首尾少量字符用于确认。

## 生成脚本的运行方式

```bash
# 查看线路状态，不进入无限循环
/usr/local/bin/cf_failover_6.py --status

# 手动进行一次健康检查（适合演练）
/usr/local/bin/cf_failover_6.py --once

# 手动切主 / 切备
/usr/local/bin/cf6.py main
/usr/local/bin/cf6.py backup
```

## 重要安全提醒

- `systemctl enable --now`、DNS 更新和删除线路都是生产动作，删除菜单需要输入 `DELETE` 二次确认。
- Check-Host 请求失败时，生成的监控脚本按保守策略拦截切线，避免监控机出口异常造成误切。
- 备份包可能包含 Cloudflare/Telegram 凭证，必须限制权限并妥善保存；不要直接提交到公开 GitHub。
- 生产环境请为 Token 设置最小权限，并定期轮换；失效 Token 应立即撤销。
- 脚本默认只更新 `ttl=60`、`proxied=false` 的 A 记录，不会主动开启 Cloudflare 代理。

## 测试

```bash
python -m unittest discover -s tests -v
python cf_manager.py --self-test
```

## 许可

MIT License。使用者需自行确认当地法律、云厂商条款和网络服务商政策。
