# 部署到常驻主机

目标：让机器人 24/7 跑，不依赖你自己的电脑。整套方案只用到 Python + 一个 Docker 容器（或 systemd 服务），代码不用改。

## 0. 先选主机

**GitHub Actions 已排除**：2026-10-20 用 `.github/workflows/binance-probe.yml` 实测，GitHub 托管 runner 访问 Binance 返回 **HTTP 451**（runner 在美国，Binance 对美区 IP 封禁）。所以要么换主机，要么自建 runner（那又需要一台常开的机器）。

**区域一定不能是美国。** 建议东京、大阪、首尔、新加坡——离 Binance 的 AWS 东京机房近，且不受地区限制。

| 方案 | 成本 | 说明 |
| --- | --- | --- |
| **Oracle Cloud Always Free** | $0 | 2 OCPU + 12 GB ARM / 200 GB 盘 / 10 TB 出网。注意：不是网上流传的 4/24，2026 年官方已减半。区域在注册时锁定（home region），选大阪/新加坡。 |
| Oracle 闲置回收 | — | 7 天内 CPU / 网络 / 内存 95 分位都低于 20% 就会被判定闲置并回收。**一个交易机器人必然满足“闲置”**，所以要么让机器干点别的活，要么升级成 Pay As You Go（Always Free 资源仍收费 $0，但不再被回收）。 |
| GCP 免费 e2-micro | $0 | 只在 us-central1 / us-west1 / us-east1 三个美国区，对 Binance 就是 451，**不可用**。 |
| AWS / Azure 免费额度 | 12 个月内 $0 | 有东京区可用，但到期转收费，适合短期。 |
| 廉价 VPS（Vultr 东京 / Contabo 新加坡） | $5~6/月 | 最省心，1 GB 内存够用。 |

Oracle 注册经常被风控拒（资料要真实、别挂 VPN、别用隐私邮箱），拒了换时间再试；资金上只用验证卡，Always Free 不扣费。

## 1. 准备机器

Ubuntu 22.04 / 24.04：

```bash
sudo apt update && sudo apt install -y git python3-venv curl
# Docker 方案（推荐）
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"   # 重新登录生效
```

## 2. 网络自检（必做，不通就别往下走）

```bash
for u in https://api.binance.com/api/v3/ping \
         https://fapi.binance.com/fapi/v1/ping \
         https://demo-fapi.binance.com/fapi/v1/ping; do
  echo "$u -> $(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$u")"
done
```

三个都必须是 `200`。任何一个 `451` → 换区域（或换主机商）。

## 3. 拉代码

私有仓库用只读 deploy key：

```bash
ssh-keygen -t ed25519 -C tradingbot-deploy -f ~/.ssh/id_deploy -N ""
cat ~/.ssh/id_deploy.pub      # 复制到 GitHub → Repo → Settings → Deploy keys（不要勾 write）
GIT_SSH_COMMAND="ssh -i ~/.ssh/id_deploy" git clone git@github.com:lazuli111/tradingbot.git
cd tradingbot
```

如果仓库已经改成公开，直接 `git clone https://github.com/lazuli111/tradingbot.git` 即可。

## 4. 密钥与配置

```bash
cp .env.example .env && chmod 600 .env
nano .env                     # 填 BINANCE_API_KEY / BINANCE_API_SECRET（Demo 的，不是实盘）
cp config.portfolio.example.yaml config.portfolio.yaml
```

`equity_fraction` 要和品种数对上：2 个品种填 `0.5`，3 个填 `0.33`。其余参数（4h、20/50、ADX≥20、1x、1% 风险）不要动。

## 5. 启动

**Docker（推荐）**

```bash
docker compose up -d --build
docker compose ps                 # btc / sol / guard 三个都该是 running
docker compose logs -f btc        # 应看到“已加载 301 根 K 线”和 K 线收盘日志
```

**systemd（1 GB 小机器、不想装 Docker）**

```bash
sudo useradd -r -m -d /opt/tradingbot tradingbot
sudo cp -r . /opt/tradingbot && sudo chown -R tradingbot:tradingbot /opt/tradingbot
cd /opt/tradingbot && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
sudo cp deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tradingbot-btc tradingbot-guard.timer
journalctl -u tradingbot-btc -f
```

