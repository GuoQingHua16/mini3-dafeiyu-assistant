#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

/* Validate frame boundaries before passing a downloaded file to the native
 * decoder. Accept Layer III MPEG-1/2/2.5, VBR and an optional leading ID3v2 or
 * trailing ID3v1 tag. This validates framing, not every compressed payload bit. */
static bool local_mp3_has_complete_frames(const uint8_t *data, size_t size)
{
    static const uint16_t rates[2][15] = {
        {0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160},
        {0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320},
    };
    static const uint32_t sample_rates[] = {44100, 48000, 32000};
    size_t offset = 0;
    unsigned frames = 0, first_version = 0, first_sample = 0;
    if (size >= 10 && memcmp(data, "ID3", 3) == 0) {
        if ((data[6] | data[7] | data[8] | data[9]) & 0x80) return false;
        offset = 10U + ((size_t)data[6] << 21) + ((size_t)data[7] << 14) +
                 ((size_t)data[8] << 7) + data[9];
        if (data[3] == 4 && (data[5] & 0x10)) offset += 10;
        if (offset > size) return false;
    }
    while (offset < size) {
        if (size - offset == 128 && memcmp(data + offset, "TAG", 3) == 0) break;
        if (size - offset < 4) return false;
        const uint8_t *p = data + offset;
        unsigned version = (p[1] >> 3) & 3, layer = (p[1] >> 1) & 3;
        unsigned bitrate = p[2] >> 4, sample = (p[2] >> 2) & 3;
        if (p[0] != 0xff || (p[1] & 0xe0) != 0xe0 || version == 1 || layer != 1 ||
            bitrate == 0 || bitrate == 15 || sample == 3) return false;
        if (frames && (version != first_version || sample != first_sample)) return false;
        first_version = version;
        first_sample = sample;
        uint32_t hz = sample_rates[sample] / (version == 3 ? 1U : version == 2 ? 2U : 4U);
        size_t length = (version == 3 ? 144000U : 72000U) * rates[version == 3][bitrate] / hz +
                        ((p[2] >> 1) & 1);
        if (length < 4 || length > size - offset) return false;
        offset += length;
        ++frames;
    }
    return frames >= 2;
}
