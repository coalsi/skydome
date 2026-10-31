// Stitch tables written by pipeline/skydome_pipeline/gpu_lut.py (GridLUT.save).
#pragma once

#include <cstdint>
#include <string>
#include <vector>

struct GridLut {
    struct Cam {
        std::string name;
        int width = 0;       // the camera resolution u and v refer to
        int height = 0;
        float feather = 0;   // blend ramp at the image border, in those pixels
    };

    int size = 0;            // dome canvas, pixels per side
    int step = 0;            // grid step in canvas pixels
    int nodes = 0;           // grid nodes per side
    float edge_zenith_deg = 90;
    std::vector<Cam> cams;
    std::vector<float> u, v;     // [cam][node_y][node_x], NaN = ray misses the lens
    std::vector<uint8_t> mask;   // [cell_y][cell_x], bit c = camera c can reach the cell

    // Throws std::runtime_error on a bad or truncated file
    static GridLut load(const std::string& path);

    int camera_index(const std::string& name) const;   // -1 if absent
};