> **1 GB 实例的实际情况（实测）**：一个机器人进程 RSS ≈ **340 MB**，加上系统和阿里云的 aegis 监控，1 GB 机器只剩不到 200 MB 余量。
> 所以：**只开一个品种**，闸门用 `tradingbot-guard.timer`（每 5 分钟跑一次 `--once` 就退出），不要开 `tradingbot-guard.service` 的常驻模式，也不要同时开 `tradingbot-sol`。
> 想跑两个品种，先把实例升到 2 GB。unit 里已经写了 `MemoryMax` 和 `OPENBLAS_NUM_THREADS=1`，超限只会重启那个服务，不会拖垮整机。

## 6. 上线当天验证（10 分钟）

```bash
docker compose exec btc python tools/forward_status.py          # 两个 run 的持仓/权益/成交
docker compose exec guard python tools/portfolio_guard.py --status   # 账户权益、峰值、回撤、持仓
```

对着 Binance Demo 网页核一遍：持仓方向、数量、止损价、权益。第一次开仓后重点看：成交价与当时最新价偏差 < 0.2%、手续费率 0.04%、止损 = 入场 ± 2ATR。

## 7. 日常运维

| 动作 | 命令 |
| --- | --- |
| 看运行状态 | `docker compose ps`；`python tools/forward_status.py` |
| 看组合风险 | `python tools/portfolio_guard.py --status` |
| 更新代码 | `git pull && docker compose up -d --build` |
| 停机 | `docker compose down`（持仓留在交易所，但软止损会失效，建议先平仓） |
| 闸门触发后恢复 | `python tools/portfolio_guard.py --resume`（先人工看过账户再执行；会删掉停机标志 `data/HALT`） |
| 让机器人停止开新仓（不平仓） | 建 `data/HALT`，内容写原因（服务器上：`sudo -u tradingbot sh -c 'echo 原因 > /opt/tradingbot/data/HALT'`）；平仓和止损照常。删掉这个文件即恢复 |
| 改品种 | 改 `docker-compose.yml` 里的 `--symbol` 和 `equity_fraction`，再 `up -d` |

## 8. 安全清单

- SSH 只允许密钥登录，关掉密码登录；防火墙只开 22。
- `.env` 权限 600，永远不要 `git add`。
- 只用 Demo Key；将来真上实盘要单独生成 key、只开合约交易、不开提现、加 IP 白名单。
- 不要在多人共享的机器上跑，`.env` 和 SQLite 账本都带账户信息。

## 9. 成本速查

- Oracle Always Free：$0（升级 PAYG 后 Always Free 资源仍为 $0，只是不再受闲置回收影响）
- Vultr 东京：约 $6/月；Hetzner（欧洲）：约 €4/月；Contabo 新加坡：约 $5/月
- 阿里云轻量（境外节点）：约 ¥24~40/月，支付宝付款

## 10. 阿里云轻量应用服务器（支付宝付款）

适合没有外币卡、想用支付宝结算的情况。**必须买境外节点**：中国大陆地域访问不了币安，还涉及备案。

1. **购买**：aliyun.com → 轻量应用服务器 → 地域选 **日本（东京）**，没有就 **新加坡**；镜像 **Ubuntu 22.04**；套餐最低配（1 核 2 G 足够，机器人常驻只占 150~250 MB）。先买 1 个月试。
2. **登录凭据**：创建时设 root 密码（或用"重置密码"），记下公网 IP。
3. **防火墙**：控制台 → 防火墙，只保留 **22（SSH）**；80/443 都不需要。
4. **连上去**：`ssh root@<公网IP>`
5. **装 Docker**（境外节点可以直接拉，国内节点要换镜像源）：
   ```bash
   curl -fsSL https://get.docker.com | sh
   ```
