# 交易机器人交接文档：Binance 验证与测试网接入

2026-10-20 · 在线版（可评论）：https://claude.ai/code/artifact/16cdc8c9-d766-44a8-8839-7dfd9fb6282b

## 背景

接手人要在一个能合规访问 Binance 的网络上完成两件事：确认 Binance 行情接入可用，以及在 Binance 模拟交易（Demo Trading）账户上验证下单。这两件事在当前开发环境里做不了。

- 当前环境访问 Binance API 返回 HTTP 451（地区受限），所以之前所有的回测、实验和模拟盘都是用 OKX 公开行情完成的。
- 代码的默认交易所仍是 Binance，换交易所只需要改配置或加 `--exchange` 参数，策略和风控代码不用动。
- 第一版**不支持实盘**：下单模块只接受测试网，传入非测试网会直接报错。

## 当前状态

策略、风控、回测和模拟盘都已经在 OKX 真实数据上验证过，49 个单元测试全部通过。只有和 Binance 相关的两条路径（行情、测试网下单）从未实际跑过。

| 模块 | 状态 | 怎么验证的 / 为什么没验证 |
| --- | --- | --- |
| 回测引擎（现货 + 永续；手续费、滑点、资金费、强平） | 已验证 | 单元测试；OKX BTC 2025-01 起的真实数据，逐笔核对过交易明细 |
| walk-forward 样本外检验 | 已验证 | 单元测试（含“篡改未来数据后参数选择不变”）；OKX BTC/ETH/SOL 4h |
| ADX / 波动率入场过滤 | 已验证 | 单元测试；`experiments/compare_filters.py` |
| 模拟盘（paper） | 已验证 | OKX 永续 1m 跑了 3 根 K 线；重启后从 SQLite 恢复持仓 |
| Binance 行情（K 线、资金费率历史） | 未验证 | 当前网络返回 HTTP 451 |
| Binance Demo Trading 下单 | 未验证 | 无法访问 Binance，也没有测试网 Key |
| 实盘交易 | 不支持 | 刻意不做：非测试网直接抛出 NotImplementedError |

代码在 GitHub 私有仓库里，本文件随代码一起提交。

## 环境搭建

拿到代码后大约 5 分钟能跑通测试；测试完全离线，不需要联网。

1. 克隆仓库。`.venv/`、`data/`、`reports/`、`config.yaml`、`.env` 都在 `.gitignore` 里，不会被提交；数据缓存和报告接手后会重新生成。
2. 开发时用的版本是 Python 3.13.5、ccxt 4.5.88。`requirements.txt` 里只写了 `ccxt>=4.5`，建议先固定到 4.5.88 做验证：Binance 模拟交易的接入方式在 ccxt 各版本间变过。
3. 安装依赖：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt "ccxt==4.5.88"
```

4. 复制配置：`cp config.example.yaml config.yaml`，`cp .env.example .env`。
5. 跑测试，预期结果是 `49 passed`：

```bash
.venv/bin/python -m pytest -q
```

## Binance 验证清单

按顺序做，前一步不通过就先停下排查。这一节只用公开行情，不需要 API Key；命令都在项目根目录运行，默认交易所就是 Binance。

- [ ] **公开行情能访问**：运行下面这条命令，预期打印出两根 K 线和两条资金费率，没有 451 报错。这条命令走的是代码自己的 `make_exchange`，永续用的是 `defaultType=future`。
- [ ] **现货回测**：`main.py backtest --since 2025-01-01`。预期 `data/` 下生成 `binance_spot_*.csv`。再加 `--exchange okx` 跑同样的参数对照：两家价格几乎一致，收益、交易次数应该接近。
- [ ] **永续回测 + 资金费**：`main.py backtest --since 2023-01-01 --market perp --timeframe 4h`。预期**不出现**“交易所只提供 … 之后的资金费率”的警告（OKX 只给约 3 个月），并且“资金费净支出”不为 0。这是第一次拿到完整资金费的回测。
- [ ] **重跑过滤器实验**：`experiments/compare_filters.py --exchange binance --since 2023-01-01`。重点看均线交叉 + ADX≥20 是否仍在大多数格子里变好。如果结论反转，说明之前的结论依赖于 OKX 数据或不计资金费，需要重新评估默认配置。
- [ ] **重跑 walk-forward**：`main.py walkforward --since 2023-01-01 --market perp --timeframe 4h --grid "fast=20;slow=50"`，和下文“研究结论”里 OKX 的数字对照。
- [ ] **模拟盘**：复制一份配置，把 `timeframe` 改成 `1m`、`poll_seconds` 改成 `10`，然后 `main.py paper --config <那份配置> --market perp --max-bars 3`。预期每分钟打印一条 K 线收盘日志，开仓后重启会显示“已恢复上次运行状态”。

第一步用的命令：

```bash
.venv/bin/python -c "from bot.data import make_exchange as m; print(m('binance','spot').fetch_ohlcv('BTC/USDT','1h',limit=2)); print(m('binance','perp').fetch_funding_rate_history('BTC/USDT:USDT',limit=2))"
```

其余命令都要加 `.venv/bin/python` 前缀。

## 测试网（Demo Trading）接入

只能使用 Binance 模拟交易账户的 API Key，**绝不能填实盘 Key**。代码开启 `testnet` 后会调用 ccxt 的 `enable_demo_trading(True)`，请求会发往 `demo-api.binance.com`（现货）和 `demo-fapi.binance.com`（合约），这是在 ccxt 4.5.88 上确认过的。

1. 在 Binance Demo Trading 的 API 管理页生成 Key（ccxt 文档里给的入口是 demo.binance.com 的 API Management 页面）。
2. 填入 `.env`：`BINANCE_API_KEY=` 和 `BINANCE_API_SECRET=`。`.env` 不要提交、不要发给别人。
3. **先确认连的是模拟环境**：在发任何订单之前，打印 `make_exchange('binance','perp',creds,testnet=True).urls['api']`，确认里面全是 `demo-` 开头的域名。
4. **现货先跑**：用 1m 配置跑 `main.py testnet --config <1m 配置> --market spot --max-bars 5`。检查：订单成交；成交价和手续费写进 `data/trades.db`；平仓数量不超过实际持币。
5. **再跑永续**：`--market perp`。检查：启动时逐仓和杠杆设置成功（失败只会打印警告，不会停止）；开仓后 `fetch_positions` 能读回持仓；平仓单带 `reduceOnly`；下单数量精度被交易所接受。
6. **软止损**：临时把 `stop_atr_mult` 调小（例如 0.3），确认价格碰到止损后下一次轮询就市价平仓。
7. **重启恢复**：持仓时按 Ctrl+C 停掉再启动。现货持仓从 SQLite 恢复，永续持仓从交易所重新同步。

代码里三处可能在测试网上暴露问题的地方，需要重点观察：

- **手续费币种**：如果手续费用 BNB 支付，代码只会打印警告，不计入统计。建议在模拟账户里关闭“用 BNB 抵扣手续费”。
- **最小下单量**：只检查了交易所的最小数量（`limits.amount.min`），没检查最小名义价值。合约的最小名义价值可能比现货高，小资金时订单可能被拒。
- **成交回报**：市价单返回的状态不是 closed 时，代码会再查一次订单，但只查一次。如果模拟环境成交慢，可能拿到不完整的成交价。

## 代码结构

三个引擎共用同一个决策核心：

```
引擎 bot/engine/
  optimize.py   网格搜索、walk-forward 选参 ──(反复调用)──> backtest.py
  backtest.py   逐根 K 线回测，信号次根开盘执行
  live.py       模拟盘 / 测试网，定时轮询最新行情 ──> storage.py（SQLite：成交、权益、运行状态）
      ↑ data.py（K 线 + 资金费率，本地 CSV 缓存）
      ↓
