# -*- coding: utf-8 -*-
import json
import logging
import queue
import re
import subprocess
import threading
import time
import xml.etree.ElementTree as ET

APP_LOG_LEVEL = 25
logging.addLevelName(APP_LOG_LEVEL, "APP_LOG")

DEVICE_ID = "10AD8G18YC001N1"
APP_PACKAGE = "com.niu.bletools"
MAIN_ACTIVITY = "com.niu.productionhelper.MainActivity"

CONNECT_SUCCESS_TEXT = "蓝牙建立连接成功"
TARGET_MENU_TEXT = "雷达RCU蓝牙指令验证"
OTA_MENU_TEXT = "蓝牙OTA升级"
OTA_SUCCESS_TEXTS = ("升级成功", "OTA升级成功", "升级完成", "OTA完成")
TARGET_COMMANDS = {
    "hub_rcu_bsd_length": "BSD纵向长度",
    "hub_rcu_rcw_length": "后向碰撞预警",
    "hub_rcu_bsd_degree": "盲区角度",
}


class NiuBleTools:
    def __init__(self, device_id: str = DEVICE_ID, logger: logging.Logger | None = None):
        self.device_id = device_id
        self.logger = logger
        self.read_commands_selected = False
        self.last_app_log_lines: list[str] = []

    def force_stop_app(self) -> None:
        self.log("INFO", "Force-stop NIUBleTools app")
        self.run_adb("shell", "am", "force-stop", APP_PACKAGE)
        self.read_commands_selected = False
        self.last_app_log_lines = []
        time.sleep(0.5)

    def run_adb(self, *args: str, timeout: int = 30, retries: int = 3, retry_delay: float = 2.0) -> str:
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                result = subprocess.run(
                    ["adb", "-s", self.device_id, *args],
                    check=True,
                    capture_output=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                )
                return result.stdout.strip()
            except subprocess.CalledProcessError as exc:
                last_exc = exc
                if attempt < retries:
                    self.log("WARNING", f"ADB command failed (attempt {attempt}/{retries}): {exc.cmd} -> exit {exc.returncode}; retrying in {retry_delay}s")
                    time.sleep(retry_delay)
        raise last_exc  # type: ignore[misc]

    def run_adb_bytes(self, *args: str, timeout: int = 30) -> bytes:
        result = subprocess.run(
            ["adb", "-s", self.device_id, *args],
            check=True,
            capture_output=True,
            timeout=timeout,
        )
        return result.stdout

    def check_device(self) -> None:
        devices = subprocess.run(
            ["adb", "devices"],
            check=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        ).stdout
        if f"{self.device_id}\tdevice" not in devices:
            raise RuntimeError(f"ADB device is not connected or authorized: {self.device_id}")

    def dump_ui(self) -> ET.Element:
        self.run_adb("shell", "uiautomator", "dump", "/sdcard/window.xml", timeout=10)
        xml_bytes = self.run_adb_bytes("exec-out", "cat", "/sdcard/window.xml")
        return ET.fromstring(xml_bytes.decode("utf-8", errors="replace"))

    def open_app(self) -> None:
        self.log("INFO", "Open NIUBleTools app")
        self.run_adb("shell", "am", "start", "-W", "-n", f"{APP_PACKAGE}/{MAIN_ACTIVITY}")
        self.read_commands_selected = False
        time.sleep(0.8)

    def open_app_fresh(self) -> None:
        self.force_stop_app()
        self.open_app()

    def return_home_if_needed(self) -> None:
        for _ in range(4):
            root = self.dump_ui()
            if self.find_node_by_text(root, TARGET_MENU_TEXT) is not None and self.find_node_by_text(root, OTA_MENU_TEXT) is not None:
                return
            self.run_adb("shell", "input", "keyevent", "BACK")
            time.sleep(0.4)

    def enter_radar_rcu_page(self) -> None:
        self.return_home_if_needed()
        root = self.dump_ui()
        if self.find_node_by_text(root, TARGET_MENU_TEXT) is not None and self.find_node_by_id(root, self.id("connectBtn")) is not None:
            return

        target = self.find_node_and_parent_by_text(root, TARGET_MENU_TEXT)
        if target is None:
            raise RuntimeError(f"Home page entry not found: {TARGET_MENU_TEXT}")

        target_node, parent_node = target
        self.tap_node(parent_node if parent_node is not None else target_node, TARGET_MENU_TEXT)

        deadline = time.time() + 20
        while time.time() < deadline:
            root = self.dump_ui()
            if self.find_node_by_text(root, TARGET_MENU_TEXT) is not None and self.find_node_by_id(root, self.id("connectBtn")) is not None:
                return
            time.sleep(0.3)
        raise RuntimeError("Timed out waiting for Radar RCU page")

    def enter_ota_page(self) -> None:
        self.return_home_if_needed()
        root = self.dump_ui()
        if self.find_node_by_text(root, OTA_MENU_TEXT) is not None and self.find_node_by_id(root, self.id("otaStart")) is not None:
            return

        target = self.find_node_and_parent_by_text(root, OTA_MENU_TEXT)
        if target is None:
            raise RuntimeError(f"Home page entry not found: {OTA_MENU_TEXT}")

        target_node, parent_node = target
        self.tap_node(parent_node if parent_node is not None else target_node, OTA_MENU_TEXT)

        deadline = time.time() + 20
        while time.time() < deadline:
            root = self.dump_ui()
            if self.find_node_by_text(root, OTA_MENU_TEXT) is not None and self.find_node_by_id(root, self.id("otaStart")) is not None:
                return
            time.sleep(0.3)
        raise RuntimeError("Timed out waiting for OTA page")

    def connect_ble(self) -> None:
        self.log("INFO", "Connect BLE")
        self.tap_by_id(self.id("disConnectBtn"), "断开")
        time.sleep(0.3)
        self.tap_by_id(self.id("clearLogBtn"), "清除日志")
        self.last_app_log_lines = []
        time.sleep(0.3)
        self.tap_by_id(self.id("connectBtn"), "连接")
        self.wait_log_contains(CONNECT_SUCCESS_TEXT, 60)
        self.log("INFO", "BLE connected successfully")

    def read_radar_rcu_values(self) -> tuple[dict[str, str], str]:
        self.log("INFO", "Read Radar RCU command values")
        previous_log = self.read_log()
        self.tap_by_id(self.id("readCmdTestBtn"), "读指令")
        time.sleep(0.5)

        self.ensure_read_commands_selected()

        self.tap_by_id(self.id("okBtn"), "确定")
        self.read_commands_selected = False
        try:
            final_log = self.wait_read_result(previous_log, 30)
        except RuntimeError:
            self.log("WARNING", "Read command result timeout; retry once with fresh command selection")
            self.read_commands_selected = False
            self.tap_by_id(self.id("readCmdTestBtn"), "读指令")
            time.sleep(0.5)
            self.ensure_read_commands_selected()
            self.tap_by_id(self.id("okBtn"), "确定")
            final_log = self.wait_read_result(previous_log, 30)
        values = self.parse_received_values(final_log)
        missing = [key for key in TARGET_COMMANDS if key not in values]
        if missing:
            raise RuntimeError(f"Read result missing keys: {missing}\nLog:\n{final_log}")
        self.log("INFO", f"Radar RCU read values successfully: {values}")
        return values, final_log

    def ensure_read_commands_selected(self) -> None:
        if self.read_commands_selected:
            self.log("INFO", "Read command options already selected; skip toggling selections")
            return

        for command_key, label in TARGET_COMMANDS.items():
            self.select_command_by_key(command_key, label)
        self.read_commands_selected = True

    def select_ota_radar_device(self) -> None:
        self.tap_by_id(self.id("chooseDeviceBtn"), "选择设备")
        time.sleep(0.5)

        for _attempt in range(8):
            root = self.dump_ui()
            found = self.find_node_and_parent_by_text(root, "外设-雷达")
            if found is not None:
                target_node, parent_node = found
                self.tap_node(parent_node if parent_node is not None else target_node, "外设-雷达")
                time.sleep(0.5)
                return
            self.run_adb("shell", "input", "swipe", "540", "2100", "540", "650", "500")
            time.sleep(0.25)
        raise RuntimeError("OTA device option not found: 外设-雷达")

    def prepare_ota_upgrade(self) -> None:
        self.open_app_fresh()
        self.enter_ota_page()
        self.connect_ble()
        self.select_ota_radar_device()

    def start_ota_upgrade(self) -> None:
        self.run_adb("logcat", "-c")
        self.tap_by_id(self.id("otaStart"), "开始")
        time.sleep(0.3)

    def kill_app_at_ota_progress(self, target_percent: int, timeout_seconds: int = 180) -> float:
        self.log("INFO", f"Wait OTA progress >= {target_percent}% then kill app")
        progress = self.monitor_ota_progress(target_percent=target_percent, timeout_seconds=timeout_seconds, kill_on_target=True)
        return progress

    def kill_app_at_ota_elapsed_percent(self, target_percent: int, baseline_seconds: float) -> float:
        target_delay = baseline_seconds * target_percent / 100.0
        self.log("INFO", f"Start OTA and kill app after {target_delay:.3f}s ({target_percent}% of baseline {baseline_seconds:.3f}s)")
        start_time = time.monotonic()
        self.start_ota_upgrade()
        remaining_seconds = target_delay - (time.monotonic() - start_time)
        if remaining_seconds > 0:
            time.sleep(remaining_seconds)
        elapsed_seconds = time.monotonic() - start_time
        self.force_stop_app()
        return elapsed_seconds

    def run_ota_upgrade_to_success(self, timeout_seconds: int = 180, max_retries: int = 3) -> float:
        self.log("INFO", "Run OTA upgrade to success")
        start_time = time.monotonic()
        self.start_ota_upgrade()
        last_exc: Exception | None = None
        for attempt in range(1, max_retries + 1):
            try:
                self.monitor_ota_progress(target_percent=None, timeout_seconds=timeout_seconds, kill_on_target=False)
                elapsed_seconds = time.monotonic() - start_time
                self.log("INFO", f"OTA upgrade elapsed time: {elapsed_seconds:.3f}s")
                return elapsed_seconds
            except RuntimeError as exc:
                last_exc = exc
                if attempt < max_retries:
                    self.log("WARNING", f"OTA monitor failed (attempt {attempt}/{max_retries}): {exc}; retrying with fresh logcat")
                    self.run_adb("logcat", "-c")
                    time.sleep(1)
                else:
                    raise

    def monitor_ota_progress(self, target_percent: int | None, timeout_seconds: int, kill_on_target: bool) -> float:
        process = self._start_logcat_process()
        deadline = time.time() + timeout_seconds
        last_progress = 0.0
        last_bucket = -1
        line_queue: queue.Queue[str] = queue.Queue()
        last_output_time = time.time()

        def read_logcat_stdout() -> None:
            if process.stdout is None:
                return
            for line in process.stdout:
                line_queue.put(line)

        reader = threading.Thread(target=read_logcat_stdout, daemon=True)
        reader.start()
        try:
            while time.time() < deadline:
                try:
                    line = line_queue.get(timeout=0.2)
                except queue.Empty:
                    if process.poll() is not None:
                        self.log("WARNING", "logcat process exited; restarting logcat")
                        process = self._restart_logcat(process, line_queue)
                        reader = threading.Thread(target=read_logcat_stdout, daemon=True)
                        reader.start()
                        continue
                    continue
                last_output_time = time.time()
                line = line.strip()

                progress = self.parse_ota_packet_progress(line)
                if progress is not None:
                    last_progress = progress
                    bucket = int(progress)
                    if bucket != last_bucket:
                        self.log("INFO", f"OTA progress: {progress:.1f}%")
                        last_bucket = bucket
                    if target_percent is not None and progress >= target_percent:
                        if kill_on_target:
                            self.force_stop_app()
                        return progress

                if any(text in line for text in OTA_SUCCESS_TEXTS):
                    self.log("INFO", f"OTA success: {line}")
                    return 100.0

            raise RuntimeError(f"Timed out monitoring OTA progress, last_progress={last_progress:.1f}%")
        finally:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()

    def _start_logcat_process(self) -> subprocess.Popen:
        return subprocess.Popen(
            ["adb", "-s", self.device_id, "logcat", "-v", "time", "NiuBleOtaManager:E", "BleOtaActivity:I", "*:S"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            encoding="utf-8",
            errors="replace",
        )

    def _restart_logcat(self, old_process: subprocess.Popen, line_queue: queue.Queue) -> subprocess.Popen:
        old_process.terminate()
        try:
            old_process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            old_process.kill()
        while not line_queue.empty():
            try:
                line_queue.get_nowait()
            except queue.Empty:
                break
        time.sleep(0.5)
        return self._start_logcat_process()

    @staticmethod
    def parse_ota_packet_progress(line: str) -> float | None:
        match = re.search(r"\b(\d+)/(\d+)\s*-->", line)
        if not match:
            return None
        current = int(match.group(1))
        total = int(match.group(2))
        if total <= 0:
            return None
        return max(0.0, min(100.0, current * 100.0 / total))

    def read_log(self) -> str:
        root = self.dump_ui()
        node = self.find_node_by_id(root, self.id("resultTv"))
        log_text = "" if node is None else node.attrib.get("text", "")
        self.emit_new_app_log_lines(log_text)
        return log_text

    def wait_log_contains(self, text: str, timeout_seconds: int) -> str:
        deadline = time.time() + timeout_seconds
        last_log = ""
        while time.time() < deadline:
            last_log = self.read_log()
            if text in last_log:
                return last_log
            time.sleep(0.5)
        raise RuntimeError(f"Timed out waiting for log text: {text}\nLast log:\n{last_log[-1000:]}")

    def wait_read_result(self, previous_log: str, timeout_seconds: int) -> str:
        deadline = time.time() + timeout_seconds
        last_log = previous_log
        while time.time() < deadline:
            log_text = self.read_log()
            if log_text and log_text != previous_log and "收到:" in log_text:
                return log_text
            last_log = log_text or last_log
            time.sleep(0.5)
        raise RuntimeError(f"Timed out waiting for read result\nLast log:\n{last_log[-1000:]}")

    def select_command_by_key(self, command_key: str, label: str) -> None:
        for _attempt in range(12):
            root = self.dump_ui()
            found = self.find_node_and_parent_by_text(root, command_key)
            if found is not None:
                desc_node, parent_node = found
                self.tap_node(parent_node if parent_node is not None else desc_node, label)
                time.sleep(0.15)
                return

            self.run_adb("shell", "input", "swipe", "540", "2100", "540", "650", "500")
            time.sleep(0.25)
        raise RuntimeError(f"Read command option not found: {label} ({command_key})")

    @staticmethod
    def parse_received_values(log_text: str) -> dict[str, str]:
        matches = re.findall(r"收到:(\{.*?\})", log_text)
        if not matches:
            return {}
        try:
            return json.loads(matches[-1])
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Failed to parse received JSON: {matches[-1]}") from exc

    @staticmethod
    def id(short_id: str) -> str:
        return f"{APP_PACKAGE}:id/{short_id}"

    @staticmethod
    def iter_nodes(root: ET.Element):
        yield root
        for child in root:
            yield from NiuBleTools.iter_nodes(child)

    @staticmethod
    def iter_nodes_with_parent(root: ET.Element, parent: ET.Element | None = None):
        yield root, parent
        for child in root:
            yield from NiuBleTools.iter_nodes_with_parent(child, root)

    @staticmethod
    def parse_bounds(bounds: str) -> tuple[int, int, int, int]:
        nums = [int(value) for value in re.findall(r"\d+", bounds)]
        if len(nums) != 4:
            raise ValueError(f"Cannot parse bounds: {bounds}")
        return nums[0], nums[1], nums[2], nums[3]

    @classmethod
    def node_center(cls, node: ET.Element) -> tuple[int, int]:
        left, top, right, bottom = cls.parse_bounds(node.attrib["bounds"])
        return (left + right) // 2, (top + bottom) // 2

    @classmethod
    def find_node_by_id(cls, root: ET.Element, resource_id: str) -> ET.Element | None:
        for node in cls.iter_nodes(root):
            if node.attrib.get("resource-id") == resource_id:
                return node
        return None

    @classmethod
    def find_node_by_text(cls, root: ET.Element, text: str) -> ET.Element | None:
        for node in cls.iter_nodes(root):
            if text in node.attrib.get("text", ""):
                return node
        return None

    @classmethod
    def find_node_and_parent_by_text(cls, root: ET.Element, text: str) -> tuple[ET.Element, ET.Element | None] | None:
        for node, parent in cls.iter_nodes_with_parent(root):
            if text in node.attrib.get("text", ""):
                return node, parent
        return None

    def tap_node(self, node: ET.Element, label: str) -> None:
        x, y = self.node_center(node)
        self.log("DEBUG", f"Tap {label}: ({x}, {y})")
        self.run_adb("shell", "input", "tap", str(x), str(y))

    def tap_by_id(self, resource_id: str, label: str) -> None:
        root = self.dump_ui()
        node = self.find_node_by_id(root, resource_id)
        if node is None:
            raise RuntimeError(f"Control not found: {label} ({resource_id})")
        self.tap_node(node, label)

    def log(self, level: str, message: str) -> None:
        if self.logger is None:
            print(message, flush=True)
            return
        self.logger.log(getattr(logging, level), message)

    def emit_new_app_log_lines(self, log_text: str) -> None:
        if not log_text or self.logger is None:
            return

        lines = log_text.splitlines()
        previous_len = len(self.last_app_log_lines)
        if previous_len and lines[:previous_len] == self.last_app_log_lines:
            new_lines = lines[previous_len:]
        elif lines == self.last_app_log_lines:
            new_lines = []
        else:
            new_lines = lines

        for line in new_lines:
            if line.strip():
                self.logger.log(APP_LOG_LEVEL, line)

        self.last_app_log_lines = lines
