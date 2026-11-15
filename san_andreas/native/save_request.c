#include "save_shared.h"
#include <stdlib.h>

static DWORD target_pid;
static HWND target_window;

static BOOL CALLBACK find_window(HWND window, LPARAM unused) {
    DWORD pid = 0;
    GetWindowThreadProcessId(window, &pid);
    if (pid == target_pid && IsWindowVisible(window) && !GetWindow(window, GW_OWNER)) {
        target_window = window;
        return FALSE;
    }
    return TRUE;
}

int main(int argc, char **argv) {
    char library_path[MAX_PATH], name[80], *slash;
    HMODULE library;
    HOOKPROC callback;
    HHOOK hook;
    HANDLE mapping, game;
    SaveRequest *request;
    DWORD thread, started, status, duration, calls, reason;
    DWORD_PTR response;
    MSG message;
    if (sizeof(void *) != 4) return 2;
    GetModuleFileNameA(NULL, library_path, MAX_PATH);
    slash = strrchr(library_path, '\\');
    if (!slash) return 2;
    strcpy(slash + 1, "save_bridge.dll");
    library = LoadLibraryA(library_path);
    callback = library ? (HOOKPROC)GetProcAddress(library, "SaveHook") : NULL;
    if (!callback && library) callback = (HOOKPROC)GetProcAddress(library, "_SaveHook@12");
    if (!callback) { puts("{\"error\":\"Cannot load native save bridge\"}"); return 2; }
    if (argc == 2 && strcmp(argv[1], "--self-test") == 0) {
        puts("{\"ready\":true,\"architecture\":\"x86\"}");
        FreeLibrary(library);
        return 0;
    }
    if (argc != 3 || atoi(argv[2]) < 0 || atoi(argv[2]) > 7) return 2;
    target_pid = strtoul(argv[1], NULL, 10);
    EnumWindows(find_window, 0);
    if (!target_window) { puts("{\"status\":\"deferred\"}"); return 3; }
    thread = GetWindowThreadProcessId(target_window, NULL);
    game = OpenProcess(SYNCHRONIZE, FALSE, target_pid);
    if (!game) { puts("{\"error\":\"Cannot observe game process\"}"); return 2; }
    mapping_name(name, target_pid);
    mapping = CreateFileMappingA(INVALID_HANDLE_VALUE, NULL, PAGE_READWRITE, 0, sizeof(SaveRequest), name);
    if (!mapping || GetLastError() == ERROR_ALREADY_EXISTS) {
        puts("{\"error\":\"Another native save request is active\"}"); return 2;
    }
    request = (SaveRequest *)MapViewOfFile(mapping, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(SaveRequest));
    if (!request) return 2;
    memset(request, 0, sizeof(*request));
    request->magic = SAVE_MAGIC;
    request->nonce = GetTickCount() ^ GetCurrentProcessId();
    request->slot = atoi(argv[2]);
    hook = SetWindowsHookExA(WH_CALLWNDPROC, callback, library, thread);
    if (!hook) {
        printf("{\"error\":\"Cannot queue native save\",\"code\":%lu}\n", GetLastError());
        if (hook) UnhookWindowsHookEx(hook);
        return 2;
    }
    started = GetTickCount();
    SendMessageTimeoutA(target_window, SAVE_MESSAGE, request->nonce, SAVE_MAGIC,
                        SMTO_ABORTIFHUNG, 5000, &response);
    for (;;) {
        status = request->status;
        if (status >= 2 || WaitForSingleObject(game, 0) == WAIT_OBJECT_0) break;
        if (status == 0 && GetTickCount() - started > 5000
            && InterlockedCompareExchange(&request->status, 5, 0) == 0) break;
        while (PeekMessageA(&message, NULL, 0, 0, PM_REMOVE)) {
            TranslateMessage(&message);
            DispatchMessageA(&message);
        }
        Sleep(2);
    }
    status = request->status;
    duration = request->duration;
    calls = request->hook_calls;
    reason = request->reason;
    UnhookWindowsHookEx(hook);
    UnmapViewOfFile(request);
    CloseHandle(mapping);
    CloseHandle(game);
    FreeLibrary(library);
    if (status == 2) {
        printf("{\"status\":\"saved\",\"duration_ms\":%lu}\n", duration);
        return 0;
    }
    if (status == 3) { puts("{\"status\":\"deferred\",\"reason\":\"unsafe_gameplay\"}"); return 3; }
    if (status == 5) {
        printf("{\"error\":\"Native save callback was not dispatched\",\"hook_calls\":%lu}\n", calls);
        return 1;
    }
    if (reason == 1) { puts("{\"error\":\"Native save function signatures do not match\"}"); return 1; }
    puts("{\"error\":\"Native game save failed or executable unsupported\"}");
    return 1;
}
