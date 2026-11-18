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
From a source checkout, use `.\build\setup\Setup.cmd` instead of `.\Setup.cmd`.

## Run

Launch the game and local chat controls:

```powershell
& ".\Start Grand Theft Walk.cmd" --mode dev --game <game>
```

For Twitch chat and OBS streaming:

```powershell
& ".\Start Grand Theft Walk.cmd" --mode prod --game <game>
```

## Installed mods

- **San Andreas:** SilentPatch, SkyGfx (PS2 preset and timecycle), Widescreen Fix,
  GInput, ZMenu (first-person camera), Project2DFX + Open Limit Adjuster,
  Ultimate ASI Loader, classic 1.0 compatibility executable.
- **Vice City:** SilentPatch + DDraw fix, SkyGfx (PS2 lighting/water), Widescreen Fix,
  Project2DFX + Open Limit Adjuster, D3D8-to-D3D9 wrapper, Ultimate ASI Loader,
  First Person View Mod (GenuisZ), classic 1.0 compatibility executable.
