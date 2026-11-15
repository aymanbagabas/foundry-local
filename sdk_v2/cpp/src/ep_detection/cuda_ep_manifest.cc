// Copyright (c) Microsoft Corporation. All rights reserved.
// Licensed under the MIT License.
#include "ep_detection/cuda_ep_manifest.h"

#include <cstdint>
#include <string>
#include <utility>
#include <vector>

namespace fl {

namespace {

constexpr const char* kCdnBase = "https://foundrypackages-ffhrdhbxb7gpdreh.b02.azurefd.net/";
constexpr uint64_t kMiB = 1024ULL * 1024;

EpBundleArtifact Archive(std::string id, std::string filename, std::string sha256, uint64_t max_bytes,
                         std::vector<EpBundleFile> files) {
  return EpBundleArtifact{
      .id = std::move(id),
      .url = std::string(kCdnBase) + filename,
      .is_archive = true,
      .archive_sha256 = std::move(sha256),
      .extracted_files = std::move(files),
      .ignored_archive_paths = {"version.json"},
      .archive_max_bytes = max_bytes,
      .raw_relative_path = "",
      .raw_sha256 = "",
      .raw_max_bytes = 0,
  };
}

EpBundleManifest WindowsX64Manifest() {
  return EpBundleManifest{
      .bundle_id = "cuda-ep-win-x64-ort-1.32.0-genai-0.20.0-20261115-065910",
      .artifacts =
          {
              Archive(
                  "cuda-ep", "cuda-ep-bins-win-x64-20261115-065910.zip",
                  "c81044d84ca5057b2b06c399034525ae0d130980f9a16d3366a5800869172fb6", 320 * kMiB,
                  {
                      {.relative_path = "onnxruntime-genai-cuda.dll",
                       .sha256 = "f8079c81370c4849d14eacafe582c22f234c10b0993b838e7d6cd5bb3186e075"},
                      {.relative_path = "onnxruntime_providers_cuda.dll",
                       .sha256 = "5ea42368527fe22e6047618b1c6767eb39ffaefbbe6ac7dd613fbf1442d60d94"},
                  }),
          },
      .provider_relative_path = "onnxruntime_providers_cuda.dll",
  };
}

EpBundleManifest WindowsArm64Manifest() {
  return EpBundleManifest{
      .bundle_id = "cuda-ep-win-arm64-ort-1.32.0-genai-0.20.0-20261115-065941",
      .artifacts =
          {
              Archive(
                  "cuda-ep", "cuda-ep-bins-win-arm64-20261115-065941.zip",
                  "1cf5be1e45e6c085a33d05deb3fce75e2c648626dde41e630fb529bde473d038", 128 * kMiB,
                  {
                      {.relative_path = "onnxruntime-genai-cuda.dll",
                       .sha256 = "7932011f2b4920992982bc706d7d7c7db6da60885ad9cc26a1454826dc971f03"},
                      {.relative_path = "onnxruntime_providers_cuda.dll",
                       .sha256 = "7a56da0b543c35cc9b4f7cf2ccc158e5769c43f2f341990aaa42edde8dd8b0d3"},
                  }),
          },
      .provider_relative_path = "onnxruntime_providers_cuda.dll",
  };
}

EpBundleManifest LinuxX64Manifest() {
  return EpBundleManifest{
      .bundle_id = "cuda-ep-linux-x64-ort-1.32.0-genai-0.20.0-20261115-065948",
      .artifacts =
          {
              Archive(
                  "cuda-ep", "cuda-ep-linux-x64-20261115-065948.zip",
                  "6fc55699890e191d6290f39d2893dab24382534ec90aed0bd6beec5e8218e66b", 448 * kMiB,
                  {
                      {.relative_path = "libonnxruntime-genai-cuda.so",
                       .sha256 = "15611904f8bd5b3eccd620eecb25869d3d672f6f948c15312deb8dbaf146c7d7"},
                      {.relative_path = "libonnxruntime_providers_cuda.so",
                       .sha256 = "4e91c1a23733e5996392c74c0339753574c12806b385d0a1f03f6b63e3b80619"},
                  }),
          },
      .provider_relative_path = "libonnxruntime_providers_cuda.so",
  };
}

EpBundleManifest LinuxArm64Manifest() {
  return EpBundleManifest{
      .bundle_id = "cuda-ep-linux-arm64-ort-1.32.0-genai-0.20.0-20261115-070032",
      .artifacts =
          {
              Archive(
                  "cuda-ep", "cuda-ep-linux-arm64-20261115-070032.zip",
                  "7c53af4740da81baf4e4f531578b05c253b36340aadb0da2985a7f688c264126", 320 * kMiB,
                  {
                      {.relative_path = "libonnxruntime-genai-cuda.so",
                       .sha256 = "8c9c2232bae26ffc89d87bd753264d7b1741e97714dd99d7ad75d47881055924"},
                      {.relative_path = "libonnxruntime_providers_cuda.so",
                       .sha256 = "2dc1f76ca1f531eb25e3fda1a9c153e5aa4c400269a0bb47e940b91b9e45fc6a"},
                  }),
          },
      .provider_relative_path = "libonnxruntime_providers_cuda.so",
  };
}

}  // namespace

std::optional<EpBundleManifest> BuildCudaEpManifest(CudaEpPlatform platform) {
  switch (platform) {
    case CudaEpPlatform::WindowsX64:
      return WindowsX64Manifest();
    case CudaEpPlatform::WindowsArm64:
      return WindowsArm64Manifest();
    case CudaEpPlatform::LinuxX64:
      return LinuxX64Manifest();
    case CudaEpPlatform::LinuxArm64:
      return LinuxArm64Manifest();
    case CudaEpPlatform::Unsupported:
      return std::nullopt;
  }

  return std::nullopt;
}

}  // namespace fl
