// Copyright (c) Microsoft Corporation. All rights reserved.
// Licensed under the MIT License.
#pragma once

#include "addon_data.h"

#include <memory>
#include <utility>

namespace foundry_local_node {

class EnvironmentWorker : public Napi::AsyncWorker {
 public:
  explicit EnvironmentWorker(Napi::Env env)
      : Napi::AsyncWorker(env), environment_(env.GetInstanceData<AddonData>()->worker_environment) {
    ++environment_->workers;
  }

  ~EnvironmentWorker() override { environment_->Retire(); }

  void Queue(ExecutionPolicy policy = ExecutionPolicy::Libuv) {
    if (environment_->CheckClosing()) throw std::runtime_error("Native worker environment is closing");
    if (policy == ExecutionPolicy::Libuv) {
      Napi::AsyncWorker::Queue();
      return;
    }
    // uv_async callbacks have neither a handle scope nor an N-API callback context. Supply both explicitly before
    // reusing AsyncWorker's completion/error conversion; the async-work object itself is never queued to libuv.
    auto context = std::make_shared<CompletionContext>(Env());
    auto complete = [this, context] {
      Napi::Env env = Env();
      Napi::HandleScope handles(env);
      if (CheckEnvironmentClosing()) {
        OnShutdown();
        Destroy();
        return;
      }
      Napi::CallbackScope callback(env, context->async);
      OnWorkComplete(env, napi_ok);
    };
    environment_->QueueDedicated([this] { Execute(); }, std::move(complete));
  }

 protected:
  bool EnvironmentClosing() const noexcept { return environment_->closing.load(std::memory_order_acquire); }
  bool CheckEnvironmentClosing() noexcept { return environment_->CheckClosing(); }  // Environment thread only.
  virtual void OnShutdown() {}

  void OnWorkComplete(Napi::Env env, napi_status status) override {
    // AsyncWorker::OnWorkComplete constructs JS errors before calling OnError. Guard before entering it at all.
    if (environment_->CheckClosing()) {
      OnShutdown();
      Destroy();
      return;
    }
    Napi::AsyncWorker::OnWorkComplete(env, status);
  }

 private:
  struct CompletionContext {
    explicit CompletionContext(Napi::Env env)
        : resource(Napi::Persistent(Napi::Object::New(env))),
          async(env, "FoundryLocalDedicatedWorker", resource.Value()) {}

    // napi_async_init does not root a supplied async resource. Keep it alive through callback-scope closure.
    Napi::ObjectReference resource;
    Napi::AsyncContext async;
  };

  std::shared_ptr<WorkerEnvironment> environment_;
};

}  // namespace foundry_local_node