6. **验网络**（三个必须 200，有 451 就换地域重建）：
   ```bash
   for u in https://api.binance.com/api/v3/ping https://fapi.binance.com/fapi/v1/ping https://demo-fapi.binance.com/fapi/v1/ping; do
     echo "$u -> $(curl -s -o /dev/null -w '%{http_code}' --max-time 15 $u)"; done
   ```
7. 之后按本文第 3~6 节走：`apt install -y git` → 克隆 → `.env` → `config.portfolio.yaml` → `docker compose up -d --build`。

轻量的带宽通常 1~3 Mbps 或按流量包计费，这套机器人每 4 小时只发几十个请求，一个月几百 MB，完全够用。


## 11. 只读看板（`tools/dashboard.py`）

在自己的电脑或手机上看服务器上的权益曲线、回撤、持仓、成交和运行健康度。

**安全设计**
- 只监听 `127.0.0.1:8765`，不开任何新端口，代码也拒绝绑定非回环地址；只能通过 SSH 隧道访问。
- 只读：只有 GET，页面上没有下单、平仓、启停按钮；账本用 SQLite `mode=ro` 打开，不写、不锁机器人的 `portfolio.db`。
- systemd 用 `DynamicUser=yes` 的临时用户运行，整个文件系统只读，`.env` / `.git` 不可见；不需要也读不到 API Key。浮动盈亏用 Binance Demo 的公开标记价接口计算。
- 只认 `localhost` / `127.0.0.1` 的 Host 头（防 DNS rebinding；本地端口不限，隧道可以用别的本地端口），带 CSP、`X-Frame-Options: DENY`，页面不加载任何外部资源。

**数据来源**：账本 `data/portfolio.db`（持仓、止损、K 线、成交）、`data/portfolio_guard.json`（峰值、停机状态）、`/var/log/tradingbot-guard.log`（账户总权益，每 5 分钟一行）、`/var/log/tradingbot-{btc,sol}.log`（ERROR、对账警告、最后一次 K 线处理）、`systemctl show`（服务和 guard 定时器状态；在沙箱里读不到时，服务是否在跑改看 `/sys/fs/cgroup` 里该服务有没有进程，guard 只看日志是否按时更新，告警区会有一条“读不到 systemd 状态”的提示，原因写进 `journalctl -u tradingbot-dashboard`）。

**流量**：首次打开约 9 KB（gzip），之后每分钟拉一次约 3 KB 的快照；页面一直开着一天约 4 MB。服务端快照缓存 30 秒，多开几个页面也只采集一次。

### 安装（admin 执行一次）

```bash
sudo -u tradingbot git -C /opt/tradingbot pull --ff-only
sudo cp /opt/tradingbot/deploy/systemd/tradingbot-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tradingbot-dashboard
systemctl status tradingbot-dashboard --no-pager          # active (running)，内存二三十 MB
curl -s -H 'Host: 127.0.0.1:8765' http://127.0.0.1:8765/api/snapshot | head -c 300; echo
```

不影响两个机器人：不需要重启它们，看板挂了也只影响看板自己。

### 打开看板

**电脑**（保持这个终端开着，然后浏览器打开 http://127.0.0.1:8765 ）：

```bash
ssh -N -L 8765:127.0.0.1:8765 admin@8.216.9.170
```

**手机**：用支持端口转发的 SSH App（iOS / Android 都可用 Termius）：新建 Port Forwarding → Local，本地端口 `8765`，目标 `127.0.0.1:8765`，主机选这台服务器；连上后手机浏览器打开 `http://127.0.0.1:8765`。

> 前提：服务器防火墙要放行你当前 IP 的 22 端口。手机网络 IP 常变，连不上先去阿里云控制台 → 防火墙看规则。SSH 建议只用密钥登录。

### 怎么判断数据是否过期

| 看哪里 | 正常 | 过期 / 异常 |
| --- | --- | --- |
| 顶部“数据生成于” | 1 分钟内（页面每 60 秒刷新） | 超过 2 分钟，或出现红色“连接看板服务失败”横条：SSH 隧道断了或看板服务停了，页面上是旧数据 |
| “权益记录于” | 5 分钟内 | 超过 12 分钟：guard 没在跑（告警里会写） |
| 机器人“距收盘” | 小于 4 小时 | 超过 4.5 小时（4h K 线 + 30 分钟余量）：机器人卡住了，告警标红 |
| 告警区 | 只有提示 | 服务停止、guard 失败或停机、订单待核对、最近 24 小时的 ERROR / 对账警告都会列出 |

