from smbus2 import SMBus
import time

I2C_BUS = 1
AS3935_ADDR = 0x03

with SMBus(I2C_BUS) as bus:
    # Set Indoor mode (recommended for bench testing)
    # Reg 0x00, bits [5:1]
    reg0 = bus.read_byte_data(AS3935_ADDR, 0x00)
    reg0 = (reg0 & 0xC1) | (0x12 << 1)  # Indoor preset
    bus.write_byte_data(AS3935_ADDR, 0x00, reg0)

    # Enable disturber rejection
    reg3 = bus.read_byte_data(AS3935_ADDR, 0x03)
    reg3 |= (1 << 5)
    bus.write_byte_data(AS3935_ADDR, 0x03, reg3)

    print("AS3935 configured: INDOOR mode + disturber rejection")

print("Configuration complete")
