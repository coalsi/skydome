#include "jpeg_check.h"

#include <cstring>

namespace {

inline unsigned be16(const uint8_t* p) { return (unsigned(p[0]) << 8) | p[1]; }

}  // namespace

JpegCheck check_jpeg(const uint8_t* d, size_t n) {
    if (n < 4 || d[0] != 0xFF || d[1] != 0xD8) return JpegCheck::NoStart;

    // Ignore zero padding some cameras add after EOI
    size_t end = n;
    while (end > 2 && d[end - 1] == 0) --end;
    if (end < 4 || d[end - 2] != 0xFF || d[end - 1] != 0xD9) return JpegCheck::NoEnd;

    // Header: up to and including SOS
    size_t i = 2;
    unsigned width = 0, height = 0, hmax = 1, vmax = 1, dri = 0;
    bool sof = false;
    while (true) {
        if (i + 4 > end || d[i] != 0xFF) return JpegCheck::BadHeader;
        const uint8_t m = d[i + 1];
        if (m == 0xFF) {   // fill byte
            ++i;
            continue;
        }
        const unsigned len = be16(d + i + 2);
        if (len < 2 || i + 2 + len > end) return JpegCheck::BadHeader;
        const uint8_t* seg = d + i + 4;
        if (m >= 0xC0 && m <= 0xC3) {   // SOF0..3
            if (len < 8) return JpegCheck::BadHeader;
            height = be16(seg + 1);
            width = be16(seg + 3);
            const unsigned comps = seg[5];
            if (len < 8 + 3 * comps) return JpegCheck::BadHeader;
            for (unsigned k = 0; k < comps; ++k) {
                const uint8_t s = seg[6 + 3 * k + 1];
                hmax = s >> 4 > hmax ? s >> 4 : hmax;
                vmax = (s & 15) > vmax ? s & 15 : vmax;
            }
            sof = true;
        } else if (m == 0xDD && len >= 4) {
            dri = be16(seg);
        }
        i += 2 + len;
        if (m == 0xDA) break;   // SOS: scan data follows
    }
    if (!sof || width == 0 || height == 0) return JpegCheck::BadHeader;
    if (dri == 0) return JpegCheck::Ok;   // nothing more to check without restart markers

    const unsigned mcu_w = 8 * hmax, mcu_h = 8 * vmax;
    const unsigned mcus = ((width + mcu_w - 1) / mcu_w) * ((height + mcu_h - 1) / mcu_h);
    const unsigned expected = (mcus + dri - 1) / dri - 1;

    // Scan data: 0xFF is either stuffed (0xFF00), a restart marker (D0..D7) or the end
    unsigned count = 0;
    const uint8_t* p = d + i;
    const uint8_t* stop = d + end - 2;
    while (p < stop) {
        p = static_cast<const uint8_t*>(std::memchr(p, 0xFF, static_cast<size_t>(stop - p)));
        if (!p) break;
        const uint8_t m = p[1];
        if (m >= 0xD0 && m <= 0xD7) {
            if (m - 0xD0 != static_cast<int>(count % 8)) return JpegCheck::RestartOrder;
            ++count;
            if (count > expected) return JpegCheck::RestartCount;
        }
        p += 2;
    }
    return count == expected ? JpegCheck::Ok : JpegCheck::RestartCount;
}

const char* jpeg_check_name(JpegCheck c) {
    switch (c) {
        case JpegCheck::Ok: return "ok";
        case JpegCheck::NoStart: return "no start marker";
        case JpegCheck::BadHeader: return "bad header";
        case JpegCheck::NoEnd: return "no end marker";
        case JpegCheck::RestartCount: return "missing restart markers";
        case JpegCheck::RestartOrder: return "restart markers out of order";
    }
    return "?";
}
