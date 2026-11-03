"""只读看板：读取、汇总、告警和 HTTP 层，全部用模拟账本 / 日志，不碰网络和 systemd。"""
import gzip
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import dashboard as dash  # noqa: E402
from bot.broker import Fill  # noqa: E402
from bot.storage import Storage  # noqa: E402

H = 3_600_000
BAR0 = int(datetime(2026, 10, 22, 0, tzinfo=timezone.utc).timestamp() * 1000)  # 最后一根 K 线开盘
NOW = BAR0 + 4 * H + 20 * 60_000  # 收盘后 20 分钟
BTC = "testnet:binance:perp:BTC/USDT:USDT:4h:ma_cross"
SOL = "testnet:binance:perp:SOL/USDT:USDT:4h:ma_cross"


def utc(ms, fmt="%Y-%m-%d %H:%M:%S"):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime(fmt)


def log_line(ms, level, name, msg):
    return f"{utc(ms)},123 {level} {name}: {msg}"


def state(position=None, signal=1, unresolved=None, halted=False):
    return {"trader": {"last_signal": signal,
                       "risk": {"peak_equity": 590.0, "day": "2026-10-22", "day_start_equity": 580.0,
                                "halted": halted, "halt_reason": "日亏损" if halted else None},
                       "broker": {"position": position, "unresolved": unresolved}},
            "last_funding_ts": None}


SOL_POS = {"side": 1, "qty": 1.59, "entry": 108.28, "opened_at": BAR0 - 4 * H, "entry_fee": 0.07,
           "margin": 166.8, "liq_price": 0.05, "stop": 104.62, "take_profit": None, "funding": 0.0}


class Env:
    """一套模拟的 /opt/tradingbot + /var/log：机器人连接保持打开，模拟正在运行的写入方。"""

    def __init__(self, tmp: Path):
        self.root = tmp / "opt"
        self.logs = tmp / "log"
        self.logs.mkdir()
        db = self.root / "data" / "portfolio.db"
        self.btc = Storage(db, BTC)
        self.sol = Storage(db, SOL)
        for i, eq in enumerate([590.0, 585.0, 578.0]):
            self.btc.record_equity(BAR0 - (2 - i) * 4 * H, eq)
        for i, eq in enumerate([586.0, 575.85]):
            self.sol.record_equity(BAR0 - (1 - i) * 4 * H, eq)
        self.btc.record_fill(Fill(BAR0 - 9 * H, "buy", 0.0035, 84630.3, 0.118, "signal"))
        self.btc.record_fill(Fill(BAR0 - 2 * H, "sell", 0.0035, 83222.8, 0.116, "stop_loss", -5.16))
        self.sol.record_fill(Fill(BAR0 - 4 * H, "buy", 1.59, 108.28, 0.069, "signal"))
        self.btc.save_state(state())
        self.sol.save_state(state(SOL_POS))
        (self.root / "data" / "portfolio_guard.json").write_text(
            json.dumps({"peak": 1200.0, "halted": False, "reason": None, "updated": None}))

        self.guard_lines = [
            f"{utc(BAR0 - 2 * H)}  权益 1180.00  峰值 1190.00  回撤 -0.84%  持仓 2  免费 800.00",
            f"{utc(BAR0 - H)}  清仓失败 BTC/USDT:USDT: timeout",
            f"{utc(NOW - 5 * 60_000)}  权益 1150.00  峰值 1190.00  回撤 -3.36%  持仓 1  免费 980.00",
        ]
        self.write_guard()
        (self.logs / "btc.log").write_text("\n".join([
            log_line(BAR0 - H, "WARNING", "bot.broker.ccxt_broker", "set_margin_mode 失败（可能已设置）: -4046"),
            log_line(BAR0 - H, "INFO", "bot.engine.live", "启动 testnet 模式: binance BTC/USDT:USDT 4h"),
            log_line(BAR0 - 30 * 60_000, "ERROR", "bot.engine.live", "本轮处理出错：下一轮先与交易所核对持仓，再重试"),
            "Traceback (most recent call last):",
            '  File "x.py", line 1, in <module>',
            'ccxt.base.errors.InvalidOrder: binance {"code":-2022,"msg":"ReduceOnly Order is rejected."}',
            log_line(BAR0 - 20 * 60_000, "WARNING", "bot.broker.ccxt_broker",
                     "本地持仓与交易所不一致，以交易所为准：本地 +1 0.0035，交易所 无"),
            log_line(BAR0 + 4 * H + 5000, "INFO", "bot.engine.live",
                     f"K 线收盘 {utc(BAR0, '%Y-%m-%d %H:%M')} close=82887.40 信号=+1 权益=578.00 持仓=无"),
        ]) + "\n")
        (self.logs / "sol.log").write_text(log_line(BAR0 + 4 * H + 3000, "INFO", "bot.engine.live",
                                                    f"K 线收盘 {utc(BAR0, '%Y-%m-%d %H:%M')} close=105.47 "
                                                    f"信号=+1 权益=575.85 持仓=+1 1.590000 止损 104.62") + "\n")
        self.services = {
            "tradingbot-btc.service": {"ActiveState": "active", "SubState": "running",
                                       "ActiveEnterTimestamp": f"@{(BAR0 - H) // 1000}", "NRestarts": "0"},
            "tradingbot-sol.service": {"ActiveState": "active", "SubState": "running",
                                       "ActiveEnterTimestamp": f"@{(BAR0 - H) // 1000}", "NRestarts": "0"},
            "tradingbot-guard.service": {"Result": "success", "ExecMainStatus": "0",
                                         "ExecMainExitTimestamp": f"@{(NOW - 300_000) // 1000}"},
            "tradingbot-guard.timer": {"ActiveState": "active", "LastTriggerUSec": "Thu 2026-10-22 04:15:00 UTC"},
        }
        self.mark_price = 105.0
        self.marks = {}  # 品种 -> 标记价；没列的用 mark_price
        self.cgroup_states = {"tradingbot-btc.service": "active", "tradingbot-sol.service": "active"}
        self.cfg = dash.Config(root=self.root, guard_log=self.logs / "guard.log", log_tz=timezone.utc, bots={
            "BTC": ("tradingbot-btc.service", self.logs / "btc.log"),
            "SOL": ("tradingbot-sol.service", self.logs / "sol.log"),
        })

    def write_guard(self):
        (self.logs / "guard.log").write_text("\n".join(self.guard_lines) + "\n")

    def show(self, unit, props):
        return {k: v for k, v in self.services[unit].items() if k in props}

    def mark(self, symbol):
        if self.mark_price is None:
            raise TimeoutError("no network")
        return self.marks.get(symbol, self.mark_price)

    def snapshot(self, now=NOW, show=None):
        return dash.build_snapshot(self.cfg, now, show=show or self.show, mark=self.mark, cgroup=self.cgroup)

    def cgroup(self, unit):
        return {"ActiveState": self.cgroup_states.get(unit, "inactive"), "source": "cgroup"}


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.btc.conn.close()
    e.sol.conn.close()


