#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include "FreeRTOS.h"
#include "task.h"
#include "shell.h"
#include "voice_player/voice_player_tts.h"
#include "service_local_agent.h"
#include <stdlib.h>
#include "service_brightness.h"
#include "service_volume.h"

#define LOCAL_OWNER 0x4C4F4341U
static volatile uint32_t button_sequence;
static bool has_playback;
static const char *last_state = "LOCAL_AGENT_IDLE";
static local_pet_phase_t pet_phase = PET_IDLE;
static local_pet_mood_t pet_mood = PET_NEUTRAL;
static local_pet_scene_t pet_scene = PET_COMPANION;
static TickType_t last_seen, phase_started, last_activity;
static bool seen_host;
static uint32_t recording_ms;
static char pet_caption[49];
static bool hud_valid, hud_paused;
static uint32_t hud_clock;
static int32_t hud_remaining = -1;
static TickType_t hud_updated;
static char hud_label[25];

static void pet_set(local_pet_phase_t phase, uint32_t duration, const char *caption)
{
    taskENTER_CRITICAL();
    pet_phase = phase;
    recording_ms = duration;
    phase_started = last_activity = xTaskGetTickCount();
    if (caption) {
        strncpy(pet_caption, caption, sizeof(pet_caption) - 1);
        pet_caption[sizeof(pet_caption) - 1] = 0;
    }
    taskEXIT_CRITICAL();
}

void local_agent_pet_playing(void)
{
    pet_set(PET_SPEAKING, 0, NULL);
}

void local_agent_pet_snapshot(local_pet_snapshot_t *snapshot)
{
    TickType_t now = xTaskGetTickCount();
    taskENTER_CRITICAL();
    snapshot->phase = pet_phase;
    snapshot->mood = pet_mood;
    snapshot->scene = pet_scene;
    snapshot->duration_ms = recording_ms;
    uint32_t elapsed = (now - phase_started) * portTICK_PERIOD_MS;
    snapshot->remaining_ms = elapsed < recording_ms ? recording_ms - elapsed : 0;
    memcpy(snapshot->caption, pet_caption, sizeof(pet_caption));
    uint32_t hud_elapsed = (now - hud_updated) / configTICK_RATE_HZ;
    snapshot->clock_valid = hud_valid;
    snapshot->clock_seconds = (hud_clock + hud_elapsed) % 86400;
    snapshot->timer_active = hud_remaining >= 0;
    snapshot->timer_paused = hud_paused;
    snapshot->timer_seconds = hud_remaining < 0 ? 0 : hud_paused ? (uint32_t)hud_remaining :
        hud_elapsed < (uint32_t)hud_remaining ? (uint32_t)hud_remaining - hud_elapsed : 0;
    memcpy(snapshot->timer_label, hud_label, sizeof(hud_label));
    if (!seen_host || now - last_seen > pdMS_TO_TICKS(12000)) {
        snapshot->phase = PET_OFFLINE;
        snapshot->caption[0] = 0;
    } else if (pet_phase == PET_IDLE && now - last_activity > pdMS_TO_TICKS(60000)) {
        snapshot->phase = PET_SLEEPING;
        snapshot->caption[0] = 0;
    }
    taskEXIT_CRITICAL();
}

static int hex_digit(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    return -1;
}

static int hud_command(int argc, char **argv)
{
    if (argc == 2) {
        local_pet_snapshot_t s;
        local_agent_pet_snapshot(&s);
        shellPrint(shellGetCurrent(), "LOCAL_AGENT_HUD CLOCK=%lu REMAIN=%ld PAUSED=%u ACTIVE=%u\n",
            (unsigned long)s.clock_seconds, s.timer_active ? (long)s.timer_seconds : -1L,
            (unsigned)s.timer_paused, (unsigned)s.timer_active);
        return 0;
    }
    if (argc < 5 || argc > 6) return -1;
    char *end;
    unsigned long clock = strtoul(argv[2], &end, 10);
    if (!argv[2][0] || *end || clock > 86399) return -1;
    long remaining = strtol(argv[3], &end, 10);
    if (!argv[3][0] || *end || remaining < -1 || remaining > 604800) return -1;
    unsigned long paused = strtoul(argv[4], &end, 10);
    if (!argv[4][0] || *end || paused > 1 || (remaining == -1 && (paused || argc == 6))) return -1;
    char label[25] = {0};
    if (argc == 6) {
        size_t length = strlen(argv[5]);
        if (!length || length > 48 || length % 2) return -1;
        for (size_t i = 0; i < length; i += 2) {
            int hi = hex_digit(argv[5][i]), lo = hex_digit(argv[5][i + 1]);
            if (hi < 0 || lo < 0 || (hi == 0 && lo < 32)) return -1;
            label[i / 2] = (char)((hi << 4) | lo);
        }
    }
    taskENTER_CRITICAL();
    hud_valid = true;
    hud_clock = (uint32_t)clock;
    hud_remaining = (int32_t)remaining;
    hud_paused = paused != 0;
    hud_updated = xTaskGetTickCount();
    memcpy(hud_label, label, sizeof(hud_label));
    taskEXIT_CRITICAL();
    shellPrint(shellGetCurrent(), "LOCAL_AGENT_HUD_OK\n");
    return 0;
}

