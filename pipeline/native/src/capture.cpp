#include "capture.h"

#include "jpeg_check.h"

#include <cstdio>
#include <sys/stat.h>

#include <cudaEGL.h>
#include <gst/app/gstappsink.h>
#include <nvbufsurface.h>

namespace {

constexpr auto kStaleAfter = std::chrono::seconds(3);
constexpr auto kFirstFrameTimeout = std::chrono::seconds(10);
constexpr auto kBackoff = std::chrono::seconds(3);

bool device_exists(const std::string& path) {
    struct stat st{};
    return stat(path.c_str(), &st) == 0;
}

std::string pipeline_desc(const std::string& device, const CaptureFormat& f) {
    char buf[512];
    if (f.kind == CaptureFormat::Kind::MJPEG) {
        // Hardware JPEG decode straight into NVMM (Y42B for these cameras)
        std::snprintf(buf, sizeof buf,
                      "v4l2src name=src device=%s io-mode=2 do-timestamp=true "
                      "! image/jpeg,width=%d,height=%d,framerate=%d/1 "
                      "! nvv4l2decoder mjpeg=1 "
                      "! appsink name=sink max-buffers=1 drop=true sync=false",
                      device.c_str(), f.width, f.height, f.fps);
    } else {
        // 1080p YUY2 (night modes, handoff §2.1): VIC converts to planar I420 in NVMM
        std::snprintf(buf, sizeof buf,
                      "v4l2src device=%s io-mode=2 do-timestamp=true "
                      "! video/x-raw,format=YUY2,width=%d,height=%d,framerate=%d/1 "
                      "! nvvidconv ! video/x-raw(memory:NVMM),format=I420 "
                      "! appsink name=sink max-buffers=1 drop=true sync=false",
                      device.c_str(), f.width, f.height, f.fps);
    }
    return buf;
}

}  // namespace

// Drops damaged MJPEG frames before the decoder: decoded, they show up as
// streaks (F-010). The dome keeps the camera's last good frame meanwhile.
// A good frame is also where a raw download is saved from (request_raw).
GstPadProbeReturn CameraSource::check_frame(GstPad*, GstPadProbeInfo* info, gpointer self) {
    auto* cam = static_cast<CameraSource*>(self);
    GstBuffer* buf = GST_PAD_PROBE_INFO_BUFFER(info);
    if (!buf) return GST_PAD_PROBE_OK;
    GstMapInfo m;
    if (!gst_buffer_map(buf, &m, GST_MAP_READ)) return GST_PAD_PROBE_OK;
    const JpegCheck c = GST_BUFFER_FLAG_IS_SET(buf, GST_BUFFER_FLAG_CORRUPTED) ? JpegCheck::NoEnd
                                                                               : check_jpeg(m.data, m.size);
    if (c == JpegCheck::Ok && cam->raw_wanted_.exchange(false)) {
        const std::string tmp = cam->raw_path_ + ".tmp";
        if (FILE* f = std::fopen(tmp.c_str(), "wb")) {
            const bool ok = std::fwrite(m.data, 1, m.size, f) == m.size;
            if (std::fclose(f) == 0 && ok) std::rename(tmp.c_str(), cam->raw_path_.c_str());
        }
    }
    gst_buffer_unmap(buf, &m);
    if (c == JpegCheck::Ok) return GST_PAD_PROBE_OK;
    cam->dropped_.fetch_add(1);
    return GST_PAD_PROBE_DROP;
}

void CameraSource::request_raw(const std::string& base) {
    if (fmt_.kind != CaptureFormat::Kind::MJPEG || raw_wanted_.load()) return;
    raw_path_ = base + ".jpg";
    raw_wanted_.store(true);
}

CameraSource::CameraSource(std::string name, std::string device, CaptureFormat fmt)
    : name_(std::move(name)), device_(std::move(device)), fmt_(fmt) {}

CameraSource::~CameraSource() { stop(); }

const char* CameraSource::state() const {
    switch (state_) {
        case State::Waiting: return "waiting";
        case State::Running: return held_ ? "live" : "starting";
        case State::Backoff: return "retrying";
    }
    return "?";
}

uint64_t CameraSource::take_frame_count() {
    const uint64_t n = frames_;
    frames_ = 0;
    return n;
}

void CameraSource::start(Clock::time_point now) {
    GError* err = nullptr;
    pipeline_ = gst_parse_launch(pipeline_desc(device_, fmt_).c_str(), &err);
    if (!pipeline_) {
        std::fprintf(stderr, "[%s] pipeline: %s\n", name_.c_str(), err ? err->message : "?");
        g_clear_error(&err);
        state_ = State::Backoff;
        retry_at_ = now + kBackoff;
        return;
    }
    sink_ = gst_bin_get_by_name(GST_BIN(pipeline_), "sink");
    if (fmt_.kind == CaptureFormat::Kind::MJPEG) {
        GstElement* src = gst_bin_get_by_name(GST_BIN(pipeline_), "src");
        GstPad* pad = gst_element_get_static_pad(src, "src");
        gst_pad_add_probe(pad, GST_PAD_PROBE_TYPE_BUFFER, check_frame, this, nullptr);
        gst_object_unref(pad);
        gst_object_unref(src);
    }
    if (gst_element_set_state(pipeline_, GST_STATE_PLAYING) == GST_STATE_CHANGE_FAILURE) {
        std::fprintf(stderr, "[%s] %s won't start\n", name_.c_str(), device_.c_str());
        stop();
        state_ = State::Backoff;
        retry_at_ = now + kBackoff;
        return;
    }
    state_ = State::Running;
    started_ = now;
    std::fprintf(stderr, "[%s] started on %s\n", name_.c_str(), device_.c_str());
}

