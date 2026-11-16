// Copyright (c) Microsoft Corporation. All rights reserved.
// Licensed under the MIT License.
#include <foundry_local/foundry_local_cpp.h>

#include <iostream>
#include <vector>

struct flConfiguration {};
struct flKeyValuePairs {};
struct flStatus {
  flErrorCode code;
  const char* message;
};

namespace {

flConfiguration configuration;
flKeyValuePairs options;
int configurations_created = 0;
int configurations_released = 0;
int options_applied = 0;
uint32_t runtime_api_version = 2;
std::vector<uint32_t> requested_versions;
flStatus callback_status{};
int statuses_created = 0;
int statuses_released = 0;

flStatus* FL_API_CALL CreateStatus(flErrorCode code, const char* message) noexcept {
  ++statuses_created;
  callback_status = {code, message};
  return &callback_status;
}

void FL_API_CALL ReleaseStatus(flStatus*) noexcept { ++statuses_released; }

flErrorCode FL_API_CALL StatusCode(const flStatus* status) noexcept { return status->code; }

const char* FL_API_CALL StatusMessage(const flStatus* status) noexcept { return status->message; }

flStatus* FL_API_CALL CreateConfiguration(const char*, flConfiguration** output) noexcept {
  ++configurations_created;
  *output = &configuration;
  return nullptr;
}

void FL_API_CALL ReleaseConfiguration(flConfiguration*) noexcept { ++configurations_released; }

flStatus* FL_API_CALL SetOptions(flConfiguration*, const flKeyValuePairs*) noexcept {
  ++options_applied;
  return nullptr;
}

flStatus* FL_API_CALL SetSharedHost(flConfiguration*, bool) noexcept { return nullptr; }

const flConfigurationApi* FL_API_CALL ConfigurationApi() noexcept {
  static const flConfigurationApi api = [] {
    flConfigurationApi table{};
    table.Create = CreateConfiguration;
    table.Configuration_Release = ReleaseConfiguration;
    table.SetAdditionalOptions = SetOptions;
    table.SetUseSharedHost = SetSharedHost;
    return table;
  }();
  return &api;
}

void FL_API_CALL CreateOptions(flKeyValuePairs** output) noexcept { *output = &options; }

void FL_API_CALL AddOption(flKeyValuePairs*, const char*, const char*) noexcept {}
void FL_API_CALL ReleaseOptions(flKeyValuePairs*) noexcept {}

}  // namespace

extern "C" const flApi* FL_API_CALL FoundryLocalGetApi(uint32_t version) noexcept {
  requested_versions.push_back(version);
  static const flApi api = [] {
    flApi table{};
    table.GetConfigurationApi = ConfigurationApi;
    table.CreateKeyValuePairs = CreateOptions;
    table.AddKeyValuePair = AddOption;
    table.KeyValuePairs_Release = ReleaseOptions;
    table.Status_Create = CreateStatus;
    table.Status_Release = ReleaseStatus;
    table.Status_GetErrorCode = StatusCode;
    table.Status_GetErrorMessage = StatusMessage;
    return table;
  }();
  return version <= runtime_api_version ? &api : nullptr;
}

extern "C" const char* FL_API_CALL FoundryLocalGetVersionString() noexcept { return "v2-test-runtime"; }

int main(int argc, char** argv) {
  if (argc > 1) {
    runtime_api_version = static_cast<uint32_t>(std::strtoul(argv[1], nullptr, 10));
  }

  if (runtime_api_version == 0) {
    try {
      foundry_local::detail::api();
    } catch (const std::runtime_error&) {
      return requested_versions == std::vector<uint32_t>{5, 4, 3, 2} ? 0 : 1;
    }

    return 1;
  }

  try {
    if (foundry_local::detail::api_version() != runtime_api_version || !foundry_local::detail::api()) {
      throw std::runtime_error("C++ wrapper did not negotiate the highest available API");
    }

    std::vector<uint32_t> expected_versions;
    for (uint32_t version = 5; version >= runtime_api_version; --version) {
      expected_versions.push_back(version);
    }

    expected_versions.push_back(runtime_api_version);
    if (requested_versions != expected_versions) {
      throw std::runtime_error("C++ wrapper did not try API versions in descending order");
    }

    bool callback_error_received = false;
    try {
      foundry_local::Check(foundry_local::CreateStatus(FOUNDRY_LOCAL_ERROR_NETWORK, "callback network failure"));
    } catch (const foundry_local::Error& error) {
      callback_error_received =
          error.Code() == FOUNDRY_LOCAL_ERROR_NETWORK && std::string(error.what()) == "callback network failure";
    }

    if (!callback_error_received || statuses_created != 1 || statuses_released != 1) {
      throw std::runtime_error("C++ wrapper did not create and release the callback status in the negotiated runtime");
    }

    {
      foundry_local::Configuration config("v2-client");
      config.SetUseSharedHost(false);
      bool rejected = false;
      try {
        config.SetUseSharedHost(true);
      } catch (const foundry_local::Error& error) {
        rejected = error.Code() == FOUNDRY_LOCAL_ERROR_NOT_IMPLEMENTED;
      }

      if (rejected != (runtime_api_version < 3) || configurations_created != 1 || options_applied != 1) {
        throw std::runtime_error("v2 configuration failed or the v3-only setting did not fail explicitly");
      }

      rejected = false;
      try {
        config.SetModelHttpTransport(nullptr);
      } catch (const foundry_local::Error& error) {
        rejected = error.Code() == FOUNDRY_LOCAL_ERROR_NOT_IMPLEMENTED;
      }

      if (!rejected) {
        throw std::runtime_error("pre-v5 runtime must reject the v5-only transport setter without reading its table tail");
      }
    }

    if (configurations_released != 1) {
      throw std::runtime_error("v2 configuration was not released");
    }
  } catch (const std::exception& error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
