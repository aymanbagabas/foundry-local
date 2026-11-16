// Copyright (c) Microsoft Corporation. All rights reserved.
// Licensed under the MIT License.
#pragma once

#include "addon_data.h"

#include <memory>

namespace foundry_local_node {

class EnvironmentWorker : public Napi::AsyncWorker {
 public:
  explicit EnvironmentWorker(Napi::Env env)
      : Napi::AsyncWorker(env), environment_(env.GetInstanceData<AddonData>()->worker_environment) {
    ++environment_->workers;
  }

  ~EnvironmentWorker() override { environment_->Retire(); }

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
  std::shared_ptr<WorkerEnvironment> environment_;
};

}  // namespace foundry_local_node