void CameraSource::stop() {
    release_frame();
    if (pipeline_) {
        gst_element_set_state(pipeline_, GST_STATE_NULL);
        if (sink_) gst_object_unref(sink_);
        gst_object_unref(pipeline_);
    }
    pipeline_ = nullptr;
    sink_ = nullptr;
}

void CameraSource::release_frame() {
    if (!held_) return;
    if (res_) cuGraphicsUnregisterResource(res_);
    res_ = nullptr;
    if (surf_) NvBufSurfaceUnMapEglImage(static_cast<NvBufSurface*>(surf_), 0);
    surf_ = nullptr;
    gst_buffer_unmap(gst_sample_get_buffer(held_), &map_);
    gst_sample_unref(held_);
    held_ = nullptr;
    image_ = PlanarImage{};
}

bool CameraSource::hold(GstSample* sample) {
    release_frame();
    GstBuffer* buf = gst_sample_get_buffer(sample);
    if (!buf || !gst_buffer_map(buf, &map_, GST_MAP_READ)) {
        gst_sample_unref(sample);
        return false;
    }
    held_ = sample;
    auto* surf = reinterpret_cast<NvBufSurface*>(map_.data);
    const NvBufSurfaceParams& sp = surf->surfaceList[0];
    if (sp.planeParams.num_planes != 3 || sp.layout != NVBUF_LAYOUT_PITCH) {
        std::fprintf(stderr, "[%s] unexpected decoder output (planes %u, layout %d)\n", name_.c_str(),
                     sp.planeParams.num_planes, sp.layout);
        release_frame();
        return false;
    }
    if (NvBufSurfaceMapEglImage(surf, 0) != 0) {
        release_frame();
        return false;
    }
    surf_ = surf;
    CUeglFrame ef;
    if (cuGraphicsEGLRegisterImage(&res_, static_cast<EGLImageKHR>(sp.mappedAddr.eglImage),
                                   CU_GRAPHICS_MAP_RESOURCE_FLAGS_READ_ONLY) != CUDA_SUCCESS ||
        cuGraphicsResourceGetMappedEglFrame(&ef, res_, 0, 0) != CUDA_SUCCESS) {
        std::fprintf(stderr, "[%s] EGL->CUDA mapping failed\n", name_.c_str());
        release_frame();
        return false;
    }
    for (int k = 0; k < 3; ++k) {
        image_.plane[k] = static_cast<const uint8_t*>(ef.frame.pPitch[k]);
        image_.pitch[k] = static_cast<int>(sp.planeParams.pitch[k]);
        image_.width[k] = static_cast<int>(sp.planeParams.width[k]);
        image_.height[k] = static_cast<int>(sp.planeParams.height[k]);
    }
    return true;
}

void CameraSource::poll(Clock::time_point now) {
    if (state_ == State::Waiting || state_ == State::Backoff) {
        if (state_ == State::Backoff && now < retry_at_) return;
        if (!device_exists(device_)) {
            state_ = State::Waiting;
            return;
        }
        start(now);
        return;
    }

    // Running: bus first, so an unplug is seen even with a frame still held
    bool failed = false;
    GstBus* bus = gst_element_get_bus(pipeline_);
    while (GstMessage* msg = gst_bus_pop_filtered(bus, static_cast<GstMessageType>(GST_MESSAGE_ERROR | GST_MESSAGE_EOS))) {
        if (GST_MESSAGE_TYPE(msg) == GST_MESSAGE_ERROR) {
            GError* err = nullptr;
            gst_message_parse_error(msg, &err, nullptr);
            std::fprintf(stderr, "[%s] error: %s\n", name_.c_str(), err ? err->message : "?");
            g_clear_error(&err);
        } else {
            std::fprintf(stderr, "[%s] end of stream\n", name_.c_str());
        }
        gst_message_unref(msg);
        failed = true;
    }
    gst_object_unref(bus);

    if (!failed) {
        // Keep only the newest frame
        GstSample* newest = nullptr;
        while (GstSample* s = gst_app_sink_try_pull_sample(GST_APP_SINK(sink_), 0)) {
            if (newest) gst_sample_unref(newest);
            newest = s;
        }
        if (newest && hold(newest)) {
            ++frames_;
            ++seq_;
            last_frame_ = now;
        }
        const bool stale = held_ ? now - last_frame_ > kStaleAfter : now - started_ > kFirstFrameTimeout;
        if (stale) {
            std::fprintf(stderr, "[%s] no frames, restarting\n", name_.c_str());
            failed = true;
        }
    }
    if (failed) {
        stop();
        ++restarts_;
        state_ = State::Backoff;
        retry_at_ = now + kBackoff;
    }
}
