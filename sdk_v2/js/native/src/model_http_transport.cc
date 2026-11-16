// Copyright (c) Microsoft Corporation. All rights reserved.
// Licensed under the MIT License.
#include "model_http_transport.h"

#include "addon_data.h"
#include "errors.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstring>
#include <limits>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>
#include <vector>

namespace foundry_local_node {
namespace {

using Headers = std::vector<std::pair<std::string, std::string>>;
using Clock = std::chrono::steady_clock;
class Bridge;

struct Ticket {
  std::mutex mutex;
  std::condition_variable condition;
  bool done = false;
  flErrorCode error = FOUNDRY_LOCAL_OK;
  std::string message;
  uint32_t status = 0;
  Headers headers;
  std::vector<uint8_t> bytes;

  void Fail(flErrorCode code, const char* text) {
    std::lock_guard lock(mutex);
    if (done) return;
    error = code;
    message = text;
    done = true;
    condition.notify_all();
  }
};

struct Operation {
  std::shared_ptr<Bridge> bridge;
  uint64_t id;
  std::string method;
  std::string url;
  Headers request_headers;
  std::vector<uint8_t> body;
  uint64_t timeout_ms = 0;
  std::mutex mutex;
  bool cancelled = false;
  bool released = false;
  bool send_started = false;
  bool sent = false;
  bool eof = false;
  Clock::time_point deadline = Clock::time_point::max();
  std::shared_ptr<Ticket> pending;
  std::atomic_bool probe_pending{false};
  Headers response_headers;
  std::vector<flHttpHeader> response_views;

