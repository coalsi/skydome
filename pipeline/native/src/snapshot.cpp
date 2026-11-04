#include "snapshot.h"

#include <cstdio>
#include <stdexcept>
#include <sys/stat.h>
#include <unistd.h>

#include <gst/app/gstappsink.h>
#include <gst/app/gstappsrc.h>

Snapshot::Snapshot(std::string path, int width, int height)
    : path_(std::move(path)), request_(path_ + ".request"), width_(width), height_(height) {
    char desc[512];
    std::snprintf(desc, sizeof desc,
                  "appsrc name=src format=time block=false max-buffers=1 leaky-type=downstream "
                  "caps=video/x-raw,format=I420,width=%d,height=%d,framerate=0/1 "
                  "! nvjpegenc quality=90 ! appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false",
                  width_, height_);
    GError* err = nullptr;
    pipeline_ = gst_parse_launch(desc, &err);
    if (!pipeline_) {
        std::string msg = err ? err->message : "?";
        g_clear_error(&err);
        throw std::runtime_error("snapshot pipeline: " + msg);
    }
    src_ = gst_bin_get_by_name(GST_BIN(pipeline_), "src");
    GstElement* sink = gst_bin_get_by_name(GST_BIN(pipeline_), "sink");
    g_signal_connect(sink, "new-sample", G_CALLBACK(on_sample), this);
    gst_object_unref(sink);
    if (gst_element_set_state(pipeline_, GST_STATE_PLAYING) == GST_STATE_CHANGE_FAILURE)
        throw std::runtime_error("snapshot pipeline won't start");
    unlink(request_.c_str());   // a request left over from before a restart
}

Snapshot::~Snapshot() {
    gst_element_set_state(pipeline_, GST_STATE_NULL);
    gst_object_unref(src_);
    gst_object_unref(pipeline_);
}

bool Snapshot::requested() const {
    struct stat st{};
    return stat(request_.c_str(), &st) == 0;
}

void Snapshot::take(const uint8_t* i420, size_t bytes) {
    unlink(request_.c_str());
    GstBuffer* buf = gst_buffer_new_allocate(nullptr, bytes, nullptr);
    gst_buffer_fill(buf, 0, i420, bytes);
    gst_app_src_push_buffer(GST_APP_SRC(src_), buf);
}

GstFlowReturn Snapshot::on_sample(GstElement* sink, gpointer self) {
    auto* s = static_cast<Snapshot*>(self);
    GstSample* sample = gst_app_sink_pull_sample(GST_APP_SINK(sink));
    if (!sample) return GST_FLOW_OK;
    GstBuffer* buf = gst_sample_get_buffer(sample);
    GstMapInfo m;
    if (buf && gst_buffer_map(buf, &m, GST_MAP_READ)) {
        const std::string tmp = s->path_ + ".tmp";
        if (FILE* f = std::fopen(tmp.c_str(), "wb")) {
            const bool ok = std::fwrite(m.data, 1, m.size, f) == m.size;
            if (std::fclose(f) == 0 && ok) std::rename(tmp.c_str(), s->path_.c_str());
        }
        gst_buffer_unmap(buf, &m);
    }
    gst_sample_unref(sample);
    return GST_FLOW_OK;
}
