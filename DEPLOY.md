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
>
> **东京服务器现在的形态**：BTC + SOL 由一个进程 `tradingbot-usdt.service` 跑（第 14 节），`tradingbot-btc` / `tradingbot-sol` 两个 unit 已被它取代，留在仓库里只为回滚。

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

在自己的电脑或手机上看服务器上的权益曲线、回撤、持仓、成交和运行健康度。同一个 Demo 账户按保证金资产分两套显示（页面上两个标签）：USDT（BTC + SOL）和 USDC 9 币篮子（第 12、13 节），各有自己的权益曲线、峰值、回撤、离 20% 停机线的距离、持仓、止损、guard 和停机标志；告警合在一起列在最上面，每条前面标着 USDT 或 USDC。

**安全设计**
- 只监听 `127.0.0.1:8765`，不开任何新端口，代码也拒绝绑定非回环地址；只能通过 SSH 隧道访问。
- 只读：只有 GET，页面上没有下单、平仓、启停按钮；账本用 SQLite `mode=ro` 打开，不写、不锁机器人的 `portfolio.db` / `usdc_basket.db`。
- systemd 用 `DynamicUser=yes` 的临时用户运行，整个文件系统只读，`.env` / `.git` 不可见；不需要也读不到 API Key。浮动盈亏用 Binance Demo 的公开标记价接口计算。
- 只认 `localhost` / `127.0.0.1` 的 Host 头（防 DNS rebinding；本地端口不限，隧道可以用别的本地端口），带 CSP、`X-Frame-Options: DENY`，页面不加载任何外部资源。

**数据来源（USDT）**：账本 `data/portfolio.db`（持仓、止损、K 线、成交）、`data/portfolio_guard.json`（峰值、停机状态）、`/var/log/tradingbot-guard.log`（账户总权益，每 5 分钟一行）、`/var/log/tradingbot-usdt.log`（ERROR、对账警告、最后一次 K 线处理；BTC、SOL 共用一个日志，按每行的 `[BTC/USDT]` / `[SOL/USDT]` 标签分开，进程级的 `[-]` 行两个币都算。还在跑单币服务 `tradingbot-btc/sol` 时自动改读 `/var/log/tradingbot-{btc,sol}.log`，见第 14 节）、`systemctl show`（服务和 guard 定时器状态；在沙箱里读不到时，服务是否在跑改看 `/sys/fs/cgroup` 里该服务有没有进程，guard 只看日志是否按时更新，告警区会有一条“读不到 systemd 状态”的提示，原因写进 `journalctl -u tradingbot-dashboard`）、停机标志 `data/HALT`（存在就告警：机器人不开新仓）。

**数据来源（USDC）**：同样的东西换成 `data/usdc_basket.db`、`data/portfolio_guard_usdc.json`、`/var/log/tradingbot-guard-usdc.log`、`/var/log/tradingbot-usdc.log`（9 个币共用，按 `[BTC/USDC]` 这样的标签分开）、`tradingbot-guard-usdc.service` / `.timer`、`data/HALT_USDC`。**`data/usdc_basket.db` 不存在时这一套显示“未启用”**，不查也不报警；以后停掉篮子，要看板不再报“服务未运行”，把这个账本挪走（改名归档）就行，看板不用改也不用重启。USDT 那套不是可选的：账本读不到就报严重。

**标记价**：只给有仓的币算浮动盈亏，每个品种每分钟最多请求一次 Demo 公开接口（两套最多 11 个，每次权重 1）；网络不通或被限频（418 / 429）时所有品种一起停一分钟，不会接着打。

**流量**：首次打开约 11 KB（gzip），之后每分钟拉一次快照，刚开始约 3~5 KB，两套都跑满几个月后约 14 KB（两条权益曲线各最多 600 点）；页面一直开着一天约 5~20 MB。服务端快照缓存 30 秒，多开几个页面也只采集一次。

### 安装（admin 执行一次）

```bash
sudo -u tradingbot git -C /opt/tradingbot pull --ff-only
sudo cp /opt/tradingbot/deploy/systemd/tradingbot-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tradingbot-dashboard
systemctl status tradingbot-dashboard --no-pager          # active (running)，内存二三十 MB
curl -s -H 'Host: 127.0.0.1:8765' http://127.0.0.1:8765/api/snapshot | head -c 300; echo
```