def levels(snap):
    return [(a["level"], a["text"]) for a in snap["alerts"]]


# ---- 汇总 -----------------------------------------------------------------

def test_account_from_guard_log_and_state(env):
    a = env.snapshot()["account"]
    assert a["equity"] == 1150.0 and a["free"] == 980.0 and a["positions"] == 1
    assert a["peak"] == 1200.0  # portfolio_guard.json 里的峰值比日志里的高，以它为准（guard 也这么算）
    assert a["drawdown"] == pytest.approx(1150 / 1200 - 1)
    assert a["guard_line"] == pytest.approx(960.0)
    assert a["distance"] == pytest.approx(190.0)
    assert a["distance_pct"] == pytest.approx(1150 / 1200 - 1 + 0.20)
    assert a["start_equity"] == 1180.0 and a["return"] == pytest.approx(1150 / 1180 - 1)
    assert not a["halted"]


def test_bots_position_pnl_and_last_bar(env):
    snap = env.snapshot()
    btc, sol = snap["bots"]
    assert btc["name"] == "BTC" and btc["position"] is None and btc["last_signal"] == 1
    assert btc["last_bar"]["open_ts"] == BAR0 and btc["last_bar"]["close_ts"] == BAR0 + 4 * H
    assert btc["last_bar"]["close"] == 82887.40 and btc["last_bar"]["equity"] == 578.0
    assert btc["last_bar"]["processed_at"] == BAR0 + 4 * H + 5000 - 5000 % 1000
    assert not btc["bar_overdue"]

    p = sol["position"]
    assert (p["side"], p["qty"], p["entry"], p["stop"]) == (1, 1.59, 108.28, 104.62)
    assert p["mark"] == 105.0
    assert p["upnl"] == pytest.approx(1.59 * (105.0 - 108.28))
    assert p["upnl_pct"] == pytest.approx(105.0 / 108.28 - 1)
    assert sol["service"]["ActiveState"] == "active"


def test_short_position_pnl_sign(env):
    env.sol.save_state(state({**SOL_POS, "side": -1}, signal=-1))
    p = env.snapshot()["bots"][1]["position"]
    assert p["upnl"] == pytest.approx(-1.59 * (105.0 - 108.28)) and p["upnl"] > 0


def test_fills_newest_first_with_symbol(env):
    fills = env.snapshot()["fills"]
    assert [(f["symbol"], f["side"], f["reason"]) for f in fills] == [
        ("BTC", "sell", "stop_loss"), ("SOL", "buy", "signal"), ("BTC", "buy", "signal")]
    assert fills[0]["pnl"] == -5.16 and fills[0]["fee"] == 0.116 and fills[0]["price"] == 83222.8
    assert fills[1]["pnl"] is None


def test_log_errors_and_reconcile_warnings(env):
    btc = env.snapshot()["bots"][0]
    assert len(btc["errors"]) == 1
    assert btc["errors"][0]["text"].startswith("本轮处理出错")
    assert "ReduceOnly Order is rejected" in btc["errors"][0]["detail"]
    # 启动时的 set_margin_mode 噪音不算对账警告
    assert [w["text"][:9] for w in btc["warnings"]] == ["本地持仓与交易所不"]
    assert btc["errors_24h"] == 1 and btc["warnings_24h"] == 1
    assert btc["last_start"] == BAR0 - H
    assert ("warning", "BTC 最近 24 小时有 1 条 ERROR") in levels(env.snapshot())


def test_guard_section(env):
    g = env.snapshot()["guard"]
    assert g["result"] == "success" and g["exit_status"] == "0" and g["timer_active"] == "active"
    assert g["last_exit"] == (NOW - 300_000) // 1000 * 1000
    assert g["last_trigger"] == int(datetime(2026, 10, 22, 4, 15, tzinfo=timezone.utc).timestamp() * 1000)
    assert g["last_line"] == NOW - 5 * 60_000
    assert g["events"] == [{"ts": BAR0 - H, "text": "清仓失败 BTC/USDT:USDT: timeout"}]


def test_no_alerts_beyond_log_findings_when_healthy(env):
    assert {lvl for lvl, _ in levels(env.snapshot())} == {"warning"}


# ---- 告警 -----------------------------------------------------------------

def test_overdue_bar_alert(env):
    snap = env.snapshot(now=BAR0 + 8 * H + 31 * 60_000)  # 下一根早该收盘了
    assert snap["bots"][0]["bar_overdue"]
    assert any(lvl == "serious" and "没有处理新 K 线" in t for lvl, t in levels(snap))
    assert not env.snapshot(now=BAR0 + 8 * H + 29 * 60_000)["bots"][0]["bar_overdue"]


def test_service_down_and_guard_failures(env):
    env.services["tradingbot-sol.service"]["ActiveState"] = "failed"
    env.services["tradingbot-guard.service"].update(Result="exit-code", ExecMainStatus="1")
    env.services["tradingbot-guard.timer"]["ActiveState"] = "inactive"
    found = levels(env.snapshot(now=NOW + 20 * 60_000))
    assert ("critical", "SOL 服务未运行（failed）") in found
    assert ("critical", "guard 定时器未运行（inactive）") in found
    assert ("serious", "guard 上次运行失败：exit-code，退出码 1") in found
    assert any(lvl == "serious" and "guard 已 25 分钟没有新记录" in t for lvl, t in found)
    assert found[0][0] == "critical"  # 严重的排在前面


def test_halted_drawdown_and_unresolved(env):
    env.guard_lines.append(f"{utc(NOW)}  权益 970.00  峰值 1200.00  回撤 -19.17%  持仓 0  免费 970.00  [已停机]")
    env.write_guard()
    env.btc.save_state(state(unresolved={"id": "123", "side": "sell"}, halted=True))
    found = levels(env.snapshot())
    assert any(lvl == "critical" and "guard 已停机" in t for lvl, t in found)
    assert ("serious", "BTC 有状态未知的订单 123，等待核对") in found
    assert ("serious", "BTC 单币熔断：日亏损") in found
    # 持仓数不一致：guard 说 0，账本里 SOL 还有仓
    assert any("与账本持仓数 1 不一致" in t for _, t in found)


def test_near_guard_line_warning(env):
    env.guard_lines.append(f"{utc(NOW)}  权益 1000.00  峰值 1200.00  回撤 -16.67%  持仓 1  免费 900.00")
    env.write_guard()
    assert any("不到 5 个百分点" in t for _, t in levels(env.snapshot()))


