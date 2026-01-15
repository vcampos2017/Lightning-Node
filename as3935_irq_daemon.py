#!/usr/bin/env python3
import time
import logging
from logging.handlers import RotatingFileHandler
from smbus2 import SMBus
import RPi.GPIO as GPIO

# ===== User config =====
I2C_BUS = 1
AS3935_ADDR = 0x03          # keep this consistent with your setup
IRQ_PIN = 17                # GPIO17 (pin 11)
LOG_PATH = "/var/log/as3935.log"

# Noise / stability controls
COOLDOWN_SEC = 60           # ignore new IRQs for N seconds after an event
GPIO_BOUNCE_MS = 8          # debounce for IRQ pin
I2C_RETRIES = 5             # retry if I2C glitches
I2C_RETRY_DELAY = 0.05

# ===== Register helpers (AS3935) =====
REG_INT = 0x03              # bits[3:0] interrupt source; read to clear IRQ
REG_DISTANCE = 0x07         # estimated distance (km) in low 6 bits
REG_ENERGY_1 = 0x04
REG_ENERGY_2 = 0x05
REG_ENERGY_3 = 0x06

def i2c_read(bus, reg):
    for _ in range(I2C_RETRIES):
        try:
            return bus.read_byte_data(AS3935_ADDR, reg)
        except OSError:
            time.sleep(I2C_RETRY_DELAY)
    raise

def i2c_write(bus, reg, val):
    for _ in range(I2C_RETRIES):
        try:
            bus.write_byte_data(AS3935_ADDR, reg, val & 0xFF)
            return
        except OSError:
            time.sleep(I2C_RETRY_DELAY)
    raise

def read_energy(bus):
    e1 = i2c_read(bus, REG_ENERGY_1)
    e2 = i2c_read(bus, REG_ENERGY_2)
    e3 = i2c_read(bus, REG_ENERGY_3)
    # 20-bit energy value: E3[4:0] << 16 | E2 << 8 | E1
    energy = ((e3 & 0x1F) << 16) | (e2 << 8) | e1
    return energy

def read_distance_km(bus):
    d = i2c_read(bus, REG_DISTANCE) & 0x3F
    # 0x3F typically means "out of range"
    return d

def read_interrupt_source(bus):
    # reading REG_INT clears the IRQ
    val = i2c_read(bus, REG_INT) & 0x0F
    # typical meanings:
    # 0x01 = noise level too high (disturber)
    # 0x04 = disturber detected
    # 0x08 = lightning detected
    return val

def setup_logger():
    logger = logging.getLogger("as3935")
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=5)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handler.setFormatter(fmt)
    logger.addHandler(handler)
    return logger

def main():
    logger = setup_logger()
    logger.info("AS3935 daemon starting (IRQ GPIO%d, addr 0x%02X)", IRQ_PIN, AS3935_ADDR)

    GPIO.setmode(GPIO.BCM)
    GPIO.setup(IRQ_PIN, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)

    last_event = 0.0

    with SMBus(I2C_BUS) as bus:
        # prime read to confirm chip is reachable
        try:
            _ = i2c_read(bus, 0x00)
        except Exception as e:
            logger.exception("Initial I2C read failed: %s", e)
            raise

        def irq_handler(channel):
            nonlocal last_event
            now = time.time()
            if (now - last_event) < COOLDOWN_SEC:
                return

            try:
                src = read_interrupt_source(bus)
                if src == 0x08:
                    energy = read_energy(bus)
                    dist = read_distance_km(bus)
                    logger.info("LIGHTNING src=0x%X distance_km=%s energy=%s", src, dist, energy)
                    last_event = now
                elif src in (0x01, 0x04):
                    logger.info("DISTURBER/NOISE src=0x%X", src)
                    last_event = now
                else:
                    logger.info("IRQ src=0x%X (other)", src)
                    last_event = now
            except Exception as e:
                logger.exception("IRQ handler error: %s", e)
                last_event = now

        GPIO.add_event_detect(IRQ_PIN, GPIO.RISING, callback=irq_handler, bouncetime=GPIO_BOUNCE_MS)

        try:
            while True:
                time.sleep(1)
        finally:
            GPIO.cleanup()

if __name__ == "__main__":
    main()
