// Copyright (c) Microsoft Corporation. All rights reserved.
// Licensed under the MIT License.
// Test-only runtime double. The production addon never calls the ABI directly.
#include <foundry_local/foundry_local_c.h>

#include <cstdlib>
#include <string>

struct flConfiguration {};
struct flManager {};
struct flCatalog {};
struct flKeyValuePairs {};
struct flStatus {
  flErrorCode code;
  std::string message;
};

namespace {
flConfiguration configuration;
flManager manager;
flCatalog catalog;
flKeyValuePairs options;

flStatus* FL_API_CALL CreateStatus(flErrorCode code, const char* message) noexcept {
  return new flStatus{code, message};
}
void FL_API_CALL ReleaseStatus(flStatus* status) noexcept { delete status; }
flErrorCode FL_API_CALL StatusCode(const flStatus* status) noexcept { return status->code; }
const char* FL_API_CALL StatusMessage(const flStatus* status) noexcept { return status->message.c_str(); }
flStatus* FL_API_CALL CreateConfiguration(const char*, flConfiguration** output) noexcept {
  *output = &configuration;
  return nullptr;
}
void FL_API_CALL ReleaseConfiguration(flConfiguration*) noexcept {}
flStatus* FL_API_CALL SetOptions(flConfiguration*, const flKeyValuePairs*) noexcept { return nullptr; }
flStatus* FL_API_CALL SetTransport(flConfiguration*, const flHttpTransport*) noexcept {
  std::abort();  // Negotiation must reject before touching slot 12 in an older table.
}
const flConfigurationApi* FL_API_CALL ConfigurationApi() noexcept {
  static const flConfigurationApi api = [] {
    flConfigurationApi table{};
    table.Create = CreateConfiguration;
    table.Configuration_Release = ReleaseConfiguration;
    table.SetAdditionalOptions = SetOptions;
    table.SetModelHttpTransport = SetTransport;
    return table;
  }();
  return &api;
}
void FL_API_CALL CreateOptions(flKeyValuePairs** output) noexcept { *output = &options; }
void FL_API_CALL AddOption(flKeyValuePairs*, const char*, const char*) noexcept {}
void FL_API_CALL ReleaseOptions(flKeyValuePairs*) noexcept {}
flStatus* FL_API_CALL CreateManager(const flConfiguration*, flManager** output) noexcept {
  *output = &manager;
  return nullptr;
}
void FL_API_CALL ReleaseManager(flManager*) noexcept {}
flStatus* FL_API_CALL GetCatalog(const flManager*, flCatalogType, flCatalog** output) noexcept {
  *output = &catalog;
  return nullptr;
}
}  // namespace

extern "C" const flApi* FL_API_CALL FoundryLocalGetApi(uint32_t version) noexcept {
  static const flApi api = [] {
    flApi table{};
    table.GetConfigurationApi = ConfigurationApi;
    table.CreateKeyValuePairs = CreateOptions;
    table.AddKeyValuePair = AddOption;
    table.KeyValuePairs_Release = ReleaseOptions;
    table.Manager_Create = CreateManager;
    table.Manager_Release = ReleaseManager;
    table.Manager_GetCatalogByType = GetCatalog;
    table.Status_Create = CreateStatus;
    table.Status_Release = ReleaseStatus;
    table.Status_GetErrorCode = StatusCode;
    table.Status_GetErrorMessage = StatusMessage;
    return table;
  }();
  const auto* maximum = std::getenv("FOUNDRY_TEST_OLD_API");
  return maximum && version <= std::strtoul(maximum, nullptr, 10) ? &api : nullptr;
}

extern "C" const char* FL_API_CALL FoundryLocalGetVersionString() noexcept { return "old-js-test-runtime"; }
