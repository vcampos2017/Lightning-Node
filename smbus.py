# shim so libraries that do "import smbus" work on systems using smbus2
from smbus2 import SMBus

__all__ = ["SMBus"]