def test_systemd_unreadable_falls_back_to_cgroup(env):
    """看板的 systemd 沙箱里 systemctl 读不到状态：服务看 cgroup，guard 看日志，不误报“未运行”。"""
    def broken(unit, props):
        raise RuntimeError("systemctl show 退出码 1: Failed to connect to bus")

    env.cgroup_states["tradingbot-sol.service"] = "inactive"
    snap = env.snapshot(show=broken)
    btc, sol = snap["bots"]
    assert btc["service"]["ActiveState"] == "active" and btc["service"]["source"] == "cgroup"
    assert "Failed to connect to bus" in btc["service"]["error"]
    found = levels(snap)
    assert ("critical", "SOL 服务未运行（inactive）") in found
    assert not any("BTC 服务未运行" in t or "定时器" in t or "上次运行失败" in t for _, t in found)
    assert any(lvl == "info" and "读不到 systemd 状态" in t and "Failed to connect to bus" in t for lvl, t in found)
    assert "Failed to connect to bus" in snap["guard"]["systemd_error"]
    # guard 停了照样能发现：日志过期
    assert any("guard 已 25 分钟没有新记录" in t for _, t in levels(env.snapshot(NOW + 20 * 60_000, show=broken)))


def test_systemctl_show_raises_on_failure(monkeypatch):
    class R:
        returncode, stdout, stderr = 1, "", "Failed to connect to bus: Permission denied"
    monkeypatch.setattr(dash.subprocess, "run", lambda *a, **k: R)
    with pytest.raises(RuntimeError, match="Permission denied"):
        dash.systemctl_show("x.service", ["ActiveState"])
    R.returncode, R.stdout = 0, "ActiveState=active\n"
    assert dash.systemctl_show("x.service", ["ActiveState"]) == {"ActiveState": "active"}


def test_cgroup_state(tmp_path):
    root, proc = tmp_path / "cg", tmp_path / "proc"
    (root / "a.service").mkdir(parents=True)
    (root / "a.service" / "cgroup.events").write_text("populated 1\nfrozen 0\n")
    (root / "a.service" / "cgroup.procs").write_text("4400\n4401\n")
    (root / "b.service").mkdir()
    (root / "b.service" / "cgroup.events").write_text("populated 0\nfrozen 0\n")
    (root / "b.service" / "cgroup.procs").write_text("")
    (proc / "4400").mkdir(parents=True)
    (proc / "4401").mkdir()
    (proc / "stat").write_text("cpu 1 2 3\nbtime 1792640000\n")
    ticks = os.sysconf("SC_CLK_TCK")
    fields = lambda start: " ".join(["S"] + ["0"] * 18 + [str(start)] + ["0"] * 10)
    (proc / "4400" / "stat").write_text(f"4400 (python (x)) {fields(500 * ticks)}")
    (proc / "4401" / "stat").write_text(f"4401 (python) {fields(900 * ticks)}")
    assert dash.cgroup_state("a.service", root, proc) == {
        "ActiveState": "active", "SubState": "running", "source": "cgroup", "ActiveEnterTimestamp": "@1792640500"}
    assert dash.cgroup_state("b.service", root, proc)["ActiveState"] == "inactive"
    assert dash.cgroup_state("missing.service", root, proc)["ActiveState"] == "inactive"


def test_mark_price_failure_is_soft(env):
    env.mark_price = None
    snap = env.snapshot()
    p = snap["bots"][1]["position"]
    assert p["mark"] is None and p["upnl"] is None and p["entry"] == 108.28
    assert ("info", "SOL 标记价获取失败，浮动盈亏暂缺: TimeoutError") in levels(snap)


def test_missing_files_do_not_crash(tmp_path):
    cfg = dash.Config(root=tmp_path, guard_log=tmp_path / "nope.log", log_tz=timezone.utc,
                      bots={"BTC": ("x.service", tmp_path / "nope-btc.log")})
    snap = dash.build_snapshot(cfg, NOW, show=lambda u, p: {}, mark=lambda s: 1.0)
    assert snap["account"] is None and snap["curve"] == []
    found = levels(snap)
    assert found[0][0] == "critical"
    assert any("读取账本失败" in t for _, t in found)
    assert any("guard 日志里没有权益记录" in t for _, t in found)


# ---- 一个进程跑 BTC + SOL（tradingbot-usdt.service） ----------------------------

def tagged_line(ms, level, name, tag, msg):
    """main.py testnet-multi 的日志格式：模块名后带币种标签，进程级的行是 [-]。"""
    return f"{utc(ms)},123 {level} {name} [{tag}]: {msg}"


def bar_msg(close, signal, equity):
    return f"K 线收盘 {utc(BAR0, '%Y-%m-%d %H:%M')} close={close} 信号={signal} 权益={equity} 持仓=无"


@pytest.fixture
def shared(env):
    """切换之后：BTC、SOL 共用 tradingbot-usdt.service 和一个日志，旧的单币服务已停。"""
    (env.logs / "usdt.log").write_text("\n".join([
        tagged_line(BAR0 - H, "INFO", "bot.engine.live_multi", "-",
                    "启动 testnet 模式（一个进程 2 个币）: binance BTC/USDT,SOL/USDT 4h"),
        tagged_line(BAR0 - H, "WARNING", "bot.broker.ccxt_broker", "BTC/USDT", "set_margin_mode 失败（可能已设置）: -4046"),
        tagged_line(BAR0 - H + 1000, "INFO", "bot.engine.live_multi", "SOL/USDT", "启动: SOL/USDT:USDT 4h 策略 ma_cross"),
        tagged_line(BAR0 - 30 * 60_000, "ERROR", "bot.engine.live", "SOL/USDT", "本轮处理出错：下一轮先与交易所核对持仓，再重试"),
        "Traceback (most recent call last):",
        '  File "x.py", line 1, in <module>',
        "ccxt.base.errors.NetworkError: binance GET https://demo-fapi.binance.com/fapi/v2/positionRisk timeout",
        tagged_line(BAR0 - 20 * 60_000, "WARNING", "bot.broker.ccxt_broker", "BTC/USDT",
                    "本地持仓与交易所不一致，以交易所为准：本地 +1 0.0035，交易所 无"),
        tagged_line(BAR0 + 4 * H + 3000, "INFO", "bot.engine.live", "BTC/USDT", bar_msg("82887.40", "+1", "578.00")),
        tagged_line(BAR0 + 4 * H + 4000, "INFO", "bot.engine.live", "SOL/USDT", bar_msg("105.47", "+1", "575.85")),
    ]) + "\n")
    usdt = ("tradingbot-usdt.service", env.logs / "usdt.log")
    env.cfg.bots = {"BTC": [usdt, ("tradingbot-btc.service", env.logs / "btc.log")],
                    "SOL": [usdt, ("tradingbot-sol.service", env.logs / "sol.log")]}
    env.services["tradingbot-usdt.service"] = {"ActiveState": "active", "SubState": "running",
                                               "ActiveEnterTimestamp": f"@{(BAR0 - H) // 1000}", "NRestarts": "0"}
    for old in ("tradingbot-btc.service", "tradingbot-sol.service"):
        env.services[old] = {"ActiveState": "inactive", "SubState": "dead"}
    env.cgroup_states = {"tradingbot-usdt.service": "active"}
    return env


