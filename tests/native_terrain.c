/* Standalone x86 ABI/mailbox test. Uses fake engine functions, never a game.
   tcc -o runtime/native-terrain-tests.exe tests/native_terrain.c */
#include <assert.h>
#include <stdio.h>
#include "../build/native/background_input.c"

static int calls;
static unsigned char __cdecl fake_line(Vector *a, Vector *b, void *out, void **entity,
        int buildings, int vehicles, int peds, int objects, int dummies,
        int see_through, int camera_ignore, int shoot_through) {
    Vector point = {a->x, a->y, 0}, normal = {0, 0, 1};
    assert(buildings == 1 && peds == 0 && objects == 1 && dummies == 1);
    assert(see_through == 0 && camera_ignore == 0 && shoot_through == 0);
    *(Vector *)out = point;
    *(Vector *)((BYTE *)out + 16) = normal;
    *entity = NULL;
    calls++;
    return 1;
}

static unsigned char __cdecl fake_iii_line(Vector *a, Vector *b, void *out, void **entity,
        int buildings, int vehicles, int peds, int objects, int dummies,
        int see_through, int camera_ignore) {
    return fake_line(a,b,out,entity,buildings,vehicles,peds,objects,dummies,
                     see_through,camera_ignore,0);
}

static unsigned char __cdecl fake_water(float x, float y, float z, float *level, int a, int b) {
    assert(a == 0 && b == 0);
    *level = 1;
    return 1;
}

static unsigned char __cdecl fake_vc_water(float x, float y, float z, float *level) {
    *level = 1;
    return 1;
}

static void setup(DWORD game) {
    DWORD i;
    ZeroMemory(&GTWTerrain, sizeof(GTWTerrain));
    GTWTerrain.protocol = 1;
    terrain_init(game);
    line_address = (DWORD)(game == 3 ? fake_iii_line : fake_line);
    water_address = game == 1 ? (DWORD)fake_water : game == 2 ? (DWORD)fake_vc_water : 0;
    active_request = 0;
    calls = 0;
    GTWTerrain.count = 17;
    GTWTerrain.heartbeat = GetTickCount();
    for (i = 0; i < GTWTerrain.count; i++) {
        GTWTerrain.rays[i].from.z = 1;
        GTWTerrain.rays[i].to.z = -1;
        GTWTerrain.rays[i].flags = 2;
    }
    InterlockedExchange(&GTWTerrain.request, 1);
}

int main(void) {
    DWORD game;
    for (game = 1; game <= 3; game++) {
        setup(game);
        terrain_update(0);
        assert(calls == 0);
        terrain_update(1);
        assert(calls == 12 && GTWTerrain.complete == 0);
        assert(GTWTerrain.rays[0].hit == 0); /* No partial results published. */
        terrain_update(1);
        assert(calls == 17 && GTWTerrain.complete == 1);
        assert(GTWTerrain.rays[0].hit == 1);
        assert(GTWTerrain.rays[0].normal.z == 1);
        assert(GTWTerrain.rays[0].wet == (game != 3));
        terrain_update(1);
        assert(calls == 17);
    }
    setup(1);
    GTWTerrain.count = 65;
    terrain_update(1);
    assert(calls == 0);
    setup(1);
    GTWTerrain.heartbeat -= 2000;
    terrain_update(1);
    assert(calls == 0);
    setup(1);
    GTWTerrain.count = 1;
    GTWTerrain.rays[0].to.x = 10000;
    terrain_update(1);
    assert(calls == 0 && GTWTerrain.complete == 1);
    puts("Native terrain ABI, water signatures, batch budget and mailbox checks passed.");
    return 0;
}
