#!/usr/bin/env bash
set -e
FILE="/home/pi/Lightning-Node/.venv/lib/python3.7/site-packages/RPi_AS3935/RPi_AS3935.py"
cp -n "$FILE" "$FILE.bak" || true
sed -i "s/read_i2c_block_data(self.address, 0x00)/read_i2c_block_data(self.address, 0x00, 9)/g" "$FILE"
grep -n "read_i2c_block_data" "$FILE"
echo "AS3935 library patched."
