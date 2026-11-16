#include "save_shared.h"

static int supported_game(void) {
    char path[MAX_PATH], *name;
    const unsigned char save_sig[] = {0x81,0xec,0x38,0x01,0x00,0x00,0xc6,0x05,0xfc,0x6e,0xc1,0x00};
    const unsigned char name_sig[] = {0x81,0xec,0x04,0x01,0x00,0x00,0x8b,0x84,0x24,0x08,0x01,0x00};
    const unsigned char vc_save_sig[] = {0x53,0x56,0x57,0x55,0x31,0xdb,0x81,0xec,0x58,0x01,0x00,0x00};
    const unsigned char vc_name_sig[] = {0x8b,0x44,0x24,0x04,0xc6,0x05,0x28,0x84,0x97,0x00,0x00,0x40};
    const unsigned char vc_open_sig[] = {0x8b,0x44,0x24,0x04,0x8b,0x4c,0x24,0x08,0x51,0x50};
    const unsigned char vc_close_sig[] = {0x8b,0x44,0x24,0x04,0x50,0xe8,0x96,0x46,0x1c,0x00,0x59,0xc3};
    if (GetModuleHandleA(NULL) != (HMODULE)0x400000) return 0;
    if (!GetModuleFileNameA(NULL, path, sizeof(path))) return 0;
    name = strrchr(path, '\\');
    name = name ? name + 1 : path;
    if (lstrcmpiA(name, "gta_sa.exe") == 0
        && memcmp((void *)0x5d13e0, save_sig, sizeof(save_sig)) == 0
        && memcmp((void *)0x5d0e90, name_sig, sizeof(name_sig)) == 0) return 1;
    if (lstrcmpiA(name, "gta-vc.exe") == 0
        && memcmp((void *)0x61c420, vc_save_sig, sizeof(vc_save_sig)) == 0
        && memcmp((void *)0x61a630, vc_name_sig, sizeof(vc_name_sig)) == 0
        && memcmp((void *)0x48df90, vc_open_sig, sizeof(vc_open_sig)) == 0
        && memcmp((void *)0x48dea0, vc_close_sig, sizeof(vc_close_sig)) == 0) return 2;
    return 0;
}

static int safe_to_save(int game) {
    DWORD focus, info, ped, mission;
    if (game == 2) {
        int pause_menu = *(BYTE *)0x869668 && *(DWORD *)0x869728 == 32;
        if (*(BYTE *)0x869669 || *(BYTE *)0x86966c
            || (*(BYTE *)0x869668 && !pause_menu)
            || ((*(BYTE *)0xA10B36 || *(BYTE *)0xA10B76 || *(WORD *)0x7DBDA0) && !pause_menu)
            || *(BYTE *)0xA10AB2 || *(BYTE *)0xA10B8D) return 0;
        ped = *(DWORD *)0x94AD28;
        if (ped <= 0x10000 || ped >= 0x80000000) return 0;
        if (*(BYTE *)0x94ADF4 || !(*(float *)(ped + 0x354) > 0)
            || *(BYTE *)(ped + 0x3AC) || *(DWORD *)0x978810
            || *(DWORD *)(ped + 0x244) > 1
            || !(*(BYTE *)(ped + 0x14C) & 1)
            || (*(BYTE *)(ped + 0x14D) & 8)) return 0;
        mission = *(DWORD *)0x978748;
        return mission < 34336 && !(mission & 3)
            && (!mission || (mission >= 8 && *(DWORD *)(0x821280 + mission) != 1));
    }
    int pause_menu = *(BYTE *)0xBA67A4 && *(BYTE *)0xBA68A5 == 41;
    if ((*(BYTE *)0xBA67A4 && !pause_menu)
        || ((*(WORD *)0xB7CB48 || *(WORD *)0xB73566) && !pause_menu)
        || *(BYTE *)0xB5F851 || *(DWORD *)0x96A7CC) return 0;
    focus = *(BYTE *)0xB7CD74;
    if (focus > 1) return 0;
    info = 0xB7CD98 + focus * 0x190;
    ped = *(DWORD *)info;
    if (ped <= 0x10000 || ped >= 0x80000000) return 0;
    if (*(BYTE *)(info + 0xDC) || *(float *)(ped + 0x540) <= 0
        || (*(BYTE *)(ped + 0x46D) & 1) || *(BYTE *)(ped + 0x2F)
        || *(DWORD *)(ped + 0x530) > 1) return 0;
    mission = *(DWORD *)0xA476AC;
    return mission < 200000 && (!mission || *(DWORD *)(0xA49960 + mission) != 1);
}

static int save_game(int game, int slot) {
    typedef void (__cdecl *NameSave)(int);
    typedef unsigned char (__cdecl *SaveGame)(int);
    if (game == 2) {
        typedef int (__cdecl *OpenFile)(const char *, const char *);
        typedef int (__cdecl *CloseFile)(int);
        int file;
        unsigned char saved;
        ((NameSave)0x61A630)(slot);
        *(DWORD *)0x9753C4 = 0;
        file = ((OpenFile)0x48DF90)((const char *)0x978428, "wb");
        if (!file) { *(DWORD *)0x9753C4 = 1; return 0; }
        /* Skip DoGameSpecificStuffBeforeSave: it advances the clock six hours.
           Serialize on the main thread, as with the San Andreas autosave. */
        saved = ((SaveGame)0x61C420)(file);
        if (saved && ((CloseFile)0x48DEA0)(file)) *(DWORD *)0x9753C4 = 3;
        return saved && *(DWORD *)0x9753C4 == 0;
    }
    ((NameSave)0x5D0E90)(slot);
    return ((SaveGame)0x5D13E0)(0);
}

__declspec(dllexport) LRESULT CALLBACK SaveHook(int code, WPARAM removed, LPARAM data) {
    CWPSTRUCT *message = (CWPSTRUCT *)data;
    if (code >= 0 && message->message == SAVE_MESSAGE
        && message->lParam == SAVE_MAGIC) {
        char name[80];
        HANDLE mapping;
        SaveRequest *request;
        mapping_name(name, GetCurrentProcessId());
        mapping = OpenFileMappingA(FILE_MAP_ALL_ACCESS, FALSE, name);
        request = mapping ? (SaveRequest *)MapViewOfFile(mapping, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(SaveRequest)) : NULL;
        if (request) InterlockedIncrement(&request->hook_calls);
        if (request && request->magic == SAVE_MAGIC && request->nonce == (LONG)message->wParam
            && request->slot >= 0 && request->slot < 8
            && InterlockedCompareExchange(&request->status, 1, 0) == 0) {
            int game = supported_game();
            if (!game) {
                request->reason = 1;
                InterlockedExchange(&request->status, 4);
            } else if (!safe_to_save(game)) {
                request->reason = 2;
                InterlockedExchange(&request->status, 3);
            } else {
                DWORD started = GetTickCount();
                unsigned char saved;
                saved = save_game(game, request->slot);
                request->duration = GetTickCount() - started;
                InterlockedExchange(&request->status, saved ? 2 : 4);
            }
        }
        if (request) UnmapViewOfFile(request);
        if (mapping) CloseHandle(mapping);
    }
    return CallNextHookEx(NULL, code, removed, data);
}
