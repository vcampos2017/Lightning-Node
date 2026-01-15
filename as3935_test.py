from smbus2 import SMBus
import time

I2C_BUS = 1
AS3935_ADDR = 0x03  # detected via i2cdetect

with SMBus(I2C_BUS) as bus:
    # Read register 0x00 (AFE_GAIN, should return something non-zero)
    reg0 = bus.read_byte_data(AS3935_ADDR, 0x00)
    print(f"Register 0x00 (AFE_GAIN): 0x{reg0:02X}")

    # Read register 0x01 (THRESHOLD)
    reg1 = bus.read_byte_data(AS3935_ADDR, 0x01)
    print(f"Register 0x01 (THRESHOLD): 0x{reg1:02X}")

    # Read register 0x02 (LIGHTNING_REG)
    reg2 = bus.read_byte_data(AS3935_ADDR, 0x02)
    print(f"Register 0x02 (LIGHTNING): 0x{reg2:02X}")

print("AS3935 basic I2C read OK")
