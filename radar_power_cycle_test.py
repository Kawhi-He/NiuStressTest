# -*- coding: utf-8 -*-
"""
雷达 RCU 压力测试主脚本
==========================

Author: Kawhi.He

用途：
    通过 ADB 自动操作手机端 NIU Ble Tools，并通过 ITECH IT6121B 程控电源
    控制雷达断电/上电，完成雷达 RCU 的 OTA 中断恢复和上电后状态读取压力测试。

运行前准备：
    1. Windows 电脑已安装 Python 3.10+，并安装 pyserial。
    2. 手机已开启 USB 调试，`adb devices` 能看到目标设备且状态为 device。
    3. 手机已安装 NIU Ble Tools，且蓝牙、定位等权限已提前允许。
    4. 程控电源串口号、波特率、电压、电流限制配置正确。
    5. 雷达和手机处于可连接状态，测试期间尽量不要手动操作手机。

压力 1：OTA 升级杀后台压力
    目的：
        验证 OTA 升级被 APP 杀后台打断后，重新进入 APP 是否还能恢复并最终升级成功；
        升级成功后再通过断电重启读取雷达状态，确认设备仍可正常通信。

    流程：
        1. 打开 NIU Ble Tools，进入“蓝牙OTA升级”。
        2. 连接蓝牙，选择“外设-雷达”。
        3. 先完整跑一次 OTA，记录基准升级耗时。
        4. 按配置的百分比区间监控 OTA 进度，默认 1%~100%，每 1% 杀一次 APP。
        5. 每次杀后台后重新打开 APP，重新进入 OTA 页面并继续执行 OTA 直到成功。
        6. OTA 成功后控制电源断电，再按配置电压上电。
        7. 重新连接后读取三项雷达 RCU 状态：
           BSD 纵向长度、后向碰撞预警、盲区角度。

压力 2：断电上电读取雷达状态压力
    目的：
        验证雷达反复断电/上电后，NIU Ble Tools 是否能稳定连接并读取关键状态。

    流程：
        1. 程控电源断电。
        2. 检查实际电压是否低于下电阈值，默认 <= 0.2V。
        3. 程控电源按配置电压上电，默认 12V / 3A。
        4. 检查实际电压是否接近目标电压，默认允许 +/-0.5V。
        5. 打开 NIU Ble Tools，进入“雷达RCU蓝牙指令验证”。
        6. 连接蓝牙并读取 BSD 纵向长度、后向碰撞预警、盲区角度。
        7. 按配置次数重复执行，默认 100 次。

常用配置：
    DEFAULT_STRESS_MODE:
        0 = 串行执行压力 1 和压力 2
        1 = 只执行压力 1
        2 = 只执行压力 2
    DEFAULT_ITERATIONS:
        压力 2 默认循环次数。
    DEFAULT_POWER_PORT / DEFAULT_BAUDRATE:
        程控电源串口和波特率。
    DEFAULT_VOLTAGE / DEFAULT_CURRENT:
        上电目标电压和电流限制。
    DEFAULT_OTA_KILL_START_PERCENT / END / STEP:
        压力 1 中杀 APP 的 OTA 进度范围和步进。
    DEFAULT_LOG_DIR:
        日志输出目录，默认 logs。

运行示例：
    python .\radar_power_cycle_test.py --stress-mode 1
    python .\radar_power_cycle_test.py --stress-mode 2 --iterations 200
    python .\radar_power_cycle_test.py --stress-mode 0 --stop-on-fail

输出：
    每次运行会在日志目录生成一份文本日志和一份 CSV 结果文件。
    CSV 会记录每轮测试的压力类型、目标进度/轮次、电压读数、状态值和错误信息。

中断与失败处理：
    1. 默认单轮失败后继续跑后续轮次，并在日志/CSV 中记录失败原因。
    2. 加 `--stop-on-fail` 后，遇到失败会立即停止。
    3. 按 Ctrl+C 会打印当前压力项、执行步骤、通过/失败计数等摘要。
"""

import argparse
import csv
import ctypes
import datetime
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any

from niu_bletools import DEVICE_ID, TARGET_COMMANDS, NiuBleTools
from programmable_power import ItechIt6121B

