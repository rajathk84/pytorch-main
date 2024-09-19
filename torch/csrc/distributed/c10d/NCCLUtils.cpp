#include <torch/csrc/distributed/c10d/NCCLUtils.hpp>
#include <torch/csrc/distributed/c10d/ProcessGroupNCCL.hpp>
#include <torch/csrc/distributed/c10d/control_plane/Handlers.hpp>

#include <c10/util/CallOnce.h>
#include <c10/util/env.h>
#include <algorithm>

#ifdef USE_C10D_NCCL
#include <vector>

#include <cuda_runtime.h>
#include <mutex>

#include <nlohmann/json.hpp>

namespace {
constexpr int64_t kCommInitBusyWaitMillis = 10;
} // namespace

namespace c10d {

ncclComm_t NCCLComm::getNcclComm() {
  std::unique_lock<std::mutex> lock(mutex_);
  if (aborted_) {
    auto commFailureMsg = commFailureReason_ != std::nullopt
        ? c10::str(" Original reason for failure was: ", *commFailureReason_)
        : "";
    TORCH_CHECK_WITH(
        DistBackendError,
        false,
        c10::str(
            "NCCL communicator was aborted on rank ",
            rank_,
            ". ",
            commFailureMsg));
  }
  // only wait for initialization if nonblocking mode is enabled
  if (!initialized_ && nccl_use_nonblocking()) {
    waitUntilInitialized(nccl_nonblocking_timeout());
  }

  return ncclComm_;
}

void NCCLComm::waitUntilInitialized(int timeoutSecs) {
  auto startTimepoint = std::chrono::steady_clock::now();
  while (!initialized_) {
    if (ncclComm_) {
      ncclResult_t result;
      ncclCommGetAsyncError(ncclComm_, &result);
      if (result == ncclSuccess) {
        LOG(INFO) << "Rank " << rank_ << ": NCCL communicator is initialized.";
        initialized_ = true;
        break;
      }
    }
    auto currentTimepoint = std::chrono::steady_clock::now();
    auto timeElapsed = std::chrono::duration_cast<std::chrono::seconds>(
                           currentTimepoint - startTimepoint)
                           .count();
    if (timeElapsed > timeoutSecs) {
      std::string err = "NCCL timeout in communicator initialization.";
      TORCH_CHECK_WITH(DistBackendError, false, err);
    }
    std::this_thread::sleep_for(
        std::chrono::milliseconds(kCommInitBusyWaitMillis));
  }
}

#if defined(NCCL_HAS_COMM_SPLIT) && !defined(FBCODE_CAFFE2)
// last argument to split() API is not used to support
// multiple implementations
std::shared_ptr<NCCLComm> NCCLComm::split(
    NCCLComm* source,
    int color_id,
    int rank,
    ncclConfig_t& config,
    std::vector<uint64_t>& ranks_ull) {
  auto comm = std::make_shared<NCCLComm>();
  C10D_NCCL_CHECK(
      ncclCommSplit(
          source->ncclComm_, color_id, rank, &(comm->ncclComm_), &config),
      std::nullopt);
  ++source->ncclCommSplitCounter_;
  comm->rank_ = rank;
  if (!nccl_use_nonblocking()) {
    comm->initialized_ = true;
  }
  return comm;
}
#endif

std::string getNcclVersion() {
  static c10::once_flag ncclGetVersionFlag;
  static std::string versionString;

  c10::call_once(ncclGetVersionFlag, []() {
    int version;
    ncclResult_t status = ncclGetVersion(&version);
    // can't compute the version if call did not return successfully or version
    // code < 100 (corresponding to 0.1.0)
    if (status != ncclSuccess || version < 100) {
      versionString = "Unknown NCCL version";
    } else {
      // NCCL changed version coding starting 2.9
      const int majorBase = version < 2900 ? 1000 : 10000;
      const int minorBase = 100;
      auto ncclMajor = version / majorBase;
      auto ncclMinor = (version % majorBase) / minorBase;
      auto ncclPatch =
          version % (ncclMajor * majorBase + ncclMinor * minorBase);
      versionString = std::to_string(ncclMajor) + "." +
          std::to_string(ncclMinor) + "." + std::to_string(ncclPatch);
#ifdef NCCL_SUFFIX
      const auto ncclSuffix = std::string(NCCL_SUFFIX);
      if (ncclSuffix.length()) {
        versionString += "." + ncclSuffix;
      }
#endif
    }
  });

  return versionString;
}

#ifdef USE_C10D_NCCL
size_t hashTensors(const std::vector<at::Tensor>& tensors) {
  size_t hash = 0;
  for (auto& tensor : tensors) {
    if (tensor.numel() > 0 && tensor.storage()) {
      size_t data_size = tensor.storage().nbytes();
      if (data_size > 0 && tensor.storage().data_ptr()) {
        auto src = static_cast<const char*>(tensor.storage().data_ptr().get());
        char* dst = (char*)std::calloc(data_size, sizeof(char));
        // This is needed so that we trigger a device synchronization so we can
        // get the collective finished if launched on GPU and hash its output.
        cudaMemcpy(dst, src, data_size, cudaMemcpyDeviceToHost);
        for (size_t i = 0; i < data_size; ++i) {
          // Update the hash for each byte in the tensor
          hash = c10::hash_combine(
              hash, c10::get_hash(((char*)dst)[i], data_size));
        }
        free(dst);
      }
    }
  }
  return hash;
}
#endif

bool nccl_use_nonblocking() {
  static bool nccl_use_nonblocking_ =
      c10::utils::check_env("TORCH_NCCL_USE_COMM_NONBLOCKING") == true;
  if (nccl_use_nonblocking_) {
    TORCH_WARN_ONCE("Using experimental non-blocking NCCL communicator.");
  }
  return nccl_use_nonblocking_;
}

int _parse_nccl_nonblocking_timeout() {
  const char* val = getenv("TORCH_NCCL_NONBLOCKING_TIMEOUT");
  int timeout = -1;
  if (val) {
    const std::string config(val);
    timeout = std::stoi(config);
    if (!nccl_use_nonblocking() && timeout > 0) {
      TORCH_WARN(
          "TORCH_NCCL_NONBLOCKING_TIMEOUT has no effect when TORCH_NCCL_USE_COMM_NONBLOCKING is false.");
      timeout = -1;
    }
  }
  return timeout;
}

int nccl_nonblocking_timeout() {
  static int timeout = _parse_nccl_nonblocking_timeout();
  return timeout;
}

std::string ncclGetErrorWithVersion(ncclResult_t error) {
  return std::string(ncclGetErrorString(error)) + ", NCCL version " +
      getNcclVersion();
}

// Provides additional detail into NCCL error codes based on when these are
// thrown in the NCCL codebase.
std::string getNcclErrorDetailStr(
    ncclResult_t error,
    std::optional<std::string> processGroupFailureReason /* = std::nullopt */
) {
  // Prioritize failure reason provided by PG NCCL first, as it can abort
  // communicators when it encounters collective timeouts, etc.
  if (processGroupFailureReason != std::nullopt) {
    return *processGroupFailureReason;
  }
  std::string interpret;
  std::string err;
#ifdef ENABLE_NCCL_GET_LAST_ERROR
  auto ret = ncclGetLastError(NULL);
  if (ret) {
    err = "\nLast error:\n" + std::string(ret);
  } else {
    err = "\nLast error: Unknown NCCL Error\n";
  }
#endif
  switch (error) {
    case ncclUnhandledCudaError:
      interpret = "ncclUnhandledCudaError: Call to CUDA function failed.";
      break;
    case ncclSystemError:
      interpret =
          "ncclSystemError: System call (e.g. socket, malloc) or external library call failed or device error. ";
#ifndef NCCL_REMOTE_ERROR
      // Before ncclRemoteError was created, unexpected remote disconnect was
      // categorized as ncclSystemError
      interpret += "It can be also caused by unexpected exit of a remote peer.";
#endif
      break;
    case ncclInternalError:
      interpret = "ncclInternalError: Internal check failed.";
      break;
    case ncclInvalidArgument:
      interpret = "ncclInvalidArgument: Invalid value for an argument.";
      break;
    case ncclInvalidUsage:
      interpret =
          "ncclInvalidUsage: This usually reflects invalid usage of NCCL library.";
      break;
#ifdef NCCL_REMOTE_ERROR
    case ncclRemoteError:
      interpret =
          "ncclRemoteError: A call failed possibly due to a network error or a remote process exiting prematurely.";
      break;
#endif
    default:
      interpret = "Unknown NCCL error!";
  }
  return interpret + err;
}

control_plane::RegisterHandler dumpHandler{
    "dump_nccl_trace_pickle",
    [](const control_plane::Request& req, control_plane::Response& res) {
      const auto params = req.params();
      size_t validParamCount = 0;

      // valid params
      const std::string includeCollectivesStr = "includecollectives";
      const std::string includeStackTracesStr = "includestacktraces";
      const std::string onlyActiveStr = "onlyactive";

      std::unordered_map<std::string, bool> processedParams = {
          {includeCollectivesStr, true},
          {includeStackTracesStr, true},
          {onlyActiveStr, false}};

      for (const auto& [paramName, paramValue] : params) {
        auto it = processedParams.find(paramName);
        if (it != processedParams.end()) {
          validParamCount++;
          if (paramValue == "true") {
            it->second = true;
          } else if (paramValue == "false") {
            it->second = false;
          } else {
            res.setStatus(400);
            res.setContent(
                "Invalid value for " + paramName +
                    " valid values are true or false",
                "text/plain");
            return;
          }
        }
      }
      if (validParamCount < params.size()) {
        res.setStatus(400);
        res.setContent(
            "Invalid parameters - unexpected param passed in", "text/plain");
        return;
      }
      res.setContent(
          dump_nccl_trace(
              processedParams[includeCollectivesStr],
              processedParams[includeStackTracesStr],
              processedParams[onlyActiveStr]),
          "application/octet-stream");
    }};

control_plane::RegisterHandler jsonDumpHandler{
    "dump_nccl_trace_json",
    [](const control_plane::Request& req, control_plane::Response& res) {
      const auto params = req.params();
      size_t validParamCount = 0;

      // valid params
      const std::string includeCollectivesStr = "includecollectives";
      const std::string onlyActiveStr = "onlyactive";

      std::unordered_map<std::string, bool> processedParams = {
          {includeCollectivesStr, true}, {onlyActiveStr, false}};

      for (const auto& [paramName, paramValue] : params) {
        auto it = processedParams.find(paramName);
        if (it != processedParams.end()) {
          validParamCount++;
          if (paramValue == "true") {
            it->second = true;
          } else if (paramValue == "false") {
            it->second = false;
          } else {
            res.setStatus(400);
            res.setContent(
                "Invalid value for " + paramName +
                    " valid values are true or false",
                "text/plain");
            return;
          }
        }
      }
      if (validParamCount < params.size()) {
        res.setStatus(400);
        res.setContent(
            "Invalid parameters - unexpected param passed in", "text/plain");
        return;
      }
      res.setStatus(200);
      res.setContent(
          dump_nccl_trace_json(
              processedParams[includeCollectivesStr],
              processedParams[onlyActiveStr]),
          "application/json");
    }};

void DebugInfoWriter::write(const std::string& ncclTrace) {
  // Open a file for writing. The ios::binary flag is used to write data as
  // binary.
  std::ofstream file(filename_, std::ios::binary);

  // Check if the file was opened successfully.
  if (!file.is_open()) {
    LOG(ERROR) << "Error opening file for writing NCCLPG debug info: "
               << filename_;
    return;
  }

  file.write(ncclTrace.data(), ncclTrace.size());
  LOG(INFO) << "Finished writing NCCLPG debug info to " << filename_;
}

DebugInfoWriter& DebugInfoWriter::getWriter(int rank) {
  if (writer_ == nullptr) {
    std::string fileNamePrefix = getCvarString(
        {"TORCH_NCCL_DEBUG_INFO_TEMP_FILE"}, "/tmp/nccl_trace_rank_");
    // Using std::unique_ptr here to auto-delete the writer object
    // when the pointer itself is destroyed.
    std::unique_ptr<DebugInfoWriter> writerPtr(
        new DebugInfoWriter(fileNamePrefix, rank));
    DebugInfoWriter::registerWriter(std::move(writerPtr));
  }
  return *writer_;
}

void DebugInfoWriter::registerWriter(std::unique_ptr<DebugInfoWriter> writer) {
  TORCH_CHECK_WITH(
      DistBackendError,
      hasWriterRegistered_.load() == false,
      "debugInfoWriter already registered");
  hasWriterRegistered_.store(true);
  writer_ = std::move(writer);
}

std::optional<size_t> NCCLTraceBuffer::record(
    size_t pg_id,
    const std::tuple<std::string, std::string>& pg_name,
    size_t collective_seq_id,
    size_t p2p_seq_id,
    size_t op_id,
    std::string profiling_name,
    const std::vector<at::Tensor>& inputs,
    const std::vector<at::Tensor>& outputs,
    Event* start,
    Event* end,
    std::chrono::milliseconds timeout_ms,
    std::shared_ptr<ProcessGroupStatus> pg_status,
    bool isP2P) {
  if (!enabled_) {
    return std::nullopt;
  }
  if (all_pg_status_.find(pg_id) == all_pg_status_.end()) {
    // Current pg_status is not in FR.
    all_pg_status_[pg_id] = pg_status;
  }
  auto traceback =
      torch::CapturedTraceback::gather(true, true, capture_cpp_stack_);
  std::lock_guard<std::mutex> guard(mutex_);

  auto te = Entry{
      id_,
      pg_id,
      pg_name,
      collective_seq_id,
      p2p_seq_id,
      op_id,
      std::move(profiling_name),
      std::move(traceback),
      std::move(start),
      std::move(end),
      c10::getTime(),
      timeout_ms.count(),
      isP2P,
      std::nullopt,
      std::nullopt,
      std::nullopt,
      {},
      {},
      {},
      {},
      {},
      false};

  for (const auto& input : inputs) {
    c10::IntArrayRef sizes = input.sizes();
    te.input_dtypes_.push_back(input.dtype().toScalarType());
    te.input_dims_.push_back(sizes.size());
    te.sizes_.insert(te.sizes_.end(), sizes.begin(), sizes.end());
  }

  for (const auto& output : outputs) {
    c10::IntArrayRef sizes = output.sizes();
    te.output_dtypes_.push_back(output.dtype().toScalarType());
    te.output_dims_.push_back(sizes.size());
    te.sizes_.insert(te.sizes_.end(), sizes.begin(), sizes.end());
  }

  if (entries_.size() < max_entries_) {
    entries_.emplace_back(std::move(te));
  } else {
    entries_[next_++] = std::move(te);
    if (next_ == max_entries_) {
      next_ = 0;
    }
  }
  return id_++;
}

void NCCLTraceBuffer::record_pg_ranks(
    const std::tuple<std::string, std::string>& pg_name,
    std::vector<uint64_t> ranks) {
  if (!enabled_) {
    return;
  }
  std::lock_guard<std::mutex> guard(mutex_);
  pg_name_to_ranks_[pg_name] = ranks;
}

void NCCLTraceBuffer::update_state(Entry& r) {
  if (r.start_ != nullptr) {
    bool started = r.start_->query();
    if (started && !r.time_discovered_started_) {
      r.time_discovered_started_ = c10::getTime();
    }
  }
  if (r.end_ != nullptr) {
    bool completed = r.end_->query();
    if (completed && !r.time_discovered_completed_) {
      r.time_discovered_completed_ = c10::getTime();
    }
  }
}

std::vector<NCCLTraceBuffer::Entry> NCCLTraceBuffer::dump_entries() {
  std::lock_guard<std::mutex> guard(mutex_);
  std::vector<Entry> result;
  result.reserve(entries_.size());
  result.insert(result.end(), entries_.begin() + next_, entries_.end());
  result.insert(result.end(), entries_.begin(), entries_.begin() + next_);
  // query any remaining events
  for (auto& r : result) {
    update_state(r);
    r.start_ = r.end_ = nullptr;
  }
  return result;
}

void NCCLTraceBuffer::retire_id(
    std::optional<size_t> id,
    bool compute_duration) {
  if (!enabled_ || !id) {
    return;
  }

  bool can_compute_duration = false;
  Event* startEvent = nullptr;
  Event* endEvent = nullptr;
  std::optional<float> duration = std::nullopt;

  std::unique_lock<std::mutex> guard(mutex_);

  Entry* entry = &entries_.at(*id % max_entries_);
  if (entry->id_ == *id) {
    update_state(*entry);

    if (compute_duration) {
      can_compute_duration = entry->time_discovered_completed_.has_value() &&
          entry->start_ && entry->end_;
      startEvent = entry->start_;
      endEvent = entry->end_;
    }
    entry->retired_ = true;
    entry->start_ = entry->end_ = nullptr;
  }

  if (can_compute_duration) {
    // Compute duration without without holding the lock, because
    // cudaEventDuration() can hang, and we need to acquire the lock before we
    // can dump(), which we never want to block.
    guard.unlock();
    duration = getDurationFromEvent(*startEvent, *endEvent);
    guard.lock();

    // Refresh the entry pointer, see if the entry has been overwritten
    entry = &entries_.at(*id % max_entries_);
    if (entry->id_ != *id) {
      LOG(INFO) << "retire_id abandoned for id " << *id
                << ", event was overwritten while waiting to compute duration.";
      return;
    }
    if (duration.has_value()) {
      entry->duration_ = duration.value();
    }
  }
}

const c10::List<c10::IValue> NCCLTraceBuffer::getCollectiveTrace(
    bool includeStacktraces,
    bool onlyActive) {
  auto entries = new_list();
  // Entries are returned in the order they were recorded
  auto result = dump_entries();
  std::vector<torch::CapturedTraceback*> tracebacks;
  torch::SymbolizedTracebacks stracebacks;
  std::vector<c10::IValue> all_frames;
  if (includeStacktraces) {
    for (auto& e : result) {
      tracebacks.push_back(e.traceback_.get());
    }
    stracebacks = torch::symbolize(tracebacks);
    for (const auto& f : stracebacks.all_frames) {
      auto d = new_dict();
      d.insert(name_key, f.funcname);
      d.insert(filename_key, f.filename);
      d.insert(line_key, int64_t(f.lineno));
      all_frames.emplace_back(std::move(d));
    }
  }
  for (auto i : c10::irange(result.size())) {
    auto dict = new_dict();
    auto& e = result.at(i);
    // Skip completed events
    if (onlyActive && e.time_discovered_completed_.has_value()) {
      continue;
    }
    if (includeStacktraces) {
      auto& tb = stracebacks.tracebacks.at(i);
      auto frames = new_list();
      for (int64_t frame : tb) {
        frames.push_back(all_frames.at(frame));
      }
      dict.insert(frames_key, frames);
    }

    dict.insert(record_id_key, int64_t(e.id_));
    dict.insert(pg_id_key, int64_t(e.pg_id_));
    dict.insert(pg_name_key, e.pg_name_);
    dict.insert(collective_seq_id_key, int64_t(e.collective_seq_id_));
    dict.insert(p2p_seq_id_key, int64_t(e.p2p_seq_id_));
    dict.insert(op_id_key, int64_t(e.op_id_));
    dict.insert(profiling_name_key, e.profiling_name_);
    dict.insert(time_created_key, int64_t(e.time_created_));
    if (e.duration_) {
      dict.insert(duration_key, *e.duration_);
    }

    auto it = e.sizes_.begin();
    auto read_sizes = [&](const c10::SmallVector<int, 4>& dims) {
      auto sizes = new_list();
      for (auto dim : dims) {
        auto arg_sizes = new_list();
        for (C10_UNUSED auto i : c10::irange(dim)) {
          arg_sizes.push_back(*it++);
        }
        sizes.push_back(arg_sizes);
      }
      return sizes;
    };

    dict.insert(input_sizes_key, read_sizes(e.input_dims_));
    std::vector<std::string> input_dtypes_strs;
    input_dtypes_strs.reserve(e.input_dtypes_.size());
    for (const auto& input_dtype : e.input_dtypes_) {
      input_dtypes_strs.push_back(c10::toString(input_dtype));
    }
    dict.insert(input_dtypes_key, input_dtypes_strs);
    dict.insert(output_sizes_key, read_sizes(e.output_dims_));
    std::vector<std::string> output_dtypes_strs;
    output_dtypes_strs.reserve(e.output_dtypes_.size());
    for (const auto& output_dtype : e.output_dtypes_) {
      output_dtypes_strs.push_back(c10::toString(output_dtype));
    }
    dict.insert(output_dtypes_key, output_dtypes_strs);
    if (e.time_discovered_completed_.has_value()) {
      dict.insert(state_key, completed_state);
    } else if (e.time_discovered_started_.has_value()) {
      dict.insert(state_key, started_state);
    } else {
      dict.insert(state_key, scheduled_state);
    }

    dict.insert(
        time_discovered_started_key,
        e.time_discovered_started_.has_value()
            ? int64_t(*e.time_discovered_started_)
            : c10::IValue());
    dict.insert(
        time_discovered_completed_key,
        e.time_discovered_completed_.has_value()
            ? int64_t(*e.time_discovered_completed_)
            : c10::IValue());
    dict.insert(retired_key, e.retired_);
    dict.insert(timeout_key, e.timeout_ms_);
    dict.insert(is_p2p_key, e.isP2P_);

    entries.push_back(dict);
  }
  return entries;
}

const c10::Dict<c10::IValue, c10::IValue> NCCLTraceBuffer::getPgConfig() {
  auto pg_config = new_dict();
  for (const auto& [pg_name, ranks] : pg_name_to_ranks_) {
    auto pg_info = new_dict();
    pg_info.insert("name", std::get<0>(pg_name));
    pg_info.insert("desc", std::get<1>(pg_name));
    pg_info.insert("ranks", ranks_str(ranks));
    pg_config.insert(std::get<0>(pg_name), pg_info);
  }
  return pg_config;
}

const std::map<std::string, std::map<std::string, std::string>> NCCLTraceBuffer::
    getPgConfigJson() {
  std::map<std::string, std::map<std::string, std::string>> result;
  for (const auto& [pg_name, ranks] : pg_name_to_ranks_) {
    auto pg_info = std::map<std::string, std::string>();
    pg_info["name"] = std::get<0>(pg_name);
    pg_info["desc"] = std::get<1>(pg_name);
    pg_info["ranks"] = ranks_str(ranks);
    result.emplace(std::get<0>(pg_name), pg_info);
  }
  return result;
}

const c10::Dict<c10::IValue, c10::IValue> NCCLTraceBuffer::getPgStatus() {
  auto all_pg_status = new_dict();
  for (const auto& [pg_id, status] : all_pg_status_) {
    auto pg_status = new_dict();
    pg_status.insert("last_enqueued_collective", status->lastEnqueuedSeq);
    pg_status.insert("last_started_collective", status->lastStartedSeq);
    pg_status.insert("last_completed_collective", status->lastCompletedSeq);
    all_pg_status.insert(std::to_string(pg_id), pg_status);
  }
  return all_pg_status;
}

const std::map<std::string, std::map<std::string, std::string>> NCCLTraceBuffer::
    getPgStatusJson() {
  std::map<std::string, std::map<std::string, std::string>> result;
  for (const auto& [pg_id, status] : all_pg_status_) {
    auto pg_status = std::map<std::string, std::string>();
    pg_status["last_enqueued_collective"] =
        std::to_string(status->lastEnqueuedSeq);
    pg_status["last_started_collective"] =
        std::to_string(status->lastStartedSeq);
    pg_status["last_completed_collective"] =
        std::to_string(status->lastCompletedSeq);
    result[std::to_string(pg_id)] = pg_status;
  }
  return result;
}

std::string NCCLTraceBuffer::dump_json(
    const std::optional<std::unordered_map<
        std::string,
        std::unordered_map<std::string, std::string>>>& ncclDumpMap,
    bool includeCollectives,
    bool onlyActive) {
  using json = nlohmann::json;
  json result;
  result[version_key_str] = version_val_str;
  result[pg_config_key_str] = getPgConfigJson();
  result[pg_status_key_str] = getPgStatusJson();

  // collective trace
  if (includeCollectives) {
    std::list<json> entries;
    for (auto& e : dump_entries()) {
      json j;
      if (onlyActive && e.time_discovered_completed_.has_value()) {
        continue;
      }
      j[record_id_key_str] = int64_t(e.id_);
      j[pg_id_key_str] = int64_t(e.pg_id_);
      j[pg_name_key_str] = e.pg_name_;
      j[collective_seq_id_key_str] = int64_t(e.collective_seq_id_);
      j[p2p_seq_id_key_str] = int64_t(e.p2p_seq_id_);
      j[op_id_key_str] = int64_t(e.op_id_);
      j[profiling_name_key_str] = e.profiling_name_;
      j[time_created_key_str] = int64_t(e.time_created_);
      if (e.duration_) {
        j[duration_key_str] = *e.duration_;
      }
      auto it = e.sizes_.begin();
      auto read_sizes = [&](const c10::SmallVector<int, 4>& dims) {
        auto sizes = std::list<std::list<int>>();
        for (auto dim : dims) {
          auto arg_sizes = std::list<int>();
          for (auto i : c10::irange(dim)) {
            (void)i;
            arg_sizes.push_back(*it++);
          }
          sizes.push_back(arg_sizes);
        }
        return sizes;
      };
      j[input_sizes_key_str] = read_sizes(e.input_dims_);
      std::vector<std::string> input_dtypes_strs;
      input_dtypes_strs.reserve(e.input_dtypes_.size());
      for (const auto& input_dtype : e.input_dtypes_) {
        input_dtypes_strs.push_back(c10::toString(input_dtype));
      }
      j[input_dtypes_key_str] = input_dtypes_strs;
      j[output_sizes_key_str] = read_sizes(e.output_dims_);
      std::vector<std::string> output_dtypes_strs;
      output_dtypes_strs.reserve(e.output_dtypes_.size());
      for (const auto& output_dtype : e.output_dtypes_) {
        output_dtypes_strs.push_back(c10::toString(output_dtype));
      }
      j[output_dtypes_key_str] = output_dtypes_strs;
      if (e.time_discovered_completed_.has_value()) {
        j[state_key_str] = completed_state_str;
      } else if (e.time_discovered_started_.has_value()) {
        j[state_key_str] = started_state_str;
      } else {
        j[state_key_str] = scheduled_state_str;
      }
      j[time_discovered_started_key_str] =
          e.time_discovered_started_.has_value()
          ? int64_t(*e.time_discovered_started_)
          : 0;
      j[time_discovered_completed_key_str] =
          e.time_discovered_completed_.has_value()
          ? int64_t(*e.time_discovered_completed_)
          : 0;
      j[retired_key_str] = e.retired_;
      j[timeout_key_str] = e.timeout_ms_;
      j[is_p2p_key_str] = e.isP2P_;
      entries.emplace_back(j);
    }

    if (entries.size() > 0) {
      result[entries_key_str] = entries;
    }
  }

  if (ncclDumpMap.has_value()) {
    result[nccl_comm_key_str] = ncclDumpMap.value();
  }

  return result.dump();
}

std::string NCCLTraceBuffer::dump(
    const std::optional<std::unordered_map<
        std::string,
        std::unordered_map<std::string, std::string>>>& ncclDumpMap,
    bool includeCollectives,
    bool includeStackTraces,
    bool onlyActive) {
  auto result = new_dict();
  // common values
  result.insert(version_key, version_val);
  result.insert(pg_config_key, getPgConfig());
  result.insert(pg_status_key, getPgStatus());

  // collective trace
  if (includeCollectives) {
    result.insert(
        entries_key, getCollectiveTrace(includeStackTraces, onlyActive));
  }
  // convert ncclDumpMap into a dictionary
  auto per_comm_dict = new_dict();
  if (ncclDumpMap.has_value()) {
    for (const auto& [ncclId, ncclDump] : ncclDumpMap.value()) {
      auto inner_dict = new_dict();
      for (const auto& [key, value] : ncclDump) {
        inner_dict.insert(key, value);
      }
      per_comm_dict.insert(ncclId, inner_dict);
    }
  }
  if (per_comm_dict.size() > 0) {
    result.insert(nccl_comm_key, per_comm_dict);
  }
  return pickle_str(result);
}

std::unique_ptr<DebugInfoWriter> DebugInfoWriter::writer_ = nullptr;
std::atomic<bool> DebugInfoWriter::hasWriterRegistered_(false);

float getDurationFromEvent(
    at::cuda::CUDAEvent& ncclStartEvent,
    at::cuda::CUDAEvent& ncclEndEvent) {
  TORCH_CHECK(
      ncclEndEvent.query(),
      "getDuration can only be called after work is succeeded.")
  return ncclStartEvent.elapsed_time(ncclEndEvent);
}

} // namespace c10d

#endif // USE_C10D_NCCL
