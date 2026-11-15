#include "save_shared.h"

static int supported_game(void) {
    const unsigned char save_sig[] = {0x81,0xec,0x38,0x01,0x00,0x00,0xc6,0x05,0xfc,0x6e,0xc1,0x00};
    const unsigned char name_sig[] = {0x81,0xec,0x04,0x01,0x00,0x00,0x8b,0x84,0x24,0x08,0x01,0x00};
    return GetModuleHandleA(NULL) == (HMODULE)0x400000
        && memcmp((void *)0x5d13e0, save_sig, sizeof(save_sig)) == 0
        && memcmp((void *)0x5d0e90, name_sig, sizeof(name_sig)) == 0;
}

static int safe_to_save(void) {
    DWORD focus, info, ped, mission;
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
            if (!supported_game()) {
                request->reason = 1;
                InterlockedExchange(&request->status, 4);
            } else if (!safe_to_save()) {
                request->reason = 2;
                InterlockedExchange(&request->status, 3);
            } else {
                DWORD started = GetTickCount();
                unsigned char saved;
                typedef void (__cdecl *NameSave)(int);
                typedef unsigned char (__cdecl *SaveGame)(int);
                ((NameSave)0x5D0E90)(request->slot);
                saved = ((SaveGame)0x5D13E0)(0);
                request->duration = GetTickCount() - started;
                InterlockedExchange(&request->status, saved ? 2 : 4);
            }
        }
        if (request) UnmapViewOfFile(request);
        if (mapping) CloseHandle(mapping);
    }
    return CallNextHookEx(NULL, code, removed, data);
}
