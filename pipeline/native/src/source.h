// What the live loop needs from a camera, whatever drives it: a UVC camera
// through V4L2 and the hardware JPEG decoder (capture.h), or a ZWO
// astronomy camera through ZWO's SDK (asi_source.h, DECISIONS.md D-029).
#pragma once

#include <chrono>
#include <cstdint>
#include <string>

#include "stitch.h"

using Clock = std::chrono::steady_clock;

class FrameSource {
public:
    virtual ~FrameSource() = default;

    // Handle restarts and new frames. Call from the thread that owns the
    // CUDA context, never while a stitch is reading image().
    virtual void poll(Clock::time_point now) = 0;

    virtual const std::string& name() const = 0;
    virtual bool has_frame() const = 0;
    virtual const PlanarImage& image() const = 0;
    virtual const char* state() const = 0;

    // Frames received since the last call (for fps reporting)
    virtual uint64_t take_frame_count() = 0;
    // Damaged or lost frames since the last call
    virtual uint64_t take_dropped() = 0;
    virtual uint64_t restarts() const = 0;
    // Counts every new frame (never reset): a change means image() is a new picture
    virtual uint64_t frame_seq() const = 0;

    // Save the next good frame as the camera gave it to `base` plus the
    // source's own extension (.jpg for MJPEG, .pgm for raw sensor data):
    // the raw download (D-023). Written to a .tmp, then renamed
    virtual void request_raw(const std::string& base) = 0;

    // More for the stats file, as JSON members starting with a comma ("" = none)
    virtual std::string stats_json() const { return ""; }
};
