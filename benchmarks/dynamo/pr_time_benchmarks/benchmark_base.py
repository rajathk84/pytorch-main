import csv
import gc
from abc import ABC, abstractmethod

from fbscribelogger import make_scribe_logger

import torch._C._instruction_counter as i_counter
import torch._dynamo.config as config
from torch._dynamo.utils import CompileTimeInstructionCounter


scribe_log_torch_benchmark_compile_time = make_scribe_logger(
    "TorchBenchmarkCompileTime",
    """
struct TorchBenchmarkCompileTimeLogEntry {

  # The commit SHA that triggered the workflow, e.g., 02a6b1d30f338206a71d0b75bfa09d85fac0028a. Derived from GITHUB_SHA.
  4: optional string commit_sha;

  # The unit timestamp in second for the Scuba Time Column override
  6: optional i64 time;
  7: optional i64 instruction_count; # Instruction count of compilation step
  8: optional string name; # Benchmark name

  # Commit date (not author date) of the commit in commit_sha as timestamp, e.g., 1724208105.  Increasing if merge bot is used, though not monotonic; duplicates occur when stack is landed.
  16: optional i64 commit_date;

  # A unique number for each workflow run within a repository, e.g., 19471190684. Derived from GITHUB_RUN_ID.
  17: optional string github_run_id;

  # A unique number for each attempt of a particular workflow run in a repository, e.g., 1. Derived from GITHUB_RUN_ATTEMPT.
  18: optional string github_run_attempt;

  # Indicates if branch protections or rulesets are configured for the ref that triggered the workflow run. Derived from GITHUB_REF_PROTECTED.
  20: optional bool github_ref_protected;

  # The fully-formed ref of the branch or tag that triggered the workflow run, e.g., refs/pull/133891/merge or refs/heads/main. Derived from GITHUB_REF.
  21: optional string github_ref;

  # The weight of the record according to current sampling rate
  25: optional i64 weight;

  # The name of the current job. Derived from JOB_NAME, e.g., linux-jammy-py3.8-gcc11 / test (default, 3, 4, amz2023.linux.2xlarge).
  26: optional string github_job;

  # The GitHub user who triggered the job.  Derived from GITHUB_TRIGGERING_ACTOR.
  27: optional string github_triggering_actor;

  # A unique number for each run of a particular workflow in a repository, e.g., 238742. Derived from GITHUB_RUN_NUMBER.
  28: optional string github_run_number_str;
}
""",  # noqa: B950
)


class BenchmarkBase(ABC):
    # measure total number of instruction spent in _work.
    _enable_instruction_count = False

    # measure total number of instruction spent in convert_frame.compile_inner
    # TODO is there other parts we need to add ?
    _enable_compile_time_instruction_count = False

    # number of iterations used to run when collecting instruction_count or compile_time_instruction_count.
    _num_iterations = 5

    def with_iterations(self, value):
        self._num_iterations = value
        return self

    def enable_instruction_count(self):
        self._enable_instruction_count = True
        return self

    def enable_compile_time_instruction_count(self):
        self._enable_compile_time_instruction_count = True
        return self

    def name(self):
        return ""

    def description(self):
        return ""

    @abstractmethod
    def _prepare(self):
        pass

    @abstractmethod
    def _work(self):
        pass

    def _prepare_once(self):  # noqa: B027
        pass

    def _count_instructions(self):
        print(f"collecting instruction count for {self.name()}")
        results = []
        for i in range(self._num_iterations):
            self._prepare()
            id = i_counter.start()
            self._work()
            count = i_counter.end(id)
            print(f"instruction count for iteration {i} is {count}")
            results.append(count)
        return min(results)

    def _count_compile_time_instructions(self):
        gc.disable()

        try:
            print(f"collecting compile time instruction count for {self.name()}")
            config.record_compile_time_instruction_count = True

            results = []
            for i in range(self._num_iterations):
                self._prepare()
                gc.collect()
                # CompileTimeInstructionCounter.record is only called on convert_frame._compile_inner
                # hence this will only count instruction count spent in compile_inner.
                CompileTimeInstructionCounter.clear()
                self._work()
                count = CompileTimeInstructionCounter.value()
                if count == 0:
                    raise RuntimeError(
                        "compile time instruction count is 0, please check your benchmarks"
                    )
                print(f"compile time instruction count for iteration {i} is {count}")
                results.append(count)

            config.record_compile_time_instruction_count = False
            return min(results)
        finally:
            gc.enable()

    def append_results(self, path):
        with open(path, "a", newline="") as csvfile:
            # Create a writer object
            writer = csv.writer(csvfile)
            # Write the data to the CSV file
            for entry in self.results:
                writer.writerow(entry)

    def print(self):
        for entry in self.results:
            print(f"{entry[0]},{entry[1]},{entry[2]}")

    def collect_all(self):
        self._prepare_once()
        self.results = []
        if (
            self._enable_instruction_count
            and self._enable_compile_time_instruction_count
        ):
            raise RuntimeError(
                "not supported until we update the logger, both logs to the same field now"
            )

        if self._enable_instruction_count:
            r = self._count_instructions()
            self.results.append((self.name(), "instruction_count", r))
            scribe_log_torch_benchmark_compile_time(
                name=self.name(),
                instruction_count=r,
            )
        if self._enable_compile_time_instruction_count:
            r = self._count_compile_time_instructions()

            self.results.append(
                (
                    self.name(),
                    "compile_time_instruction_count",
                    r,
                )
            )
            # TODO add a new field compile_time_instruction_count to the logger.
            scribe_log_torch_benchmark_compile_time(
                name=self.name(),
                instruction_count=r,
            )
        return self