不影响机器人：不需要重启它们，看板挂了也只影响看板自己。

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
| 告警区 | 只有提示 | 服务停止、guard 失败或停机、停机标志存在、有持仓没止损、订单待核对、最近 24 小时的 ERROR / 对账警告都会列出，前面标着 USDT / USDC |

账户总权益来自 guard 每 5 分钟查一次交易所的结果，和 `portfolio_guard.py --status` 的实时值最多差 5 分钟的行情波动。持仓、止损、成交和 `forward_status.py` 读的是同一个账本，应完全一致。

### 更新与卸载

```bash
sudo -u tradingbot git -C /opt/tradingbot pull --ff-only && sudo systemctl restart tradingbot-dashboard
sudo systemctl disable --now tradingbot-dashboard && sudo rm /etc/systemd/system/tradingbot-dashboard.service && sudo systemctl daemon-reload
```

不在服务器上时，本地调试可以 `python3 tools/dashboard.py --root /path/to/tradingbot --once` 打印一次快照 JSON。

**要公网网址？** 不要改 `--host`（代码会拒绝）。必须在前面加带 HTTPS 和登录认证的反向代理（例如 Caddy + 域名 + basic auth / OAuth），并且只在防火墙里开 443；在那之前，SSH 隧道是唯一的访问方式。


## 12. USDC 篮子：一个进程跑 9 个币（`tradingbot-usdc.service`）

在同一个 Demo 账户上用 USDC 做保证金，跑 BTC SOL ETH XRP LTC BCH ADA LINK BNB 的 `/USDC` 永续，每笔风险 3%，和 USDT 机器人（BTC + SOL）平行前向测试。USDT 那边（`tradingbot-usdt`（第 14 节）、`config.portfolio.yaml`、`data/portfolio.db`、`data/HALT`）一律不动。配置是 `config.usdc-basket.example.yaml`，账本 `data/usdc_basket.db`，停机标志 `data/HALT_USDC`。

**为什么一个进程**：一个机器人进程约 340 MB，大头是 ccxt 的市场表（`load_markets`），9 个币各开一个进程放不下。`main.py testnet-multi` 让 9 个币共用一个行情连接和一个下单连接；每个币仍然有自己的 Trader、风控、存档（run 名带币种）、对账和软止损，一个币出错只影响它自己，下一轮（30 秒后）先对账再重试。本机用假交易所实测：9 个币比 1 个币只多约 3 MB（RSS 125 → 128 MB，跑 120 根 K 线后），所以服务器上预计和单币进程差不多，约 345~360 MB。日志每行带币种，例如 `[SOL/USDC]`。

### 启动之前（缺一不可）

1. **USDC 的 portfolio_guard 已经部署**（第 13 节）。没有它，USDC 篮子没有 20% 组合停机线；而且旧 guard 触发时会平掉账户里**所有**仓位，USDC 的也会被平。
2. Demo 账户是**单资产模式**（不是联合保证金 / Multi-Assets）：联合保证金不支持逐仓，USDT 和 USDC 的盈亏也会互相影响。
3. **内存以第 14 节的预算为准**：USDT 已经切到一个进程 `tradingbot-usdt`（btc / sol 已停、已取消自启），跑过至少一根 K 线；启动前 `free -m` 的 available 至少 **700 MB**（预计约 850）。原来写的“至少约 450 MB”没算 guard 每 5 分钟约 150 MB 的峰值，2026-11-02 就是这样卡死的（HANDOVER“踩过的坑”5）。

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

unit 里先给 `MemoryMax=600M`、`OOMScoreAdjust=500`（内存不够时内核先杀这个服务，不杀 USDT 机器人）。跑满一根 K 线后看：