def test_default_config_reads_the_usdt_unit_and_its_log():
    unit = (Path(__file__).resolve().parents[1] / "deploy/systemd/tradingbot-usdt.service").read_text(encoding="utf-8")
    cfg = dash.Config()
    for name, old in (("BTC", "btc"), ("SOL", "sol")):
        assert cfg.bots[name] == [("tradingbot-usdt.service", Path("/var/log/tradingbot-usdt.log")),
                                  (f"tradingbot-{old}.service", Path(f"/var/log/tradingbot-{old}.log"))]
    assert "StandardOutput=append:/var/log/tradingbot-usdt.log" in unit
    assert cfg.db == "data/portfolio.db"  # run 名没变，账本也没变：看板照旧按 run 名找两个币


def test_shared_log_lines_are_split_by_coin_tag(shared):
    lines = dash.tail_lines(shared.logs / "usdt.log", 1 << 20)
    btc = dash.parse_bot_log(lines, timezone.utc, "BTC")
    sol = dash.parse_bot_log(lines, timezone.utc, "SOL")
    assert btc["last_bar"]["close"] == 82887.40 and sol["last_bar"]["close"] == 105.47
    assert btc["errors"] == []
    [err] = sol["errors"]
    assert err["text"].startswith("本轮处理出错") and "positionRisk timeout" in err["detail"]
    assert [w["text"][:9] for w in btc["warnings"]] == ["本地持仓与交易所不"] and sol["warnings"] == []
    assert btc["last_start"] == sol["last_start"] == BAR0 - H  # 进程级 [-] 的启动算两个币都有
    # 不给 coin 就全算（单币日志的老用法），一行 ERROR 的 traceback 不会挂到别的币头上
    whole = dash.parse_bot_log(lines, timezone.utc)
    assert len(whole["errors"]) == 1 and len(whole["warnings"]) == 1


def test_process_level_error_counts_for_every_coin(shared):
    with open(shared.logs / "usdt.log", "a", encoding="utf-8") as f:
        f.write(tagged_line(BAR0 + 4 * H + 9000, "ERROR", "bot.engine.live_multi", "-", "保存状态失败") + "\n")
        f.write("sqlite3.OperationalError: database is locked\n")
    lines = dash.tail_lines(shared.logs / "usdt.log", 1 << 20)
    btc = dash.parse_bot_log(lines, timezone.utc, "BTC")
    assert [e["detail"] for e in btc["errors"]] == ["sqlite3.OperationalError: database is locked"]
    assert len(dash.parse_bot_log(lines, timezone.utc, "SOL")["errors"]) == 2


def test_snapshot_with_one_service_for_both_coins(shared):
    calls = []

    def show(unit, props):
        calls.append(unit)
        return shared.show(unit, props)

    snap = shared.snapshot(show=show)
    btc, sol = snap["bots"]
    assert btc["unit"] == sol["unit"] == "tradingbot-usdt.service"
    assert btc["service"]["ActiveState"] == sol["service"]["ActiveState"] == "active"
    assert calls.count("tradingbot-usdt.service") == 1  # 共用的服务只查一次
    assert btc["last_bar"]["close"] == 82887.40 and sol["last_bar"]["close"] == 105.47
    assert btc["last_bar"]["processed_at"] == BAR0 + 4 * H + 3000
    assert (btc["errors_24h"], btc["warnings_24h"], sol["errors_24h"], sol["warnings_24h"]) == (0, 1, 1, 0)
    assert sol["position"]["stop"] == 104.62  # 持仓、止损照旧从账本读
    found = levels(snap)
    assert {lvl for lvl, _ in found} == {"warning"}
    assert ("warning", "SOL 最近 24 小时有 1 条 ERROR") in found
    assert ("warning", "BTC 最近 24 小时有 1 条对账类警告") in found


def test_shared_service_down_is_one_alert_and_other_rules_still_fire(shared):
    shared.services["tradingbot-usdt.service"]["ActiveState"] = "failed"
    snap = shared.snapshot(now=BAR0 + 8 * H + 31 * 60_000)  # 也早该处理下一根了
    found = levels(snap)
    assert [t for lvl, t in found if "服务未运行" in t] == ["BTC、SOL 服务未运行（failed）"]
    assert found[0] == ("critical", "BTC、SOL 服务未运行（failed）")
    assert all(b["unit"] == "tradingbot-usdt.service" for b in snap["bots"])  # 都没在跑：按第一个候选报
    assert sum(lvl == "serious" and "没有处理新 K 线" in t for lvl, t in found) == 2


def test_shared_service_through_cgroup_when_systemd_is_unreadable(shared):
    def broken(unit, props):
        raise RuntimeError("systemctl show 退出码 1: Failed to connect to bus")

    snap = shared.snapshot(show=broken)
    assert all(b["service"]["source"] == "cgroup" and b["service"]["ActiveState"] == "active" for b in snap["bots"])
    assert not any("服务未运行" in t for _, t in levels(snap))
    shared.cgroup_states = {}
    assert ("critical", "BTC、SOL 服务未运行（inactive）") in levels(shared.snapshot(show=broken))


def test_before_the_switch_and_after_a_rollback_the_single_coin_services_count(shared):
    shared.services["tradingbot-usdt.service"] = {"ActiveState": "inactive", "SubState": "dead"}
    for old in ("tradingbot-btc.service", "tradingbot-sol.service"):
        shared.services[old] = {"ActiveState": "active", "SubState": "running"}
    snap = shared.snapshot()
    btc, sol = snap["bots"]
    assert (btc["unit"], sol["unit"]) == ("tradingbot-btc.service", "tradingbot-sol.service")
    assert btc["errors"][0]["detail"].endswith("ReduceOnly Order is rejected.\"}")  # btc.log，不是 usdt.log
    assert not any("服务未运行" in t for _, t in levels(snap))


def test_two_services_trading_one_coin_is_critical(shared):
    shared.services["tradingbot-btc.service"]["ActiveState"] = "active"  # 忘了停旧的
    found = levels(shared.snapshot())
    assert found[0] == ("critical", "BTC 同时有 2 个服务在跑（tradingbot-usdt.service、tradingbot-btc.service），"
                                    "会重复下单：停掉多余的")
    assert not any(t.startswith("SOL 同时") for _, t in found)


# ---- USDC 篮子：9 个币一个进程，和 USDT 并排的第二套 -------------------------------

