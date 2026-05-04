# Production services
Only `lightning-bluesky.service` should be 
enabled in production. The legacy 
`as3935.service` IRQ daemon should remain 
disabled because `lightning_bluesky.py` reads 
the AS3935 sensor directly. Running both 
services can cause two processes to read/clear 
the same AS3935 interrupt register. Current 
production target: lightning-bluesky.service 
enabled / active as3935.service disabled / 
inactive Command used on the node:
sudo systemctl disable --now as3935.service
