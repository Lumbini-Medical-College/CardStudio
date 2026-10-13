# LMC Card Studio

Fill in an employee's details, add a photo and signature, and print an LMC ID card
on the Datacard / Entrust SD160, with a PDF417 barcode sized to scan. Cards are
saved in `data/cards.db` (SQLite), so you can search for one and print it again.

## Windows desktop app (recommended for one card-making station)

**Build once** on any Windows PC with Python 3.9+ (tick "Add python.exe to PATH"):
double-click **build_windows.bat**. It downloads the packages, the Poppins font and the
photo model (~170 MB), and creates `dist\CardStudio\`. Copy that whole folder to the
printer PC; the printer PC does **not** need Python.

**On the printer PC:**

1. Install Entrust's **XPS Card Printer Driver** (entrust.com, SD160 support page), plug the
   SD160 in directly with a USB cable and print the driver's own test card once.
2. In **Printing preferences**: ribbon **YMCKT**, and turn on K-panel (black panel) printing
   for black text / monochrome images if offered, so the barcode and QR print sharp.
3. Double-click **CardStudio.exe**. It opens in its own window and prints directly.
4. Choose the card printer from the printer list at the top (once; it is remembered).

Your data lives in `CardStudio\data\` (`cards.db` = saved cards; back it up). To bring
existing cards, put your `cards.db` there before starting. The chosen printer is kept in
`data\settings.json`.
Problems are logged to `data\cardstudio.log`.

**Printer:** pick the card printer from the list at the top. The Print buttons work whenever
a printer is chosen, even if Windows reports it offline (card printer drivers often do while
the printer sleeps), so check the printer is on and loaded. Click the status to see details;
the ↻ button checks again right away and reloads the printer list, e.g. after plugging it in.

**Light / dark:** the button next to it switches between the computer's setting, light and dark.

**Updates:** the app checks GitHub Releases every few hours. When a newer version is out it
shows a bar at the top; **Update and restart** downloads it, replaces the program files
(your `data` folder is kept) and reopens the app. To publish a new version, push a tag:
`git tag v1.2 && git push origin v1.2`; GitHub Actions builds it and attaches the zip to the
release. Automatic updates need the program folder to be writable (not under Program Files)
and the GitHub repository to be public.

## Windows (run from source)

1. Install **Python 3.9+** from python.org (tick "Add python.exe to PATH").
2. Install Entrust's **XPS Card Printer Driver** from entrust.com (SD160 support page) and
   plug the SD160 in **directly** with a USB cable. Print the driver's own test card once.
3. In **Printers & scanners → XPS Card Printer → Printing preferences**: ribbon **YMCKT**,
   and turn on K-panel (black panel) printing for black text / monochrome images if the
   driver offers it. This keeps the barcode and QR sharp.
4. Copy this `card_studio` folder to the PC (include `data/cards.db` to keep saved cards).
5. In the folder, open a terminal and run once:

       python run.py --setup

   This installs Pillow, pdf417gen, pywin32, zxing-cpp, rembg and the Poppins font.
   The first photo you add downloads the background-removal model (~170 MB).
6. Start it by double-clicking **start.bat**, or run `python run.py`. The page opens at
   http://localhost:8765.

Check what's installed and which printer is used: `python run.py --check`.
If the printer has a different name: `python run.py --printer "Exact Printer Name"`.

## macOS

The SD160 has no Mac driver. It prints through Entrust's Linux driver in the Docker
container `sd160-cups`; `printer_link.py` gives the printer its address over USB and
relays the driver's connection. Docker Desktop must be running. Start with
**start.command** or `python3 run.py`. If the printer shows offline, unplug and
replug its USB cable.

## Server mode (shared server + print station)

Staff use the app from any computer on the office network; the Windows PC next to the
printer runs a small print agent that collects the jobs. The agent only connects *out*
to the server, so nothing needs opening on the printer PC.

**On the server** (Windows or Linux, any machine with a fixed IP):

    python run.py --setup
    python run.py --add-user anita        # one login per staff member (asks for a password)
    python run.py --server                # or start_server.bat on Windows

The server prints an **agent key** when it starts (also in `data/agent_token.txt`).
Staff open `http://SERVER-IP:8765` and sign in. Allow port 8765 in the server's firewall.
For HTTPS add `--cert cert.pem --key key.pem`.

**On the Windows PC with the printer** (steps 1-3 of the Windows section first):

    python run.py --setup
    python agent.py --server http://SERVER-IP:8765 --key AGENT-KEY

After the first run the settings are saved in `agent.json`; afterwards just double-click
**start_agent.bat**. To start it automatically, put a shortcut to `start_agent.bat` in
the Startup folder (Win+R, `shell:startup`). The page shows "Print station offline"
whenever the agent isn't running.

Other commands: `python run.py --users` (list logins), `--del-user NAME`.

## Printing a card

1. Fill in the form (or search a saved card at the top), add the photo and the cardholder's
   signature (a PNG with a transparent background works best; a photo of a signature on white
   paper also works). For a passport instead of a citizenship certificate, switch the
   "Citizenship No" label on the back to "Passport No".
2. Load a blank card and press **Print front**.
3. Put the same card back in, printed side down, same end first, and press **Print back**.

Every print is saved automatically with its date.

## Files

| File | Purpose |
|---|---|
| `desktop.py` | Desktop app entry point (CardStudio.exe) |
| `build_windows.bat`, `CardStudio.spec` | Build the Windows desktop app with PyInstaller |
| `paths.py` | Where app files and your data are kept |
| `updater.py`, `version.py` | Checks GitHub Releases and installs new versions of the Windows app |
| `run.py` | Launcher: `--setup`, `--check`, `--server`, `--add-user`, `--port`, `--printer` |
| `agent.py` | Print agent for the printer PC (server mode) |
| `server.py` | Local web server and API |
| `card.py` | Card layout, barcode, photo cut-out |
| `db.py` | SQLite storage and print history |
| `printer.py` | Picks `printer_windows.py` or `printer_mac.py` |
| `templates/` | Blank front and back designs |
| `data/cards.db` | Saved cards, logins, print queue. Contains personal data: back it up and keep it private |
| `data/agent_token.txt` | Agent key. Keep it secret; delete it and restart the server to make a new one |
# CardStudio