账户总权益来自 guard 每 5 分钟查一次交易所的结果，和 `portfolio_guard.py --status` 的实时值最多差 5 分钟的行情波动。持仓、止损、成交和 `forward_status.py` 读的是同一个账本，应完全一致。

### 更新与卸载

```bash
sudo -u tradingbot git -C /opt/tradingbot pull --ff-only && sudo systemctl restart tradingbot-dashboard
sudo systemctl disable --now tradingbot-dashboard && sudo rm /etc/systemd/system/tradingbot-dashboard.service && sudo systemctl daemon-reload
```

不在服务器上时，本地调试可以 `python3 tools/dashboard.py --root /path/to/tradingbot --once` 打印一次快照 JSON。

**要公网网址？** 不要改 `--host`（代码会拒绝）。必须在前面加带 HTTPS 和登录认证的反向代理（例如 Caddy + 域名 + basic auth / OAuth），并且只在防火墙里开 443；在那之前，SSH 隧道是唯一的访问方式。


## 12. USDC 篮子：一个进程跑 9 个币（`tradingbot-usdc.service`）

在同一个 Demo 账户上用 USDC 做保证金，跑 BTC SOL ETH XRP LTC BCH ADA LINK BNB 的 `/USDC` 永续，每笔风险 3%，和 USDT 的两个机器人平行前向测试。USDT 那边（`tradingbot-btc/sol`、`config.portfolio.yaml`、`data/portfolio.db`、`data/HALT`）一律不动。配置是 `config.usdc-basket.example.yaml`，账本 `data/usdc_basket.db`，停机标志 `data/HALT_USDC`。

**为什么一个进程**：一个机器人进程约 340 MB，大头是 ccxt 的市场表（`load_markets`），9 个币各开一个进程放不下。`main.py testnet-multi` 让 9 个币共用一个行情连接和一个下单连接；每个币仍然有自己的 Trader、风控、存档（run 名带币种）、对账和软止损，一个币出错只影响它自己，下一轮（30 秒后）先对账再重试。本机用假交易所实测：9 个币比 1 个币只多约 3 MB（RSS 125 → 128 MB，跑 120 根 K 线后），所以服务器上预计和单币进程差不多，约 345~360 MB。日志每行带币种，例如 `[SOL/USDC]`。

### 启动之前（缺一不可）

1. **USDC 的 portfolio_guard 已经部署**（guard 按 USDT / USDC 拆开的那个 PR）。没有它，USDC 篮子没有 20% 组合停机线；而且旧 guard 触发时会平掉账户里**所有**仓位，USDC 的也会被平。
2. Demo 账户是**单资产模式**（不是联合保证金 / Multi-Assets）：联合保证金不支持逐仓，USDT 和 USDC 的盈亏也会互相影响。
3. `free -m` 的 available 至少约 450 MB（现在约 470 MB）。

### 安装（admin 执行一次）

```bash
sudo -u tradingbot git -C /opt/tradingbot pull --ff-only
# 配置复制一份再用：config.*.yaml 不进 git，以后改它不会挡住 pull
sudo -u tradingbot cp /opt/tradingbot/config.usdc-basket.example.yaml /opt/tradingbot/config.usdc-basket.yaml
sudo cp /opt/tradingbot/deploy/systemd/tradingbot-usdc.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable tradingbot-usdc.service      # 开机自启；先不启动，由 Claude 挑不在收盘的时间启动

# 让 claude-ops 能启停这一个服务（和 btc / sol 那几条同样的写法）
sudo tee /etc/sudoers.d/claude-ops-usdc >/dev/null <<'RULES'
claude-ops ALL=(root) NOPASSWD: /usr/bin/systemctl start tradingbot-usdc.service, /usr/bin/systemctl stop tradingbot-usdc.service, /usr/bin/systemctl restart tradingbot-usdc.service
RULES
sudo chmod 440 /etc/sudoers.d/claude-ops-usdc
sudo visudo -cf /etc/sudoers.d/claude-ops-usdc       # 必须输出 parsed OK
```

