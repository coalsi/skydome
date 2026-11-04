#include "encoder.h"

#include <cstdio>
#include <stdexcept>

#include <gst/app/gstappsrc.h>

namespace {
constexpr auto kBackoff = std::chrono::seconds(2);
}

FramePool::FramePool(int width, int height, int count) : frames_(count) {
    const size_t ysz = size_t(width) * height, csz = ysz / 4;
    for (Frame& f : frames_) {
        f.bytes = ysz + 2 * csz;
        void* host = nullptr;
        cuda_check(cudaHostAlloc(&host, f.bytes, cudaHostAllocMapped), "cudaHostAlloc output frame");
        void* dev = nullptr;
        cuda_check(cudaHostGetDevicePointer(&dev, host, 0), "cudaHostGetDevicePointer");
        f.host = static_cast<uint8_t*>(host);
        auto* d = static_cast<uint8_t*>(dev);
        f.dev = {d, d + ysz, d + ysz + csz};
    }
}

FramePool::~FramePool() {
    for (Frame& f : frames_) cudaFreeHost(f.host);
}

FramePool::Frame* FramePool::acquire() {
    for (Frame& f : frames_) {
        bool expected = false;
        if (f.busy.compare_exchange_strong(expected, true)) return &f;
    }
    return nullptr;
}

Encoder::Encoder(EncoderConfig cfg) : cfg_(std::move(cfg)) {}

Encoder::~Encoder() { stop(); }

void Encoder::start(Clock::time_point now) {
    if (!cfg_.encode) {
        state_ = State::Running;
        return;
    }
    char desc[1024];
    // Full-range BT.601 like the JPEG decoder's output, so x264 flags it in the VUI
    std::snprintf(desc, sizeof desc,
                  "appsrc name=src is-live=true format=time do-timestamp=true block=false max-buffers=2 leaky-type=downstream "
                  "caps=video/x-raw,format=I420,width=%d,height=%d,framerate=%d/1,colorimetry=1:4:5:1 "
                  "! x264enc speed-preset=ultrafast tune=zerolatency bitrate=%d threads=0 key-int-max=%d "
                  "! h264parse config-interval=-1 ! %s",
                  cfg_.width, cfg_.height, cfg_.fps, cfg_.bitrate_kbps, cfg_.fps * 2, cfg_.sink.c_str());
    GError* err = nullptr;
    pipeline_ = gst_parse_launch(desc, &err);
    if (!pipeline_) {
        // A bad pipeline description won't fix itself
        std::string msg = err ? err->message : "?";
        g_clear_error(&err);
        throw std::runtime_error("encoder pipeline: " + msg);
    }
    src_ = gst_bin_get_by_name(GST_BIN(pipeline_), "src");
    if (gst_element_set_state(pipeline_, GST_STATE_PLAYING) == GST_STATE_CHANGE_FAILURE) {
        std::fprintf(stderr, "[encoder] won't start (is mediamtx running?)\n");
        stop();
        state_ = State::Backoff;
        retry_at_ = now + kBackoff;
        return;
    }
    state_ = State::Running;
    std::fprintf(stderr, "[encoder] started: %dx%d @ %d fps, %d kbps\n", cfg_.width, cfg_.height, cfg_.fps,
                 cfg_.bitrate_kbps);
}

void Encoder::stop() {
    if (pipeline_) {
        gst_element_set_state(pipeline_, GST_STATE_NULL);
        if (src_) gst_object_unref(src_);
        gst_object_unref(pipeline_);
    }
    pipeline_ = nullptr;
    src_ = nullptr;
    if (state_ == State::Running) state_ = State::Stopped;
}

void Encoder::poll(Clock::time_point now) {
    if (state_ == State::Stopped || (state_ == State::Backoff && now >= retry_at_)) {
        start(now);
        return;
    }
    if (state_ != State::Running || !pipeline_) return;
    bool failed = false;
    GstBus* bus = gst_element_get_bus(pipeline_);
    while (GstMessage* msg = gst_bus_pop_filtered(bus, static_cast<GstMessageType>(GST_MESSAGE_ERROR | GST_MESSAGE_EOS))) {
        if (GST_MESSAGE_TYPE(msg) == GST_MESSAGE_ERROR) {
            GError* err = nullptr;
            gst_message_parse_error(msg, &err, nullptr);
            std::fprintf(stderr, "[encoder] error: %s\n", err ? err->message : "?");
            g_clear_error(&err);
        }
        gst_message_unref(msg);
        failed = true;
    }
    gst_object_unref(bus);
    if (failed) {
        stop();
        ++restarts_;
        state_ = State::Backoff;
        retry_at_ = now + kBackoff;
    }
}

bool Encoder::push(FramePool::Frame* f) {
    if (!cfg_.encode) {
        FramePool::release(f);
        return true;
    }
    if (state_ != State::Running || !src_) {
        FramePool::release(f);
        return false;
    }
    GstBuffer* buf = gst_buffer_new_wrapped_full(
        GST_MEMORY_FLAG_READONLY, f->host, f->bytes, 0, f->bytes, f,
        [](gpointer p) { FramePool::release(static_cast<FramePool::Frame*>(p)); });
    // appsrc stamps it with the pipeline's running time (do-timestamp), which
    // stays right across encoder restarts
    GST_BUFFER_DURATION(buf) = gst_util_uint64_scale_int(GST_SECOND, 1, cfg_.fps);
    // Takes ownership; with leaky-type=downstream a full queue drops the oldest
    return gst_app_src_push_buffer(GST_APP_SRC(src_), buf) == GST_FLOW_OK;
}
