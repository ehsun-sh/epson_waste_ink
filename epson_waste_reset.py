#!/usr/bin/env python3
"""
Epson L1250 waste ink pad counter tool (over the network or USB).

The printer is driven with Epson's own "remote mode" (EPSON-CTRL) commands.
Over the network they travel wrapped inside SNMP GET OIDs; over USB they
travel on the IEEE 1284.4 (D4) "EPSON-CTRL" channel:

    st  -> printer status (errors, maintenance box state, serial number)
    di  -> device identification (model name)
    ||  -> EEPROM read / write (permanent counter reset)
    rw  -> temporary waste counter reset (until the printer is switched off)

Protocol details and the L3250-family EEPROM map come from the open-source
epson_print_conf project (https://github.com/Ircama/epson_print_conf, EUPL-1.2).
The L1250 is the print-only model of the L3250 family.

The network mode uses only the Python standard library. The USB mode needs
the "epson-usb" package (pip install epson-usb, MIT licence); on Windows it
talks to the USBPRINT interface of the installed Epson driver, so no driver
has to be replaced. Newer firmware locks EEPROM access over the network but
still allows it over USB.
"""

import argparse
import datetime
import hashlib
import json
import random
import re
import socket
import struct
import sys
import time

EPSON_CTRL_OID = "1.3.6.1.4.1.1248.1.2.2.44.1.1.2.1"
OID_DEVICE_DESCR = "1.3.6.1.2.1.25.3.2.1.3.1"

# EEPROM map of the L3250 family (L1250, L3250, L3251, ET-2810, ET-2814 ...).
PROFILE = {
    "read_key": (74, 54),
    "write_key": b"Maribaya",
    "levels": {
        "main pad": ((48, 49, 47), 63.46),
        "borderless pad": ((50, 51, 47), 34.16),
        "third pad": ((252, 253, 254), 13.0),
    },
    # address -> value written by a permanent reset
    "raw_waste_reset": {
        48: 0, 49: 0, 47: 0, 52: 0, 53: 0, 54: 94, 50: 0, 51: 0,
        55: 94, 28: 0, 252: 0, 253: 0, 254: 0, 255: 94,
    },
    # address -> AND mask applied after the counters are written
    # (clears the "waste pad overflow" latch bit of this family)
    "commit_masks": {0x100: 0xFE},
    "serial_number": range(1604, 1614),
}

SUPPORTED_MODELS = re.compile(r"\b(L12[5-9]\d|L32[5-6]\d|ET-28[1-2]\d)\b", re.I)

STATUS_NAMES = {0: "Error", 1: "Self printing", 2: "Busy", 3: "Waiting",
                4: "Idle", 5: "Paused"}
ERROR_NAMES = {
    0x10: "Waste ink pad counter overflow (ink pad full)",
    0x2C: "Ink cartridge overflow error",
    0x41: "Maintenance request",
    0x4A: "Maintenance box near end",
}
BOX_STATES = {0: "not full", 1: "near full", 2: "FULL"}


class PrinterError(Exception):
    pass


# --------------------------------------------------------------------------
# Minimal SNMPv1 client (GET only)
# --------------------------------------------------------------------------

def _ber_len(n):
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def _tlv(tag, payload):
    return bytes([tag]) + _ber_len(len(payload)) + payload