static int pet_command(int argc, char **argv)
{
    static const char *names[] = {"offline", "idle", "listening", "thinking", "synthesizing", "speaking", "error", "sleeping", "ready"};
    if (argc < 4 || argc > 6) return -1;
    unsigned phase;
    for (phase = 0; phase < 9; ++phase) if (!strcmp(argv[2], names[phase])) break;
    char *end;
    unsigned long seconds = strtoul(argv[3], &end, 10);
    if (phase == 9 || !argv[3][0] || *end || seconds > 20 || argc > 5) return -1;
    char caption[49] = {0};
    if (argc == 5) {
        size_t length = strlen(argv[4]);
        if (length > 96 || length % 2) return -1;
        for (size_t i = 0; i < length; i += 2) {
            int hi = hex_digit(argv[4][i]), lo = hex_digit(argv[4][i+1]);
            if (hi < 0 || lo < 0 || (hi == 0 && lo < 32)) return -1;
            caption[i/2] = (char)((hi << 4) | lo);
        }
    }
    pet_set((local_pet_phase_t)phase, seconds * 1000, caption);
    shellPrint(shellGetCurrent(), "LOCAL_AGENT_UI_OK\n");
    return 0;
}

void local_agent_button_click(void)
{
    taskENTER_CRITICAL();
    ++button_sequence;
    last_activity = xTaskGetTickCount();
    taskEXIT_CRITICAL();
}

static void refresh_state(void)
{
    if (!has_playback) return;
    voice_tts_owned_result_t result = voice_player_tts_owned_result(LOCAL_OWNER);
    if (result == VOICE_TTS_OWNED_PENDING) {
        last_state = "LOCAL_AGENT_PLAYING";
        return;
    }
    last_state = result == VOICE_TTS_OWNED_COMPLETED ? "LOCAL_AGENT_DONE" : "LOCAL_AGENT_ERROR";
    pet_set(result == VOICE_TTS_OWNED_COMPLETED ? PET_IDLE : PET_ERROR, 0, NULL);
    voice_player_tts_release_owned(LOCAL_OWNER);
    has_playback = false;
}