  bool Cancel() {
    std::lock_guard lock(mutex);
    if (cancelled) return false;
    cancelled = true;
    if (pending) pending->Fail(FOUNDRY_LOCAL_ERROR_OPERATION_CANCELLED, "Model HTTP operation cancelled");
    return true;
  }
};

struct Event {
  const char* type;
  std::shared_ptr<Operation> operation;
  std::shared_ptr<Ticket> ticket;
  size_t capacity = 0;
};

void ClearPendingException(napi_env env) noexcept {
  bool pending = false;
  if (napi_is_exception_pending(env, &pending) == napi_ok && pending) {
    napi_value ignored;
    napi_get_and_clear_last_exception(env, &ignored);
  }
}

bool IsHeaderName(const std::string& value) {
  if (value.empty()) return false;
  for (unsigned char ch : value) {
    if ((ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z') || (ch >= '0' && ch <= '9')) continue;
    if (std::strchr("!#$%&'*+-.^_`|~", ch) == nullptr || ch == 0) return false;
  }
  return true;
}

bool IsHeaderValue(const std::string& value) {
  return value.find_first_of(std::string("\0\r\n", 3)) == std::string::npos;
}

std::vector<uint8_t> ReadBytes(Napi::Value value) {
  if (!value.IsTypedArray()) throw std::invalid_argument("HTTP result must be a Uint8Array");
  auto array = value.As<Napi::TypedArray>();
  if (array.TypedArrayType() != napi_uint8_array) throw std::invalid_argument("HTTP result must be a Uint8Array");
  // Copy on the JS thread: settlement never retains a pointer into V8 or a native caller's stack.
  auto bytes = value.As<Napi::Uint8Array>();
  if (bytes.ByteLength() == 0) return {};
  return {bytes.Data(), bytes.Data() + bytes.ByteLength()};
}

Headers ReadHeaders(Napi::Value value) {
  if (!value.IsArray()) throw std::invalid_argument("HTTP headers must be an array");
  auto array = value.As<Napi::Array>();
  Headers headers;
  headers.reserve(array.Length());
  for (uint32_t i = 0; i < array.Length(); ++i) {
    auto entry = array.Get(i);
    if (!entry.IsArray()) throw std::invalid_argument("HTTP header must be a [name, value] pair");
    auto pair = entry.As<Napi::Array>();
    if (pair.Length() != 2 || !pair.Get(uint32_t{0}).IsString()) {
      throw std::invalid_argument("HTTP header must be a [name, value] pair");
    }
    auto name = pair.Get(uint32_t{0}).As<Napi::String>().Utf8Value();
    auto bytes = ReadBytes(pair.Get(uint32_t{1}));
    std::string text(bytes.begin(), bytes.end());
    if (!IsHeaderName(name) || !IsHeaderValue(text)) throw std::invalid_argument("Invalid HTTP header");
    headers.emplace_back(std::move(name), std::move(text));
  }
  return headers;
}

Napi::Uint8Array CopyBytes(Napi::Env env, const uint8_t* data, size_t size) {
  auto out = Napi::Uint8Array::New(env, size);
  if (size != 0) std::memcpy(out.Data(), data, size);
  return out;
}

Napi::Array CopyHeaders(Napi::Env env, const Headers& headers) {
  auto out = Napi::Array::New(env, headers.size());
  for (size_t i = 0; i < headers.size(); ++i) {
    auto pair = Napi::Array::New(env, 2);
    pair.Set(uint32_t{0}, headers[i].first);
    const auto& text = headers[i].second;
    pair.Set(uint32_t{1}, CopyBytes(env, reinterpret_cast<const uint8_t*>(text.data()), text.size()));
    out.Set(static_cast<uint32_t>(i), pair);
  }
  return out;
}

void Settle(const std::shared_ptr<Ticket>& ticket, const char* type, size_t capacity, Napi::Value value) {
  // Parsing is outside the lock; cancellation can wake the worker even during a malicious result getter.
  uint32_t status = 0;
  Headers headers;
  std::vector<uint8_t> bytes;
  if (std::strcmp(type, "send") == 0) {
    if (!value.IsObject()) throw std::invalid_argument("HTTP send must resolve to a response head");
    auto head = value.As<Napi::Object>();
    auto code = head.Get("statusCode");
    if (!code.IsNumber()) throw std::invalid_argument("HTTP statusCode must be an integer from 100 to 599");
    double number = code.As<Napi::Number>().DoubleValue();
    if (!std::isfinite(number) || number < 100 || number > 599 || number != static_cast<uint32_t>(number)) {
      throw std::invalid_argument("HTTP statusCode must be an integer from 100 to 599");
    }
    status = static_cast<uint32_t>(number);
    headers = ReadHeaders(head.Get("headers"));
  } else if (std::strcmp(type, "read") == 0) {
    bytes = ReadBytes(value);
    if (bytes.size() > capacity) throw std::invalid_argument("HTTP read exceeded requested capacity");
  }
  std::lock_guard lock(ticket->mutex);
  if (ticket->done) return;
  ticket->status = status;
  ticket->headers = std::move(headers);
  ticket->bytes = std::move(bytes);
  ticket->done = true;
  ticket->condition.notify_all();
}

void Reject(const std::shared_ptr<Ticket>& ticket, Napi::Value reason) {
  flErrorCode code = FOUNDRY_LOCAL_ERROR_NETWORK;
  std::string text = "Model HTTP transport failed";
  if (reason.IsObject()) {
    auto object = reason.As<Napi::Object>();
    auto name = object.Get("name");
    if (name.IsString() && name.As<Napi::String>().Utf8Value() == "AbortError") {
      code = FOUNDRY_LOCAL_ERROR_OPERATION_CANCELLED;
    }
    auto message = object.Get("message");
    if (message.IsString()) text = message.As<Napi::String>().Utf8Value();
  } else if (reason.IsString()) {
    text = reason.As<Napi::String>().Utf8Value();
  }
  ticket->Fail(code, text.c_str());
}

class Bridge {
 public:
  static std::shared_ptr<Bridge> New(Napi::Env env, Napi::Function callback) {
    auto bridge = std::shared_ptr<Bridge>(new Bridge());
    bridge->environment_ = env.GetInstanceData<AddonData>()->worker_environment;
    auto* root = new std::shared_ptr<Bridge>(bridge);
    auto resource = Napi::String::New(env, "ModelHttpTransport");
    napi_status status = napi_create_threadsafe_function(
        env, callback, nullptr, resource, 0, 1, root,
        [](napi_env, void* data, void*) {
          std::unique_ptr<std::shared_ptr<Bridge>> root(static_cast<std::shared_ptr<Bridge>*>(data));
          // TSFN finalization and async cleanup hooks both run on the environment thread.
          (*root)->FinalizeTsfn();
        },
        nullptr, CallJs, &bridge->tsfn_);
    if (status != napi_ok) {
      delete root;
      throw std::runtime_error("Unable to create model HTTP transport TSFN");
    }
    napi_unref_threadsafe_function(env, bridge->tsfn_);
    auto* cleanup = new std::shared_ptr<Bridge>(bridge);
    bridge->cleanup_root_ = cleanup;
    status = napi_add_async_cleanup_hook(
        env,
        [](napi_async_cleanup_hook_handle handle, void* data) {
          std::unique_ptr<std::shared_ptr<Bridge>> root(static_cast<std::shared_ptr<Bridge>*>(data));
          (*root)->cleanup_ = nullptr;
          (*root)->cleanup_root_ = nullptr;
          // Completions can run during RunCleanup as soon as Stop wakes native waits. Block JS before that happens.
          (*root)->environment_->BeginShutdown();
          // Waking native waiters never needs JS, Promise settlement, or a libuv worker-pool slot.
          (*root)->Stop(true);
          // The environment-wide worker hook stays registered until all admitted workers have retired.
          napi_remove_async_cleanup_hook(handle);
        },
        cleanup, &bridge->cleanup_);
    if (status != napi_ok) {
      delete cleanup;
      bridge->cleanup_root_ = nullptr;
      bridge->Stop(true);
      throw std::runtime_error("Unable to register model HTTP transport cleanup hook");
    }
    bridge->stop_ = std::make_shared<std::function<void()>>([weak = std::weak_ptr<Bridge>(bridge)] {
      if (auto active = weak.lock()) active->Stop(true);
    });
    bridge->environment_->RegisterShutdown(bridge->stop_);
    return bridge;
  }

  void Register(const std::shared_ptr<Operation>& operation) {
    std::lock_guard lock(mutex_);
    if (stopped_) {
      throw foundry_local::Error("Model HTTP environment is closing", FOUNDRY_LOCAL_ERROR_OPERATION_CANCELLED);
    }
    // Drop expired entries so repeated downloads do not accumulate a per-request weak allocation.
    for (auto it = operations_.begin(); it != operations_.end();) {
      if (it->expired()) {
        it = operations_.erase(it);
      } else {
        ++it;
      }
    }
    operations_.push_back(operation);
  }

  bool Schedule(Event event) {
    auto queued = std::make_unique<Event>(std::move(event));
    std::lock_guard lock(mutex_);
    if (stopped_) return false;
    auto status = napi_call_threadsafe_function(tsfn_, queued.get(), napi_tsfn_nonblocking);
    if (status != napi_ok) return false;
    queued.release();
    return true;
  }

  void Stop(bool abort, bool release_tsfn = true) noexcept {
    std::vector<std::weak_ptr<Operation>> operations;
    {
      std::lock_guard lock(mutex_);
      if (stopped_) return;
      stopped_ = true;
      operations.swap(operations_);
      if (release_tsfn) napi_release_threadsafe_function(tsfn_, abort ? napi_tsfn_abort : napi_tsfn_release);
    }
    for (auto& weak : operations) {
      if (auto operation = weak.lock()) operation->Cancel();
    }
  }

  bool IsJsThread() const { return std::this_thread::get_id() == js_thread_; }

 private:
  Bridge() = default;

  void FinalizeTsfn() noexcept {
    bool forced;
    {
      std::lock_guard lock(mutex_);
      forced = !stopped_;
    }
    if (forced) {
      // Node may finalize the TSFN before our async hook runs. Its already-delivered tickets still need waking.
      Stop(true, false);  // The TSFN itself is already finalized: never release it again.
      environment_->BeginShutdown();
    }
    RemoveCleanupHook();
  }

  void RemoveCleanupHook() noexcept {
    if (!cleanup_) return;
    napi_remove_async_cleanup_hook(cleanup_);
    cleanup_ = nullptr;
    delete cleanup_root_;
    cleanup_root_ = nullptr;
  }

  struct ActiveJsOperation {
    Bridge& bridge;
    napi_env env;
    bool closing;

    ActiveJsOperation(Bridge& bridge, napi_env env, const char* type)
        : bridge(bridge), env(env), closing(std::strcmp(type, "close") == 0) {
      if (std::strcmp(type, "create") == 0 && bridge.active_js_operations_++ == 0) {
        napi_ref_threadsafe_function(env, bridge.tsfn_);
      }
    }

    ~ActiveJsOperation() {
      if (closing && --bridge.active_js_operations_ == 0) napi_unref_threadsafe_function(env, bridge.tsfn_);
    }
  };

  static void CallJs(napi_env raw_env, napi_value raw_callback, void*, void* data) noexcept {
    std::unique_ptr<Event> event(static_cast<Event*>(data));
    auto environment = event->operation->bridge->environment_;
    if (!raw_env || !raw_callback) environment->BeginShutdown();
    if (!raw_env || !raw_callback || environment->CheckClosing()) {
      // Some Node releases drain native work before running cleanup hooks. Stop from this TSFN turn instead.
      event->operation->bridge->Stop(true);
      if (event->ticket) event->ticket->Fail(FOUNDRY_LOCAL_ERROR_OPERATION_CANCELLED, "JavaScript environment closed");
      return;
    }
    if (event->type == nullptr) {
      event->operation->probe_pending.store(false, std::memory_order_release);
      return;
    }
    Napi::Env env(raw_env);
    auto ticket = event->ticket;
    try {
      Napi::HandleScope scope(env);
      ActiveJsOperation active(*event->operation->bridge, raw_env, event->type);
      auto object = Napi::Object::New(env);
      object.Set("type", event->type);
      object.Set("operationId", Napi::Number::New(env, static_cast<double>(event->operation->id)));
      if (std::strcmp(event->type, "create") == 0) {
        const auto& operation = *event->operation;
        auto request = Napi::Object::New(env);
        request.Set("method", operation.method);
        request.Set("url", operation.url);
        request.Set("headers", CopyHeaders(env, operation.request_headers));
        request.Set("body", CopyBytes(env, operation.body.data(), operation.body.size()));
        request.Set("timeoutMs", Napi::Number::New(env, static_cast<double>(operation.timeout_ms)));
        object.Set("request", request);
      } else if (std::strcmp(event->type, "read") == 0) {
        object.Set("capacity", Napi::Number::New(env, static_cast<double>(event->capacity)));
      }
      auto result = Napi::Function(env, raw_callback).Call({object});
      auto fulfilled = Napi::Function::New(
          env, [ticket, environment, type = event->type, capacity = event->capacity](const Napi::CallbackInfo& info) {
            if (!ticket || environment->closing.load(std::memory_order_acquire)) return;
            try {
              Settle(ticket, type, capacity, info[0]);
            } catch (const std::exception& error) {
              ClearPendingException(info.Env());
              ticket->Fail(FOUNDRY_LOCAL_ERROR_INVALID_ARGUMENT, error.what());
            } catch (...) {
              ClearPendingException(info.Env());
              ticket->Fail(FOUNDRY_LOCAL_ERROR_INTERNAL, "Unknown HTTP result error");
            }
          });
      auto rejected = Napi::Function::New(env, [ticket, environment](const Napi::CallbackInfo& info) {
        if (!ticket || environment->closing.load(std::memory_order_acquire)) return;
        try {
          Reject(ticket, info[0]);
        } catch (...) {
          ClearPendingException(info.Env());
          ticket->Fail(FOUNDRY_LOCAL_ERROR_NETWORK, "HTTP Promise rejected");
        }
      });
      // Notifications also need assimilation: a rejected cancel/close Promise must never become unhandled.
      auto promise_ctor = env.Global().Get("Promise").As<Napi::Object>();
      auto promise = promise_ctor.Get("resolve").As<Napi::Function>().Call(promise_ctor, {result}).As<Napi::Object>();
      promise.Get("then").As<Napi::Function>().Call(promise, {fulfilled, rejected});
    } catch (const Napi::Error& error) {
      ClearPendingException(env);
      if (ticket) {
        try {
          Reject(ticket, error.Value());
        } catch (...) {
          ClearPendingException(env);
          ticket->Fail(FOUNDRY_LOCAL_ERROR_NETWORK, "HTTP callback threw");
        }
      }
    } catch (const std::exception& error) {
      ClearPendingException(env);
      if (ticket) ticket->Fail(FOUNDRY_LOCAL_ERROR_NETWORK, error.what());
    } catch (...) {
      ClearPendingException(env);
      if (ticket) ticket->Fail(FOUNDRY_LOCAL_ERROR_INTERNAL, "Unknown HTTP callback error");
    }
  }

  std::mutex mutex_;
  bool stopped_ = false;
  napi_threadsafe_function tsfn_ = nullptr;
  napi_async_cleanup_hook_handle cleanup_ = nullptr;
  std::shared_ptr<WorkerEnvironment> environment_;
  std::shared_ptr<std::function<void()>> stop_;
  std::shared_ptr<Bridge>* cleanup_root_ = nullptr;  // Removed on the environment thread, never by native Release.
  std::vector<std::weak_ptr<Operation>> operations_;
  size_t active_js_operations_ = 0;  // JS thread only; idle transports must not keep Node alive.
  const std::thread::id js_thread_ = std::this_thread::get_id();
};

struct Context {
  std::atomic_size_t references{1};
  std::shared_ptr<Bridge> bridge;
};

template <typename Callback>
flStatus* CheckedCallback(Callback&& callback) noexcept {
  try {
    callback();
    return nullptr;
  } catch (const foundry_local::Error& error) {
    return foundry_local::CreateStatus(error.Code(), error.what());
  } catch (const std::invalid_argument& error) {
    return foundry_local::CreateStatus(FOUNDRY_LOCAL_ERROR_INVALID_ARGUMENT, error.what());
  } catch (const std::exception& error) {
    return foundry_local::CreateStatus(FOUNDRY_LOCAL_ERROR_NETWORK, error.what());
  } catch (...) {
    return foundry_local::CreateStatus(FOUNDRY_LOCAL_ERROR_INTERNAL, "Unknown model HTTP callback exception");
  }
}

void FL_API_CALL Retain(void* data) noexcept {
  static_cast<Context*>(data)->references.fetch_add(1, std::memory_order_relaxed);
}

void FL_API_CALL Release(void* data) noexcept {
  auto* context = static_cast<Context*>(data);
  if (context->references.fetch_sub(1, std::memory_order_acq_rel) == 1) {
    // Let queued close notifications drain on normal release; env teardown uses Abort instead.
    context->bridge->Stop(false);
    delete context;
  }
}

std::atomic_uint64_t next_id{1};

flStatus* FL_API_CALL Create(void* data, const flHttpRequestData* request, void** output) noexcept {
  if (output) *output = nullptr;
  return CheckedCallback([&] {
    if (!output || !request || request->version != FOUNDRY_LOCAL_HTTP_TRANSPORT_VERSION || !request->method ||
        !request->url || (request->header_count && !request->headers) || (request->body_size && !request->body)) {
      throw std::invalid_argument("Invalid model HTTP request");
    }
    auto operation = std::make_shared<Operation>();
    operation->bridge = static_cast<Context*>(data)->bridge;
    operation->id = next_id.fetch_add(1);
    if (operation->id > 9007199254740991ULL) throw std::runtime_error("HTTP operation ID space exhausted");
    operation->method = request->method;
    if (operation->method != "GET" && operation->method != "HEAD" && operation->method != "POST") {
      throw std::invalid_argument("Unsupported HTTP method");
    }
    operation->url = request->url;
    if (request->timeout_ms > 9007199254740991ULL) throw std::invalid_argument("HTTP timeout is not a safe integer");
    operation->timeout_ms = request->timeout_ms;
    if (request->body_size) operation->body.assign(request->body, request->body + request->body_size);
    for (size_t i = 0; i < request->header_count; ++i) {
      const auto& header = request->headers[i];
      if (!header.name || !header.value || !IsHeaderName(header.name) || !IsHeaderValue(header.value)) {
        throw std::invalid_argument("Invalid HTTP request header");
      }
      operation->request_headers.emplace_back(header.name, header.value);
    }
    auto ticket = std::make_shared<Ticket>();
    operation->pending = ticket;
    operation->bridge->Register(operation);
    auto holder = std::make_unique<std::shared_ptr<Operation>>(operation);
    // Create never waits for JS. FIFO TSFN dispatch orders the JS allocation before Send.
    if (!operation->bridge->Schedule({"create", operation, ticket})) {
      throw foundry_local::Error("JavaScript environment closed", FOUNDRY_LOCAL_ERROR_OPERATION_CANCELLED);
    }
    *output = holder.release();
  });
}

std::shared_ptr<Operation> Unwrap(void* data) {
  if (!data) throw std::invalid_argument("Invalid HTTP operation");
  return *static_cast<std::shared_ptr<Operation>*>(data);
}

void Wait(const std::shared_ptr<Operation>& operation, const std::shared_ptr<Ticket>& ticket) {
  std::unique_lock lock(ticket->mutex);
  while (!ticket->done) {
    const auto probe_at = (std::min)(operation->deadline, Clock::now() + std::chrono::milliseconds(25));
    if (ticket->condition.wait_until(lock, probe_at, [&] { return ticket->done; })) break;
    lock.unlock();
    if (Clock::now() >= operation->deadline) {
      if (operation->Cancel()) operation->bridge->Schedule({"cancel", operation, nullptr});
      throw foundry_local::Error("Model HTTP timeout exceeded", FOUNDRY_LOCAL_ERROR_NETWORK);
    }
    // At most one private liveness event per operation, even when the JS thread is blocked indefinitely.
    if (!operation->probe_pending.exchange(true, std::memory_order_acq_rel)) {
      if (!operation->bridge->Schedule({nullptr, operation, nullptr})) operation->Cancel();
    }
    lock.lock();
  }
  if (ticket->error != FOUNDRY_LOCAL_OK) throw foundry_local::Error(ticket->message, ticket->error);
}

std::shared_ptr<Ticket> Begin(const std::shared_ptr<Operation>& operation, const char* type, size_t capacity = 0) {
  if (operation->bridge->IsJsThread()) {
    throw foundry_local::Error("JavaScript model HTTP transports require an async SDK entry point",
                               FOUNDRY_LOCAL_ERROR_INVALID_USAGE);
  }
  auto ticket = std::make_shared<Ticket>();
  {
    std::lock_guard lock(operation->mutex);
    if (operation->cancelled || operation->released) {
      throw foundry_local::Error("Model HTTP operation cancelled", FOUNDRY_LOCAL_ERROR_OPERATION_CANCELLED);
    }
    operation->pending = ticket;
  }
  if (!operation->bridge->Schedule({type, operation, ticket, capacity})) operation->Cancel();
  Wait(operation, ticket);
  {
    std::lock_guard lock(operation->mutex);
    if (operation->cancelled) {
      throw foundry_local::Error("Model HTTP operation cancelled", FOUNDRY_LOCAL_ERROR_OPERATION_CANCELLED);
    }
  }
  return ticket;
}

flStatus* FL_API_CALL Send(void* data, flHttpResponseData* response) noexcept {
  return CheckedCallback([&] {
    auto operation = Unwrap(data);
    if (!response || response->version != FOUNDRY_LOCAL_HTTP_TRANSPORT_VERSION) {
      throw std::invalid_argument("Invalid HTTP response storage");
    }
    if (operation->bridge->IsJsThread()) {
      throw foundry_local::Error("JavaScript model HTTP transports require an async SDK entry point",
                                 FOUNDRY_LOCAL_ERROR_INVALID_USAGE);
    }
    {
      std::lock_guard lock(operation->mutex);
      if (operation->cancelled) {
        throw foundry_local::Error("Model HTTP operation cancelled", FOUNDRY_LOCAL_ERROR_OPERATION_CANCELLED);
      }
      if (operation->send_started) throw std::invalid_argument("HTTP operation already sent");
      operation->send_started = true;
      if (operation->timeout_ms) {
        const auto start = Clock::now();
        const auto available = std::chrono::duration_cast<std::chrono::milliseconds>(Clock::time_point::max() - start);
        const auto delay = (std::min)(operation->timeout_ms, static_cast<uint64_t>(available.count()));
        operation->deadline = start + std::chrono::milliseconds(delay);
      }
    }
    Wait(operation, operation->pending);  // Propagate synchronous createOperation failures before sending.
    auto ticket = Begin(operation, "send");
    operation->response_headers = std::move(ticket->headers);
    operation->response_views.reserve(operation->response_headers.size());
    for (const auto& header : operation->response_headers) {
      operation->response_views.push_back({header.first.c_str(), header.second.c_str()});
    }
    response->status_code = ticket->status;
    response->headers = operation->response_views.data();
    response->header_count = operation->response_views.size();
    {
      std::lock_guard lock(operation->mutex);
      operation->sent = true;
    }
  });
}

flStatus* FL_API_CALL Read(void* data, uint8_t* buffer, size_t capacity, size_t* count) noexcept {
  if (count) *count = 0;
  return CheckedCallback([&] {
    if (!buffer || !capacity || !count || capacity > 9007199254740991ULL) {
      throw std::invalid_argument("Invalid HTTP read buffer");
    }
    auto operation = Unwrap(data);
    {
      std::lock_guard lock(operation->mutex);
      if (operation->cancelled) {
        throw foundry_local::Error("Model HTTP operation cancelled", FOUNDRY_LOCAL_ERROR_OPERATION_CANCELLED);
      }
      if (!operation->sent) throw std::invalid_argument("HTTP read requires a successful Send");
      if (operation->eof) return;
    }
    auto ticket = Begin(operation, "read", capacity);
    if (!ticket->bytes.empty()) std::memcpy(buffer, ticket->bytes.data(), ticket->bytes.size());
    *count = ticket->bytes.size();
    if (ticket->bytes.empty()) {
      std::lock_guard lock(operation->mutex);
      operation->eof = true;
    }
  });
}

void FL_API_CALL Cancel(void* data) noexcept {
  try {
    auto operation = Unwrap(data);
    // Terminal state and worker wakeup precede the JS notification. Notify at most once.
    if (operation->Cancel()) operation->bridge->Schedule({"cancel", operation, nullptr});
  } catch (...) {
  }
}

void FL_API_CALL Close(void* data) noexcept {
  std::unique_ptr<std::shared_ptr<Operation>> holder(static_cast<std::shared_ptr<Operation>*>(data));
  if (!holder) return;
  try {
    auto operation = *holder;
    {
      std::lock_guard lock(operation->mutex);
      operation->released = true;
    }
    operation->bridge->Schedule({"close", operation, nullptr});
  } catch (...) {
  }
}

}  // namespace

void SetModelHttpTransport(Napi::Env env, foundry_local::Configuration& config, Napi::Function callback) {
  auto* context = new Context();
  try {
    context->bridge = Bridge::New(env, callback);
  } catch (...) {
    delete context;
    throw;
  }
  flHttpTransport transport{};
  transport.version = FOUNDRY_LOCAL_HTTP_TRANSPORT_VERSION;
  transport.user_data = context;
  transport.Retain = Retain;
  transport.Release = Release;
  transport.CreateOperation = Create;
  transport.Send = Send;
  transport.Read = Read;
  transport.Cancel = Cancel;
  transport.ReleaseOperation = Close;
  try {
    config.SetModelHttpTransport(&transport);
  } catch (...) {
    Release(context);
    throw;
  }
  Release(context);
}

bool RejectSyncModelHttpTransport(Napi::Env env, bool configured) {
  if (!configured) return false;
  ThrowFoundryLocalError(env, FOUNDRY_LOCAL_ERROR_INVALID_USAGE,
                         "JavaScript model HTTP transports require an async SDK entry point");
  return true;
}

}  // namespace foundry_local_node