def _enc_int(v):
    return _tlv(0x02, v.to_bytes(max(1, (v.bit_length() + 8) // 8), "big", signed=True))


def _enc_oid(oid):
    parts = [int(x) for x in oid.split(".")]
    out = bytearray([parts[0] * 40 + parts[1]])
    for p in parts[2:]:
        chunk = [p & 0x7F]
        p >>= 7
        while p:
            chunk.append(0x80 | (p & 0x7F))
            p >>= 7
        out += bytes(reversed(chunk))
    return _tlv(0x06, bytes(out))


def _dec_oid(data):
    parts = [data[0] // 40, data[0] % 40]
    v = 0
    for b in data[1:]:
        v = (v << 7) | (b & 0x7F)
        if not b & 0x80:
            parts.append(v)
            v = 0
    return ".".join(str(p) for p in parts)


def _dec_tlv(data, pos):
    tag = data[pos]
    ln = data[pos + 1]
    pos += 2
    if ln & 0x80:
        n = ln & 0x7F
        ln = int.from_bytes(data[pos:pos + n], "big")
        pos += n
    return tag, data[pos:pos + ln], pos + ln


def _dec_seq(data):
    items, pos = [], 0
    while pos < len(data):
        tag, val, pos = _dec_tlv(data, pos)
        items.append((tag, val))
    return items


def build_get(oid, community=b"public", request_id=1):
    varbind = _tlv(0x30, _enc_oid(oid) + b"\x05\x00")
    pdu = _tlv(0xA0, _enc_int(request_id) + _enc_int(0) + _enc_int(0) + _tlv(0x30, varbind))
    return _tlv(0x30, _enc_int(0) + _tlv(0x04, community) + pdu)


def parse_response(packet):
    """Return (request_id, error_status, value) of an SNMP GetResponse."""
    _, msg, _ = _dec_tlv(packet, 0)
    _, _, pdu = _dec_seq(msg)
    if pdu[0] != 0xA2:
        raise ValueError("not a GetResponse")
    (_, rid), (_, err), _, (_, varbinds) = _dec_seq(pdu[1])
    (_, varbind), = _dec_seq(varbinds)
    _, (vtag, value) = _dec_seq(varbind)
    if vtag == 0x02:
        value = int.from_bytes(value, "big", signed=True)
    elif vtag != 0x04:
        value = None
    return int.from_bytes(rid, "big", signed=True), int.from_bytes(err, "big"), value


class Snmp:
    def __init__(self, host, port=161, community="public", timeout=3.0, retries=2):
        self.addr = (host, port)
        self.community = community.encode()
        self.timeout = timeout
        self.retries = retries

    def get(self, oid):
        """Return the value (bytes or int) of an OID, or None if the printer has none."""
        rid = random.randint(1, 0x7FFFFFFF)
        packet = build_get(oid, self.community, rid)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(self.timeout)
            for _ in range(self.retries + 1):
                sock.sendto(packet, self.addr)
                deadline = time.monotonic() + self.timeout
                try:
                    while time.monotonic() < deadline:
                        data, _ = sock.recvfrom(65535)
                        try:
                            resp_id, err, value = parse_response(data)
                        except (ValueError, IndexError):
                            continue
                        if resp_id == rid:
                            return None if err else value
                except (socket.timeout, ConnectionResetError):
                    # Windows reports an ICMP "port unreachable" as a reset
                    pass
        raise PrinterError(f"No SNMP answer from {self.addr[0]}:{self.addr[1]}")

    def request(self, frame):
        """Send an EPSON-CTRL frame wrapped in an OID; return the reply bytes (or None)."""
        value = self.get(EPSON_CTRL_OID + "." + ".".join(str(b) for b in frame))
        return value if isinstance(value, bytes) else None

    def device_descr(self):
        descr = self.get(OID_DEVICE_DESCR)
        return descr.decode("latin-1").strip() if isinstance(descr, bytes) else None

    def close(self):
        pass


# --------------------------------------------------------------------------
# USB (IEEE 1284.4 / D4), through the epson-usb package
# --------------------------------------------------------------------------

def _epson_usb():
    try:
        import epson_usb
    except ImportError:
        raise PrinterError("USB mode needs the 'epson-usb' package:  pip install epson-usb")
    return epson_usb


class UsbLink:
    def __init__(self, device=None, transport=None):
        self._lib = _epson_usb()
        try:
            self.dev = self._lib.EpsonUsbPrinter(device=device, transport=transport)
        except self._lib.EpsonUsbError as e:
            raise PrinterError(f"USB: {e}")

    def request(self, frame):
        try:
            return self.dev.request(frame)
        except self._lib.EpsonUsbError as e:
            raise PrinterError(f"USB: {e}")

    def close(self):
        self.dev.close()


def find_usb_printers():
    try:
        lib = _epson_usb()
    except PrinterError:
        return None
    return lib.find_devices()


def discover(port=161, community="public", wait=3.0):
    """Broadcast an SNMP GET and list every device that answers."""
    found = {}
    rid = random.randint(1, 0x7FFFFFFF)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.5)
        sock.sendto(build_get(OID_DEVICE_DESCR, community.encode(), rid),
                    ("255.255.255.255", port))
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            try:
                data, (ip, _) = sock.recvfrom(65535)
                resp_id, err, value = parse_response(data)
            except (socket.timeout, ConnectionResetError, ValueError, IndexError):
                continue
            if resp_id == rid and not err and isinstance(value, bytes):
                found[ip] = value.decode("latin-1").strip()
    return found


# --------------------------------------------------------------------------
# Epson printer
# --------------------------------------------------------------------------

def parse_st2(data):
    """Decode the binary '@BDC ST2' status block returned by the 'st' command."""
    start = data.find(b"@BDC ST2\r\n")
    if start < 0:
        return None
    pos = start + 10
    end = min(len(data), pos + 2 + int.from_bytes(data[pos:pos + 2], "little"))
    pos += 2
    info = {"boxes": []}
    while pos + 2 <= end:
        ftype, ln = data[pos], data[pos + 1]
        item = data[pos + 2:pos + 2 + ln]
        pos += 2 + ln
        if not item:
            continue
        if ftype == 0x01:
            info["status"] = item[0]
        elif ftype == 0x02:
            info["error"] = item[0]
        elif ftype == 0x37:
            step = item[0]
            if step in (1, 2):
                for i in range(1, len(item) - step + 1, step):
                    resets = item[i + 1] if step == 2 else None
                    info["boxes"].append((item[i], resets))
        elif ftype == 0x40:
            serial = "".join(chr(c) for c in item if 0x20 < c < 0x7F)
            if serial:
                info["serial"] = serial
    return info


class EpsonPrinter:
    def __init__(self, link, profile=PROFILE):
        """``link`` is an Snmp or a UsbLink: anything with request(frame)."""
        self.link = link
        self.p = profile

    def ctrl(self, cmd, payload):
        """Send an EPSON-CTRL command and return the raw reply bytes (or None)."""
        return self.link.request(cmd + struct.pack("<H", len(payload)) + payload)

    def model(self):
        reply = self.ctrl(b"di", b"\x01")
        m = reply and re.search(rb"MDL:([^;]+);", reply)
        if m:
            return m.group(1).decode("latin-1").strip()
        return self.link.device_descr() if hasattr(self.link, "device_descr") else None

    def status(self):
        reply = self.ctrl(b"st", b"\x01")
        return parse_st2(reply) if reply else None

    def read_byte(self, addr):
        """Read one EEPROM byte. Returns None when the printer refuses (locked EEPROM)."""
        k0, k1 = self.p["read_key"]
        reply = self.ctrl(b"||", bytes([k0, k1, 0x41, 0xBE, 0xA0, addr & 0xFF, addr >> 8]))
        if reply is None:
            raise PrinterError(f"No reply when reading EEPROM address {addr}")
        m = re.search(rb"EE:([0-9A-Fa-f]{4})([0-9A-Fa-f]{2})", reply)
        if not m:
            return None
        if int(m.group(1), 16) != addr:
            raise PrinterError(f"EEPROM address mismatch: asked {addr}, got 0x{m.group(1).decode()}")
        return int(m.group(2), 16)

    def write_byte(self, addr, value):
        """Write one EEPROM byte and confirm it by reading it back."""
        k0, k1 = self.p["read_key"]
        key = bytes(0 if b == 0 else b + 1 for b in self.p["write_key"])
        reply = self.ctrl(b"||", bytes([k0, k1, 0x42, 0xBD, 0x21, addr & 0xFF, addr >> 8, value]) + key)
        if not reply or b":OK;" not in reply:
            return False
        return self.read_byte(addr) == value

    def eeprom_serial(self):
        values = [self.read_byte(a) for a in self.p["serial_number"]]
        if None in values:
            return None
        return "".join(chr(v) for v in values)

    def waste_levels(self):
        levels = {}
        for name, (addrs, divider) in self.p["levels"].items():
            values = [self.read_byte(a) for a in addrs]
            if None in values:
                return None
            raw = values[0] | values[1] << 8 | values[2] << 16
            levels[name] = round(raw / divider, 2)
        return levels

    def temporary_reset(self, serial):
        payload = struct.pack("<H", 1) + hashlib.sha1(serial.encode()).digest()
        reply = self.ctrl(b"rw", payload)
        return bool(reply) and b"rw:" in reply and b":OK;" in reply


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def confirm(question, assume_yes):
    if assume_yes:
        return True
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def print_status(printer):
    model = printer.model()
    print(f"Model          : {model or 'unknown'}")
    st = printer.status()
    if not st:
        print("Status         : no status reply")
        return model, None
    print(f"Serial number  : {st.get('serial', 'unknown')}")
    if "status" in st:
        print(f"Printer state  : {STATUS_NAMES.get(st['status'], 'code %d' % st['status'])}")
    if "error" in st:
        print(f"Error          : {ERROR_NAMES.get(st['error'], 'code 0x%02X' % st['error'])}")
    for i, (state, resets) in enumerate(st["boxes"], 1):
        extra = f", temporary resets used: {resets}" if resets is not None else ""
        print(f"Ink pad {i}      : {BOX_STATES.get(state, 'unknown (%d)' % state)}{extra}")
    return model, st


def cmd_discover(args):
    found = discover(args.port, args.community)
    print("Network:")
    for ip, descr in found.items():
        print(f"  {ip:16} {descr}")
    if not found:
        print("  no SNMP device answered")
    usb = find_usb_printers()
    print("USB:")
    if usb is None:
        print("  not checked (pip install epson-usb)")
    for dev in usb or []:
        print(f"  {dev.vid_pid}  {dev.description or dev.path}")
    if usb == []:
        print("  no Epson USB printer found")
    return 0 if found or usb else 1


def cmd_status(printer, args):
    print_status(printer)
    levels = printer.waste_levels()
    if levels is None:
        print("\nEEPROM counters: access refused by the printer firmware (locked).")
        if isinstance(printer.link, UsbLink):
            print("The permanent 'reset' will not work; 'temp-reset' still does.")
        else:
            print("Try again over USB (target 'usb'); otherwise use 'temp-reset'.")
    else:
        print("\nWaste ink counters (100% = pad considered full):")
        for name, pct in levels.items():
            print(f"  {name:15}: {pct}%")
    return 0


def cmd_temp_reset(printer, args):
    _, st = print_status(printer)
    serial = st and st.get("serial")
    if not serial:
        print("\nCannot read the serial number from the printer status.")
        return 1
    if not any(state == 2 for state, _ in st["boxes"]):
        print("\nNote: the printer does not report a full ink pad; "
              "the temporary reset only has an effect when it does.")
    print("\nThe temporary reset lets the printer work again until it is switched off.")
    if not confirm("Send the temporary reset now?", args.yes):
        return 1
    if printer.temporary_reset(serial):
        print("Done. The printer accepted the temporary reset (do not turn it off).")
        return 0
    print("The printer refused the temporary reset.")
    return 1


def cmd_reset(printer, args):
    model, st = print_status(printer)
    if not model or not SUPPORTED_MODELS.search(model):
        print(f"\nAborted: this EEPROM map is for the L1250/L3250 family, not '{model}'.")
        return 1

    eeprom_serial = printer.eeprom_serial()
    if eeprom_serial is None:
        if isinstance(printer.link, UsbLink):
            print("\nAborted: the printer refuses EEPROM access. Use 'temp-reset' instead.")
        else:
            print("\nAborted: the printer refuses EEPROM access over the network.")
            print("Newer firmware locks it there; connect the USB cable and use target 'usb'.")
        return 2
    status_serial = st and st.get("serial")
    if status_serial and status_serial != eeprom_serial:
        print(f"\nAborted: EEPROM serial '{eeprom_serial}' does not match the status "
              f"serial '{status_serial}', so the EEPROM map does not fit this printer.")
        return 1

    addrs = list(printer.p["raw_waste_reset"]) + list(printer.p["commit_masks"])
    backup = {a: printer.read_byte(a) for a in addrs}
    if None in backup.values():
        print("\nAborted: some EEPROM addresses could not be read.")
        return 1
    name = f"backup_{eeprom_serial}_{datetime.datetime.now():%Y%m%d_%H%M%S}.json"
    with open(name, "w") as f:
        json.dump({"model": model, "serial": eeprom_serial,
                   "eeprom": {str(a): v for a, v in backup.items()}}, f, indent=2)
    print(f"\nBackup of the current EEPROM values saved to {name}")

    print("\nWaste ink counters before the reset:")
    for pad, pct in printer.waste_levels().items():
        print(f"  {pad:15}: {pct}%")
    print("\nWARNING: the counter only tracks how much ink is in the pad. Resetting it"
          "\ndoes not empty the pad; a full pad can leak ink inside the printer."
          "\nReplace/clean the pad or fit an external waste ink bottle.")
    if not confirm("\nWrite the reset to the EEPROM now?", args.yes):
        return 1

    for addr, value in printer.p["raw_waste_reset"].items():
        if not printer.write_byte(addr, value):
            print(f"Write failed at address {addr}. Restore with:  restore {name}")
            return 1
    for addr, mask in printer.p["commit_masks"].items():
        current = printer.read_byte(addr)
        if current is None:
            print(f"Cannot read address {addr}. Restore with:  restore {name}")
            return 1
        if current & mask != current and not printer.write_byte(addr, current & mask):
            print(f"Write failed at address {addr}. Restore with:  restore {name}")
            return 1

    print("\nReset done and verified. Switch the printer off and on, then run 'status'.")
    return 0


def cmd_restore(printer, args):
    with open(args.backup) as f:
        data = json.load(f)
    eeprom_serial = printer.eeprom_serial()
    if eeprom_serial is None:
        print("The printer refuses EEPROM access.")
        return 2
    if eeprom_serial != data["serial"]:
        print(f"Aborted: backup is for serial {data['serial']}, printer is {eeprom_serial}.")
        return 1
    values = {int(a): v for a, v in data["eeprom"].items()}
    if not confirm(f"Write {len(values)} bytes from {args.backup} back to the EEPROM?", args.yes):
        return 1
    for addr, value in values.items():
        if not printer.write_byte(addr, value):
            print(f"Write failed at address {addr}.")
            return 1
    print("Restore done and verified. Switch the printer off and on.")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Epson L1250 waste ink pad counter tool (network or USB).")
    parser.add_argument("--port", type=int, default=161, help="SNMP port (default 161)")
    parser.add_argument("--community", default="public", help="SNMP community (default public)")
    parser.add_argument("--timeout", type=float, default=3.0, help="seconds per SNMP request")
    parser.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    parser.add_argument("--usb-device", help="USB device path, when more than one is connected")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("discover", help="find printers on the network and on USB")
    target_help = "printer IP address, or 'usb' for the USB cable"
    for name, text in (("status", "show printer and ink pad status"),
                       ("temp-reset", "temporary reset, until the printer is switched off"),
                       ("reset", "permanent reset of the EEPROM counters (backup first)")):
        sub.add_parser(name, help=text).add_argument("target", help=target_help)
    restore = sub.add_parser("restore", help="write a backup file back to the EEPROM")
    restore.add_argument("target", help=target_help)
    restore.add_argument("backup", help="backup_*.json file made by 'reset'")
    args = parser.parse_args()

    link = None
    try:
        if args.command == "discover":
            return cmd_discover(args)
        if args.target.lower() == "usb":
            link = UsbLink(device=args.usb_device)
        else:
            link = Snmp(args.target, args.port, args.community, args.timeout)
        handler = {"status": cmd_status, "temp-reset": cmd_temp_reset,
                   "reset": cmd_reset, "restore": cmd_restore}[args.command]
        return handler(EpsonPrinter(link), args)
    except PrinterError as e:
        print(f"Error: {e}")
        return 1
    except KeyboardInterrupt:
        return 1
    finally:
        if link:
            link.close()


if __name__ == "__main__":
    sys.exit(main())
