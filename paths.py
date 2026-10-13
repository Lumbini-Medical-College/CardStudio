"""
Where things live, both when run from source and inside the packaged CardStudio.exe.

RES   read-only app files (static/, templates/, fonts/, models/, bin/). In the
      packaged app these are unpacked by PyInstaller (sys._MEIPASS).
DATA  your data (cards.db, agent key, temporary files). Next to the program in
      a "data" folder, or in %LOCALAPPDATA%\\LMC Card Studio if that isn't
      writable (e.g. installed under Program Files). CARDSTUDIO_DATA overrides.
"""

import os
import sys
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)
SOURCE = Path(__file__).resolve().parent
RES = Path(getattr(sys, "_MEIPASS", SOURCE))


def _data_dir():
    if os.environ.get("CARDSTUDIO_DATA"):
        d = Path(os.environ["CARDSTUDIO_DATA"])
        d.mkdir(parents=True, exist_ok=True)
        return d
    base = Path(sys.executable).resolve().parent if FROZEN else SOURCE
    d = base / "data"
    try:
        d.mkdir(exist_ok=True)
        probe = d / ".write_test"
        probe.touch()
        probe.unlink()
        return d
    except OSError:
        d = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "LMC Card Studio" / "data"
        d.mkdir(parents=True, exist_ok=True)
        return d


DATA = _data_dir()
WORK = DATA / "work"
WORK.mkdir(exist_ok=True)
