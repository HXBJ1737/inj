"""RS485 Modbus RTU client for syringe pump communication."""

import threading
from datetime import datetime
from pymodbus.client import ModbusSerialClient
from pymodbus.exceptions import ModbusException


class SerialDataLogger:
    """Wraps a serial port to intercept and log raw TX/RX bytes."""

    def __init__(self, serial_port, on_data=None):
        self._port = serial_port
        self.on_data = on_data  # callback(direction: str, data: bytes)

    def write(self, data):
        if self.on_data and data:
            self.on_data("TX", bytes(data))
        return self._port.write(data)

    def read(self, size=1):
        data = self._port.read(size)
        if self.on_data and data:
            self.on_data("RX", bytes(data))
        return data

    def close(self):
        return self._port.close()

    @property
    def is_open(self):
        return self._port.is_open

    def __getattr__(self, name):
        return getattr(self._port, name)


def _crc16(data: bytes) -> bytes:
    """Calculate Modbus CRC16 and return as 2 bytes (little-endian)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return bytes([crc & 0xFF, (crc >> 8) & 0xFF])


class ModbusClient:
    """Thread-safe Modbus RTU client wrapper for RS485 communication."""

    def __init__(self, port="COM3", baudrate=9600, parity="N", stopbits=1, timeout=1.0):
        self.port = port
        self.baudrate = baudrate
        self.parity = parity
        self.stopbits = stopbits
        self.timeout = timeout
        self._client = None
        self._lock = threading.Lock()
        self._on_data = None  # callback(direction: str, hex_str: str)

    def set_data_logger(self, callback):
        """Set callback for serial data logging: callback(direction, hex_str)."""
        self._on_data = callback

    def connect(self) -> bool:
        with self._lock:
            if self._client and self._client.connected:
                return True
            self._client = ModbusSerialClient(
                port=self.port,
                baudrate=self._baudrate_val(),
                parity=self.parity,
                stopbits=self.stopbits,
                timeout=self.timeout,
                bytesize=8,
            )
            ok = self._client.connect()
            if ok and self._on_data:
                ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                self._on_data("SYS", f"[{ts}] 已连接 {self.port} @ {self.baudrate}")
            return ok

    def disconnect(self):
        with self._lock:
            if self._client and self._client.connected:
                self._client.close()
            self._client = None

    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.connected

    def _baudrate_val(self) -> int:
        return self.baudrate

    def _log(self, direction: str, msg: str):
        if self._on_data:
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            self._on_data(direction, f"[{ts}] {msg}")

    def _log_frame(self, direction: str, addr: int, func: int, data: bytes):
        """Log a complete Modbus RTU frame (data + CRC16) in hex format."""
        crc = _crc16(data)
        full = data + crc
        hex_str = " ".join(f"{b:02X}" for b in full)
        self._log(direction, f"Addr={addr:02X} Func={func:02X} | {hex_str}")

    def read_holding_registers(self, addr: int, reg: int, count: int = 1) -> list | None:
        """Read holding registers. Returns list of register values or None on error."""
        with self._lock:
            if not self._client or not self._client.connected:
                return None
            tx = bytes([addr, 0x03, (reg >> 8) & 0xFF, reg & 0xFF, (count >> 8) & 0xFF, count & 0xFF])
            self._log_frame("TX", addr, 0x03, tx)
            try:
                result = self._client.read_holding_registers(address=reg, count=count, device_id=addr)
            except ModbusException as e:
                self._log("ERR", f"Addr={addr:02X} Read reg {reg:#04x}: {e}")
                return None
            if result.isError() or not result.registers:
                self._log("ERR", f"Addr={addr:02X} Read reg {reg:#04x} failed")
                return None
            rx_data = bytes([addr, 0x03, count * 2])
            for v in result.registers:
                rx_data += bytes([(v >> 8) & 0xFF, v & 0xFF])
            self._log_frame("RX", addr, 0x03, rx_data)
            return result.registers

    def write_single_register(self, addr: int, reg: int, value: int) -> bool:
        """Write a single register. Returns True on success."""
        with self._lock:
            if not self._client or not self._client.connected:
                return False
            tx = bytes([addr, 0x06, (reg >> 8) & 0xFF, reg & 0xFF, (value >> 8) & 0xFF, value & 0xFF])
            self._log_frame("TX", addr, 0x06, tx)
            try:
                result = self._client.write_register(address=reg, value=value, device_id=addr)
            except ModbusException as e:
                self._log("ERR", f"Addr={addr:02X} Write reg {reg:#04x}={value}: {e}")
                return False
            if result.isError():
                self._log("ERR", f"Addr={addr:02X} Write reg {reg:#04x}={value} failed")
                return False
            self._log_frame("RX", addr, 0x06, tx)
            return True

    def write_multiple_registers(self, addr: int, reg: int, values: list[int]) -> bool:
        """Write multiple registers (max 2 for this device). Returns True on success."""
        with self._lock:
            if not self._client or not self._client.connected:
                return False
            byte_count = len(values) * 2
            tx = bytes([addr, 0x10, (reg >> 8) & 0xFF, reg & 0xFF, 0, len(values), byte_count])
            for v in values:
                tx += bytes([(v >> 8) & 0xFF, v & 0xFF])
            self._log_frame("TX", addr, 0x10, tx)
            try:
                result = self._client.write_registers(address=reg, values=values, device_id=addr)
            except ModbusException as e:
                self._log("ERR", f"Addr={addr:02X} Write regs {reg:#04x}={values}: {e}")
                return False
            if result.isError():
                self._log("ERR", f"Addr={addr:02X} Write regs {reg:#04x}={values} failed")
                return False
            rx = bytes([addr, 0x10, (reg >> 8) & 0xFF, reg & 0xFF, 0, len(values)])
            self._log_frame("RX", addr, 0x10, rx)
            return True

    def read_int32(self, addr: int, reg_high: int, reg_low: int) -> int | None:
        """Read a 32-bit signed integer from two consecutive registers."""
        regs = self.read_holding_registers(addr, reg_high, count=2)
        if not regs or len(regs) < 2:
            return None
        raw = (regs[0] << 16) | regs[1]
        if raw >= 0x80000000:
            raw -= 0x100000000
        return raw

    def write_int32(self, addr: int, reg_high: int, reg_low: int, value: int) -> bool:
        """Write a 32-bit signed integer to two consecutive registers."""
        if value < 0:
            value += 0x100000000
        high = (value >> 16) & 0xFFFF
        low = value & 0xFFFF
        return self.write_multiple_registers(addr, reg_high, [high, low])