static int local_agent_command(int argc, char **argv)
{
    refresh_state();
    if (argc == 2 && !strcmp(argv[1], "info")) {
        shellPrint(shellGetCurrent(), "LOCAL_AGENT_INFO HUD=1 AUDIO_BUFFER=1 POLL=1\n");
        return 0;
    }
    if (argc == 2 && !strcmp(argv[1], "audio")) {
        bool busy;
        uint32_t bytes, download_ms;
        voice_player_tts_local_audio_info(&busy, &bytes, &download_ms);
        shellPrint(shellGetCurrent(), "LOCAL_AGENT_AUDIO BUFFER=1 BUSY=%u BYTES=%lu DOWNLOAD_MS=%lu MAX=524288\n",
            (unsigned)busy, (unsigned long)bytes, (unsigned long)download_ms);
        return 0;
    }
    if (argc >= 2 && !strcmp(argv[1], "hud")) return hud_command(argc, argv);
    if (argc == 2 && !strcmp(argv[1], "theme")) {
        /* Keep the normal status line within the shell's 128-byte buffer. */
        shellPrint(shellGetCurrent(), "LOCAL_AGENT_THEME DAFEIYU1\n");
        return 0;
    }
    if (argc >= 2 && !strcmp(argv[1], "ui")) return pet_command(argc, argv);
    if (argc == 3 && (!strcmp(argv[1], "mood") || !strcmp(argv[1], "scene"))) {
        bool mood = !strcmp(argv[1], "mood");
        static const char *moods[] = {"neutral", "happy", "sad", "angry"};
        static const char *scenes[] = {"companion", "english", "story"};
        const char **names = mood ? moods : scenes;
        unsigned count = mood ? 4 : 3;
        unsigned value;
        for (value = 0; value < count; ++value) if (!strcmp(argv[2], names[value])) break;
        if (value == count) return -1;
        taskENTER_CRITICAL();
        if (mood) pet_mood = (local_pet_mood_t)value;
        else pet_scene = (local_pet_scene_t)value;
        last_activity = xTaskGetTickCount();
        taskEXIT_CRITICAL();
        shellPrint(shellGetCurrent(), mood ? "LOCAL_AGENT_MOOD_OK MOOD=%s\n" : "LOCAL_AGENT_SCENE_OK SCENE=%s\n", names[value]);
        return 0;
    }
    if (argc == 5 && !strcmp(argv[1], "control")) {
        bool brightness = !strcmp(argv[2], "brightness");
        if (!brightness && strcmp(argv[2], "volume")) return -1;
        bool adjust = !strcmp(argv[3], "adjust");
        if (!adjust && strcmp(argv[3], "set")) return -1;
        char *end;
        long value = strtol(argv[4], &end, 10);
        if (!argv[4][0] || *end || value < -100 || value > 100 || (!adjust && value < 10)) return -1;
        int current = brightness ? service_brightness_get() : service_volume_get();
        int target = adjust ? current + (int)value : (int)value;
        if (target < 10) target = 10;
        if (target > 100) target = 100;
        int result = brightness ? service_brightness_set_checked(target) : service_volume_set_checked(target);
        if (result != 0) {
            shellPrint(shellGetCurrent(), "LOCAL_AGENT_CONTROL_ERROR\n");
            return -1;
        }
        int applied = brightness ? service_brightness_get() : service_volume_get();
        shellPrint(shellGetCurrent(), "LOCAL_AGENT_CONTROL_OK %s=%d\n", brightness ? "BRIGHTNESS" : "VOLUME", applied);
        return 0;
    }
    if (argc == 2 && (!strcmp(argv[1], "status") || !strcmp(argv[1], "poll"))) {
        taskENTER_CRITICAL();
        last_seen = xTaskGetTickCount();
        seen_host = true;
        taskEXIT_CRITICAL();
        if (!strcmp(argv[1], "poll")) {
            shellPrint(shellGetCurrent(), "%s BUTTON=%lu CLOUD=DISABLED PET=1 CONTROL=1 PHASE=%u\n",
                last_state, (unsigned long)button_sequence, (unsigned)pet_phase);
            return 0;
        }
        static const char *moods[] = {"neutral", "happy", "sad", "angry"};
        static const char *scenes[] = {"companion", "english", "story"};
        shellPrint(shellGetCurrent(), "%s BUTTON=%lu CLOUD=DISABLED PET=1 CONTROL=1 ",
            last_state, (unsigned long)button_sequence);
        shellPrint(shellGetCurrent(), "MOOD=%s SCENE=%s BRIGHTNESS=%d VOLUME=%d PHASE=%u\n", moods[pet_mood], scenes[pet_scene],
            service_brightness_get(), service_volume_get(), (unsigned)pet_phase);
        return 0;
    }
    if (argc == 2 && strcmp(argv[1], "stop") == 0) {
        voice_player_tts_cancel_owned(LOCAL_OWNER);
        voice_player_tts_release_owned(LOCAL_OWNER);
        has_playback = false;
        last_state = "LOCAL_AGENT_IDLE";
        pet_set(PET_IDLE, 0, "");
        shellPrint(shellGetCurrent(), "LOCAL_AGENT_STOPPING\n");
        return 0;
    }
    if (argc == 3 && strcmp(argv[1], "play") == 0) {
        size_t size = strlen(argv[2]);
        if (size < 16 || size > 240 || strncmp(argv[2], "http://", 7) != 0) {
            shellPrint(shellGetCurrent(), "LOCAL_AGENT_ERROR invalid URL\n");
            return -1;
        }
        if (has_playback || !voice_player_tts_claim(LOCAL_OWNER)) {
            shellPrint(shellGetCurrent(), "LOCAL_AGENT_BUSY\n");
            return -1;
        }
        if (!voice_player_tts_play_owned(LOCAL_OWNER, argv[2])) {
            voice_player_tts_cancel_owned(LOCAL_OWNER);
            voice_player_tts_release_owned(LOCAL_OWNER);
            last_state = "LOCAL_AGENT_ERROR";
            shellPrint(shellGetCurrent(), "LOCAL_AGENT_ERROR queue\n");
            return -1;
        }
        has_playback = true;
        last_state = "LOCAL_AGENT_QUEUED";
        shellPrint(shellGetCurrent(), "LOCAL_AGENT_QUEUED\n");
        return 0;
    }
    shellPrint(shellGetCurrent(), "local_agent status | stop | play http://PC:8765/audio/token.mp3\n");
    return -1;
}

SHELL_EXPORT_CMD(SHELL_CMD_PERMISSION(0) | SHELL_CMD_TYPE(SHELL_TYPE_CMD_MAIN) |
    SHELL_CMD_DISABLE_RETURN, local_agent, local_agent_command, Mini3 local agent);