APP_LOG_LEVEL = 25
logging.addLevelName(APP_LOG_LEVEL, "APP_LOG")
LOCAL_TZ = datetime.timezone(datetime.timedelta(hours=8), name="Asia/Shanghai")
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

# ===== 测试工程师配置区 =====
# 压力选择：0=两个压力串行跑；1=OTA升级杀后台压力；2=断电上电读取雷达状态压力
DEFAULT_STRESS_MODE = 1

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
    """Generate current timestamp string in local configured timezone.

    Author: Kawhi.He

    Args:
        None.

    Returns:
        ISO-8601 timestamp string with milliseconds.
    """
    return datetime.datetime.now(LOCAL_TZ).isoformat(timespec="milliseconds")


class WindowsAwakeGuard:
    """Prevent Windows system sleep/display-off while test is running.

    Author: Kawhi.He
    """

    HEARTBEAT_INTERVAL = 30

    def __init__(self, logger: logging.Logger) -> None:
        """Initialize awake guard state.

        Author: Kawhi.He

        Args:
            logger (logging.Logger): Logger for status and warning output.

        Returns:
            None.
        """
        self.logger = logger
        self.enabled = False
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def enable(self) -> None:
        """Enable periodic execution-state refresh on Windows platforms.

        Author: Kawhi.He

        Args:
            None.

        Returns:
            None.
        """
        if sys.platform != "win32":
            return
        flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
        result = ctypes.windll.kernel32.SetThreadExecutionState(flags)
        if result == 0:
            self.logger.warning(
                "Failed to request Windows awake/display-on state"
            )
            return
        self.enabled = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._heartbeat, daemon=True)
        self._thread.start()
        self.logger.info(
            "Windows awake guard enabled: "
            "prevent sleep and display off while test is running"
        )

    def disable(self) -> None:
        """Disable awake guard and restore default Windows execution state.

        Author: Kawhi.He

        Args:
            None.

        Returns:
            None.
        """
        if not self.enabled or sys.platform != "win32":
            return
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        result = ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        if result == 0:
            self.logger.warning("Failed to restore Windows execution state")
            return
        self.enabled = False
        self.logger.info("Windows awake guard disabled")

    def _heartbeat(self) -> None:
        """Refresh Windows execution-state flags on fixed intervals.

        Author: Kawhi.He

        Args:
            None.

        Returns:
            None.
        """
        while not self._stop_event.wait(self.HEARTBEAT_INTERVAL):
            flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
            ctypes.windll.kernel32.SetThreadExecutionState(flags)


class TimezoneFormatter(logging.Formatter):
    """Logging formatter that emits timestamps in configured local timezone.

    Author: Kawhi.He
    """

    converter = None

    def formatTime(
        self,
        record: logging.LogRecord,
        datefmt: str | None = None,
    ) -> str:
        """Format log record creation time using LOCAL_TZ timezone.

        Author: Kawhi.He

        Args:
            record (logging.LogRecord): Logging record to format.
            datefmt (str | None): Optional date format string
                (unused, kept for compatibility).

        Returns:
            Formatted ISO timestamp with milliseconds.
        """
        dt = datetime.datetime.fromtimestamp(record.created, LOCAL_TZ)
        return dt.isoformat(timespec="milliseconds")


def setup_logger(log_file: Path) -> logging.Logger:
    """Create configured logger with file and console handlers.

    Author: Kawhi.He

    Args:
        log_file (Path): Path to detailed run log file.

    Returns:
        Configured logger instance used by this test suite.
    """
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
    """Compatibility wrapper for app log output.

    Author: Kawhi.He

    Args:
        logger (logging.Logger): Logger used for output.
        app_log (str): Full app log text.

    Returns:
        None.
    """
    # NiuBleTools.read_log() already emits incremental APP_LOG lines.
    # Keep this helper for compatibility without duplicating large app logs.
    del logger
    del app_log
    return


def build_status_line(state: dict[str, Any]) -> str:
    """Build one-line readable progress summary from runtime state.

    Author: Kawhi.He

    Args:
        state (dict[str, Any]): Mutable status dictionary shared by
            stress flows.

    Returns:
        Human-readable status line string.
    """
    return (
        f"stress={state.get('stress_name', 'unknown')}, "
        f"current={state.get('current_item', '-')}, "
        f"step={state.get('step', '-')}, "
        f"pass={state.get('pass_count', 0)}, "
        f"fail={state.get('fail_count', 0)}, "
        f"last_result={state.get('last_result', 'N/A')}"
    )


