# GTA III classic profile

The controller uses the shared session, navigation, missions, votes, input,
checkpoint selection, native autosave bridge, respawn, and transactional installer.
Only GTA III's layout, text format, menus, and camera provider live in this adapter.

The pinned compatibility executable is GTA III 1.1, SHA-256
`17e036ff18cdd805951fec9107746492f582ae1eb46ca028544e37e517e8fc1f`.
Do not mix 1.0 addresses or graphics plugins into this profile.

Sources:

- [Executable and published checksum](https://wiki.gtaconnected.com/Downgrading)
- [Plugin-SDK layouts and version-specific globals](https://github.com/DK22Pac/plugin-sdk/tree/master/plugin_III/game_III)
- [Native menu, save, HUD, path and bridge behavior](https://github.com/Hezkore/hez-gta-re3/tree/master/src)
- [First person provider source](https://github.com/GenisZ/FirstPerssonIII)
- [First person archive](https://www.gtainside.com/gta3/mods/modifications/135316-the-first-person-view-for-iii)
- [Completed-game seed save](https://www.gtainside.com/gta3/savegames/112087-gta-iii-100-savegame)
- [SilentPatch](https://github.com/CookiePLMonster/SilentPatch/releases/tag/1.1-BUILD34.1-SA)
- [Widescreen Fix](https://github.com/ThirteenAG/WidescreenFixesPack/releases/tag/gta3)

The installer manifests pin exact file hashes. The game data/main.scm hash is
also required before the launcher loads a checkpoint. The seed is copied only
to a newly created save slot with exclusive creation; damaged or occupied slots
are never overwritten. The installed seed remains unchanged by autosave.

Full map access comes from normal saved story progress, not patched mission
flags or deleted bridge geometry. GTA III's native pedestrian graph contains
separate island and neighborhood networks. The shared mission system filters
ballots by current reachability; GTA III uses a 100-metre meeting radius to suit
its smaller networks. Native links up to 125 metres horizontally and 40 metres
vertically preserve real bridges and Shoreside hills without changing other games.

The camera provider's active flag is module RVA 0x334445. Its native on-foot
activation cycle is camera selector 4 followed by 1. With this executable/loader,
its camera and animation-group references can remain null; the launcher binds
only null references at RVAs 0x355940/0x355944 to the provider's own 1.1 constructor
values before loading the world. Unknown nonzero references or another module
hash stop startup. The provider's binary is unmodified.

HUD hiding uses CHud::m_Wants_To_Draw_Hud at 0x95CF41, verified in the 1.1 draw
routine. This suppresses the health/armour, weapon, money, clock, wanted stars,
and radar without disabling frontend menus. The shared masked-write tracker
restores the prior flag when the controller closes.

Player-only peaceful mode sets CWanted's `m_bIgnoredByEveryone` bit (0x02 at
wanted + 0x16). Native kill-character AI idles when its target is the ignored
player, including existing gang attackers. NPC factions, weapons, threat masks,
and attacks against other NPCs stay intact. The masked write preserves other
wanted flags and restores the original bit on disable/exit only while the
player pool identity and wanted allocation still match. The existing shared
peaceful setting defaults on and reapplies after respawn.

Live validation: normal checkpoint load with both story unlocks set, first person,
free walking, native sidewalk steering, jump, native autosave, hospital respawn,
HUD restoration, and controller cleanup. Runtime reports stay under this game's
isolated runtime folder. SkyGfx 3 and Project2DFX are excluded after their 1.0-only
addresses were confirmed incompatible with this executable.

The shared 0.75 walking blend cap was verified as native move-state 2 (walking).
A GTA III turn multiplier of 3 calibrates FirstPersonIII's 0.1-degree native
mouse increments to about 24 degrees/second. It applies to left/right commands, sidewalk and mission steering,
and obstacle recovery through both desktop and private native input. The shared
turn smoothing and command durations remain in controller units.
A cold launch reloaded the native autosave at exactly the saved position. OBS's
window capture was verified while the game was foreground; an exclusive-fullscreen
game minimizes and gives a black capture when switched to another application.
