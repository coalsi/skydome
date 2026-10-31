#include "lut.h"

#include <cstring>
#include <fstream>
#include <stdexcept>

namespace {

template <typename T>
void read(std::ifstream& f, T* dst, size_t count, const std::string& path) {
    f.read(reinterpret_cast<char*>(dst), static_cast<std::streamsize>(sizeof(T) * count));
    if (!f) throw std::runtime_error(path + ": truncated stitch table");
}

}  // namespace

GridLut GridLut::load(const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) throw std::runtime_error(path + ": cannot open");
    char magic[8];
    read(f, magic, 8, path);
    if (std::memcmp(magic, "SKYLUT01", 8) != 0) throw std::runtime_error(path + ": not a SkyDome stitch table");

    uint32_t hdr[4];
    read(f, hdr, 4, path);
    GridLut lut;
    lut.size = static_cast<int>(hdr[0]);
    lut.step = static_cast<int>(hdr[1]);
    lut.nodes = static_cast<int>(hdr[2]);
    const uint32_t ncams = hdr[3];
    read(f, &lut.edge_zenith_deg, 1, path);
    if (ncams == 0 || ncams > 8 || lut.nodes < 2 || lut.size != (lut.nodes - 1) * lut.step)
        throw std::runtime_error(path + ": inconsistent header");

    for (uint32_t c = 0; c < ncams; ++c) {
        char name[17] = {};
        uint32_t wh[2];
        float feather;
        read(f, name, 16, path);
        read(f, wh, 2, path);
        read(f, &feather, 1, path);
        lut.cams.push_back({name, static_cast<int>(wh[0]), static_cast<int>(wh[1]), feather});
    }
    const size_t plane = size_t(ncams) * lut.nodes * lut.nodes;
    lut.u.resize(plane);
    lut.v.resize(plane);
    lut.mask.resize(size_t(lut.nodes - 1) * (lut.nodes - 1));
    read(f, lut.u.data(), plane, path);
    read(f, lut.v.data(), plane, path);
    read(f, lut.mask.data(), lut.mask.size(), path);
    return lut;
}

int GridLut::camera_index(const std::string& name) const {
    for (size_t i = 0; i < cams.size(); ++i)
        if (cams[i].name == name) return static_cast<int>(i);
    return -1;
}
