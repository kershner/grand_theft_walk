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

Both games use the same setup, repair, check, restore, and launch flow. Setup accepts
the original classic Rockstar game EXE directly and installs the pinned 1.0
compatibility executable and mods, keeping original files for restoration. For
custom locations, pass the game folder or its EXE (rather than `Launcher.exe`):

```powershell
.\Setup.cmd -Action Install -Game san_andreas -SanAndreasPath "D:\Games\San Andreas\gta_sa.exe"
.\Setup.cmd -Action Install -Game vice_city -ViceCityPath "D:\Games\Vice City\gta-vc.exe"
.\Setup.cmd -Action Check -Game san_andreas
.\Setup.cmd -Action Restore -Game san_andreas
```

Run Grand Theft Walk through the suite launcher after setup. Rockstar Launcher
verification or updates can replace modded files; close it and use Repair afterward.

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
  GInput, ZMenu (first-person camera), Project2DFX + Open Limit Adjuster,
  Ultimate ASI Loader, classic 1.0 compatibility executable.
- **Vice City:** SilentPatch + DDraw fix, SkyGfx (PS2 lighting/water), Widescreen Fix,
  Project2DFX + Open Limit Adjuster, D3D8-to-D3D9 wrapper, Ultimate ASI Loader,
  First Person View Mod (GenuisZ), classic 1.0 compatibility executable.

Both graphics presets use Project2DFX's maximum supported distances: +3000 m
view distance, 5x draw distance, 4x detail distance, and 8x shadow distance.
