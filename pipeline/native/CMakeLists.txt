# Live dome pipeline (DECISIONS.md D-016). Builds on the Jetson only:
#
#   cmake -S pipeline/native -B pipeline/native/build -DCMAKE_BUILD_TYPE=Release
#   cmake --build pipeline/native/build -j
cmake_minimum_required(VERSION 3.24)
set(CMAKE_CUDA_COMPILER /usr/local/cuda/bin/nvcc CACHE FILEPATH "")
set(CMAKE_CUDA_ARCHITECTURES 87)   # Orin
project(skydome_native LANGUAGES CXX CUDA)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CUDA_STANDARD 17)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

find_package(CUDAToolkit REQUIRED)
find_package(PkgConfig REQUIRED)
pkg_check_modules(GST REQUIRED IMPORTED_TARGET gstreamer-1.0 gstreamer-app-1.0)
pkg_check_modules(EGL REQUIRED IMPORTED_TARGET egl)

set(MMAPI /usr/src/jetson_multimedia_api CACHE PATH "Jetson Multimedia API")
find_library(NVBUFSURFACE nvbufsurface PATHS /usr/lib/aarch64-linux-gnu/nvidia REQUIRED)

# The CUDA kernel and table loader, free of GStreamer so nvcc stays happy
add_library(skydome_stitch STATIC src/lut.cpp src/stitch.cu)
target_include_directories(skydome_stitch PUBLIC src)
target_link_libraries(skydome_stitch PUBLIC CUDA::cudart)
target_compile_options(skydome_stitch PRIVATE $<$<COMPILE_LANGUAGE:CUDA>:-O3 --use_fast_math>)

add_executable(skydome-live src/main.cpp src/capture.cpp src/encoder.cpp src/jpeg_check.cpp)
target_include_directories(skydome-live PRIVATE ${MMAPI}/include)
target_link_libraries(skydome-live PRIVATE skydome_stitch PkgConfig::GST PkgConfig::EGL CUDA::cuda_driver ${NVBUFSURFACE})
target_compile_options(skydome-live PRIVATE -Wall -Wextra -O2)

add_executable(skydome-stitch-files src/stitch_files.cpp)
target_link_libraries(skydome-stitch-files PRIVATE skydome_stitch)

add_executable(skydome-jpeg-check src/jpeg_check_main.cpp src/jpeg_check.cpp)
