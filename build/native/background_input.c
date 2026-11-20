/* Game-thread input for the checksum-pinned III 1.1 / VC 1.0 / SA 1.0 builds.
   CPad/CMouseControllerState layouts: DK22Pac/plugin-sdk (see SOURCES.md).
   Patch CALL sites, not function prologues, so existing input hooks still run. */
#include <windows.h>
#include <string.h>

/* Collision queries executed only on the game's existing input thread.
   ABI: plugin-sdk CWorld::ProcessLineOfSight and CColPoint. III uses 1.1.
   Requests are published last, completions last; at most 12 rays per update. */
#include <windows.h>
#include <math.h>
#include <stddef.h>

#define RAY_COUNT 64
typedef struct { float x, y, z; } Vector;
typedef struct {
    Vector from, to;
    DWORD flags; /* 1: vehicles, 2: check water at the ground hit */
    DWORD hit;
    Vector point, normal;
    DWORD wet;
} Ray;
typedef struct {
    DWORD protocol, game, ready;
    volatile LONG request, complete;
    DWORD count, heartbeat;
    Ray rays[RAY_COUNT];
} TerrainState;
typedef char RaySizeMustBe60[sizeof(Ray) == 60 ? 1 : -1];
typedef char HeaderMustBe28[offsetof(TerrainState, rays) == 28 ? 1 : -1];
__declspec(dllexport) TerrainState GTWTerrain = {1};

static DWORD line_address, water_address;
static LONG active_request;
static DWORD cursor, count;
static Ray batch[RAY_COUNT];

void terrain_init(DWORD game) {
    GTWTerrain.game = game;
    if (game == 1) { line_address = 0x56BA00; water_address = 0x6E8580; }
    if (game == 2) { line_address = 0x4D92D0; water_address = 0x5C2C80; }
    if (game == 3) line_address = 0x4AFA60; /* III 1.1, NOT 1.0's 0x4AF970 */
    GTWTerrain.ready = line_address != 0;
}

static int valid_vector(Vector v) {
    return isfinite(v.x) && isfinite(v.y) && isfinite(v.z)
        && fabs(v.x) < 20000 && fabs(v.y) < 20000 && fabs(v.z) < 20000;
}

static void trace(Ray *ray) {
    /* Both classic (0x28) and SA (0x2C) results fit; vectors at 0 and 0x10. */
    union { double align; BYTE bytes[48]; } collision;
    void *entity = NULL;
    unsigned char hit;
    int vehicles = !!(ray->flags & 1);
    ray->hit = 0; ray->wet = 0;
    if (!valid_vector(ray->from) || !valid_vector(ray->to)) return;
    if (fabs(ray->to.x - ray->from.x) > 16 || fabs(ray->to.y - ray->from.y) > 16
            || fabs(ray->to.z - ray->from.z) > 16) return;
    ZeroMemory(&collision, sizeof(collision));
    if (GTWTerrain.game == 3) {
        hit = ((unsigned char (__cdecl *)(Vector*,Vector*,void*,void**,int,int,int,int,int,int,int))line_address)
            (&ray->from,&ray->to,&collision,&entity,1,vehicles,0,1,1,0,0);
    } else {
        hit = ((unsigned char (__cdecl *)(Vector*,Vector*,void*,void**,int,int,int,int,int,int,int,int))line_address)
            (&ray->from,&ray->to,&collision,&entity,1,vehicles,0,1,1,0,0,0);
    }
    if (!hit) return;
    ray->point = *(Vector *)collision.bytes;
    ray->normal = *(Vector *)(collision.bytes + 16);
    if (!valid_vector(ray->point) || !valid_vector(ray->normal)) return;
    ray->hit = 1;
    if ((ray->flags & 2) && water_address) {
        float level = 0;
        unsigned char water;
        if (GTWTerrain.game == 1)
            water = ((unsigned char (__cdecl *)(float,float,float,float*,int,int))water_address)
                (ray->point.x,ray->point.y,ray->point.z,&level,0,0);
        else
            water = ((unsigned char (__cdecl *)(float,float,float,float*))water_address)
                (ray->point.x,ray->point.y,ray->point.z,&level);
        ray->wet = water && isfinite(level) && level > ray->point.z + 0.15f;
    }
}

