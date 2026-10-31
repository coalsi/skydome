// Cheap structural check of an MJPEG frame before it reaches the decoder
// (DECISIONS.md F-010: USB 3 capture delivers ~10% damaged frames, which
// decode into streaks). No entropy decoding: it walks the header, then
// scans the scan data for restart markers. These cameras put one every 240
// MCUs (DRI), so a complete 3840x2160 4:2:2 frame has exactly 269, numbered
// 0..7 in turn; lost USB packets usually remove or reorder some.
#pragma once

#include <cstddef>
#include <cstdint>

enum class JpegCheck {
    Ok,
    NoStart,        // doesn't begin with SOI
    BadHeader,      // a marker segment runs off the end, or no SOF/SOS
    NoEnd,          // no EOI at the end (trailing zero padding allowed)
    RestartCount,   // wrong number of restart markers
    RestartOrder,   // restart markers out of sequence
};

JpegCheck check_jpeg(const uint8_t* data, size_t size);
const char* jpeg_check_name(JpegCheck c);
