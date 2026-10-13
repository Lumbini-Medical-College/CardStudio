# Keeps the SD160's USB-Ethernet link usable:
#  - DHCP: the SD160 asks for an address over USB and the Entrust driver expects to hand one out.
#  - Relay: macOS may route 169.254.x.x out of the main network instead of the USB link, so the
#    driver (in Docker) connects to 127.0.0.1:<port> here and we forward over the USB link only.
# Finds the printer's link by itself (the interface name changes on replug) and keeps running.
import re
import socket
import struct
import subprocess
import sys
import threading
import time

PRINTER_MAC = bytes.fromhex("00024e262c8c")
OFFER, MASK = "169.254.55.100", "255.255.0.0"
IP_BOUND_IF = 25
RELAY_PORTS = (9100, 80)
HEALTH_PORT = 9199      # answers "up" or "down": can the printer be reached over the USB link?
current_iface = None


def find_link():
    """Return (interface, mac_ip, index) of the SD160 USB-Ethernet link, or None.
    The index matters: after a replug the name can stay "en6" while the index changes."""
    tree = subprocess.run(["ioreg", "-l", "-w0", "-r", "-c", "IOUSBHostDevice", "-n", "SD160"],
                          capture_output=True, text=True).stdout
    m = re.search(r'"BSD Name" = "(en\d+)"', tree)
    if not m:
        return None
    iface = m.group(1)
    ip = re.search(r"inet (169\.254\.\d+\.\d+)", subprocess.run(["ifconfig", iface], capture_output=True, text=True).stdout)
    return (iface, ip.group(1), socket.if_nametoindex(iface)) if ip else None


def opt(c, v):
    return bytes([c, len(v)]) + v


def serve(iface, server_ip, index):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.setsockopt(socket.IPPROTO_IP, IP_BOUND_IF, index)
    s.bind(("0.0.0.0", 67))
    s.settimeout(1)
    print(time.strftime("%X"), f"serving {iface} #{index} ({server_ip})", flush=True)
    last_check = time.time()
    while True:
        if time.time() - last_check > 10:  # link gone or renamed -> rediscover
            last_check = time.time()
            if find_link() != (iface, server_ip, index):
                s.close()
                return
        try:
            d, _ = s.recvfrom(2048)
        except socket.timeout:
            continue
        if d[0] != 1 or d[28:34] != PRINTER_MAC:
            continue
        mtype, i = None, 240
        while i < len(d) and d[i] != 255:
            if d[i] == 0:
                i += 1
                continue
            if d[i] == 53:
                mtype = d[i + 2]
            i += 2 + d[i + 1]
        reply = {1: 2, 3: 5}.get(mtype)  # DISCOVER -> OFFER, REQUEST -> ACK
        if not reply:
            continue
        ip = socket.inet_aton
        pkt = (bytes([2, 1, 6, 0]) + d[4:8] + b"\0\0" + d[10:12] + b"\0" * 4 + ip(OFFER) + ip(server_ip)
               + b"\0" * 4 + d[28:44] + b"\0" * 192)
        pkt += (b"\x63\x82\x53\x63" + opt(53, bytes([reply])) + opt(54, ip(server_ip)) + opt(1, ip(MASK))
                + opt(51, struct.pack("!I", 86400)) + b"\xff")
        s.sendto(pkt, ("255.255.255.255", 68))
        print(time.strftime("%X"), "sent", "OFFER" if reply == 2 else "ACK", OFFER, "on", iface, flush=True)


def pump(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def relay(port):
    """127.0.0.1:port -> printer:port, sent out of the printer's USB link only."""
    lst = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    lst.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        lst.bind(("127.0.0.1", port))
    except OSError as e:  # macOS won't let a normal user bind ports < 1024 on 127.0.0.1
        print(time.strftime("%X"), f"relay {port} unavailable: {e}", flush=True)
        return
    lst.listen(16)
    while True:
        client, _ = lst.accept()
        try:
            up = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if current_iface:
                up.setsockopt(socket.IPPROTO_IP, IP_BOUND_IF, socket.if_nametoindex(current_iface))
            up.settimeout(5)
            up.connect((OFFER, port))
            up.settimeout(None)
        except OSError as e:
            print(time.strftime("%X"), f"relay {port}: printer not reachable ({e})", flush=True)
            client.close()
            continue
        print(time.strftime("%X"), f"relay {port}: connected via {current_iface}", flush=True)
        threading.Thread(target=pump, args=(client, up), daemon=True).start()
        threading.Thread(target=pump, args=(up, client), daemon=True).start()


def health():
    lst = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    lst.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    lst.bind(("127.0.0.1", HEALTH_PORT))
    lst.listen(8)
    while True:
        client, _ = lst.accept()
        ok = False
        if current_iface:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as up:
                    up.setsockopt(socket.IPPROTO_IP, IP_BOUND_IF, socket.if_nametoindex(current_iface))
                    up.settimeout(3)
                    up.connect((OFFER, 80))   # the printer's web port; leaves the print port alone
                    ok = True
            except OSError:
                pass
        try:
            client.sendall(b"up\n" if ok else b"down\n")
        finally:
            client.close()


if __name__ == "__main__":
    threading.Thread(target=health, daemon=True).start()
    for p in RELAY_PORTS:
        threading.Thread(target=relay, args=(p,), daemon=True).start()
    while True:
        link = find_link()
        current_iface = link[0] if link else None
        if link:
            try:
                serve(*link)
            except OSError as e:
                print(time.strftime("%X"), "link error:", e, flush=True)
        time.sleep(3)
