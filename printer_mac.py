"""
macOS: talks to the SD160 through the `sd160-cups` Docker container (CUPS + Entrust LXM driver).

The SD160 shows up on the Mac as a USB-Ethernet link. printer_link.py gives it
an address and relays the driver's connections over that link, because macOS
may send 169.254.x.x traffic out of the main network instead. The driver in
Docker reaches the relay at DOCKER_HOST_IP (host.docker.internal).
"""

import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONTAINER = "sd160-cups"
QUEUE = "SD160"
DOCKER_HOST_IP = "192.168.65.254"   # host.docker.internal inside the container
MASK_PATH = "/usr/share/lxm-card-printer/masks/MaskBlackFrontDTC.png"
STATUS_TOOL = "cd /opt/lxm-sdk/samples/status && timeout 60 ./status_sample.out --printer SD160 --log Console"
JOB_OPTIONS = ["-o", "PrinterType=DTC", "-o", "RibbonName=YMCKT", "-o", "PageSize=ISOID1",
               "-o", "Resolution=300x300dpi", "-o", "FrontKPrint=FrontKPrintApply", "-o", "DualSidePrinting=None"]


def run(cmd, timeout=90):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def dexec(script, timeout=90):
    return run(["docker", "exec", CONTAINER, "bash", "-c", script], timeout=timeout)


def printer_reachable():
    """Ask printer_link.py whether the printer answers over the USB link."""
    try:
        with socket.create_connection(("127.0.0.1", 9199), timeout=5) as s:
            s.settimeout(5)
            return s.recv(8).startswith(b"up")
    except OSError:
        return False


def ensure_relay():
    if "printer_link.py" not in run(["pgrep", "-fl", "printer_link.py"]).stdout:
        log = open(HERE / "work" / "printer_link.log", "a")
        subprocess.Popen([sys.executable, "-I", str(HERE / "printer_link.py")],
                         stdout=log, stderr=log, start_new_session=True)
        time.sleep(1)


def ensure_link():
    """Start the DHCP responder if the printer has no address yet."""
    if printer_reachable():
        return True
    if "printer_link.py" not in run(["pgrep", "-fl", "printer_link.py"]).stdout:
        log = open(HERE / "work" / "printer_link.log", "a")
        subprocess.Popen([sys.executable, "-I", str(HERE / "printer_link.py")],
                         stdout=log, stderr=log, start_new_session=True)
    for _ in range(30):
        time.sleep(2)
        if printer_reachable():
            return True
    return False


def ensure_container():
    state = run(["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER]).stdout.strip()
    if state != "true":
        run(["docker", "start", CONTAINER])
        time.sleep(6)
    # The LXM print service is started by the container's start.sh; restart it if it died.
    dexec("pgrep -f backend/lxm-card-printer-service >/dev/null || systemctl start lxm-card-printer")
    # The queue must point at the relay on the Mac, not at the printer's link-local address.
    dexec(f"lpstat -v {QUEUE} | grep -q {DOCKER_HOST_IP} || "
          f"lpadmin -p {QUEUE} -v lxm-card-printer-backend://{DOCKER_HOST_IP}")


def list_printers():
    """The Mac always prints through the one Docker queue: nothing to choose."""
    return []


def status():
    """Return printer status from the driver: ready/busy, ribbon %, model, serial, message."""
    ensure_relay()
    reachable = printer_reachable()
    result = {"link": reachable, "can_print": reachable, "status": "Offline", "ribbon": None,
              "model": None, "serial": None, "queue": [], "message": ""}
    if not result["link"]:
        result["message"] = "Printer not responding. Make sure it is on, then unplug and replug its USB cable."
        return result
    try:
        out = dexec(STATUS_TOOL).stdout
    except subprocess.TimeoutExpired:
        result["message"] = "Status request timed out."
        return result

    def tag(name):
        m = re.search(rf"<{name}>([^<]*)</{name}>", out)
        return m.group(1) if m else None

    result.update(status=tag("PrinterStatus") or "Unknown", ribbon=tag("RibbonRemaining"),
                  model=tag("PrinterModel"), serial=tag("PrinterSerialNumber"))
    msg = re.findall(r"<(?:ErrorString|PrinterMessage|MessageText)>([^<]+)<", out)
    result["message"] = "; ".join(msg)
    result["queue"] = [l.split()[0] for l in dexec(f"lpstat -o {QUEUE}").stdout.splitlines() if l.strip()]
    return result


def print_card(job, title):
    """Send one side: install that side's K mask, then queue the PDF. Returns the CUPS job id."""
    import card
    pdf_bytes, mask_image = card.to_pdf(job["page"]), job["mask"]
    if not ensure_link():
        raise RuntimeError("The printer is not reachable over USB. Check the cable and power.")
    ensure_container()
    with tempfile.TemporaryDirectory() as tmp:
        pdf, mask = Path(tmp) / "card.pdf", Path(tmp) / "mask.png"
        pdf.write_bytes(pdf_bytes)
        mask_image.save(mask)
        for src, dst in ((pdf, "/tmp/card.pdf"), (mask, "/tmp/mask.png")):
            r = run(["docker", "cp", str(src), f"{CONTAINER}:{dst}"])
            if r.returncode:
                raise RuntimeError(r.stderr.strip())
    r = dexec(f"cp /tmp/mask.png {MASK_PATH} && chmod 644 {MASK_PATH} && "
              f"lp -d {QUEUE} -n 1 {' '.join(JOB_OPTIONS)} -t '{title}' /tmp/card.pdf")
    m = re.search(r"request id is (\S+)", r.stdout)
    if not m:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip() or "Print job was not accepted.")
    return m.group(1)
