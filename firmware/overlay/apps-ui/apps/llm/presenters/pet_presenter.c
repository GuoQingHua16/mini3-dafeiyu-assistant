/* Mini3 LVGL UI. Character art: YunYueSama/codex-deepseek-pet, modified.
 * Attribution and complete license: mini3-agent/assets/dafeiyu/upstream/. */
#include <stdio.h>
#include <string.h>
#include "lisa_ui.h"
#include "lisa_ui_nav_scr.h"
#include "lisa_ui_nav_scr_ids.h"
#include "lisa_ui_fonts.h"
#include "model_battery.h"
#include "model_wifi.h"
#include "service_local_agent.h"
#include "pet_whale_assets.h"

typedef struct {
    lv_obj_t *root, *sprite, *name, *clock, *countdown;
    lv_obj_t *title, *caption, *battery, *link, *progress, *progress_track;
    lv_timer_t *timer;
    uint32_t frame;
    local_pet_phase_t previous;
    whale_frame_t previous_frame;
    whale_frame_t target_pose, transition_from;
    uint32_t pose_started, next_blink, blink_until, random_state;
    char previous_caption[49];
    uint32_t previous_clock;
    char previous_countdown[80];
} pet_view_t;

static lv_obj_t *shape(lv_obj_t *parent, int x, int y, int w, int h,
                       uint32_t color, int radius)
{
    lv_obj_t *obj = lv_obj_create(parent);
    lv_obj_remove_style_all(obj);
    lv_obj_set_pos(obj, x, y);
    lv_obj_set_size(obj, w, h);
    lv_obj_set_style_bg_color(obj, lv_color_hex(color), 0);
    lv_obj_set_style_bg_opa(obj, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(obj, radius, 0);
    lv_obj_clear_flag(obj, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
    return obj;
}

static lv_obj_t *label(lv_obj_t *parent, int x, int y, int width, uint32_t color)
{
    lv_obj_t *obj = lv_label_create(parent);
    lv_obj_set_pos(obj, x, y);
    lv_obj_set_width(obj, width);
    lv_obj_set_style_text_font(obj, &lv_font_chinese_16, 0);
    lv_obj_set_style_text_color(obj, lv_color_hex(color), 0);
    lv_obj_set_style_text_align(obj, LV_TEXT_ALIGN_CENTER, 0);
    lv_label_set_long_mode(obj, LV_LABEL_LONG_DOT);
    return obj;
}

static whale_frame_t character_pose(const local_pet_snapshot_t *s)
{
    if (s->phase == PET_SLEEPING || s->phase == PET_OFFLINE) return WHALE_SLEEP;
    if (s->phase == PET_ERROR) return WHALE_SURPRISE;
    if (s->phase == PET_THINKING || s->phase == PET_SYNTHESIZING) return WHALE_THINK;
    if (s->phase == PET_SPEAKING) return WHALE_SPEAK_A;
    if (s->mood == PET_HAPPY) return WHALE_HAPPY;
    if (s->mood == PET_SAD) return WHALE_SAD;
    if (s->mood == PET_ANGRY) return WHALE_ANGRY;
    if (s->phase == PET_LISTENING || s->phase == PET_READY) return WHALE_LISTEN;
    return WHALE_IDLE;
}

static whale_frame_t motion_frame(pet_view_t *v, whale_frame_t pose, uint32_t t, uint16_t *zoom)
{
    *zoom = 256;
    if (v->target_pose == WHALE_FRAME_COUNT) {
        v->target_pose = v->transition_from = pose;
        v->pose_started = t - 10;
    } else if (pose != v->target_pose) {
        v->transition_from = v->previous_frame;
        v->target_pose = pose;
        v->pose_started = t;
    }
    uint32_t age = t - v->pose_started;
    if (age < 6 && pose == WHALE_SLEEP && v->transition_from != WHALE_SLEEP) {
        static const whale_frame_t entering[] = {WHALE_DROWSY, WHALE_SIT, WHALE_CURL};
        return entering[age / 2];
    }
    if (age < 6 && pose != WHALE_SLEEP &&
        (v->transition_from == WHALE_SLEEP || v->transition_from == WHALE_CURL)) {
        static const whale_frame_t waking[] = {WHALE_CURL, WHALE_SIT, WHALE_DROWSY};
        return waking[age / 2];
    }
    if (age < 4 && v->transition_from != pose && pose != WHALE_SLEEP) {
        static const uint16_t settling[] = {256, 252, 252, 255};
        *zoom = settling[age];
        return age < 2 ? v->transition_from : pose;
    }
    if (pose == WHALE_IDLE) {
        if ((int32_t)(t - v->next_blink) >= 0) {
            v->random_state = v->random_state * 1664525U + 1013904223U;
            v->blink_until = t + 2;
            v->next_blink = t + 55 + v->random_state % 46;
        }
        if ((int32_t)(v->blink_until - t) > 0) return WHALE_BLINK;
    }
    if (pose == WHALE_SPEAK_A) return (t / 5) % 2 ? WHALE_SPEAK_A : WHALE_SPEAK_B;
    return pose;
}

static void tick(lv_timer_t *timer)
{
    pet_view_t *v = timer->user_data;
    local_pet_snapshot_t s;
    local_agent_pet_snapshot(&s);
    uint32_t t = v->frame++;
    bool idle = s.phase == PET_IDLE;
    bool sleepy = s.phase == PET_SLEEPING || s.phase == PET_OFFLINE;
    bool thinking = s.phase == PET_THINKING || s.phase == PET_SYNTHESIZING;
    bool expressive = !sleepy && !thinking && s.phase != PET_ERROR;
    bool happy = expressive && s.mood == PET_HAPPY;
    bool sad = expressive && s.mood == PET_SAD;
    bool angry = expressive && s.mood == PET_ANGRY;
    uint32_t accent = s.phase == PET_ERROR ? 0xFFAD93 :
                      thinking ? 0xC5BAFF : sleepy ? 0x90ABAA :
                      angry ? 0xFFA18B : sad ? 0x8DCBFF : happy ? 0xFFE59A : 0x79AEFF;
    static const int8_t breathing[] = {0, 0, 1, 1, 2, 2, 2, 1, 1, 0, 0, -1, -1, 0};
    int breath = breathing[(t / 5) % 14];
    if (sleepy) breath /= 2;
    lv_obj_set_y(v->sprite, 33 + breath);
    uint16_t zoom;
    whale_frame_t frame = motion_frame(v, character_pose(&s), t, &zoom);
    lv_img_set_zoom(v->sprite, zoom);
    if (frame != v->previous_frame) {
        lv_img_set_src(v->sprite, pet_whale_frame(frame));
        v->previous_frame = frame;
    }
    static const char *scene_names[] = {"陪伴", "英语", "故事"};
    lv_label_set_text(v->name, scene_names[s.scene]);

    static const char *titles[] = {"等你连接", "小聆在这里", "我在听", "让我想想", "准备说话", "说给你听", "刚才没成功", "陪你休息一会", "我在听"};
    static const char *hints[] = {"检查USB和电脑端服务", "按一下，和我聊聊", "现在可以说话啦", "正在认真想你的问题", "马上就好", "", "按键重试，连接自动恢复", "按一下就能叫醒我", "请直接说话"};
    if (s.phase != v->previous || s.phase == PET_LISTENING || s.phase == PET_IDLE) {
        char text[64];
        if (s.phase == PET_LISTENING)
            snprintf(text, sizeof(text), "我在听 · %lus", (unsigned long)((s.remaining_ms + 999) / 1000));
        else if (idle && s.mood != PET_NEUTRAL) {
            static const char *mood_names[] = {"小聆在这里", "开心的小聆", "难过的小聆", "生气的小聆"};
            snprintf(text, sizeof(text), "%s", mood_names[s.mood]);
        } else snprintf(text, sizeof(text), "%s", titles[s.phase]);
        lv_label_set_text(v->title, text);
        lv_obj_set_style_text_color(v->title, lv_color_hex(accent), 0);
    }
    if (s.phase != v->previous || strcmp(s.caption, v->previous_caption)) {
        const char *caption = s.caption[0] && (thinking || s.phase == PET_SPEAKING || s.phase == PET_ERROR || s.phase == PET_READY) ? s.caption : hints[s.phase];
        lv_label_set_text(v->caption, caption);
        memcpy(v->previous_caption, s.caption, sizeof(s.caption));
    }
    v->previous = s.phase;
    if (s.clock_valid && s.clock_seconds != v->previous_clock) {
        lv_label_set_text_fmt(v->clock, "%02lu:%02lu:%02lu",
            (unsigned long)(s.clock_seconds / 3600),
            (unsigned long)(s.clock_seconds / 60 % 60), (unsigned long)(s.clock_seconds % 60));
        v->previous_clock = s.clock_seconds;
    }
    char timer_text[80] = {0};
    if (s.timer_active) {
        if (!s.timer_paused && s.timer_seconds == 0)
            snprintf(timer_text, sizeof(timer_text), "%s 到点待播", s.timer_label[0] ? s.timer_label : "计时");
        else snprintf(timer_text, sizeof(timer_text), "%s %02lu:%02lu:%02lu%s",
            s.timer_label[0] ? s.timer_label : "计时", (unsigned long)(s.timer_seconds / 3600),
            (unsigned long)(s.timer_seconds / 60 % 60), (unsigned long)(s.timer_seconds % 60),
            s.timer_paused ? " 暂停" : "");
    }
    if (strcmp(timer_text, v->previous_countdown)) {
        lv_label_set_text(v->countdown, timer_text);
        memcpy(v->previous_countdown, timer_text, sizeof(timer_text));
    }
    if (s.phase == PET_LISTENING) {
        lv_obj_clear_flag(v->progress_track, LV_OBJ_FLAG_HIDDEN);
        uint32_t width = s.duration_ms ? 208 * s.remaining_ms / s.duration_ms : 0;
        lv_obj_set_width(v->progress, width > 0 ? width : 1);
    } else lv_obj_add_flag(v->progress_track, LV_OBJ_FLAG_HIDDEN);

    if (t % 25 == 0) {
        model_battery_info_t battery;
        if (model_battery_get_info(&battery) == 0 && battery.level <= 100 &&
            battery.status != MODEL_BATTERY_STATUS_UNKNOWN && battery.status != MODEL_BATTERY_STATUS_NO_BATTERY) {
            lv_label_set_text_fmt(v->battery, "%u%%", battery.level);
        } else lv_label_set_text(v->battery, "--");
        lv_label_set_text(v->link, model_wifi_get_status() == MODEL_WIFI_STATUS_CONNECTED ? LV_SYMBOL_WIFI : "!");
    }
}

static int pet_open(const struct lisa_ui_nav_scr *scr, void **data)
{
    (void)scr;
    pet_view_t *v = lv_mem_alloc(sizeof(*v));
    if (!v) return -1;
    memset(v, 0, sizeof(*v));
    v->previous = (local_pet_phase_t)-1;
    v->previous_frame = WHALE_FRAME_COUNT;
    v->target_pose = WHALE_FRAME_COUNT;
    v->next_blink = 57;
    v->random_state = lv_tick_get() | 1U;
    v->previous_clock = UINT32_MAX;
    v->root = shape(lv_scr_act(), 0, 0, 240, 240, 0x101E23, 0);
    v->name = label(v->root, 12, 13, 54, 0xEAF3E9);
    lv_obj_set_style_text_align(v->name, LV_TEXT_ALIGN_LEFT, 0);
    lv_label_set_text(v->name, "陪伴");
    v->clock = label(v->root, 72, 13, 84, 0xD3E4FA);
    lv_label_set_text(v->clock, "--:--:--");
    v->link = label(v->root, 162, 13, 22, 0x79AEFF);
    v->battery = label(v->root, 188, 13, 40, 0xA5B8B8);
    /* Numeric font fits 100% in the existing box without moving the HUD. */
    lv_obj_set_style_text_font(v->battery, &lv_font_montserrat_14, 0);
    lv_obj_set_style_text_align(v->battery, LV_TEXT_ALIGN_RIGHT, 0);
    lv_label_set_long_mode(v->battery, LV_LABEL_LONG_CLIP);
    v->sprite = lv_img_create(v->root);
    lv_img_set_src(v->sprite, pet_whale_frame(WHALE_IDLE));
    lv_img_set_pivot(v->sprite, 88, 144);
    lv_obj_set_pos(v->sprite, 32, 33);
    lv_obj_clear_flag(v->sprite, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
    v->title = label(v->root, 12, 176, 216, 0x79AEFF);
    v->caption = label(v->root, 16, 197, 208, 0xA5B8B8);
    lv_obj_set_height(v->caption, 20);
    v->countdown = label(v->root, 12, 218, 216, 0xD3E4FA);
    lv_obj_set_height(v->countdown, 18);
    lv_label_set_text(v->countdown, "");
    v->progress_track = shape(v->root, 16, 237, 208, 3, 0x294147, 2);
    v->progress = shape(v->progress_track, 0, 0, 208, 3, 0x79AEFF, 2);
    v->timer = lv_timer_create(tick, 80, v);
    if (!v->timer) { lv_obj_del(v->root); lv_mem_free(v); return -1; }
    tick(v->timer);
    *data = v;
    return 0;
}

static int pet_show(const struct lisa_ui_nav_scr *scr, void *data)
{
    (void)scr;
    pet_view_t *v = data;
    lv_obj_clear_flag(v->root, LV_OBJ_FLAG_HIDDEN);
    lv_timer_resume(v->timer);
    tick(v->timer);
    return 0;
}

static int pet_pause(const struct lisa_ui_nav_scr *scr, void *data)
{
    (void)scr;
    pet_view_t *v = data;
    lv_timer_pause(v->timer);
    lv_obj_add_flag(v->root, LV_OBJ_FLAG_HIDDEN);
    return 0;
}

static int pet_close(const struct lisa_ui_nav_scr *scr, void *data)
{
    (void)scr;
    pet_view_t *v = data;
    lv_timer_del(v->timer);
    lv_obj_del(v->root);
    lv_mem_free(v);
    return 0;
}

const struct lisa_ui_nav_scr home_nav_scr = {
    .unique_id = LISA_UI_NAV_SCR_ID_HOME, .open = pet_open, .show = pet_show,
    .pause = pet_pause, .resume = pet_show, .close = pet_close,
};
