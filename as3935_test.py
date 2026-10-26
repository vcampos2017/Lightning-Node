"""Quick AS3935 I2C check.

Reads registers 0x00-0x08 in one block read: the same way lightning_bluesky.py
talks to the sensor. (Single-register reads can fail with Errno 121 on the
AS3935 even when the sensor is fine.)
"""
import time

from smbus2 import SMBus

I2C_BUS = 1
AS3935_ADDR = 0x03  # detected via: i2cdetect -y -a 1
RETRIES = 5

with SMBus(I2C_BUS) as bus:
    regs = None
    for attempt in range(1, RETRIES + 1):
        try:
            regs = bus.read_i2c_block_data(AS3935_ADDR, 0x00, 9)
            break
        except OSError as e:
            print(f"Attempt {attempt}/{RETRIES} failed: {e}")
            time.sleep(0.1)

    if regs is None:
        raise SystemExit("AS3935 block read FAILED: check wiring / I2C speed (see INSTALL.md)")

    print(f"Register 0x00 (AFE_GAIN):  0x{regs[0]:02X}")
    print(f"Register 0x01 (THRESHOLD): 0x{regs[1]:02X}")
    print(f"Register 0x02 (LIGHTNING): 0x{regs[2]:02X}")
    print(f"Register 0x08 (TUN_CAP):   0x{regs[8]:02X}")

print("AS3935 basic I2C read OK")