USDC_RUNS = {c: f"testnet:binance:perp:{c}/USDC:USDC:4h:ma_cross" for c in dash.USDC_COINS}
USDC_CLOSE = {"BTC": "82901.10", "SOL": "105.52", "ETH": "3890.15", "XRP": "2.45", "LTC": "98.30",
              "BCH": "480.90", "ADA": "0.71", "LINK": "17.82", "BNB": "690.40"}
ETH_POS = {"side": 1, "qty": 0.4, "entry": 3850.0, "opened_at": BAR0 - 8 * H, "entry_fee": 0.77,
           "margin": 513.3, "liq_price": 2600.0, "stop": 3700.0, "take_profit": None, "funding": 0.0}
ADA_POS = {"side": -1, "qty": 1500.0, "entry": 0.7312, "opened_at": BAR0 - 4 * H, "entry_fee": 0.55,
           "margin": 365.6, "liq_price": 0.97, "stop": 0.7755, "take_profit": None, "funding": 0.0}


@pytest.fixture
def basket(shared):
    """切换之后的 USDT（shared）再加上 USDC 篮子：账本 usdc_basket.db、一个带 [X/USDC] 标签的日志、
    自己的 guard 日志 / 状态 / 单元；ETH 多单、ADA 空单在仓，XRP 有一条 ERROR，LINK 有一条对账警告。"""
    env = shared
    data = env.root / "data"
    env.usdc = {c: Storage(data / "usdc_basket.db", run) for c, run in USDC_RUNS.items()}
    for c, st in env.usdc.items():
        st.record_equity(BAR0 - 4 * H, 555.0)
        st.record_equity(BAR0, 556.5)
        st.save_state(state())
    env.usdc["ETH"].save_state(state(ETH_POS))
    env.usdc["ADA"].save_state(state(ADA_POS, signal=-1))
    env.usdc["ETH"].record_fill(Fill(BAR0 - 8 * H, "buy", 0.4, 3850.0, 0.77, "signal"))
    env.usdc["ADA"].record_fill(Fill(BAR0 - 4 * H, "sell", 1500.0, 0.7312, 0.55, "signal"))
    (data / "portfolio_guard_usdc.json").write_text(
        json.dumps({"peak": 5030.0, "halted": False, "reason": None, "updated": None}))
    env.usdc_guard_lines = [
        f"{utc(BAR0 - 2 * H)}  权益 5000.00  峰值 5000.00  回撤 +0.00%  持仓 2  免费 4100.00",
        f"{utc(NOW - 3 * 60_000)}  权益 5012.30  峰值 5030.00  回撤 -0.35%  持仓 2  免费 3950.00",
    ]
    env.write_usdc_guard = lambda: (env.logs / "guard-usdc.log").write_text("\n".join(env.usdc_guard_lines) + "\n")
    env.write_usdc_guard()
    lines = [tagged_line(BAR0 - H, "INFO", "bot.engine.live_multi", "-",
                         "启动 testnet 模式（一个进程 9 个币）: binance BTC/USDC,... 4h 每个币按权益的 11.11%，停机标志 data/HALT_USDC")]
    for i, c in enumerate(dash.USDC_COINS):
        lines.append(tagged_line(BAR0 - H + 1000 * (i + 1), "INFO", "bot.engine.live_multi", f"{c}/USDC",
                                 f"启动: {c}/USDC:USDC 4h 策略 ma_cross"))
    lines += [
        tagged_line(BAR0 - 30 * 60_000, "ERROR", "bot.engine.live", "XRP/USDC", "本轮处理出错：下一轮先与交易所核对持仓，再重试"),
        "Traceback (most recent call last):",
        '  File "x.py", line 1, in <module>',
        'ccxt.base.errors.ExchangeError: binance {"code":-1000,"msg":"An unknown error occured"}',
        tagged_line(BAR0 - 20 * 60_000, "WARNING", "bot.broker.ccxt_broker", "LINK/USDC",
                    "本地持仓与交易所不一致，以交易所为准：本地 +1 30.000000，交易所 无"),
    ]
    for i, c in enumerate(dash.USDC_COINS):
        lines.append(tagged_line(BAR0 + 4 * H + 1000 * (i + 1), "INFO", "bot.engine.live", f"{c}/USDC",
                                 bar_msg(USDC_CLOSE[c], "-1" if c == "ADA" else "+1", "556.50")))
    (env.logs / "usdc.log").write_text("\n".join(lines) + "\n")
    env.cfg.usdc = dash.Account(**{**dash.usdc_basket().__dict__, "guard_log": env.logs / "guard-usdc.log",
                                   "bots": {c: ("tradingbot-usdc.service", env.logs / "usdc.log") for c in dash.USDC_COINS}})
    env.services.update({
        "tradingbot-usdc.service": {"ActiveState": "active", "SubState": "running",
                                    "ActiveEnterTimestamp": f"@{(BAR0 - H) // 1000}", "NRestarts": "0"},
        "tradingbot-guard-usdc.service": {"Result": "success", "ExecMainStatus": "0",
                                          "ExecMainExitTimestamp": f"@{(NOW - 180_000) // 1000}"},
        "tradingbot-guard-usdc.timer": {"ActiveState": "active", "LastTriggerUSec": "Thu 2026-10-22 04:17:30 UTC"},
    })
    env.cgroup_states["tradingbot-usdc.service"] = "active"
    env.marks = {"ETH/USDC:USDC": 3900.0, "ADA/USDC:USDC": 0.71}
    yield env
    for st in env.usdc.values():
        st.conn.close()


def by_account(snap, asset):
    return [(a["level"], a["text"]) for a in snap["alerts"] if a["account"] == asset]


def test_default_config_matches_the_usdc_units_and_config():
    from bot.config import load_config
    import portfolio_guard as pg

    root = Path(__file__).resolve().parents[1]
    usdc = dash.Config().usdc
    unit = (root / "deploy/systemd/tradingbot-usdc.service").read_text(encoding="utf-8")
    guard = (root / "deploy/systemd/tradingbot-guard-usdc.service").read_text(encoding="utf-8")
    cfg = load_config(root / "config.usdc-basket.example.yaml")
    assert usdc.asset == "USDC" and usdc.optional
    assert usdc.bots == {c: ("tradingbot-usdc.service", Path("/var/log/tradingbot-usdc.log")) for c in dash.USDC_COINS}
    assert "StandardOutput=append:/var/log/tradingbot-usdc.log" in unit and "config.usdc-basket.yaml" in unit
    assert list(cfg.symbols) == [f"{c}/USDC" for c in dash.USDC_COINS]
    assert (cfg.live.db_path, cfg.live.halt_file) == (usdc.db, usdc.halt_file)
    assert pg.files_for("USDC") == (Path(usdc.guard_state), Path(usdc.halt_file))
    assert f"StandardOutput=append:{usdc.guard_log}" in guard and "--asset USDC" in guard
    assert f"--max-dd {dash.Config().max_dd:.2f}" in guard
    assert (usdc.guard_service, usdc.guard_timer) == ("tradingbot-guard-usdc.service", "tradingbot-guard-usdc.timer")
    assert (root / "deploy/systemd" / usdc.guard_timer).exists()


