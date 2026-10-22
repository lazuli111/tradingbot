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
        return self.mark_price

    def snapshot(self, now=NOW):
        return dash.build_snapshot(self.cfg, now, show=self.show, mark=self.mark)


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
    httpd.RequestHandlerClass = dash.make_handler(dash.Snapshots(env.cfg, 30, build),
                                                  dash.HTML_PATH.read_bytes(), port)
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
