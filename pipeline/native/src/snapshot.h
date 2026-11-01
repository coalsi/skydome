// Full-resolution stills of the dome on request (ONVIF GetSnapshotUri,
// DECISIONS.md D-015/D-020): hardware JPEG (nvjpegenc) of an output frame.
//
// A client asks by creating PATH.request; on the next output frame the dome
// is copied to the encoder and the JPEG lands in PATH (written to PATH.tmp,
// then renamed, so readers never see half a file). Encoding runs on
// GStreamer's thread, so the output loop only pays for the copy.
#pragma once

#include <cstdint>
#include <string>

#include <gst/gst.h>

class Snapshot {
public:
    Snapshot(std::string path, int size);
    ~Snapshot();
    Snapshot(const Snapshot&) = delete;
    Snapshot& operator=(const Snapshot&) = delete;

    bool requested() const;                          // PATH.request exists
    void take(const uint8_t* i420, size_t bytes);    // clears the request

private:
    static GstFlowReturn on_sample(GstElement* sink, gpointer self);

    std::string path_, request_;
    int size_;
    GstElement* pipeline_ = nullptr;
    GstElement* src_ = nullptr;
};