def test_nine_coin_log_is_split_by_tag(basket):
    lines = dash.tail_lines(basket.logs / "usdc.log", 1 << 20)
    parsed = {c: dash.parse_bot_log(lines, timezone.utc, c) for c in dash.USDC_COINS}
    assert {c: p["last_bar"]["close"] for c, p in parsed.items()} == {c: float(v) for c, v in USDC_CLOSE.items()}
    assert parsed["ADA"]["last_bar"]["signal"] == -1
    assert [c for c, p in parsed.items() if p["errors"]] == ["XRP"]
    assert 'ExchangeError: binance {"code":-1000' in parsed["XRP"]["errors"][0]["detail"]
    assert [c for c, p in parsed.items() if p["warnings"]] == ["LINK"]
    assert {p["last_start"] for p in parsed.values()} == {BAR0 - H}  # 进程级 [-] 的启动算每个币都有
    # BTC / SOL 在两套里都有，但 USDC 的日志只有 [BTC/USDC]：不会读成 USDT 那边的行
    assert parsed["BTC"]["last_bar"]["close"] == 82901.10


def test_usdc_basket_snapshot(basket):
    calls = []

    def show(unit, props):
        calls.append(unit)
        return basket.show(unit, props)

    snap = basket.snapshot(show=show)
    u = snap["usdc"]
    assert u["asset"] == "USDC" and u["enabled"]
    assert [b["name"] for b in u["bots"]] == list(dash.USDC_COINS)
    assert all(b["unit"] == "tradingbot-usdc.service" and b["service"]["ActiveState"] == "active" for b in u["bots"])
    assert calls.count("tradingbot-usdc.service") == 1  # 9 个币共用的服务只查一次
    bots = {b["name"]: b for b in u["bots"]}
    assert bots["BTC"]["symbol"] == "BTC/USDC:USDC" and bots["BTC"]["last_bar"]["close"] == 82901.10
    assert all(b["last_bar"]["open_ts"] == BAR0 and not b["bar_overdue"] for b in u["bots"])
    eth, ada = bots["ETH"]["position"], bots["ADA"]["position"]
    assert eth["upnl"] == pytest.approx(0.4 * 50) and eth["stop"] == 3700.0
    assert ada["mark"] == 0.71 and ada["upnl"] == pytest.approx(1500 * (0.7312 - 0.71)) and ada["upnl"] > 0
    assert [b["name"] for b in u["bots"] if b["position"]] == ["ETH", "ADA"]
    assert (bots["XRP"]["errors_24h"], bots["LINK"]["warnings_24h"], bots["BTC"]["errors_24h"]) == (1, 1, 0)

    a = u["account"]
    assert (a["equity"], a["peak"], a["positions"], a["free"]) == (5012.30, 5030.0, 2, 3950.0)
    assert a["drawdown"] == pytest.approx(5012.3 / 5030 - 1)
    assert a["guard_line"] == pytest.approx(5030 * 0.8) and a["distance"] == pytest.approx(5012.3 - 4024)
    assert a["return"] == pytest.approx(5012.3 / 5000 - 1)
    assert [p[1] for p in u["curve"]] == [5000.0, 5012.30]
    assert [(f["symbol"], f["side"]) for f in u["fills"]] == [("ADA", "sell"), ("ETH", "buy")]
    g = u["guard"]
    assert (g["service"], g["result"], g["timer_active"]) == ("tradingbot-guard-usdc.service", "success", "active")
    assert g["last_line"] == NOW - 3 * 60_000 and g["halt_file"] == "data/HALT_USDC" and g["halt_flag"] is None

    # USDT 这一套还在顶层，和以前一样
    assert snap["asset"] == "USDT" and snap["enabled"] and snap["account"]["equity"] == 1150.0
    assert [b["name"] for b in snap["bots"]] == ["BTC", "SOL"] and len(snap["fills"]) == 3
    assert snap["guard"]["service"] == "tradingbot-guard.service"
    # 健康时 USDC 只有日志里那两条
    assert by_account(snap, "USDC") == [("warning", "XRP 最近 24 小时有 1 条 ERROR"),
                                        ("warning", "LINK 最近 24 小时有 1 条对账类警告")]
    body = json.dumps(snap, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(gzip.compress(body)) < 10_000


def test_alerts_stay_in_their_own_account(basket):
    basket.services["tradingbot-usdc.service"]["ActiveState"] = "failed"
    basket.services["tradingbot-guard-usdc.timer"]["ActiveState"] = "inactive"
    basket.usdc_guard_lines.append(f"{utc(NOW + 15 * 60_000)}  权益 5012.30  峰值 5030.00  回撤 -0.35%  持仓 2  免费 3950.00")
    basket.write_usdc_guard()
    snap = basket.snapshot(now=NOW + 30 * 60_000)  # USDC guard 15 分钟前还有记录，USDT guard 已 35 分钟没有
    usdc, usdt = by_account(snap, "USDC"), by_account(snap, "USDT")
    assert ("critical", "BTC、SOL、ETH、XRP、LTC、BCH、ADA、LINK、BNB 服务未运行（failed）") in usdc
    assert ("critical", "guard 定时器未运行（inactive）") in usdc
    assert ("serious", "guard 已 15 分钟没有新记录") in usdc
    assert not any("服务未运行" in t or "定时器" in t for _, t in usdt)
    assert ("serious", "guard 已 35 分钟没有新记录") in usdt
    assert ("warning", "SOL 最近 24 小时有 1 条 ERROR") in usdt  # USDT 的 SOL，不是 USDC 的
    assert not any(t.startswith("SOL ") for _, t in usdc)
    assert {a["account"] for a in snap["alerts"]} == {"USDT", "USDC"}
    assert snap["alerts"][0]["level"] == "critical"

    # 持仓数核对也各算各的：USDC guard 说 3 个，USDC 账本 2 个；USDT 照旧一致
    basket.usdc_guard_lines.append(f"{utc(NOW)}  权益 5012.30  峰值 5030.00  回撤 -0.35%  持仓 3  免费 3950.00")
    basket.write_usdc_guard()
    snap = basket.snapshot()
    assert ("warning", "交易所持仓数 3（guard 记录）与账本持仓数 2 不一致") in by_account(snap, "USDC")
    assert not any("不一致" in t for _, t in by_account(snap, "USDT"))


def test_usdc_halt_flag_and_missing_stop(basket):
    (basket.root / "data" / "HALT_USDC").write_text("人工暂停开仓\n", encoding="utf-8")
    basket.usdc["ETH"].save_state(state({**ETH_POS, "stop": None}))
    snap = basket.snapshot()
    usdc = by_account(snap, "USDC")
    assert ("serious", "停机标志 data/HALT_USDC 存在，机器人不开新仓（平仓、止损照常）：人工暂停开仓") in usdc
    assert ("serious", "ETH 有持仓但账本里没有止损价") in usdc
    assert snap["usdc"]["guard"]["halt_flag"] == "人工暂停开仓"
    assert snap["guard"]["halt_flag"] is None and not any("停机" in t for _, t in by_account(snap, "USDT"))

    # guard 停机时它自己会写标志：只报 guard 停机那一条，不重复
    (basket.root / "data" / "portfolio_guard_usdc.json").write_text(
        json.dumps({"peak": 5030.0, "halted": True, "reason": "组合回撤 -20.10%（阈值 20%）", "updated": None}))
    usdc = by_account(basket.snapshot(), "USDC")
    assert ("critical", "组合 guard 已停机：组合回撤 -20.10%（阈值 20%）") in usdc
    assert not any(t.startswith("停机标志") for _, t in usdc)

    # USDT 的 data/HALT 同样会报，而且只算 USDT
    (basket.root / "data" / "HALT").write_text("", encoding="utf-8")
    snap = basket.snapshot()
    assert ("serious", "停机标志 data/HALT 存在，机器人不开新仓（平仓、止损照常）：停机标志存在") in by_account(snap, "USDT")


def test_usdc_missing_is_disabled_without_alerts(env):
    """env 里没有 USDC 的任何文件（例如以后停掉篮子、账本挪走了）：显示“未启用”，不查 systemd，不报警。"""
    calls = []

    def show(unit, props):
        calls.append(unit)
        return env.show(unit, props)

    snap = env.snapshot(show=show)
    assert snap["usdc"] == {"asset": "USDC", "enabled": False, "disabled_reason": "没有账本 data/usdc_basket.db",
                            "account": None, "curve": [], "bots": [], "fills": [], "guard": None}
    assert not any("usdc" in u for u in calls)
    assert by_account(snap, "USDC") == [] and snap["account"]["equity"] == 1150.0
    assert {lvl for lvl, _ in levels(snap)} == {"warning"}


def test_one_broken_account_does_not_take_down_the_other(basket):
    # USDT 读不到账本（USDT 不是可选的）：USDT 报严重，USDC 照常算
    basket.cfg.db = "data/missing.db"
    snap = basket.snapshot()
    assert any(lvl == "critical" and "读取账本失败" in t for lvl, t in by_account(snap, "USDT"))
    assert snap["usdc"]["account"]["equity"] == 5012.30 and len(snap["usdc"]["bots"]) == 9
    assert snap["usdc"]["bots"][2]["position"]["upnl"] == pytest.approx(20.0)
    assert not any("读取账本失败" in t for _, t in by_account(snap, "USDC"))


def test_usdc_guard_files_missing_is_an_alert_not_a_crash(basket):
    (basket.logs / "guard-usdc.log").unlink()
    (basket.root / "data" / "portfolio_guard_usdc.json").unlink()
    snap = basket.snapshot()
    assert snap["usdc"]["enabled"] and snap["usdc"]["account"] is None
    assert ("serious", "guard 日志里没有权益记录") in by_account(snap, "USDC")
    assert snap["account"]["equity"] == 1150.0


def test_mark_failures_are_one_alert_per_account(basket):
    basket.mark_price = None
    found = levels(basket.snapshot())
    assert [t for lvl, t in found if "标记价" in t] == ["SOL 标记价获取失败，浮动盈亏暂缺: TimeoutError",
                                                       "ETH、ADA 标记价获取失败，浮动盈亏暂缺: TimeoutError"]


# ---- 标记价缓存 -------------------------------------------------------------

class FakeFetch:
    def __init__(self):
        self.calls, self.fail = [], {}

    def __call__(self, symbol):
        self.calls.append(symbol)
        if symbol in self.fail:
            raise self.fail[symbol]
        return 100.0 + len(self.calls)


def test_mark_cache_requests_each_symbol_at_most_once_per_ttl():
    t = [0.0]
    fetch = FakeFetch()
    marks = dash.MarkCache(fetch, ttl=60, clock=lambda: t[0])
    assert marks("BTC/USDC:USDC") == 101.0 and marks("BTC/USDC:USDC") == 101.0
    assert marks("ETH/USDC:USDC") == 102.0
    t[0] = 59.9
    assert marks("BTC/USDC:USDC") == 101.0 and len(fetch.calls) == 2
    t[0] = 60.0
    assert marks("BTC/USDC:USDC") == 103.0 and len(fetch.calls) == 3


def test_mark_cache_pauses_every_symbol_when_the_network_or_rate_limit_fails():
    t = [0.0]
    fetch = FakeFetch()
    marks = dash.MarkCache(fetch, ttl=60, clock=lambda: t[0])
    fetch.fail["BTC/USDC:USDC"] = TimeoutError("timed out")
    for sym in ("BTC/USDC:USDC", "ETH/USDC:USDC", "ADA/USDC:USDC"):
        with pytest.raises(TimeoutError):
            marks(sym)
    assert fetch.calls == ["BTC/USDC:USDC"]  # 一次快照最多等一个超时
    t[0] = 61
    fetch.fail.clear()
    assert marks("ETH/USDC:USDC") == 102.0

    # 限频：同样全部暂停
    fetch.fail["ETH/USDC:USDC"] = urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)
    t[0] = 200
    with pytest.raises(urllib.error.HTTPError):
        marks("ETH/USDC:USDC")
    with pytest.raises(urllib.error.HTTPError):
        marks("BNB/USDC:USDC")
    assert "BNB/USDC:USDC" not in fetch.calls


