#include <windows.h>
#include <stdio.h>
#include <string.h>

#define SAVE_MESSAGE (WM_APP + 0x475)
#define SAVE_MAGIC 0x47545753
typedef struct {
    LONG magic;
    LONG nonce;
    LONG slot;
    volatile LONG status;
    DWORD duration;
    volatile LONG hook_calls;
    LONG reason;
} SaveRequest;

static void mapping_name(char *name, DWORD pid) {
    sprintf(name, "Local\\GrandTheftWalkSave-%lu", (unsigned long)pid);
}
