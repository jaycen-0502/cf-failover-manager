# Cloudflare 多线路智能容灾管理中心

这是一个**中文、单文件、可审计**的 Linux 运维 CLI，用来管理多条 Cloudflare DNS 容灾线路。
它可以生成线路监控脚本、手动切换脚本和 systemd 服务，并在修改 Telegram 机器人前自动备份和做 Python 语法检查。

> 重要：本项目会修改 DNS、systemd 和 Telegram 机器人，属于生产运维工具。第一次使用请先在测试目录演练，确认 IP、Zone ID、Record ID 和 Token 都正确，再放到 VPS 上运行。

## 已完成的功能

- 中文 ANSI 菜单：状态总览、添加线路、Cloudflare 记录检索、编辑、删除、服务管理、全量备份。
- 线路分项编辑：选定线路后可查看脚本名与全部绑定域名，只修改主 IP、备用 IP，手动添加/删除域名；每次保存自动重写脚本并重启对应 systemd 服务。
- Cloudflare Token：优先读取环境变量或已有 `cf_failover*.py`，也支持交互输入并调用 Token Verify API。
- Cloudflare Zone/A 记录：列出 Zone 和 A 记录，支持逗号多选，也支持现场新建 A 记录。
- 自动脚本：5 包 TCP 平均延迟、三次间隔 20 秒复核、Check-Host 区域仲裁、状态文件和锁文件。
- 通知规范：群组消息不包含具体毫秒数，私聊消息包含目标 IP 和切换细节。
- 安全注入：`tg_bot.py` 修改前生成 `.bak`，使用 AST 定位字典并在写回前再次编译；失败自动恢复。
- 备份包：包含线路脚本、systemd 单元、机器人、配置和 `restore_cf_cluster.sh`。
- 离线自测：不访问网络、不执行 systemd，验证脚本生成、语法注入和备份逻辑。

## 目录

```text
cf_manager.py       # 主程序（唯一运行时源码）
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

仓库是私有仓库，因此 VPS 需要先配置有读取权限的 GitHub SSH key，并安装 `git` 和 Python 3。下面这一行会在首次运行时克隆仓库，之后运行时自动快进更新；然后安装管理程序、运行自测并打开中文菜单：

```bash
if [ -d "$HOME/cf-failover-manager/.git" ]; then git -C "$HOME/cf-failover-manager" pull --ff-only; else git clone git@github.com:jaycen-0502/cf-failover-manager.git "$HOME/cf-failover-manager"; fi && sudo install -o root -g root -m 700 "$HOME/cf-failover-manager/cf_manager.py" /usr/local/bin/cf_manager && sudo python3 /usr/local/bin/cf_manager --self-test && sudo python3 /usr/local/bin/cf_manager
```

如果仓库没有配置 SSH key，可以先在 VPS 执行 `ssh -T git@github.com` 检查访问权限。此部署命令只更新管理工具，不会自动修改 DNS 或创建线路。

不使用 GitHub 克隆时，也可以手动上传后安装：

```bash
sudo install -o root -g root -m 700 cf_manager.py /usr/local/bin/cf_manager
sudo python3 /usr/local/bin/cf_manager --self-test
sudo python3 /usr/local/bin/cf_manager
```

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
