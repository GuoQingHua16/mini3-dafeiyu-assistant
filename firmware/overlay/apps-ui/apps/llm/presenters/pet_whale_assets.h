#ifndef PET_WHALE_ASSETS_H
#define PET_WHALE_ASSETS_H
#include "lvgl.h"
typedef enum {
    WHALE_IDLE,
    WHALE_BLINK,
    WHALE_LISTEN,
    WHALE_THINK,
    WHALE_SPEAK_A,
    WHALE_SPEAK_B,
    WHALE_HAPPY,
    WHALE_SAD,
    WHALE_ANGRY,
    WHALE_SURPRISE,
    WHALE_SLEEP,
    WHALE_DROWSY,
    WHALE_SIT,
    WHALE_CURL,
    WHALE_FRAME_COUNT
} whale_frame_t;
const lv_img_dsc_t *pet_whale_frame(whale_frame_t frame);
#endif
