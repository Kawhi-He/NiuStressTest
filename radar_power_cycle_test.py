# -*- coding: utf-8 -*-
"""
脚本逻辑说明：

压力 1：OTA 升级杀后台压力
1. 打开 NIUBleTools，进入“蓝牙OTA升级”
2. 连接蓝牙，选择“外设-雷达”
3. 开始 OTA 升级
4. 监控 OTA 包进度，每 1% 杀一次整个 APP 后台，共 1%~100% 共 100 次
5. 每次杀后台后，重新打开 APP，再完整执行一次 OTA 升级直到成功
6. OTA 成功后，程控电源断电重启
7. 重新上电后，读取雷达 RCU 三个状态：BSD纵向长度、后向碰撞预警、盲区角度

压力 2：断电上电读取雷达状态压力
1. 程控电源断电
2. 检查电压是否接近 0V
3. 程控电源 12V 上电
4. 检查电压是否接近 12V
5. 打开 NIUBleTools，进入“雷达RCU蓝牙指令验证”
6. 连接蓝牙并读取 BSD纵向长度、后向碰撞预警、盲区角度
7. 默认重复配置的测试次数

STRESS_MODE：
0 = 串行执行压力 1 和压力 2
1 = 只执行压力 1
2 = 只执行压力 2
"""

import argparse
import csv
import datetime
import logging
import sys
import time
from pathlib import Path
from typing import Any

from niu_bletools import DEVICE_ID, TARGET_COMMANDS, NiuBleTools
from programmable_power import ItechIt6121B

APP_LOG_LEVEL = 25
logging.addLevelName(APP_LOG_LEVEL, "APP_LOG")
LOCAL_TZ = datetime.timezone(datetime.timedelta(hours=8), name="Asia/Shanghai")

# ===== 测试工程师配置区 =====
# 压力选择：0=两个压力串行跑；1=OTA升级杀后台压力；2=断电上电读取雷达状态压力
DEFAULT_STRESS_MODE = 2

# 压力 2 默认循环次数
DEFAULT_ITERATIONS = 100

# 手机 ADB 设备序列号
DEFAULT_DEVICE_ID = DEVICE_ID

# 程控电源串口配置
DEFAULT_POWER_PORT = "COM25"
DEFAULT_BAUDRATE = 115200

# 程控电源上电配置
DEFAULT_VOLTAGE = 12.0
DEFAULT_CURRENT = 3.0

# 下电/上电后的等待时间，单位：秒
DEFAULT_POWER_OFF_WAIT_SECONDS = 3.0
DEFAULT_POWER_ON_WAIT_SECONDS = 3.0

# 电压确认阈值：下电后 <= 0.2V，上电后 12V ± 0.5V
DEFAULT_POWER_OFF_MAX_VOLTAGE = 0.2
DEFAULT_POWER_ON_TOLERANCE = 0.5

# 压力 1 配置：每 1% 杀一次 APP，默认 1~100 共 100 次
DEFAULT_OTA_KILL_START_PERCENT = 1
DEFAULT_OTA_KILL_END_PERCENT = 100
DEFAULT_OTA_KILL_STEP_PERCENT = 1
DEFAULT_OTA_MONITOR_TIMEOUT_SECONDS = 180

# 日志目录
DEFAULT_LOG_DIR = "logs"
# ============================


def timestamp() -> str:
    return datetime.datetime.now(LOCAL_TZ).isoformat(timespec="milliseconds")


class TimezoneFormatter(logging.Formatter):
    converter = None

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = datetime.datetime.fromtimestamp(record.created, LOCAL_TZ)
        return dt.isoformat(timespec="milliseconds")


def setup_logger(log_file: Path) -> logging.Logger:
    logger = logging.getLogger("radar_power_cycle")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    fmt = "%(asctime)s %(levelname)-7s %(filename)s:%(lineno)d %(message)s"
    formatter = TimezoneFormatter(fmt)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


def log_app(logger: logging.Logger, app_log: str) -> None:
    # NiuBleTools.read_log() already emits incremental APP_LOG lines.
    # Keep this helper for compatibility, but avoid duplicating large APP logs.
    return
    for line in app_log.splitlines():
        logger.log(APP_LOG_LEVEL, line)


def build_status_line(state: dict[str, Any]) -> str:
    return (
        f"stress={state.get('stress_name', 'unknown')}, "
        f"current={state.get('current_item', '-')}, "
        f"step={state.get('step', '-')}, "
        f"pass={state.get('pass_count', 0)}, "
        f"fail={state.get('fail_count', 0)}, "
        f"last_result={state.get('last_result', 'N/A')}"
    )


