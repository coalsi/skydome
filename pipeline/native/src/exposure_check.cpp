// Runs the automatic exposure (exposure.h) against a pretend sky and prints
// where it settles, for pipeline/tests/test_native_exposure.py:
//
//   skydome-exposure-check BRIGHTNESS [FRAMES [LAG]]
//
// BRIGHTNESS: the sky level a 1 µs exposure at gain 0 would give (full
// scale = 1; noon ~1e-4, a dark night ~1e-10). LAG: frames before the picture
// shows a new setting, as a real camera's does. Prints "exposure_us gain level",
// then the most it ever asked for (exposure_us x linear gain).
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <deque>

#include "exposure.h"

int main(int argc, char** argv) {
    if (argc < 2) {
        std::fprintf(stderr, "usage: skydome-exposure-check BRIGHTNESS [FRAMES]\n");
        return 2;
    }
    const double k = std::atof(argv[1]);
    const int frames = argc > 2 ? std::atoi(argv[2]) : 40;
    const int lag = argc > 3 ? std::atoi(argv[3]) : 0;
    AutoExposure ae;
    AutoExposure::Setting s = ae.setting();
    std::deque<AutoExposure::Setting> sent(static_cast<size_t>(lag) + 1, s);
    const auto seen = [&](const AutoExposure::Setting& x) {
        return std::min(1.0, k * static_cast<double>(x.exposure_us) * AutoExposure::linear(x.gain));
    };
    double level = 0, peak = 0;
    for (int i = 0; i < frames; ++i) {
        const AutoExposure::Setting want = ae.setting();
        if (want.exposure_us != s.exposure_us || want.gain != s.gain) ae.changed(want);
        s = want;
        sent.push_back(s);
        sent.pop_front();
        level = seen(sent.front());
        peak = std::max(peak, static_cast<double>(s.exposure_us) * AutoExposure::linear(s.gain));
        ae.update(level, s);
    }
    s = ae.setting();
    std::printf("%ld %d %.6f %.0f\n", s.exposure_us, s.gain, seen(s), peak);
    return 0;
}