看 USDC 账本要用 `tools/forward_status.py --db data/usdc_basket.db`：如果 claude-ops 现有的 forward_status 规则限定了参数，照它的写法再加一条带 `--db data/usdc_basket.db` 的；没限定参数就不用加。

### 启动（Claude 做，避开 4h 收盘：北京时间 00/04/08/12/16/20 点）

```bash
sudo -n systemctl start tradingbot-usdc.service
tail -n 80 /var/log/tradingbot-usdc.log
```

正常的样子：先一行 `账户 USDC 权益 …，每个币按 … 定仓位`，然后 9 个币各有 `[X/USDC] 启动`、`已加载 301 根 K 线`、`K 线收盘 …`。

- 出现 `交易账户读不到 USDC 余额`：进程会直接退出（systemd 30 秒后重启，还是会退出），先 `stop` 掉。先去 Demo 网页确认 USDC 在 U 本位合约账户里；如果在，可能是 ccxt 跳过了 `updateTime=0`（从没动过）的资产，这时停下来商量，不要改代码绕过这个检查。
- 某个币一直报 `初始化失败，下一轮重试`：其他币照常跑；看是网络问题还是这个币在 Demo 上有问题。
- 配置里写错币名（`BadSymbol`）：启动时直接退出。

### 内存：部署后实测，再定 MemoryMax

unit 里先给 `MemoryMax=600M`、`OOMScoreAdjust=500`（内存不够时内核先杀这个服务，不杀 USDT 的两个机器人）。跑满一根 K 线后看：

```bash
cat /sys/fs/cgroup/system.slice/tradingbot-usdc.service/memory.peak   # 启动以来峰值（含页缓存）
cat /sys/fs/cgroup/system.slice/tradingbot-btc.service/memory.peak    # 对照：单币进程
ps -o pid,rss,cmd -u tradingbot
free -m
```

峰值稳定之后，把 `MemoryMax` 改成「峰值 + 约 100M」（admin 改 unit + `daemon-reload`，Claude 挑不在收盘的时间重启）。不要低于峰值：单币进程用过 `MemoryMax=450M`，启动加载市场表时反复被回收 / 重启（HANDOVER“踩过的坑”第 2 条）。

### 请求频率

Binance U 本位按 IP 每分钟 2400 权重；行情走正式站（公开接口），下单走 Demo（签名接口），两边分开算。9 个币在一个线程里依次处理，ccxt 自带的限速会把请求错开，不需要另外错峰。

| 时候 | 正式站（行情） | Demo（下单） |
| --- | --- | --- |
| 平时每轮（每 30 秒） | 9 次 ticker × 1 = 9（每分钟 18） | 0（只有触发软止损时才有） |
| K 线收盘那一轮 | 每个币 3 次 ticker + 1 次 K 线（5）= 8，共 72 | 每个币读持仓 5 + 余额 5 = 10，共 90；9 个币同时反手最多约 290 |
| 启动 | 市场表 + 每个币约 9，共约 85 | 每个币约 58（持仓模式 30、逐仓 / 杠杆 / 单向各 1、持仓 5 × 3、余额 5 × 2），共约 530；有开仓再加，最多约 820 |

下单本身不算 IP 权重，只算订单数（每 10 秒 300 单），一轮最多 18 单。USDT 的两个机器人平时每分钟只有约 4 权重。所以 K 线收盘那一分钟 Demo 最多用到上限的约 12%，正式站约 4%；启动那一分钟 Demo 约 22%~34%。就算进程反复崩溃重启（每 30 秒一次），每分钟也在 1000 以内。

### 停止开新仓 / 恢复

和 USDT 一样，只是文件换成 `data/HALT_USDC`：建这个文件就不再开新仓，平仓和止损照常；删掉恢复。`data/HALT`（USDT 的）对 USDC 篮子不起作用，反过来也一样。
