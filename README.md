# Epson L1250 Waste Ink Pad Counter Reset

`epson_waste_reset.py` talks to the printer either **over the network (Wi-Fi or LAN)** or **over the USB cable**. It reads the waste ink pad counters and can reset them.

> ⚠️ **Important:** the counter only tracks how much ink has gone into the pad. Resetting it **does not empty the pad**. If the pad really is full, ink will leak inside the printer and onto the board or the desk. When you reset the counter, also replace or clean the pad, or fit an **external waste ink tank** (tube + bottle).

## Installation
- Python 3.8 or later.
- USB mode needs one extra package:
  ```bash
  pip install -r requirements.txt
  ```
  On Windows the Epson driver is **not replaced** (no Zadig needed). The program uses the USB interface that the installed Epson driver already provides, so **the Epson driver must be installed**.
- Network mode needs no packages. The printer and the computer must be on the same network, with SNMP enabled on the printer (it is enabled by default).

## USB or network?
On newer L1250 / L3250 firmware, Epson locks EEPROM access **over the network**, but the same commands still work **over USB**. For a permanent reset, **try USB first**.

## Commands
In every command you can write `usb` instead of the printer's IP address.

1. Find the printer (on the network and on USB):
```bash
python epson_waste_reset.py discover
```

2. Show the status. This only reads and changes nothing:
```bash
python epson_waste_reset.py status usb
```
```bash
python epson_waste_reset.py status 192.168.1.50
```
It shows the model, serial number, current error and ink pad state. If the EEPROM is not locked, it also shows the counter percentages.

3. **Permanent reset** (writes to the EEPROM):
```bash
python epson_waste_reset.py reset usb
```
Before anything is written, the program:
- checks the model and accepts only the L1250 / L3250 family;
- compares the serial number stored in the EEPROM with the one the printer reports, to make sure the address map fits this printer;
- saves a **backup file** of the current values, `backup_<serial>_<date>.json`;
- asks for your confirmation.

Every byte is read back after it is written, to confirm the write. After the reset, switch the printer off and on, then run `status` again.

4. **Temporary reset**, for when the EEPROM is locked even over USB:
```bash
python epson_waste_reset.py temp-reset usb
```
The printer works until it is switched off. Run the command again after each power cycle. This writes nothing to the EEPROM.

5. Restore the previous values from a backup file:
```bash
python epson_waste_reset.py restore usb backup_XXXXXXXXXX_20261030_120000.json
```

## USB notes
- During the reset the printer must be on, with no print job in the queue. If a print job is stuck, clear the Windows print queue.
- If more than one Epson printer is connected over USB, give the device path with `--usb-device`.

## Status
The program has been tested against simulated printers, not yet on a real L1250. A permanent USB reset has been reported working on the L3250 (same family, on Linux), and the Windows USB path of `epson-usb` has been tested on the L3251.

## Credits
The protocol (EPSON-CTRL commands over SNMP and over USB/D4) and the L3250-family EEPROM map come from the open-source
[epson_print_conf](https://github.com/Ircama/epson_print_conf) project (EUPL-1.2). USB mode uses the
[epson-usb](https://pypi.org/project/epson-usb/) package (MIT).
