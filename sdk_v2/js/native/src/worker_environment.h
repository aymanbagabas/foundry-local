// Copyright (c) Microsoft Corporation. All rights reserved.
// Licensed under the MIT License.
#pragma once

#include <napi.h>

#include <atomic>
#include <cstddef>
#include <stdexcept>

namespace foundry_local_node {

// The environment thread owns admission/retirement and the hook. Native callbacks only read closing.
struct WorkerEnvironment {
  std::atomic_bool closing{false};
  size_t workers = 0;
  bool cleanup_started = false;
  napi_async_cleanup_hook_handle cleanup = nullptr;
  napi_ref js_probe = nullptr;
  napi_env env = nullptr;

  void Install(Napi::Env environment) {
    env = environment;
    auto probe = Napi::Object::New(environment);
    probe.Set("active", true);
    if (napi_create_reference(env, probe, 1, &js_probe) != napi_ok) {
      throw std::runtime_error("Unable to create native worker environment probe");
    }
    if (napi_add_async_cleanup_hook(
            env,
            [](napi_async_cleanup_hook_handle, void* data) {
              auto* state = static_cast<WorkerEnvironment*>(data);
              state->closing.store(true, std::memory_order_release);
              state->cleanup_started = true;
              state->FinishCleanup();
            },
            this, &cleanup) != napi_ok) {
      napi_delete_reference(env, js_probe);
      js_probe = nullptr;
      throw std::runtime_error("Unable to register native worker cleanup hook");
    }
  }

  bool CheckClosing() noexcept {
    if (closing.load(std::memory_order_acquire)) return true;
    napi_value probe;
    napi_value ignored;
    if (napi_get_reference_value(env, js_probe, &probe) != napi_ok) {
      closing.store(true, std::memory_order_release);
      return true;
    }
    // Unlike primitive creation, property access checks whether Node still permits JS before touching V8. Node 22
    // drains async work before cleanup hooks; this private ordinary data property never invokes user JavaScript.
    const auto status = napi_get_named_property(env, probe, "active", &ignored);
    bool pending = false;
    napi_is_exception_pending(env, &pending);
    // N-API <10 reports cannot-run-JS as pending_exception without an actual pending exception.
    if (status == napi_closing || (status == napi_pending_exception && !pending)) {
      closing.store(true, std::memory_order_release);
    }
    return closing.load(std::memory_order_acquire);
  }

  void Retire() noexcept {
    --workers;
    FinishCleanup();
  }

 private:
  void FinishCleanup() noexcept {
    if (cleanup_started && workers == 0 && cleanup != nullptr) {
      napi_delete_reference(env, js_probe);
      js_probe = nullptr;
      napi_remove_async_cleanup_hook(cleanup);
      cleanup = nullptr;
    }
  }
};

}  // namespace foundry_local_node
