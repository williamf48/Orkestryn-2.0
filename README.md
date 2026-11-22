# Orkestryn 2.0

Orkestryn runs a local operations dashboard, an isolated judge demo and four Safety Lab studies. It requires Python 3.10 or later; the dashboard, demo and local studies run with the Python standard library. No earlier installation or account is required.

[Download Orkestryn.pyz](https://github.com/williamf48/Orkestryn-2.0/releases/latest/download/Orkestryn.pyz)

| Open | Windows, from the download folder | From this source tree |
| --- | --- | --- |
| Main app | `py -3 Orkestryn.pyz --main --console` | `py -3 run.py --main --console` |
| Judge demo | `py -3 Orkestryn.pyz --demo --console` | `py -3 run.py --demo --console` |

On macOS or Linux, use `python3` in place of `py -3`. Keep the terminal open and follow the local address it prints. Create a new account on first opening the main workspace. The judge demo prints a private local welcome page with its own generated credentials.

Accounts, sessions, runs and the verified code cache stay in the version 2 data folder: `%LOCALAPPDATA%\Orkestryn-2.0` on Windows, `~/Library/Application Support/Orkestryn-2.0` on macOS, and `$XDG_DATA_HOME/Orkestryn-2.0` (default `~/.local/share/Orkestryn-2.0`) on Linux. The main and judge workspaces use separate subdirectories. Existing Orkestryn workspaces are not imported. To use a different location, pass `--directory` and a new empty folder.

To check a download, run `py -3 Orkestryn.pyz --check`. To replay the fourth study, run `py -3 Orkestryn.pyz --research-round-4 round-004.zip` from a writable folder. Both commands are local. Live NVIDIA Nemotron requests through Nebius are optional and reviewed in the app; a key is not required for startup or the scripted demo.
