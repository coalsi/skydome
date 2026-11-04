// One camera: V4L2 capture -> hardware decode into NVMM buffers, mapped for
// CUDA through EGL with no copy (DECISIONS.md D-016). The newest decoded
// frame is held until the next one arrives.
//
// Recovers on its own: if the device is missing, the pipeline errors (USB
// glitch, unplug) or frames stop for stale_after, it tears down and retries,
// so the dome keeps running on the other cameras (handoff §5.1).
#pragma once

#include <atomic>
#include <chrono>
#include <cstdint>
#include <string>

#include <cuda.h>
#include <gst/gst.h>

#include "stitch.h"

using Clock = std::chrono::steady_clock;

struct CaptureFormat {
    enum class Kind { MJPEG, YUYV } kind = Kind::MJPEG;
    int width = 3840;
    int height = 2160;
    int fps = 15;
};

class CameraSource {
public:
    CameraSource(std::string name, std::string device, CaptureFormat fmt);
    ~CameraSource();
    CameraSource(const CameraSource&) = delete;
    CameraSource& operator=(const CameraSource&) = delete;

    // Handle bus messages, restarts and new frames. Call from the thread that
    // owns the CUDA context, never while a stitch is reading image().
    void poll(Clock::time_point now);

    const std::string& name() const { return name_; }
    const std::string& device() const { return device_; }
    bool has_frame() const { return held_ != nullptr; }
    const PlanarImage& image() const { return image_; }
    const char* state() const;

    // Frames received since the last call (for fps reporting)
    uint64_t take_frame_count();
    // Damaged frames dropped before decoding since the last call (F-010)
    uint64_t take_dropped() { return dropped_.exchange(0); }
    uint64_t restarts() const { return restarts_; }

    // Save the next good frame exactly as the camera sent it (MJPEG only) to
    // path: the raw download (D-023). Written to path.tmp, then renamed
    void request_raw(const std::string& path);

private:
    static GstPadProbeReturn check_frame(GstPad*, GstPadProbeInfo* info, gpointer self);

    void start(Clock::time_point now);
    void stop();
    void release_frame();
    bool hold(GstSample* sample);

    std::string name_, device_;
    CaptureFormat fmt_;
    GstElement* pipeline_ = nullptr;
    GstElement* sink_ = nullptr;
    enum class State { Waiting, Running, Backoff } state_ = State::Waiting;
    Clock::time_point retry_at_{};
    Clock::time_point last_frame_{};
    Clock::time_point started_{};

    GstSample* held_ = nullptr;
    GstMapInfo map_{};
    void* surf_ = nullptr;           // NvBufSurface*
    CUgraphicsResource res_ = nullptr;
    PlanarImage image_{};

    uint64_t frames_ = 0;
    uint64_t restarts_ = 0;
    std::atomic<uint64_t> dropped_{0};     // written from the streaming thread
    std::string raw_path_;                 // set before raw_wanted_
    std::atomic<bool> raw_wanted_{false};
};
