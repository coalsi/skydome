// Runs the automatic exposure (exposure.h) against a pretend sky and prints
// where it settles, for pipeline/tests/test_native_exposure.py:
//
//   skydome-exposure-check BRIGHTNESS [FRAMES]
//
// BRIGHTNESS: the sky level a 1 µs exposure at gain 0 would give (full
// scale = 1; noon ~1e-4, a dark night ~1e-10). Prints "exposure_us gain level".
#include <algorithm>
#include <cstdio>
#include <cstdlib>

#include "exposure.h"

int main(int argc, char** argv) {
    if (argc < 2) {
        std::fprintf(stderr, "usage: skydome-exposure-check BRIGHTNESS [FRAMES]\n");
        return 2;
    }
    const double k = std::atof(argv[1]);
    const int frames = argc > 2 ? std::atoi(argv[2]) : 40;
    AutoExposure ae;
    AutoExposure::Setting s = ae.setting();
    double level = 0;
    for (int i = 0; i < frames; ++i) {
        s = ae.setting();
        level = std::min(1.0, k * static_cast<double>(s.exposure_us) * AutoExposure::linear(s.gain));
        ae.update(level, s);
    }
    s = ae.setting();
    level = std::min(1.0, k * static_cast<double>(s.exposure_us) * AutoExposure::linear(s.gain));
    std::printf("%ld %d %.6f\n", s.exposure_us, s.gain, level);
    return 0;
}
