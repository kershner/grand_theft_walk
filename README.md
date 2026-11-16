# Grand Theft Walk

Walking controllers and chat controls for GTA III, Vice City, and San Andreas.
Currently, San Andreas is playable; Vice City is in development and GTA III is planned.

Run commands in PowerShell from the project folder. Close Rockstar Games Launcher first.
Replace `<game>` with a supported game ID: `san_andreas` or `vice_city`.

Launch a game:

```powershell
& ".\Launch Grand Theft Walk.cmd" launch <game>
```

Start chat controls in a second terminal, then open http://localhost:5050:

```powershell
.\venv\Scripts\python.exe -B dev_server.py --game <game>
```

F12 ends the game session. Ctrl+C stops the dev server.

First-time setup: install and activate your original PC games, install Python,
and extract the suite ZIP into a permanent writable project folder. If `venv` is missing:

```powershell
py -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

Close both games and Rockstar Launcher before setup or repair. Accept the administrator prompt.

```powershell
.\Setup.cmd -Action Install
.\Setup.cmd -Action Check
.\Setup.cmd -Action Repair -Game <game>
```

Use Repair for missing or changed mod files. To undo installation, run
`.\Setup.cmd -Action Restore -Game <game>`.

Setup handles installed, supported games. For custom locations, add
`-SanAndreasPath "D:\Games\San Andreas"` or `-ViceCityPath "D:\Games\Vice City"` to Install.

Keep the ZIP and the game's `.gtw-suite` backup folder. Saves are preserved.
Setup logs: `runtime/setup-last.log`.
