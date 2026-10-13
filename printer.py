"""Picks the printer backend for this computer: Windows driver or the macOS Docker setup."""

import sys

if sys.platform == "win32":
    from printer_windows import list_printers, print_card, status  # noqa: F401
else:
    from printer_mac import list_printers, print_card, status  # noqa: F401