void terrain_update(int playable) {
    DWORD budget = 12;
    LONG request = GTWTerrain.request;
    if (!GTWTerrain.ready || !playable || !request || request == GTWTerrain.complete
            || GetTickCount() - GTWTerrain.heartbeat > 1000) return;
    if (request != active_request) {
        count = GTWTerrain.count;
        if (!count || count > RAY_COUNT) return;
        CopyMemory(batch, GTWTerrain.rays, count * sizeof(Ray));
        if (request != GTWTerrain.request) return;
        active_request = request;
        cursor = 0;
    }
    while (budget-- && cursor < count) trace(&batch[cursor++]);
    if (cursor == count && GTWTerrain.request == active_request) {
        CopyMemory(GTWTerrain.rays, batch, count * sizeof(Ray));
        InterlockedExchange(&GTWTerrain.complete, active_request);
    }
}


typedef struct {
    DWORD protocol, status, game;
    volatile DWORD enabled, heartbeat, buttons, mouse_x, mouse_y;
    volatile DWORD frames, applied;
} InputState;
__declspec(dllexport) InputState GTWInput = {1};

static DWORD update_pads, pad, mouse, menu, menu_offset, pause_a, pause_b;
static DWORD disabled, player, state_size, mouse_size, mouse_offset;
static DWORD last_x, last_y;
static int owned;

static int playable(void) {
    DWORD ped = *(DWORD *)player;
    if (*(BYTE *)(menu + menu_offset) || *(BYTE *)pause_a || *(BYTE *)pause_b)
        return 0;
    if (GTWInput.game == 3 ? *(BYTE *)disabled : *(WORD *)disabled) return 0;
    return ped >= 0x10000 && ped < 0x80000000;
}

static void __cdecl update(void) {
    DWORD bits, x, y, foreground_pid = 0;
    LONG dx, dy;
    short *state;
    float *movement;
    int active, background;
    ((void (__cdecl *)(void))update_pads)();
    GTWInput.frames++;
    terrain_update(playable());
    x = GTWInput.mouse_x;
    y = GTWInput.mouse_y;
    dx = (LONG)(x - last_x);
    dy = (LONG)(y - last_y);
    last_x = x;
    last_y = y;
    active = GTWInput.enabled && GetTickCount() - GTWInput.heartbeat < 500;
    if (!active || !playable()) {
        /* Keep a timed-out lease neutral on every frame: the native updater can
           retain its temporary controller state while the window is inactive.
           Normal input returns when the lease is explicitly closed. Never
           clear native controls while a frontend menu is open. */
        if ((GTWInput.enabled || owned) && playable()) {
            memset((void *)pad, 0, state_size);
            memset((void *)mouse, 0, mouse_size);
        }
        owned = 0;
        return;
    }
    GetWindowThreadProcessId(GetForegroundWindow(), &foreground_pid);
    background = foreground_pid != GetCurrentProcessId();
    if (background) {
        memset((void *)pad, 0, state_size);
        memset((void *)mouse, 0, mouse_size);
    }
    state = (short *)pad;
    bits = GTWInput.buttons;
    if (bits & 1) state[1] = -128; /* forward */
    if (bits & 2) state[1] = 128;  /* back */
    if (bits & 4) state[14] = 128; /* jump */
    if (bits & 8) state[15] = 128; /* release a ledge */
    if ((bits & 16) && GTWInput.game == 1) state[21] = 128; /* SA walk */
    movement = (float *)(mouse + mouse_offset);
    /* A lost/discontinuous command cannot fling the camera on the next frame. */
    if (dx >= -2000 && dx <= 2000) movement[0] += (float)dx;
    if (dy >= -2000 && dy <= 2000) movement[1] += (float)dy;
    GTWInput.applied++;
    owned = 1;
}