def test_mark_cache_bad_symbol_does_not_block_the_others():
    t = [0.0]
    fetch = FakeFetch()
    marks = dash.MarkCache(fetch, ttl=60, clock=lambda: t[0])
    fetch.fail["XXX/USDC:USDC"] = urllib.error.HTTPError("u", 400, "Bad Request", {}, None)
    with pytest.raises(urllib.error.HTTPError):
        marks("XXX/USDC:USDC")
    with pytest.raises(urllib.error.HTTPError):
        marks("XXX/USDC:USDC")  # 失败也缓存，不每次重打
    assert marks("ETH/USDC:USDC") == 102.0
    assert fetch.calls == ["XXX/USDC:USDC", "ETH/USDC:USDC"]


def test_snapshots_share_mark_requests(basket):
    """页面每分钟刷新、快照缓存 30 秒：11 个仓位也是每个品种每分钟最多一次请求。"""
    t = [0.0]
    fetch = FakeFetch()
    marks = dash.MarkCache(fetch, ttl=60, clock=lambda: t[0])
    for t[0] in (0, 30, 59):
        dash.build_snapshot(basket.cfg, NOW, show=basket.show, mark=marks, cgroup=basket.cgroup)
    assert sorted(fetch.calls) == ["ADA/USDC:USDC", "ETH/USDC:USDC", "SOL/USDT:USDT"]


