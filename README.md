# Grand Theft Walk

## Install / repair

Requires the activated **classic Rockstar Launcher editions**, Python 3.10+, and DirectX.
Extract the full suite ZIP. Close the games and Rockstar Launcher, then run these
commands in PowerShell from the extracted folder. Accept the administrator prompt.

```powershell
.\Setup.cmd -Action Install
.\Setup.cmd -Action Repair -Game <game>
```

Use `san_andreas` or `vice_city` for `<game>`. First setup needs internet for Python packages.

## Run

Launch the game:

```powershell
& ".\Launch Grand Theft Walk.cmd" launch <game>
```

Start chat controls in a second terminal:

```powershell
.\venv\Scripts\python.exe -B dev_server.py --game <game>
```

Open [localhost:5050](http://localhost:5050). **F12** exits the game; **Ctrl+C** stops chat controls.

## Installed mods

- **San Andreas:** SilentPatch, SkyGfx (PS2 preset and timecycle), Widescreen Fix,
  GInput, ZMenu (first-person camera), Ultimate ASI Loader, classic 1.0 compatibility executable.
- **Vice City:** SilentPatch + DDraw fix, SkyGfx (PS2 lighting/water), Widescreen Fix,
  Project2DFX + Open Limit Adjuster, D3D8-to-D3D9 wrapper, Ultimate ASI Loader,
  classic 1.0 compatibility executable.
