// The dome's output: I420 frames in pinned memory (written by the GPU, read
// by x264 with no copy) -> x264 H.264 (DECISIONS.md D-014) -> a sink,
// normally RTSP into mediamtx. Restarts itself if the sink fails (mediamtx
// restarting, for example); frames pushed meanwhile are dropped.
#pragma once

#include <atomic>
#include <chrono>
#include <cstdint>
#include <string>
#include <vector>

#include <gst/gst.h>

#include "stitch.h"

using Clock = std::chrono::steady_clock;

struct EncoderConfig {
    int width = 5120;
    int height = 5120;
    int fps = 15;
    int bitrate_kbps = 20000;
    // Everything after h264parse. Empty = no encoder at all (stitch only, for benchmarks)
    std::string sink = "rtspclientsink location=rtsp://127.0.0.1:8554/dome protocols=tcp latency=0";
    bool encode = true;
};

// A pool of output frames the GPU writes into and x264 reads from
class FramePool {
public:
    FramePool(int width, int height, int count);
    ~FramePool();
    FramePool(const FramePool&) = delete;
    FramePool& operator=(const FramePool&) = delete;

    struct Frame {
        uint8_t* host = nullptr;   // pinned, mapped
        I420Out dev;               // the same memory, as GPU pointers
        size_t bytes = 0;
        std::atomic<bool> busy{false};
    };
    Frame* acquire();              // nullptr if every frame is still with the encoder
    static void release(Frame* f) { f->busy.store(false); }

private:
    std::vector<Frame> frames_;
};

class Encoder {
public:
    explicit Encoder(EncoderConfig cfg);
    ~Encoder();

    void poll(Clock::time_point now);
    // Hands the frame to the encoder (it's released when x264 is done with it).
    // Returns false and releases the frame if the encoder isn't running.
    bool push(FramePool::Frame* f);
    bool running() const { return state_ == State::Running; }
    uint64_t restarts() const { return restarts_; }

private:
    void start(Clock::time_point now);
    void stop();

    EncoderConfig cfg_;
    GstElement* pipeline_ = nullptr;
    GstElement* src_ = nullptr;
    enum class State { Stopped, Running, Backoff } state_ = State::Stopped;
    Clock::time_point retry_at_{};
    uint64_t restarts_ = 0;
};
