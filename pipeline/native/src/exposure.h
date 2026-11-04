// Automatic exposure for the all-sky camera, from daylight to a dark night
// (DECISIONS.md D-029). One number, the exposure index (microseconds x
// linear gain), follows the sky; a ladder turns it into the camera's
// exposure and gain:
//
//   1. exposure from the shortest up to `smooth_us` (1/15 s) at gain 0:
//      daylight, smooth video;
//   2. gain from 0 up to `max_gain`, still at `smooth_us`: twilight, still smooth;
//   3. exposure from `smooth_us` up to `max_us` (20 s) at `max_gain`: night,
//      one frame every few seconds, as deep as it goes.
//
// Each frame's sky level (the median of the sky circle, 0..1 of full scale)
// moves the index toward `target`, most of the way, and never more than 8x
// at once. The camera's gain is in 0.1 dB steps, so linear = 10^(gain/200).
#pragma once

#include <algorithm>
#include <cmath>

class AutoExposure {
public:
    struct Setting {
        long exposure_us = 1000;
        int gain = 0;
    };

    double min_us = 32;           // the camera's shortest
    double smooth_us = 66667;     // longest exposure that keeps 15 fps video
    double max_us = 20e6;         // longest at night
    int max_gain = 200;           // 20 dB
    double target = 0.18;         // the sky's median, linear (mid-grey after a display gamma)

    static double linear(int gain) { return std::pow(10.0, gain / 200.0); }

    double index() const { return e_; }
    void set_index(double e) { e_ = std::clamp(e, min_us, ceiling()); }

    Setting setting() const {
        const double smooth = std::min(smooth_us, max_us);
        const double top = linear(max_gain);
        Setting s;
        if (e_ <= smooth) {
            s.exposure_us = std::lround(std::max(e_, min_us));
        } else if (e_ <= smooth * top) {
            s.exposure_us = std::lround(smooth);
            s.gain = std::clamp(static_cast<int>(std::lround(200.0 * std::log10(e_ / smooth))), 0, max_gain);
        } else {
            s.exposure_us = std::lround(std::min(e_ / top, max_us));
            s.gain = max_gain;
        }
        return s;
    }

    // A frame taken with `taken` came back with sky level `level`
    void update(double level, const Setting& taken) {
        const double got = static_cast<double>(taken.exposure_us) * linear(taken.gain);
        double ratio = level > 1e-4 ? target / level : 8.0;
        ratio = std::clamp(ratio, 1.0 / 8.0, 8.0);
        // 60% of the way in log terms: settles in a few frames without hunting
        set_index(got * std::pow(ratio, 0.6));
    }

private:
    double ceiling() const { return std::max(min_us, max_us * linear(max_gain)); }
    double e_ = 1000;
};