# ---- 只读 -----------------------------------------------------------------

def files_of(data, skip=()):
    return {f.name: (f.read_bytes(), f.stat().st_mtime_ns) for f in sorted(data.iterdir()) if f.suffix not in skip}


def test_reads_wal_without_writing(env):
    """账本和 WAL 一个字节都不变。-shm 可写时读者会在里面登记读标记（SQLite 的共享内存锁，不是数据），
    服务器上看板用户对它没有写权限，那种情况见下一个测试。"""
    data = env.root / "data"
    before = files_of(data, skip=(".db-shm",))
    snap = env.snapshot()
    assert snap["bots"][0]["last_bar"]["equity"] == 578.0  # 还在 WAL 里没 checkpoint 的数据也读得到
    assert files_of(data, skip=(".db-shm",)) == before


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root 无视文件权限")
def test_works_without_write_permission(env):
    """服务器上看板用户对 data/ 和 -shm 都没有写权限，而且和机器人是不同进程
    （同一进程里 SQLite 会复用写入方已映射的 -shm，测不出来），所以在子进程里读。"""
    data = env.root / "data"
    for f in data.iterdir():
        f.chmod(0o444)
    data.chmod(0o555)
    code = ("import json, sys; sys.path.insert(0, sys.argv[1]); import dashboard as d; "
            "db = d.read_db(d.Path(sys.argv[2])); "
            "print(json.dumps([len(db['fills']), max(e for _, e in db['runs'][sys.argv[3]]['equity'])]))")
    try:
        before = files_of(data)
        out = subprocess.run([sys.executable, "-c", code, str(dash.HTML_PATH.parent),
                              str(data / "portfolio.db"), SOL], capture_output=True, text=True, timeout=30)
        assert out.returncode == 0, out.stderr
        assert json.loads(out.stdout) == [3, 586.0]  # WAL 里的数据也读得到
        assert files_of(data) == before  # 包括 -shm 在内一个字节都不变
    finally:
        data.chmod(0o755)
        for f in data.iterdir():
            f.chmod(0o644)


def test_ro_connection_rejects_writes(env):
    conn = dash.open_ro(env.root / "data" / "portfolio.db")
    with pytest.raises(Exception, match="readonly"):
        conn.execute("INSERT INTO state VALUES ('x', '{}')")
    conn.close()


# ---- 小工具 ---------------------------------------------------------------

def test_downsample_keeps_extremes_and_last():
    pts = [[i, 100.0 + (i % 50)] for i in range(10_000)]
    pts[4321][1] = 1.0  # 谷底
    out = dash.downsample(pts, 600)
    assert len(out) <= 601
    assert [4321, 1.0] in out and out[-1] == pts[-1] and out[0][0] == 0
    assert [p[0] for p in out] == sorted(p[0] for p in out)
    assert dash.downsample(pts[:10], 600) == pts[:10]


def test_curve_stays_small_for_a_long_run(env):
    start = BAR0 - 90 * 24 * H
    env.guard_lines = [f"{utc(start + i * 300_000)}  权益 {1000 + i % 97:.2f}  峰值 1200.00  回撤 -5.00%  "
                       f"持仓 1  免费 900.00" for i in range(90 * 288)]
    env.write_guard()
    snap = env.snapshot(now=start + 90 * 288 * 300_000)
    assert len(snap["curve"]) <= 601
    body = json.dumps(snap, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(gzip.compress(body)) < 20_000


def test_unix_ms_formats():
    assert dash._unix_ms("@1792649414") == 1792649414000
    assert dash._unix_ms("Thu 2026-10-22 06:10:10 UTC") == 1792649410000
    assert dash._unix_ms("") is None and dash._unix_ms("n/a") is None


def test_run_symbol():
    assert dash.run_symbol(BTC) == "BTC/USDT:USDT"


# ---- HTTP -----------------------------------------------------------------

@pytest.fixture
def server(env):
    calls = []

    def build(cfg):
        calls.append(1)
        return env.snapshot()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), None)
    port = httpd.server_port
    httpd.RequestHandlerClass = dash.make_handler(dash.Snapshots(env.cfg, 30, build), dash.HTML_PATH.read_bytes())
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield port, calls
    httpd.shutdown()
    httpd.server_close()


def request(port, path, method="GET", host=None, gzip_ok=False):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    if host:
        req.add_header("Host", host)
    if gzip_ok:
        req.add_header("Accept-Encoding", "gzip")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_http_serves_page_and_snapshot(server):
    port, calls = server
    code, headers, body = request(port, "/")
    assert code == 200 and headers["Content-Type"].startswith("text/html")
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert headers["Cache-Control"] == "no-store" and "交易看板" in body.decode()

    code, headers, body = request(port, "/api/snapshot", gzip_ok=True)
    assert code == 200 and headers["Content-Encoding"] == "gzip"
    snap = json.loads(gzip.decompress(body))
    assert snap["account"]["equity"] == 1150.0
    request(port, "/api/snapshot")
    assert len(calls) == 1  # 30 秒内共用一份快照


def test_http_rejects_writes_foreign_hosts_and_unknown_paths(server):
    port, _ = server
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        assert request(port, "/api/snapshot", method=method)[0] in (405, 501)
    assert request(port, "/", host="evil.example:8765")[0] == 403  # DNS rebinding
    assert request(port, "/api/snapshot", host=f"attacker.test:{port}")[0] == 403
    assert request(port, "/", host=f"localhost:{port}")[0] == 200
    assert request(port, "/", host="127.0.0.1:18080")[0] == 200  # 隧道换了本地端口也能用
    assert request(port, "/", host="localhost.evil.example")[0] == 403
    assert request(port, "/.env")[0] == 404
    assert request(port, "/../.env")[0] == 404


def test_serve_refuses_public_bind(env):
    with pytest.raises(SystemExit, match="只允许监听回环地址"):
        dash.serve(env.cfg, "0.0.0.0", 0, 30)


def test_page_is_read_only_and_self_contained():
    html = dash.HTML_PATH.read_text(encoding="utf-8")
    assert "<form" not in html and "<input" not in html
    assert html.count("<button") == 1 and 'id="refresh"' in html  # 唯一的按钮是重新拉取（GET）
    assert "innerHTML" not in html  # 日志 / 原因等文本一律走 textContent
    assert 'method: "POST"' not in html and "fetch(\"/api/snapshot\"" in html
    for ext in ("<script src", "<link", "@import", "https://"):
        assert ext not in html  # 不加载任何外部资源
