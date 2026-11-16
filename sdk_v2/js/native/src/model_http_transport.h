// Copyright (c) Microsoft Corporation. All rights reserved.
// Licensed under the MIT License.
#pragma once

#include <napi.h>
#include <foundry_local/foundry_local_cpp.h>

namespace foundry_local_node {

// Registers a config-owned bridge. The callback is rooted by its TSFN, not by a FunctionReference.
void SetModelHttpTransport(Napi::Env env, foundry_local::Configuration& config, Napi::Function callback);

// Sync entries must reject before any wrapper call which could wait for JavaScript transport work.
bool RejectSyncModelHttpTransport(Napi::Env env, bool configured);

}  // namespace foundry_local_node
