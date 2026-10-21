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
sudo cp deploy/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tradingbot-btc tradingbot-sol tradingbot-guard
journalctl -u tradingbot-btc -f
```

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
| 闸门触发后恢复 | `python tools/portfolio_guard.py --resume`（先人工看过账户再执行） |
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

