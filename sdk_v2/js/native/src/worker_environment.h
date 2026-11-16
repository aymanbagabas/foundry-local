// Copyright (c) Microsoft Corporation. All rights reserved.
// Licensed under the MIT License.
#pragma once

#include <napi.h>
#include <uv.h>

#include <atomic>
#include <cstddef>
#include <functional>
#include <list>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <thread>
#include <utility>
#include <vector>

namespace foundry_local_node {

enum class ExecutionPolicy {
  Libuv,
  Dedicated,
};

inline ExecutionPolicy TransportExecutionPolicy(bool configured) {
  return configured ? ExecutionPolicy::Dedicated : ExecutionPolicy::Libuv;
}

// The environment thread owns admission/retirement and the hook. Native callbacks only read closing.
struct WorkerEnvironment : std::enable_shared_from_this<WorkerEnvironment> {
  std::atomic_bool closing{false};
  size_t workers = 0;
  bool cleanup_started = false;
  napi_async_cleanup_hook_handle cleanup = nullptr;
  napi_ref js_probe = nullptr;
  napi_env env = nullptr;

  void Install(Napi::Env environment) {
    env = environment;
    uv_loop_t* loop = nullptr;
    if (napi_get_uv_event_loop(env, &loop) != napi_ok) {
      throw std::runtime_error("Unable to get native worker event loop");
    }
    completion_ = new uv_async_t{};
    auto* root = new std::shared_ptr<WorkerEnvironment>(shared_from_this());
    completion_->data = root;
    if (uv_async_init(loop, completion_, [](uv_async_t* handle) {
          auto state = *static_cast<std::shared_ptr<WorkerEnvironment>*>(handle->data);
          state->Drain();
        }) != 0) {
      delete root;
      delete completion_;
      completion_ = nullptr;
      throw std::runtime_error("Unable to create native worker completion queue");
    }
    uv_unref(reinterpret_cast<uv_handle_t*>(completion_));
    try {
      auto probe = Napi::Object::New(environment);
      probe.Set("active", true);
      if (napi_create_reference(env, probe, 1, &js_probe) != napi_ok) {
        throw std::runtime_error("Unable to create native worker environment probe");
      }
      if (napi_add_async_cleanup_hook(
              env,
              [](napi_async_cleanup_hook_handle, void* data) {
                auto* state = static_cast<WorkerEnvironment*>(data);
                state->cleanup_started = true;
                state->BeginShutdown();
                state->FinishCleanup();
              },
              this, &cleanup) != napi_ok) {
        throw std::runtime_error("Unable to register native worker cleanup hook");
      }
    } catch (...) {
      if (js_probe) napi_delete_reference(env, js_probe);
      js_probe = nullptr;
      CloseQueue();
      throw;
    }
  }

  // Environment-thread only. Each bridge owns its registration; stale weak registrations cannot retain a manager.
  void RegisterShutdown(const std::shared_ptr<std::function<void()>>& stop) {
    if (closing.load(std::memory_order_acquire)) {
      (*stop)();
      return;
    }
    for (auto it = stops_.begin(); it != stops_.end();) {
      if (it->expired()) {
        it = stops_.erase(it);
      } else {
        ++it;
      }
    }
    stops_.push_back(stop);
  }

  void BeginShutdown() noexcept {
    closing.store(true, std::memory_order_release);
    if (shutdown_notified_) return;
    shutdown_notified_ = true;
    for (auto& weak : stops_) {
      if (auto stop = weak.lock()) (*stop)();
    }
  }

  void QueueDedicated(std::function<void()> execute, std::function<void()> complete) {
    if (CheckClosing()) throw std::runtime_error("Native worker environment is closing");
    auto job = std::make_unique<DedicatedJob>();
    job->complete = std::move(complete);
    auto* pending = job.get();
    jobs_.push_back(std::move(job));
    try {
      pending->thread = std::thread([this, pending, execute = std::move(execute)] {
        execute();  // Workers catch native exceptions; this never enters N-API or V8.
        {
          std::lock_guard lock(completed_mutex_);
          pending->next = completed_;
          completed_ = pending;
        }
        uv_async_send(completion_);
      });
    } catch (...) {
      jobs_.pop_back();
      throw;
    }
    uv_ref(reinterpret_cast<uv_handle_t*>(completion_));
  }

  bool CheckClosing() noexcept {
    if (closing.load(std::memory_order_acquire)) return true;
    napi_value probe;
    napi_value ignored;
    if (napi_get_reference_value(env, js_probe, &probe) != napi_ok) {
      BeginShutdown();
      return true;
    }
    // Unlike primitive creation, property access checks whether Node still permits JS before touching V8. Node 22
    // drains async work before cleanup hooks; this private ordinary data property never invokes user JavaScript.
    const auto status = napi_get_named_property(env, probe, "active", &ignored);
    bool pending = false;
    napi_is_exception_pending(env, &pending);
    // N-API <10 reports cannot-run-JS as pending_exception without an actual pending exception.
    if (status == napi_closing || (status == napi_pending_exception && !pending)) {
      BeginShutdown();
    }
    return closing.load(std::memory_order_acquire);
  }

  void Retire() noexcept {
    --workers;
    FinishCleanup();
  }

 private:
  struct DedicatedJob {
    std::thread thread;
    std::function<void()> complete;
    DedicatedJob* next = nullptr;
  };

  void Drain() noexcept {
    DedicatedJob* ready;
    {
      std::lock_guard lock(completed_mutex_);
      ready = completed_;
      completed_ = nullptr;
    }
    while (ready) {
      auto* job = ready;
      ready = job->next;
      job->thread.join();
      // Remove the registration before completion can retire the last worker and begin closing the queue.
      auto it = jobs_.begin();
      while (it->get() != job) ++it;
      auto owned = std::move(*it);
      jobs_.erase(it);
      owned->complete();
    }
    if (jobs_.empty() && completion_) uv_unref(reinterpret_cast<uv_handle_t*>(completion_));
    FinishCleanup();
  }

  void FinishCleanup() noexcept {
    if (cleanup_started && workers == 0 && cleanup != nullptr) {
      if (completion_) {
        CloseQueue();
        return;
      }
      // uv_close must finish before the async cleanup hook lets Node destroy its loop.
      if (!queue_closed_) return;
      napi_delete_reference(env, js_probe);
      js_probe = nullptr;
      napi_remove_async_cleanup_hook(cleanup);
      cleanup = nullptr;
    }
  }

  void CloseQueue() noexcept {
    auto* handle = completion_;
    completion_ = nullptr;
    uv_close(reinterpret_cast<uv_handle_t*>(handle), [](uv_handle_t* handle) {
      auto* root = static_cast<std::shared_ptr<WorkerEnvironment>*>(handle->data);
      auto state = *root;
      delete root;
      delete reinterpret_cast<uv_async_t*>(handle);
      state->queue_closed_ = true;
      state->FinishCleanup();
    });
  }

  uv_async_t* completion_ = nullptr;
  bool queue_closed_ = false;
  bool shutdown_notified_ = false;
  std::list<std::unique_ptr<DedicatedJob>> jobs_;
  std::mutex completed_mutex_;
  DedicatedJob* completed_ = nullptr;
  std::vector<std::weak_ptr<std::function<void()>>> stops_;
};

}  // namespace foundry_local_node
