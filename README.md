# Wi-Fi CSI 3-RX Occupancy & Fall Detection System

## Folder Structure
- `web_dashboard/index.html` & `web_dashboard/vercel.json`: Deployable directly to Vercel (Free Hobby Plan). Connects to the Hub RX ESP32 via the browser Web Serial API (`navigator.serial`).
- `firmware/csi_tx/main/main.c`: Flash onto your 1 TX ESP32 board (100 Hz broadcast beacon on Channel 11).
- `firmware/csi_rx_satellite/main/main.c`: Flash onto Satellite RX #1 (`SAT_NODE_ID = 1`) and Satellite RX #2 (`SAT_NODE_ID = 2`).
- `firmware/csi_rx_hub/main/main.c`: Flash onto Hub RX #0 (connected via USB/UART to your laptop).
- `python_desktop/main_3rx.py`: Fixed and upgraded 3-RX Python desktop logger.
