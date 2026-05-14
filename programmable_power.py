# -*- coding: utf-8 -*-
"""Programmable power supply control helpers.

Author: Kawhi.He
"""

import datetime
import logging
import sys
import time

import serial


class ItechIt6121B:
    """SCPI wrapper for ITECH IT6121B programmable power supply.

    Author: Kawhi.He
    """

    def __init__(
        self,
        port: str,
        baudrate: int = 9600,
        timeout: float = 1.0,
        log_file: str = "power_supply_test.log",
        logger: logging.Logger | None = None,
    ):
        """Initialize the power supply controller.

        Author: Kawhi.He

        Args:
            port (str): Serial port name, for example "COM25".
            baudrate (int): Serial baudrate used by the power supply.
            timeout (float): Read timeout for serial communication in seconds.
            log_file (str): Fallback local log file path when no logger is provided.
            logger (logging.Logger | None): Optional Python logger instance.

        Returns:
            None.
        """
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.log_file = log_file
        self.ser: serial.Serial | None = None
        self.logger = logger

    @property
    def is_connected(self) -> bool:
        """Check whether the serial connection is currently open.

        Author: Kawhi.He

        Args:
            None.

        Returns:
            True if connected; otherwise False.
        """
        return self.ser is not None and self.ser.is_open

    def connect(self) -> None:
        """Open serial port and switch the power supply to remote mode.

        Author: Kawhi.He

        Args:
            None.

        Returns:
            None.
        """
        self.ser = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            bytesize=serial.EIGHTBITS,
            stopbits=serial.STOPBITS_ONE,
            parity=serial.PARITY_NONE,
            timeout=self.timeout,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        )
        self.log(f"[OK] Connected to power supply on {self.port}, baudrate={self.baudrate}")
        self.send_cmd("SYST:REM")
        device_id = self.query("*IDN?")
        self.log(f"[OK] Power supply ID: {device_id}")

    def send_cmd(self, cmd: str) -> None:
        """Send a command without expecting a response.

        Author: Kawhi.He

        Args:
            cmd (str): SCPI command string.

        Returns:
            None.
        """
        if not self.is_connected:
            raise RuntimeError("Power supply is not connected")
        assert self.ser is not None
        self.ser.write((cmd + "\r\n").encode("utf-8"))
        time.sleep(0.05)

    def query(self, cmd: str) -> str:
        """Send a command and return one-line response string.

        Author: Kawhi.He

        Args:
            cmd (str): SCPI query command string.

        Returns:
            Device response text with trailing spaces/newlines removed.
        """
        self.send_cmd(cmd)
        assert self.ser is not None
        response = self.ser.readline().decode("utf-8", errors="replace").strip()
        self.log(f"[QUERY] {cmd} -> {response!r}")
        return response

    def configure_output(self, voltage: float = 12.0, current: float = 3.0) -> None:
        """Configure output voltage and current limit.

        Author: Kawhi.He

        Args:
            voltage (float): Target output voltage in volts.
            current (float): Current limit in amperes.

        Returns:
            None.
        """
        if not 0 <= voltage <= 20.0:
            raise ValueError(f"Voltage out of range: {voltage}")
        if not 0 <= current <= 5.0:
            raise ValueError(f"Current out of range: {current}")
        self.send_cmd(f"VOLT {voltage:.3f}")
        self.send_cmd(f"CURR {current:.3f}")
        self.log(f"[POWER] Configured output: {voltage:.3f} V, {current:.3f} A")

    def output_on(self) -> None:
        """Turn on the power output channel.

        Author: Kawhi.He

        Args:
            None.

        Returns:
            None.
        """
        self.send_cmd("OUTP 1")
        self.log("[POWER] Output ON")

    def output_off(self) -> None:
        """Turn off the power output channel.

        Author: Kawhi.He

        Args:
            None.

        Returns:
            None.
        """
        self.send_cmd("OUTP 0")
        self.log("[POWER] Output OFF")

    def read_actual_voltage(self) -> float | None:
        """Read measured output voltage from the device.

        Author: Kawhi.He

        Args:
            None.

        Returns:
            Measured voltage in volts, or None when parsing/query fails.
        """
        for cmd in ("MEAS:VOLT?", "MEAS:VOLTAGE?", "VOLT?"):
            try:
                response = self.query(cmd)
                if response:
                    return float(response)
            except (TypeError, ValueError):
                continue
        return None

    def read_actual_current(self) -> float | None:
        """Read measured output current from the device.

        Author: Kawhi.He

        Args:
            None.

        Returns:
            Measured current in amperes, or None when parsing/query fails.
        """
        for cmd in ("MEAS:CURR?", "MEAS:CURRENT?", "CURR?"):
            try:
                response = self.query(cmd)
                if response:
                    return float(response)
            except (TypeError, ValueError):
                continue
        return None

    def close(self) -> None:
        """Close serial connection if open.

        Author: Kawhi.He

        Args:
            None.

        Returns:
            None.
        """
        if self.ser is not None and self.ser.is_open:
            self.ser.close()
            self.log("[OK] Serial port closed")

    def log(self, msg: str) -> None:
        """Write log message to configured logger or fallback file/stdout.

        Author: Kawhi.He

        Args:
            msg (str): Log message to output.

        Returns:
            None.
        """
        if self.logger is not None:
            self.logger.info(msg)
            return

        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] {msg}"
        print(line, flush=True)
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as exc:
            print(f"[ERROR] Failed to write power log: {exc}", file=sys.stderr, flush=True)