def log_interrupt_summary(logger: logging.Logger, state: dict[str, Any]) -> None:
    logger.warning("Interrupted by CTRL+C")
    logger.warning("Current status: %s", build_status_line(state))


def append_csv_row(path: Path, row: dict[str, object]) -> None:
    fieldnames = [
        "time",
        "stress",
        "iteration",
        "ota_target_percent",
        "ota_killed_progress",
        "status",
        "hub_rcu_bsd_length",
        "hub_rcu_rcw_length",
        "hub_rcu_bsd_degree",
        "error",
    ]
    file_exists = path.exists()
    with path.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def read_radar_once(bletools: NiuBleTools) -> tuple[dict[str, str], str]:
    bletools.open_app()
    bletools.enter_radar_rcu_page()
    bletools.connect_ble()
    return bletools.read_radar_rcu_values()


def read_voltage_or_raise(power: ItechIt6121B) -> float:
    voltage = power.read_actual_voltage()
    if voltage is None:
        raise RuntimeError("Failed to read actual voltage from power supply")
    return voltage


def assert_power_off(power: ItechIt6121B, max_voltage: float, logger: logging.Logger) -> float:
    voltage = read_voltage_or_raise(power)
    current = power.read_actual_current()
    logger.debug("Measured after power off: voltage=%.3f V, current=%s A", voltage, "N/A" if current is None else f"{current:.3f}")
    if voltage > max_voltage:
        raise RuntimeError(f"Power off verification failed: voltage={voltage:.3f} V > {max_voltage:.3f} V")
    return voltage


def assert_power_on(power: ItechIt6121B, expected_voltage: float, tolerance: float, logger: logging.Logger) -> float:
    voltage = read_voltage_or_raise(power)
    current = power.read_actual_current()
    logger.debug("Measured after power on: voltage=%.3f V, current=%s A", voltage, "N/A" if current is None else f"{current:.3f}")
    low = expected_voltage - tolerance
    high = expected_voltage + tolerance
    if not low <= voltage <= high:
        raise RuntimeError(f"Power on verification failed: voltage={voltage:.3f} V not in [{low:.3f}, {high:.3f}] V")
    return voltage


def power_cycle_and_verify(power: ItechIt6121B, args: argparse.Namespace, logger: logging.Logger) -> tuple[float, float]:
    logger.info("Power OFF")
    power.output_off()
    time.sleep(args.power_off_wait)
    off_voltage = assert_power_off(power, args.power_off_max_voltage, logger)
    logger.info("Power OFF verified: voltage=%.3f V", off_voltage)

    logger.info("Power ON: %.3f V", args.voltage)
    power.configure_output(args.voltage, args.current)
    power.output_on()
    time.sleep(args.power_on_wait)
    on_voltage = assert_power_on(power, args.voltage, args.power_on_tolerance, logger)
    logger.info("Power ON verified: voltage=%.3f V", on_voltage)
    return off_voltage, on_voltage


def reset_app_selection_state(bletools: NiuBleTools) -> None:
    bletools.read_commands_selected = False


def run_power_cycle_read_stress(
    args: argparse.Namespace,
    logger: logging.Logger,
    csv_log: Path,
    bletools: NiuBleTools,
    power: ItechIt6121B,
    state: dict[str, Any],
) -> tuple[int, int]:
    logger.info("===== Start stress 2: power-cycle read Radar RCU values =====")
    pass_count = 0
    fail_count = 0
    state["stress_name"] = "stress_2_power_cycle_read"

    for iteration in range(1, args.iterations + 1):
        logger.info("===== Stress 2 iteration %s/%s =====", iteration, args.iterations)
        state["current_item"] = f"iteration {iteration}/{args.iterations}"
        state["step"] = "power cycle"
        state["last_result"] = "IN_PROGRESS"
        row: dict[str, object] = {
            "time": timestamp(),
            "stress": "power_cycle_read",
            "iteration": iteration,
            "ota_target_percent": "",
            "ota_killed_progress": "",
            "status": "FAIL",
            "hub_rcu_bsd_length": "",
            "hub_rcu_rcw_length": "",
            "hub_rcu_bsd_degree": "",
            "error": "",
        }

        try:
            power_cycle_and_verify(power, args, logger)
            state["step"] = "read radar values"
            values, app_log = read_radar_once(bletools)
            row.update(
                {
                    "status": "PASS",
                    "hub_rcu_bsd_length": values.get("hub_rcu_bsd_length", ""),
                    "hub_rcu_rcw_length": values.get("hub_rcu_rcw_length", ""),
                    "hub_rcu_bsd_degree": values.get("hub_rcu_bsd_degree", ""),
                }
            )
            pass_count += 1
            state["pass_count"] = pass_count
            state["last_result"] = "PASS"
            logger.info("Stress 2 PASS values=%s", values)
            log_app(logger, app_log)
        except Exception as exc:
            fail_count += 1
            state["fail_count"] = fail_count
            state["last_result"] = "FAIL"
            row["error"] = str(exc)
            logger.exception("Stress 2 FAIL: %s", exc)
            if args.stop_on_fail:
                append_csv_row(csv_log, row)
                raise
        finally:
            append_csv_row(csv_log, row)

    logger.info("Stress 2 finished: pass=%s, fail=%s, total=%s", pass_count, fail_count, args.iterations)
    return pass_count, fail_count