决策核心（每根 K 线收盘调用）
  strategies/  均线交叉、唐奇安；base.py 里的 ADX / 波动率过滤 ──> trader.py
  trader.py    开平仓、止损、强平、熔断判断
  risk.py      按 1% 风险定仓位；日亏损 / 回撤熔断 ──> trader.py
      ↓
执行 bot/broker/
  sim.py          回测 / 模拟盘本地撮合
  ccxt_broker.py  只连 Binance 模拟交易
```

每根 K 线收盘时，引擎先算策略信号，再交给 `trader.py` 结合风控做决定，最后由 broker 执行。从回测换到测试网，只是把 `sim.py` 换成 `ccxt_broker.py`。新策略只需实现 `raw_signals` 和 `base_warmup`，入场过滤由基类自动套用。

## 研究结论

在 OKX 数据上，唯一站得住的改进是给均线交叉加上 ADX≥20 入场过滤，它已经是 `config.example.yaml` 的默认设置。目前没有证据表明策略能稳定盈利，只是在单边下跌里亏得比持币少。

均线交叉 20/50，4h 永续 1x，样本外区间 2023-07 至 2026-10：

| 币种 | 无过滤 夏普 / 最大回撤 | ADX≥20 夏普 / 最大回撤 |
| --- | --- | --- |
| BTC | 0.74 / −16.9% | 0.99 / −12.7% |
| ETH | 0.04 / −17.8% | 0.36 / −18.0% |
| SOL | 0.92 / −16.5% | 1.05 / −15.2% |

- **不要滚动优化参数**：BTC 4h 永续（2025-06-30 起）上，walk-forward 滚动选参只有 +2.0%，固定 20/50 是 +21.8%，事后最优 +39.3%，买入持有 −24.1%。把 `adx_min` 加进优化网格后，三个币种的样本外结果都更差。
- **1h 周期不适用**：反复被止损，全部设置都亏损。
- **唐奇安突破加 ADX 没有效果**：突破本身已经要求强势行情。
- **波动率过滤变差**：12 个格子里只有 2 个改善，默认关闭。

这些结论的局限：只用了 3 个高度相关的币种、约 3.8 年数据；资金费只计入了最近约 3 个月；上表的区间和挑选过滤器的实验数据有重叠。用 Binance 完整数据重跑一遍（见验证清单），是下一个最有价值的检验。

## 已知限制与合规提醒

接手前必须确认：所在地区可以合规使用 Binance。不要用 VPN 或代理绕过地区限制，那会违反 Binance 的服务条款。

- **第一版没有实盘**：要支持实盘需要改代码，并单独评审。不要只删掉 `NotImplementedError` 就上线。
- **软止损**：测试网上的止损和止盈靠轮询触发，程序停止运行时止损不起作用。上实盘前应改成交易所端的条件单。
- **强平价是估算的**：逐仓、忽略手续费的简化公式，和交易所实际值会有小差异。
- **熔断是永久的**：从高点回撤 20% 后平仓并一直停止，需要人工处理后才能恢复。
- **walk-forward 略偏保守**：每个测试窗口都从空仓开始、结束时平仓，会多付一点手续费。
- **不构成投资建议**：回测结果不代表未来收益。