```bash
cat /sys/fs/cgroup/system.slice/tradingbot-usdc.service/memory.peak   # 启动以来峰值（含页缓存）
cat /sys/fs/cgroup/system.slice/tradingbot-usdt.service/memory.peak   # 对照：USDT 两个币的进程
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

下单本身不算 IP 权重，只算订单数（每 10 秒 300 单），一轮最多 18 单。USDT 机器人（BTC + SOL）平时每分钟只有约 4 权重。所以 K 线收盘那一分钟 Demo 最多用到上限的约 12%，正式站约 4%；启动那一分钟 Demo 约 22%~34%。就算进程反复崩溃重启（每 30 秒一次），每分钟也在 1000 以内。

### 停止开新仓 / 恢复

和 USDT 一样，只是文件换成 `data/HALT_USDC`：建这个文件就不再开新仓，平仓和止损照常；删掉恢复。`data/HALT`（USDT 的）对 USDC 篮子不起作用，反过来也一样。


## 13. USDC 组合闸门（`portfolio_guard.py --asset USDC`）

同一个 Binance Demo 合约账户里，USDT 机器人（BTC + SOL）和 USDC 机器人（9 币篮子）各有一个闸门，按保证金资产分开，互不影响：

| | USDT（现有） | USDC（新增） |
| --- | --- | --- |
| 命令 | `portfolio_guard.py`（缺省就是 `--asset USDT`） | `portfolio_guard.py --asset USDC` |
| 权益 | 只读 USDT 余额 | 只读 USDC 余额 |
| 管哪些仓位 | symbol 以 `:USDT` 结尾的（`BTC/USDT:USDT`） | symbol 以 `:USDC` 结尾的（`BTC/USDC:USDC`） |
| 状态文件 | `data/portfolio_guard.json` | `data/portfolio_guard_usdc.json` |
| 停机标志 | `data/HALT` | `data/HALT_USDC`（USDC 机器人配置 `live.halt_file` 同名，不要改） |
| systemd | `tradingbot-guard.service` / `.timer` | `tradingbot-guard-usdc.service` / `.timer` |
| 日志 | `/var/log/tradingbot-guard.log` | `/var/log/tradingbot-guard-usdc.log` |

- 哪个仓位归哪个闸门，看 ccxt 统一 symbol 冒号后的结算币（ccxt 取自 Binance 的 `marginAsset`）。`--status`、`--flatten`、`--resume` 和自动清仓都只作用于自己那一套。
- **USDT 闸门唯一的行为变化**：以前触发时平掉账户里**所有**仓位，现在只平 USDT 保证金的仓位，USDC 仓位不动；`--status` 里的“持仓 N”也只数 USDT 仓位。账户里只有 USDT 仓位时，输出、状态文件、停机标志和下单都和以前一样。
- 前提：Demo 账户是**单资产模式**（不是联合保证金 / Multi-Assets）。联合保证金模式下两种资产的盈亏会互相影响，分开管就没有意义了。
- USDC 的回撤阈值和 USDT 一样是 20%（用户 2026-11-02 定），unit 里写明了 `--max-dd 0.20`。
- 看板目前只读 USDT 闸门的日志和状态，不显示 USDC。

### 安装（admin 执行一次）

两个 Python 进程不要同时跑，所以 USDC 闸门比 USDT 闸门晚约 2.5 分钟：开机后分别在 2 分钟、4 分 30 秒时第一次运行，之后各自每 5 分钟一次。机器已经开着时，`enable --now` 会立刻运行一次，之后按那一刻每 5 分钟一次，所以要挑时间启用。

```bash
sudo -u tradingbot git -C /opt/tradingbot pull --ff-only
sudo cp /opt/tradingbot/deploy/systemd/tradingbot-guard-usdc.service /opt/tradingbot/deploy/systemd/tradingbot-guard-usdc.timer /etc/systemd/system/
sudo systemctl daemon-reload
systemctl list-timers 'tradingbot-guard*' --no-pager       # 看 tradingbot-guard.timer 的 LAST
# 等 USDT 闸门上一次运行过去约 2.5 分钟，再执行下面这行
sudo systemctl enable --now tradingbot-guard-usdc.timer
```

验证：

```bash
systemctl list-timers 'tradingbot-guard*' --no-pager       # 两个 timer 的 NEXT 相差约 2.5 分钟
tail -n 3 /var/log/tradingbot-guard-usdc.log                # 权益是 USDC 余额（约 5000），持仓只数 USDC 仓位
tail -n 3 /var/log/tradingbot-guard.log                     # USDT 那边照旧
```

- 最好在 USDC 机器人开第一笔仓之前装好，峰值从起始的 5000 USDC 算起。
- USDT 闸门是 oneshot，`git pull` 之后下一次定时运行就是新代码，不用重启任何服务。

**给 claude-ops 的 sudoers 规则**（和现有 `--status` 那条同一写法；sudo 按参数逐字匹配，顺序不能变）：

```bash
sudo visudo -f /etc/sudoers.d/claude-ops-guard-usdc
```

文件内容一行：

```
claude-ops ALL=(tradingbot) NOPASSWD: /opt/tradingbot/.venv/bin/python tools/portfolio_guard.py --asset USDC --status
```

claude-ops 这样用：

```bash
cd /opt/tradingbot && sudo -n -u tradingbot .venv/bin/python tools/portfolio_guard.py --asset USDC --status
```

### 日常运维（USDC）

| 动作 | 命令（在 `/opt/tradingbot`，以 tradingbot 身份） |
| --- | --- |
| 看 USDC 组合风险 | `python tools/portfolio_guard.py --asset USDC --status` |
| USDC 闸门触发后恢复 | `python tools/portfolio_guard.py --asset USDC --resume`（只删 `data/HALT_USDC`，USDT 的 `data/HALT` 不动） |
| 手动平掉 USDC 仓位 | `python tools/portfolio_guard.py --asset USDC --flatten`（不改停机状态） |
| 只让 USDC 机器人停止开新仓 | 建 `data/HALT_USDC`，内容写原因 |
| 卸载 | `sudo systemctl disable --now tradingbot-guard-usdc.timer && sudo rm /etc/systemd/system/tradingbot-guard-usdc.{service,timer} && sudo systemctl daemon-reload` |


## 14. USDT：BTC + SOL 合成一个进程（`tradingbot-usdt.service`）

**为什么**：2026-11-02 21:25 启动 USDC 篮子后 available 只剩约 128 MB，21:27:12 USDC 的 guard（oneshot，约 150 MB）一跑，内存被挤爆，整机卡死，只能强制重启（HANDOVER“踩过的坑”5）。USDC 机器人已停、已取消开机自启。USDT 的两个单币进程各约 365 MB，合成一个能省下约 340 MB。

**只改运行方式，别的都不变**：

| | 以前（已被取代） | 现在 |
| --- | --- | --- |
| systemd | `tradingbot-btc.service`、`tradingbot-sol.service` | `tradingbot-usdt.service` |
| 命令 | `main.py testnet --config config.portfolio.yaml --symbol BTC/USDT`（SOL 同样一个） | `main.py testnet-multi --config config.portfolio.yaml --symbols BTC/USDT,SOL/USDT` |
| 日志 | `/var/log/tradingbot-btc.log`、`/var/log/tradingbot-sol.log` | `/var/log/tradingbot-usdt.log`，每行带 `[BTC/USDT]` / `[SOL/USDT]`，进程级的行是 `[-]` |
| 配置 | `config.portfolio.yaml` | 同一个文件，不用改（币种由 `--symbols` 传；`equity_fraction` 0.5 × 2 个币 = 100%） |
| 账本 / run 名 | `data/portfolio.db`；`testnet:binance:perp:BTC/USDT:USDT:4h:ma_cross`、`…SOL/USDT:USDT…` | 完全一样：两个币的持仓、止损、`last_signal`、风控状态直接接着用 |
| 停机标志 / 闸门 | `data/HALT`；`tradingbot-guard.timer` | 一样 |
| 内存设置 | `MemoryHigh=600M`，不设 `OOMScoreAdjust` | 一样（这是要保护的那个；USDC 篮子是 +500，内存不够时内核先杀它） |

- 两个币仍然各有自己的 Trader、风控、对账和软止损，共用一个行情连接和一个下单连接（`bot/engine/live_multi.py`）。一个币出错只影响它自己，下一轮（30 秒后）先对账再重试；整个进程崩溃时两个币一起停，systemd 30 秒后重启。
- 离线测试 `tests/test_usdt_one_process.py`：按 unit 里的 ExecStart 跑 `main.py`，单币进程写下的状态库，多币进程能原样恢复两个币（持仓、止损、`last_signal`），恢复后不重复下单，止损照常触发；回滚方向（多币写、单币读）也测了；从空账本起步时，下的单和两个单币进程一模一样。
- `tradingbot-btc.service`、`tradingbot-sol.service` 留在仓库里，**已被 `tradingbot-usdt` 取代**，只用于回滚。**两边绝不能同时开**：同一个币会被两个进程同时交易、重复下单（看板会标红“同时有 2 个服务在跑”）。

### 内存预算（服务器总内存 1613 MB）

| 进程 | 事故时（2026-11-02 21:27） | 切换后、USDC 篮子在跑 |
| --- | --- | --- |
| 系统 + 看板 | ≈ 330 MB | ≈ 330 MB |
| USDT 机器人 | btc ≈ 365 + sol ≈ 365 = 730 MB | usdt ≈ 370 MB（预计：多币进程每多一个币只多几 MB） |
| USDC 篮子 | ≈ 380 MB | ≈ 380 MB |
| `free -m` available（平时） | ≈ 128 MB（实测） | ≈ 470 MB（128 + 省下的约 340） |
| guard 峰值（oneshot，每 5 分钟一次，每次约 150 MB；USDT、USDC 两个错开 2.5 分钟） | 一个 guard 就超了 → 卡死 | 一个 guard 在跑时约 320 MB；万一两个撞在一起约 170 MB |

- 切换后、USDC 启动前，available 预计约 850 MB；USDC 启动条件见第 12 节“启动之前”（至少 700 MB）。
- 实测之后以实测为准：`free -m`；`cat /sys/fs/cgroup/system.slice/tradingbot-usdt.service/memory.peak`；`ps -o pid,rss,cmd -u tradingbot`。平时 available 低于约 300 MB，或者 guard 运行时低于约 150 MB，就先停 USDC 篮子（`sudo -n systemctl stop tradingbot-usdc.service`），再商量。

### 切换之前（Claude 做，只读）

1. 服务器上的配置能直接用：`grep -nE 'equity_fraction|db_path|halt_file|symbols' /opt/tradingbot/config.portfolio.yaml`，应看到 `equity_fraction: 0.5`、`db_path: data/portfolio.db`，没有 `halt_file`（缺省就是 `data/HALT`）也没有 `symbols`。`equity_fraction` 大于 0.5 的话 `testnet-multi` 会报“超过账户权益”退出（systemd 会反复重启），先停下来商量。claude-ops 读不了这个文件就请 admin 用 `sudo -u tradingbot grep …` 看。
2. 记下两个币现在的状态，切换后对照：
   ```bash
   grep 'K 线收盘' /var/log/tradingbot-btc.log | tail -n 1     # close / 信号 / 持仓 / 止损
   grep 'K 线收盘' /var/log/tradingbot-sol.log | tail -n 1
   cd /opt/tradingbot && sudo -n -u tradingbot .venv/bin/python tools/forward_status.py
   cd /opt/tradingbot && sudo -n -u tradingbot .venv/bin/python tools/portfolio_guard.py --status
   ```
3. `free -m` 记一下 available（切换后应多出约 340 MB）。

### 安装（admin 执行一次）

```bash
sudo -u tradingbot git -C /opt/tradingbot pull --ff-only
sudo cp /opt/tradingbot/deploy/systemd/tradingbot-usdt.service /etc/systemd/system/
sudo systemctl daemon-reload
# 只改开机自启，不停、不启动：正在跑的 btc / sol 不受影响，切换由 Claude 挑时间做
sudo systemctl disable tradingbot-btc.service tradingbot-sol.service
sudo systemctl enable tradingbot-usdt.service

