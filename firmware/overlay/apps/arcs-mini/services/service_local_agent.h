#pragma once
#include <stdbool.h>
#include <stdint.h>

typedef enum {
    PET_OFFLINE, PET_IDLE, PET_LISTENING, PET_THINKING,
    PET_SYNTHESIZING, PET_SPEAKING, PET_ERROR, PET_SLEEPING, PET_READY
} local_pet_phase_t;

typedef enum { PET_NEUTRAL, PET_HAPPY, PET_SAD, PET_ANGRY } local_pet_mood_t;
typedef enum { PET_COMPANION, PET_ENGLISH, PET_STORY } local_pet_scene_t;

typedef struct {
    local_pet_phase_t phase;
    local_pet_mood_t mood;
    local_pet_scene_t scene;
    uint32_t remaining_ms;
    uint32_t duration_ms;
    char caption[49];
    bool clock_valid;
    bool timer_active;
    bool timer_paused;
    uint32_t clock_seconds;
    uint32_t timer_seconds;
    char timer_label[25];
} local_pet_snapshot_t;

/* Thread-safe snapshot. LVGL remains exclusively on the UI thread. */
void local_agent_pet_snapshot(local_pet_snapshot_t *snapshot);
void local_agent_pet_playing(void);