static int install(void) {
    static const DWORD sa[] = {0x53BEE6,0x53E78B,0x57C607,0x57D7C5,0x731540,0x748B17};
    static const DWORD vc[] = {0x490476,0x4A4412,0x4A5C7E,0x4A669F,0x4AB0A0,0x54460C,
                              0x5FFFD9,0x60018F,0x61D9F4,0x61DBB6,0x61DD47};
    static const DWORD iii[] = {0x48AEFB,0x48C950,0x48DEEF,0x48E7D7,0x582DE9,0x582F7C,0x592EBC};
    const DWORD *calls;
    DWORD count, i, old_protection;
    char path[MAX_PATH], *name;
    if (sizeof(void *) != 4 || GetModuleHandleA(NULL) != (HMODULE)0x400000) return 0;
    if (!GetModuleFileNameA(NULL, path, sizeof(path))) return 0;
    name = strrchr(path, '\\');
    name = name ? name + 1 : path;
    if (!lstrcmpiA(name, "gta_sa.exe")) {
        GTWInput.game = 1; calls = sa; count = sizeof(sa)/sizeof(*sa);
        update_pads = 0x541DD0; pad = 0xB73458; mouse = 0xB73418;
        menu = 0xBA6748; menu_offset = 0x5C;
        pause_a = 0xB7CB48; pause_b = 0xB7CB49; disabled = pad + 0x10E;
        player = 0xB7CD98; state_size = 48; mouse_size = 20; mouse_offset = 12;
    } else if (!lstrcmpiA(name, "gta-vc.exe")) {
        GTWInput.game = 2; calls = vc; count = sizeof(vc)/sizeof(*vc);
        update_pads = 0x4AB6C0; pad = 0x7DBCB0; mouse = 0x94D788;
        menu = 0x869630; menu_offset = 0x38;
        pause_a = 0xA10B36; pause_b = 0xA10B76; disabled = pad + 0xF0;
        player = 0x94AD28; state_size = 42; mouse_size = 16; mouse_offset = 8;
    } else if (!lstrcmpiA(name, "gta3.exe")) {
        GTWInput.game = 3; calls = iii; count = sizeof(iii)/sizeof(*iii);
        update_pads = 0x4927E0; pad = 0x6F0360; mouse = 0x8809A0;
        menu = 0x8F5A8C; menu_offset = 0x111;
        pause_a = 0x95CF34; pause_b = 0x95CF69; disabled = pad + 0xDF;
        player = 0x9414A8; state_size = 42; mouse_size = 16; mouse_offset = 8;
    } else return 0;
    /* Validate every call before modifying any of them. Python also verifies
       the complete executable hash before allowing a managed launch. */
    for (i = 0; i < count; i++) {
        if (*(BYTE *)calls[i] != 0xE8 || calls[i] + 5 + *(DWORD *)(calls[i]+1) != update_pads)
            return 0;
    }
    terrain_init(GTWInput.game);
    for (i = 0; i < count; i++) {
        if (!VirtualProtect((void *)calls[i], 5, PAGE_EXECUTE_READWRITE, &old_protection))
            return 0;
        *(DWORD *)(calls[i]+1) = (DWORD)update - (calls[i] + 5);
        VirtualProtect((void *)calls[i], 5, old_protection, &old_protection);
        FlushInstructionCache(GetCurrentProcess(), (void *)calls[i], 5);
    }
    return 1;
}

BOOL WINAPI DllMain(HINSTANCE module, DWORD reason, LPVOID reserved) {
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(module);
        GTWInput.status = install() ? 1 : 2;
    }
    return TRUE;
}
