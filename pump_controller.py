"""Pump controller — high-level API wrapping Modbus registers for syringe pump."""

from modbus_client import ModbusClient

# Register addresses
REG_ABS_POS_HIGH = 0x02    # 绝对位置 高16位
REG_ABS_POS_LOW = 0x03     # 绝对位置 低16位
REG_INCR_POS_HIGH = 0x05   # 增量位置 高16位
REG_INCR_POS_LOW = 0x06    # 增量位置 低16位
REG_CONTROL = 0x13          # 控制/状态寄存器
REG_SPEED_MIN = 0x14        # 速度 (steps/min)
REG_SPEED_SEC = 0x15        # 速度 (steps/s)
REG_ACCEL = 0x16            # 加减速等级 (1-100)
REG_ADDR = 0x0F             # 本机地址
REG_BAUD = 0x10             # 波特率
REG_SAVE = 0x20             # 参数保存
REG_CUR_POS_HIGH = 0x19    # 当前位置 高16位
REG_CUR_POS_LOW = 0x1A     # 当前位置 低16位
REG_LIMIT_SW1 = 0x35       # 限位开关1
REG_LIMIT_SW2 = 0x36       # 限位开关2

# Control commands
CMD_START = 4
CMD_STOP = 3
CMD_PAUSE = 2
CMD_RESUME = 1

# Status values
STATUS_IDLE = 0
STATUS_RUNNING = 1
STATUS_PAUSED = 2

# Unit conversion: 1 mm = 6400 steps, 1 µm = 6.4 steps
STEPS_PER_UM = 6.4


class PumpController:
    """Controls a single syringe pump via Modbus RTU."""

    def __init__(self, client: ModbusClient, addr: int):
        self.client = client
        self.addr = addr  # 1-15

    # --- Speed (µm/s, uses register 0x15 steps/s) ---

    def set_speed(self, speed_um_s: float) -> bool:
        """Set speed in µm/s. Internally converts to steps/s."""
        steps = max(1, min(65535, int(speed_um_s * STEPS_PER_UM)))
        return self.client.write_single_register(self.addr, REG_SPEED_SEC, steps)

    def get_speed(self) -> float | None:
        """Read speed in µm/s."""
        regs = self.client.read_holding_registers(self.addr, REG_SPEED_SEC)
        if not regs:
            return None
        return regs[0] / STEPS_PER_UM

    # --- Acceleration ---

    def set_accel(self, level: int) -> bool:
        """Set acceleration level (1-100)."""
        level = max(1, min(100, level))
        return self.client.write_single_register(self.addr, REG_ACCEL, level)

    def get_accel(self) -> int | None:
        """Read acceleration level."""
        regs = self.client.read_holding_registers(self.addr, REG_ACCEL)
        if not regs:
            return None
        return regs[0]

    # --- Incremental displacement (µm) ---

    def set_increment(self, distance_um: float) -> bool:
        """Set incremental movement distance in µm."""
        steps = int(distance_um * STEPS_PER_UM)
        return self.client.write_int32(self.addr, REG_INCR_POS_HIGH, REG_INCR_POS_LOW, steps)

    # --- Absolute position (µm) ---

    def set_absolute_position(self, pos_um: float) -> bool:
        """Set absolute target position in µm."""
        steps = int(pos_um * STEPS_PER_UM)
        return self.client.write_int32(self.addr, REG_ABS_POS_HIGH, REG_ABS_POS_LOW, steps)

    # --- Current position (µm) ---

    def get_current_position(self) -> float | None:
        """Read current position in µm."""
        steps = self.client.read_int32(self.addr, REG_CUR_POS_HIGH, REG_CUR_POS_LOW)
        if steps is None:
            return None
        return steps / STEPS_PER_UM

    def set_current_position_zero(self) -> bool:
        """Set current position as zero point."""
        return self.client.write_int32(self.addr, REG_CUR_POS_HIGH, REG_CUR_POS_LOW, 0)

    # --- Control ---

    def start(self) -> bool:
        return self.client.write_single_register(self.addr, REG_CONTROL, CMD_START)

    def stop(self) -> bool:
        return self.client.write_single_register(self.addr, REG_CONTROL, CMD_STOP)

    def pause(self) -> bool:
        return self.client.write_single_register(self.addr, REG_CONTROL, CMD_PAUSE)

    def resume(self) -> bool:
        return self.client.write_single_register(self.addr, REG_CONTROL, CMD_RESUME)

    def get_status(self) -> int | None:
        """Read motor status: 0=idle, 1=running, 2=paused."""
        regs = self.client.read_holding_registers(self.addr, REG_CONTROL)
        if not regs:
            return None
        return regs[0]

    # --- Limit switches ---

    def get_limit_switch1(self) -> bool | None:
        regs = self.client.read_holding_registers(self.addr, REG_LIMIT_SW1)
        if not regs:
            return None
        return regs[0] == 1

    def get_limit_switch2(self) -> bool | None:
        regs = self.client.read_holding_registers(self.addr, REG_LIMIT_SW2)
        if not regs:
            return None
        return regs[0] == 1

    # --- Config save ---

    def save_params(self) -> bool:
        return self.client.write_single_register(self.addr, REG_SAVE, 1)

    def factory_reset(self) -> bool:
        return self.client.write_single_register(self.addr, REG_SAVE, 0xAA)