# 让 claude-ops 能启停这个服务（和 btc / sol 那几条同样的写法）
sudo tee /etc/sudoers.d/claude-ops-usdt >/dev/null <<'RULES'
claude-ops ALL=(root) NOPASSWD: /usr/bin/systemctl start tradingbot-usdt.service, /usr/bin/systemctl stop tradingbot-usdt.service, /usr/bin/systemctl restart tradingbot-usdt.service
RULES
sudo chmod 440 /etc/sudoers.d/claude-ops-usdt
sudo visudo -cf /etc/sudoers.d/claude-ops-usdt       # 必须输出 parsed OK

sudo systemctl restart tradingbot-dashboard          # 看板换新代码；切换前后都能正确显示，不用等切换
```

btc / sol 原来的 sudoers 规则（start / stop / restart）留着，回滚要用。装完到切换之间如果机器重启了，开机起来的就是 `tradingbot-usdt`（btc / sol 已取消自启），等于提前切换了，按下面“核对”一节检查即可。

### 切换（Claude 做，避开 4h 收盘：北京时间 00/04/08/12/16/20 点）

收盘前 10 分钟到收盘后 15 分钟之间不要动（收盘后机器人要处理 K 线、可能下单）。**先停两个旧的，再起新的**，中间不要让两边同时在跑：

```bash
sudo -n systemctl stop tradingbot-btc.service
sudo -n systemctl stop tradingbot-sol.service
systemctl is-active tradingbot-btc.service tradingbot-sol.service   # 两行都要是 inactive
sudo -n systemctl start tradingbot-usdt.service
```

（admin 自己切也行，一条命令：`sudo systemctl disable --now tradingbot-btc.service tradingbot-sol.service && sudo systemctl enable --now tradingbot-usdt.service`。）

### 核对：两个币都接上了原来的持仓和止损

```bash
tail -n 40 /var/log/tradingbot-usdt.log
grep 'K 线收盘' /var/log/tradingbot-usdt.log | tail -n 2
```

正常的样子：

- `[-]` 一行 `账户 USDT 权益 …，每个币按 … 定仓位`，一行 `启动 testnet 模式（一个进程 2 个币）`；
- `[BTC/USDT]`、`[SOL/USDT]` 各有 `已恢复上次运行状态 (testnet:binance:perp:BTC/USDT:USDT:4h:ma_cross)`（SOL 同样），然后 `已加载 … 根 K 线`、`K 线收盘 …`；
- 两条 `K 线收盘` 和切换前记下的最后一条比：同一根 K 线（时间、close 一样）、信号一样、持仓方向和数量一样、止损一样（权益可能差一点，是重新读了余额）；
- **没有** `本地持仓与交易所不一致`、没有 ERROR，也没有新成交（`forward_status.py` 的成交数和切换前一样）；
- `portfolio_guard.py --status` 的持仓数和切换前一样；`free -m` 的 available 多出约 340 MB；看板上两个币都显示“运行中”，告警区没有“服务未运行”。

哪个币**没有**“已恢复上次运行状态”，说明 run 名对不上、它从空状态起步了：立刻按下面回滚，不要等下一根 K 线。

### 回滚（回到一个币一个进程）

同样避开收盘。**USDC 篮子要先停**：不停的话内存就回到事故时的样子。

```bash
sudo -n systemctl stop tradingbot-usdc.service       # 在跑的话
sudo -n systemctl stop tradingbot-usdt.service
systemctl is-active tradingbot-usdt.service          # inactive
sudo -n systemctl start tradingbot-btc.service
sudo -n systemctl start tradingbot-sol.service
tail -n 20 /var/log/tradingbot-btc.log /var/log/tradingbot-sol.log   # 各有“已恢复上次运行状态”
```

然后请 admin 把开机自启改回来：`sudo systemctl disable tradingbot-usdt.service && sudo systemctl enable tradingbot-btc.service tradingbot-sol.service`（USDC 篮子在 `tradingbot-usdt` 恢复之前不要再开）。账本和 run 名两边一样，多币进程期间的持仓、止损和成交单币进程直接接着用（测试里测过这个方向）；看板会自动改看 btc / sol 的服务和日志，不用改。

### 日常运维

| 动作 | 命令 |
| --- | --- |
| 看日志 | `tail -f /var/log/tradingbot-usdt.log`；只看一个币 `grep '\[SOL/USDT\]' /var/log/tradingbot-usdt.log \| tail` |
| 重启（两个币一起） | `sudo -n systemctl restart tradingbot-usdt.service`（避开收盘） |
| 部署新代码 | `git pull --ff-only` 之后重启 `tradingbot-usdt`（不再是 btc / sol） |
| 停止开新仓 / 恢复 | 和以前一样：建 / 删 `data/HALT` |