def log_interrupt_summary(
    logger: logging.Logger,
    state: dict[str, Any],
) -> None:
    """Log summary when user interrupts execution with Ctrl+C.

    Author: Kawhi.He

    Args:
        logger (logging.Logger): Logger used for writing interruption details.
        state (dict[str, Any]): Runtime status dictionary.

    Returns:
        None.
    """
    logger.warning("Interrupted by CTRL+C")
    logger.warning("Current status: %s", build_status_line(state))


def append_csv_row(path: Path, row: dict[str, object]) -> None:
    """Append one structured result row into CSV file.

    Author: Kawhi.He

    Args:
        path (Path): CSV file path.
        row (dict[str, object]): Row dictionary following predefined
            field names.

    Returns:
        None.
    """
    fieldnames = [
        "time",
        "stress",
        "iteration",
        "ota_target_percent",
        "ota_killed_progress",
        "ota_baseline_seconds",
        "ota_killed_elapsed_seconds",
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
    """Open app, enter radar page, connect BLE, and read target values once.

    Author: Kawhi.He

    Args:
        bletools (NiuBleTools): NIU app automation helper instance.

    Returns:
        Tuple of parsed command values and raw final app log.
    """
    bletools.open_app()
    bletools.enter_radar_rcu_page()
    bletools.connect_ble()
    return bletools.read_radar_rcu_values()


def read_voltage_or_raise(power: ItechIt6121B) -> float:
    """Read actual voltage and raise exception when reading fails.

    Author: Kawhi.He

    Args:
        power (ItechIt6121B): Programmable power controller instance.

    Returns:
        Measured voltage in volts.
    """
    voltage = power.read_actual_voltage()
    if voltage is None:
        raise RuntimeError("Failed to read actual voltage from power supply")
    return voltage


def assert_power_off(
    power: ItechIt6121B,
    max_voltage: float,
    logger: logging.Logger,
) -> float:
    """Validate power-off state by checking voltage below threshold.

    Author: Kawhi.He

    Args:
        power (ItechIt6121B): Programmable power controller instance.
        max_voltage (float): Maximum acceptable voltage in off state.
        logger (logging.Logger): Logger used for detailed measurement logs.

    Returns:
        Measured voltage after power off.
    """
    voltage = read_voltage_or_raise(power)
    current = power.read_actual_current()
    logger.debug(
        "Measured after power off: voltage=%.3f V, current=%s A",
        voltage,
        "N/A" if current is None else f"{current:.3f}",
    )
    if voltage > max_voltage:
        raise RuntimeError(
            "Power off verification failed: "
            f"voltage={voltage:.3f} V > {max_voltage:.3f} V"
        )
    return voltage


def assert_power_on(
    power: ItechIt6121B,
    expected_voltage: float,
    tolerance: float,
    logger: logging.Logger,
) -> float:
    """Validate power-on state by checking voltage around target range.

    Author: Kawhi.He

    Args:
        power (ItechIt6121B): Programmable power controller instance.
        expected_voltage (float): Desired output voltage target.
        tolerance (float): Allowed absolute deviation from expected voltage.
        logger (logging.Logger): Logger used for detailed measurement logs.

    Returns:
        Measured voltage after power on.
    """
    voltage = read_voltage_or_raise(power)
    current = power.read_actual_current()
    logger.debug(
        "Measured after power on: voltage=%.3f V, current=%s A",
        voltage,
        "N/A" if current is None else f"{current:.3f}",
    )
    low = expected_voltage - tolerance
    high = expected_voltage + tolerance
    if not low <= voltage <= high:
        raise RuntimeError(
            "Power on verification failed: "
            f"voltage={voltage:.3f} V not in [{low:.3f}, {high:.3f}] V"
        )
    return voltage


def power_cycle_and_verify(
    power: ItechIt6121B,
    args: argparse.Namespace,
    logger: logging.Logger,
) -> tuple[float, float]:
    """Perform one full power off/on sequence and verify voltages.

    Author: Kawhi.He

    Args:
        power (ItechIt6121B): Programmable power controller instance.
        args (argparse.Namespace): Parsed CLI args containing power
            timing and threshold settings.
        logger (logging.Logger): Logger used for progress and measurements.

    Returns:
        Tuple of (off_voltage, on_voltage).
    """
    logger.info("Power OFF")
    power.output_off()
    time.sleep(args.power_off_wait)
    off_voltage = assert_power_off(power, args.power_off_max_voltage, logger)
    logger.info("Power OFF verified: voltage=%.3f V", off_voltage)

    logger.info("Power ON: %.3f V", args.voltage)
    power.configure_output(args.voltage, args.current)
    power.output_on()
    time.sleep(args.power_on_wait)
    on_voltage = assert_power_on(
        power,
        args.voltage,
        args.power_on_tolerance,
        logger,
    )
    logger.info("Power ON verified: voltage=%.3f V", on_voltage)
    return off_voltage, on_voltage


def reset_app_selection_state(bletools: NiuBleTools) -> None:
    """Reset command-selection cache state in automation helper.

    Author: Kawhi.He

    Args:
        bletools (NiuBleTools): NIU app automation helper instance.

    Returns:
        None.
    """
    bletools.read_commands_selected = False


def run_power_cycle_read_stress(
    args: argparse.Namespace,
    logger: logging.Logger,
    csv_log: Path,
    bletools: NiuBleTools,
    power: ItechIt6121B,
    state: dict[str, Any],
) -> tuple[int, int]:
    """Run stress mode 2: repeated power cycle and radar value reading.

    Author: Kawhi.He

    Args:
        args (argparse.Namespace): Parsed CLI arguments.
        logger (logging.Logger): Logger for runtime information.
        csv_log (Path): CSV output path.
        bletools (NiuBleTools): NIU app automation helper.
        power (ItechIt6121B): Programmable power controller.
        state (dict[str, Any]): Mutable runtime state dictionary.

    Returns:
        Tuple of (pass_count, fail_count).
    """
    logger.info(
        "===== Start stress 2: "
        "power-cycle read Radar RCU values ====="
    )
    pass_count = 0
    fail_count = 0
    state["stress_name"] = "stress_2_power_cycle_read"

    for iteration in range(1, args.iterations + 1):
        logger.info(
            "===== Stress 2 iteration %s/%s =====",
            iteration,
            args.iterations,
        )
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
        except RuntimeError as exc:
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

    logger.info(
        "Stress 2 finished: pass=%s, fail=%s, total=%s",
        pass_count,
        fail_count,
        args.iterations,
    )
    return pass_count, fail_count


def run_ota_kill_app_stress(
    args: argparse.Namespace,
    logger: logging.Logger,
    csv_log: Path,
    bletools: NiuBleTools,
    power: ItechIt6121B,
    state: dict[str, Any],
) -> tuple[int, int]:
    """Run stress mode 1: timed OTA interruption and recovery verification.

    Author: Kawhi.He

    Args:
        args (argparse.Namespace): Parsed CLI arguments.
        logger (logging.Logger): Logger for runtime information.
        csv_log (Path): CSV output path.
        bletools (NiuBleTools): NIU app automation helper.
        power (ItechIt6121B): Programmable power controller.
        state (dict[str, Any]): Mutable runtime state dictionary.

    Returns:
        Tuple of (pass_count, fail_count).
    """
    logger.info(
        "===== Start stress 1: OTA timed kill-app every percent ====="
    )
    pass_count = 0
    fail_count = 0
    percents = range(
        args.ota_kill_start_percent,
        args.ota_kill_end_percent + 1,
        args.ota_kill_step_percent,
    )
    state["stress_name"] = "stress_1_ota_kill_app"

    logger.info(
        "===== Stress 1 baseline: "
        "run one full OTA to measure upgrade time ====="
    )
    state["current_item"] = "baseline ota"
    state["step"] = "measure ota baseline"
    state["last_result"] = "IN_PROGRESS"
    bletools.prepare_ota_upgrade()
    baseline_seconds = bletools.run_ota_upgrade_to_success(
        timeout_seconds=args.ota_monitor_timeout
    )
    logger.info("Stress 1 baseline OTA elapsed time: %.3fs", baseline_seconds)

    for index, percent in enumerate(percents, start=1):
        logger.info(
            "===== Stress 1 point %s: "
            "kill app at %s%% of baseline OTA time =====",
            index,
            percent,
        )
        state["current_item"] = f"target {percent}%"
        state["step"] = "prepare ota"
        state["last_result"] = "IN_PROGRESS"
        row: dict[str, object] = {
            "time": timestamp(),
            "stress": "ota_kill_app",
            "iteration": percent,
            "ota_target_percent": percent,
            "ota_killed_progress": "",
            "ota_baseline_seconds": f"{baseline_seconds:.3f}",
            "ota_killed_elapsed_seconds": "",
            "status": "FAIL",
            "hub_rcu_bsd_length": "",
            "hub_rcu_rcw_length": "",
            "hub_rcu_bsd_degree": "",
            "error": "",
        }

        try:
            bletools.prepare_ota_upgrade()
            state["step"] = f"wait ota elapsed {percent}%"
            killed_elapsed_seconds = bletools.kill_app_at_ota_elapsed_percent(
                percent,
                baseline_seconds,
            )
            row["ota_killed_elapsed_seconds"] = f"{killed_elapsed_seconds:.3f}"
            logger.info(
                "Killed app after %.3fs for target %s%% of baseline %.3fs",
                killed_elapsed_seconds,
                percent,
                baseline_seconds,
            )

            state["step"] = "restart ota"
            bletools.prepare_ota_upgrade()
            bletools.run_ota_upgrade_to_success(
                timeout_seconds=args.ota_monitor_timeout
            )

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
            logger.info(
                "Stress 1 PASS target=%s killed_elapsed=%.3fs values=%s",
                percent,
                killed_elapsed_seconds,
                values,
            )
            log_app(logger, app_log)
        except RuntimeError as exc:
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
    """Parse CLI arguments for stress test execution.

    Author: Kawhi.He

    Args:
        None.

    Returns:
        Parsed argparse namespace.
    """
    parser = argparse.ArgumentParser(
        description="Radar RCU stress test runner."
    )
    parser.add_argument(
        "--stress-mode",
        type=int,
        choices=(0, 1, 2),
        default=DEFAULT_STRESS_MODE,
        help=(
            "0=run stress 1 and 2, 1=OTA kill app stress, "
            f"2=power cycle read stress. Default: {DEFAULT_STRESS_MODE}"
        ),
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=DEFAULT_ITERATIONS,
        help=f"Loop count. Default: {DEFAULT_ITERATIONS}",
    )
    parser.add_argument(
        "--device-id",
        default=DEFAULT_DEVICE_ID,
        help=f"ADB device id. Default: {DEFAULT_DEVICE_ID}",
    )
    parser.add_argument(
        "--power-port",
        default=DEFAULT_POWER_PORT,
        help=(
            "Programmable power supply serial port. "
            f"Default: {DEFAULT_POWER_PORT}"
        ),
    )
    parser.add_argument(
        "--baudrate",
        type=int,
        default=DEFAULT_BAUDRATE,
        help=f"Power supply baudrate. Default: {DEFAULT_BAUDRATE}",
    )
    parser.add_argument(
        "--voltage",
        type=float,
        default=DEFAULT_VOLTAGE,
        help=f"Power-on voltage. Default: {DEFAULT_VOLTAGE}",
    )
    parser.add_argument(
        "--current",
        type=float,
        default=DEFAULT_CURRENT,
        help=f"Current limit. Default: {DEFAULT_CURRENT}",
    )
    parser.add_argument(
        "--power-off-wait",
        type=float,
        default=DEFAULT_POWER_OFF_WAIT_SECONDS,
        help=(
            "Wait seconds after power off. "
            f"Default: {DEFAULT_POWER_OFF_WAIT_SECONDS}"
        ),
    )
    parser.add_argument(
        "--power-on-wait",
        type=float,
        default=DEFAULT_POWER_ON_WAIT_SECONDS,
        help=(
            "Wait seconds after power on. "
            f"Default: {DEFAULT_POWER_ON_WAIT_SECONDS}"
        ),
    )
    parser.add_argument(
        "--power-off-max-voltage",
        type=float,
        default=DEFAULT_POWER_OFF_MAX_VOLTAGE,
        help=(
            "Max voltage allowed after power off. "
            f"Default: {DEFAULT_POWER_OFF_MAX_VOLTAGE}"
        ),
    )
    parser.add_argument(
        "--power-on-tolerance",
        type=float,
        default=DEFAULT_POWER_ON_TOLERANCE,
        help=(
            "Allowed voltage tolerance after power on. "
            f"Default: +/-{DEFAULT_POWER_ON_TOLERANCE}"
        ),
    )
    parser.add_argument(
        "--ota-kill-start-percent",
        type=int,
        default=DEFAULT_OTA_KILL_START_PERCENT,
        help=(
            "OTA kill start percent. "
            f"Default: {DEFAULT_OTA_KILL_START_PERCENT}"
        ),
    )
    parser.add_argument(
        "--ota-kill-end-percent",
        type=int,
        default=DEFAULT_OTA_KILL_END_PERCENT,
        help=(
            "OTA kill end percent. "
            f"Default: {DEFAULT_OTA_KILL_END_PERCENT}"
        ),
    )
    parser.add_argument(
        "--ota-kill-step-percent",
        type=int,
        default=DEFAULT_OTA_KILL_STEP_PERCENT,
        help=(
            "OTA kill step percent. "
            f"Default: {DEFAULT_OTA_KILL_STEP_PERCENT}"
        ),
    )
    parser.add_argument(
        "--ota-monitor-timeout",
        type=int,
        default=DEFAULT_OTA_MONITOR_TIMEOUT_SECONDS,
        help=(
            "OTA monitor timeout seconds. "
            f"Default: {DEFAULT_OTA_MONITOR_TIMEOUT_SECONDS}"
        ),
    )
    parser.add_argument(
        "--log-dir",
        default=DEFAULT_LOG_DIR,
        help=f"Output log directory. Default: {DEFAULT_LOG_DIR}",
    )
    parser.add_argument(
        "--stop-on-fail",
        action="store_true",
        help="Stop immediately after a failed iteration.",
    )
    return parser.parse_args()


def main() -> int:
    """Program entry: initialize resources and run selected stress flows.

    Author: Kawhi.He

    Args:
        None.

    Returns:
        Process exit code: 0 success, 1 failures, 130 interrupted.
    """
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    args = parse_args()

    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    run_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    text_log = log_dir / f"radar_power_cycle_{run_id}.log"
    csv_log = log_dir / f"radar_power_cycle_{run_id}.csv"
    logger = setup_logger(text_log)

    bletools = NiuBleTools(device_id=args.device_id, logger=logger)
    power = ItechIt6121B(
        port=args.power_port,
        baudrate=args.baudrate,
        logger=logger,
    )
    state: dict[str, Any] = {
        "stress_name": "idle",
        "current_item": "-",
        "step": "-",
        "pass_count": 0,
        "fail_count": 0,
        "last_result": "N/A",
    }

    logger.info(
        "Start test: iterations=%s, voltage=%.3fV, current=%.3fA",
        args.iterations,
        args.voltage,
        args.current,
    )
    logger.info(
        "Stress mode=%s (0=stress1+stress2, "
        "1=OTA kill app, 2=power-cycle read)",
        args.stress_mode,
    )
    logger.info("Target commands: %s", ", ".join(TARGET_COMMANDS.values()))
    awake_guard = WindowsAwakeGuard(logger)
    awake_guard.enable()

    try:
        bletools.check_device()
        power.connect()

        total_pass = 0
        total_fail = 0

        if args.stress_mode in (0, 1):
            pass_count, fail_count = run_ota_kill_app_stress(
                args,
                logger,
                csv_log,
                bletools,
                power,
                state,
            )
            total_pass += pass_count
            total_fail += fail_count

        if args.stress_mode in (0, 2):
            pass_count, fail_count = run_power_cycle_read_stress(
                args,
                logger,
                csv_log,
                bletools,
                power,
                state,
            )
            total_pass += pass_count
            total_fail += fail_count

        logger.info(
            "Finished all selected stress tests: pass=%s, fail=%s",
            total_pass,
            total_fail,
        )
        return 0 if total_fail == 0 else 1
    except KeyboardInterrupt:
        log_interrupt_summary(logger, state)
        print(f"CTRL+C current status: {build_status_line(state)}", flush=True)
        return 130
    finally:
        awake_guard.disable()
        power.close()


if __name__ == "__main__":
    raise SystemExit(main())