def run_ota_kill_app_stress(
    args: argparse.Namespace,
    logger: logging.Logger,
    csv_log: Path,
    bletools: NiuBleTools,
    power: ItechIt6121B,
    state: dict[str, Any],
) -> tuple[int, int]:
    logger.info("===== Start stress 1: OTA kill-app every percent =====")
    pass_count = 0
    fail_count = 0
    percents = range(args.ota_kill_start_percent, args.ota_kill_end_percent + 1, args.ota_kill_step_percent)
    state["stress_name"] = "stress_1_ota_kill_app"

    for index, percent in enumerate(percents, start=1):
        logger.info("===== Stress 1 point %s: kill app at OTA progress >= %s%% =====", index, percent)
        state["current_item"] = f"target {percent}%"
        state["step"] = "prepare ota"
        state["last_result"] = "IN_PROGRESS"
        row: dict[str, object] = {
            "time": timestamp(),
            "stress": "ota_kill_app",
            "iteration": percent,
            "ota_target_percent": percent,
            "ota_killed_progress": "",
            "status": "FAIL",
            "hub_rcu_bsd_length": "",
            "hub_rcu_rcw_length": "",
            "hub_rcu_bsd_degree": "",
            "error": "",
        }

        try:
            bletools.prepare_ota_upgrade()
            state["step"] = "start ota"
            bletools.start_ota_upgrade()
            state["step"] = f"wait ota >= {percent}%"
            killed_progress = bletools.kill_app_at_ota_progress(percent, timeout_seconds=args.ota_monitor_timeout)
            row["ota_killed_progress"] = f"{killed_progress:.1f}"
            logger.info("Killed app at OTA progress %.1f%% for target %s%%", killed_progress, percent)

            state["step"] = "restart ota"
            bletools.prepare_ota_upgrade()
            bletools.run_ota_upgrade_to_success(timeout_seconds=args.ota_monitor_timeout)

            state["step"] = "power cycle"
            power_cycle_and_verify(power, args, logger)
            reset_app_selection_state(bletools)

            state["step"] = "read radar values"
            values, app_log = read_radar_once(bletools)
            row.update(
                {
                    "status": "PASS",
                    "hub_rcu_bsd_length": values.get("hub_rcu_bsd_length", ""),
                    "hub_rcu_rcw_length": values.get("hub_rcu_rcw_length", ""),
                    "hub_rcu_bsd_degree": values.get("hub_rcu_bsd_degree", ""),
                }
            )
            pass_count += 1
            state["pass_count"] = pass_count
            state["last_result"] = "PASS"
            logger.info("Stress 1 PASS target=%s killed_progress=%.1f values=%s", percent, killed_progress, values)
            log_app(logger, app_log)
        except Exception as exc:
            fail_count += 1
            state["fail_count"] = fail_count
            state["last_result"] = "FAIL"
            row["error"] = str(exc)
            logger.exception("Stress 1 FAIL at target %s%%: %s", percent, exc)
            if args.stop_on_fail:
                append_csv_row(csv_log, row)
                raise
        finally:
            append_csv_row(csv_log, row)

    logger.info("Stress 1 finished: pass=%s, fail=%s", pass_count, fail_count)
    return pass_count, fail_count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Radar RCU stress test runner.")
    parser.add_argument("--stress-mode", type=int, choices=(0, 1, 2), default=DEFAULT_STRESS_MODE, help=f"0=run stress 1 and 2, 1=OTA kill app stress, 2=power cycle read stress. Default: {DEFAULT_STRESS_MODE}")
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS, help=f"Loop count. Default: {DEFAULT_ITERATIONS}")
    parser.add_argument("--device-id", default=DEFAULT_DEVICE_ID, help=f"ADB device id. Default: {DEFAULT_DEVICE_ID}")
    parser.add_argument("--power-port", default=DEFAULT_POWER_PORT, help=f"Programmable power supply serial port. Default: {DEFAULT_POWER_PORT}")
    parser.add_argument("--baudrate", type=int, default=DEFAULT_BAUDRATE, help=f"Power supply baudrate. Default: {DEFAULT_BAUDRATE}")
    parser.add_argument("--voltage", type=float, default=DEFAULT_VOLTAGE, help=f"Power-on voltage. Default: {DEFAULT_VOLTAGE}")
    parser.add_argument("--current", type=float, default=DEFAULT_CURRENT, help=f"Current limit. Default: {DEFAULT_CURRENT}")
    parser.add_argument("--power-off-wait", type=float, default=DEFAULT_POWER_OFF_WAIT_SECONDS, help=f"Wait seconds after power off. Default: {DEFAULT_POWER_OFF_WAIT_SECONDS}")
    parser.add_argument("--power-on-wait", type=float, default=DEFAULT_POWER_ON_WAIT_SECONDS, help=f"Wait seconds after power on. Default: {DEFAULT_POWER_ON_WAIT_SECONDS}")
    parser.add_argument("--power-off-max-voltage", type=float, default=DEFAULT_POWER_OFF_MAX_VOLTAGE, help=f"Max voltage allowed after power off. Default: {DEFAULT_POWER_OFF_MAX_VOLTAGE}")
    parser.add_argument("--power-on-tolerance", type=float, default=DEFAULT_POWER_ON_TOLERANCE, help=f"Allowed voltage tolerance after power on. Default: +/-{DEFAULT_POWER_ON_TOLERANCE}")
    parser.add_argument("--ota-kill-start-percent", type=int, default=DEFAULT_OTA_KILL_START_PERCENT, help=f"OTA kill start percent. Default: {DEFAULT_OTA_KILL_START_PERCENT}")
    parser.add_argument("--ota-kill-end-percent", type=int, default=DEFAULT_OTA_KILL_END_PERCENT, help=f"OTA kill end percent. Default: {DEFAULT_OTA_KILL_END_PERCENT}")
    parser.add_argument("--ota-kill-step-percent", type=int, default=DEFAULT_OTA_KILL_STEP_PERCENT, help=f"OTA kill step percent. Default: {DEFAULT_OTA_KILL_STEP_PERCENT}")
    parser.add_argument("--ota-monitor-timeout", type=int, default=DEFAULT_OTA_MONITOR_TIMEOUT_SECONDS, help=f"OTA monitor timeout seconds. Default: {DEFAULT_OTA_MONITOR_TIMEOUT_SECONDS}")
    parser.add_argument("--log-dir", default=DEFAULT_LOG_DIR, help=f"Output log directory. Default: {DEFAULT_LOG_DIR}")
    parser.add_argument("--stop-on-fail", action="store_true", help="Stop immediately after a failed iteration.")
    return parser.parse_args()


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    args = parse_args()

    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    run_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    text_log = log_dir / f"radar_power_cycle_{run_id}.log"
    csv_log = log_dir / f"radar_power_cycle_{run_id}.csv"
    logger = setup_logger(text_log)

    bletools = NiuBleTools(device_id=args.device_id, logger=logger)
    power = ItechIt6121B(port=args.power_port, baudrate=args.baudrate, logger=logger)
    state: dict[str, Any] = {
        "stress_name": "idle",
        "current_item": "-",
        "step": "-",
        "pass_count": 0,
        "fail_count": 0,
        "last_result": "N/A",
    }

    logger.info("Start test: iterations=%s, voltage=%.3fV, current=%.3fA", args.iterations, args.voltage, args.current)
    logger.info("Stress mode=%s (0=stress1+stress2, 1=OTA kill app, 2=power-cycle read)", args.stress_mode)
    logger.info("Target commands: %s", ", ".join(TARGET_COMMANDS.values()))

    try:
        bletools.check_device()
        power.connect()

        total_pass = 0
        total_fail = 0

        if args.stress_mode in (0, 1):
            pass_count, fail_count = run_ota_kill_app_stress(args, logger, csv_log, bletools, power, state)
            total_pass += pass_count
            total_fail += fail_count

        if args.stress_mode in (0, 2):
            pass_count, fail_count = run_power_cycle_read_stress(args, logger, csv_log, bletools, power, state)
            total_pass += pass_count
            total_fail += fail_count

        logger.info("Finished all selected stress tests: pass=%s, fail=%s", total_pass, total_fail)
        return 0 if total_fail == 0 else 1
    except KeyboardInterrupt:
        log_interrupt_summary(logger, state)
        print(f"CTRL+C current status: {build_status_line(state)}", flush=True)
        return 130
    finally:
        power.close()


if __name__ == "__main__":
    raise SystemExit(main())
