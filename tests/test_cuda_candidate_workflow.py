from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CandidateWorkflowTests(unittest.TestCase):
    def test_job_environment_uses_only_contexts_available_before_runner_start(self):
        workflow = (ROOT / ".github/workflows/native-cuda-sm120-candidate.yml").read_text(encoding="utf-8")
        job_environment = workflow.split("    env:\n", 1)[1].split("\n\n    steps:", 1)[0]
        self.assertNotIn("${{ runner.temp }}", job_environment)
        self.assertIn('"$RUNNER_TEMP/$SOURCE_DIR"', workflow)
        self.assertIn('"$RUNNER_TEMP/$INPUT_DIR"', workflow)

    def test_workflow_is_manual_and_artifact_only(self):
        workflow = (ROOT / ".github/workflows/native-cuda-sm120-candidate.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("release:", workflow)
        self.assertNotIn("action-gh-release", workflow)
        self.assertIn("actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02", workflow)
        self.assertIn("${{ github.run_id }}-${{ github.run_attempt }}", workflow)
        self.assertIn("https://github.com/ggml-org/llama.cpp.git", workflow)
        self.assertIn("GIT_CONFIG_NOSYSTEM=1", workflow)
        self.assertIn("core.hooksPath=/dev/null", workflow)
        self.assertNotIn("secrets.", workflow)
        self.assertIn("7acb4e8cd2ce21f457d1298e75fad729520d263c", workflow)
        self.assertIn("5a77024ea13397365f3be1610bee5573b0c42f1f", workflow)

    def test_candidate_compiles_networkless_runc_without_gpu_and_with_finite_limits(self):
        workflow = (ROOT / ".github/workflows/native-cuda-sm120-candidate.yml").read_text(encoding="utf-8")
        builder = (ROOT / "scripts/build_cuda_sm120_candidate.sh").read_text(encoding="utf-8")
        self.assertIn("timeout --signal=TERM --kill-after=60s 120m", workflow)
        self.assertIn("timeout-minutes: 125", workflow)
        self.assertIn("--runtime=runc --platform linux/amd64 --network=none", builder)
        self.assertIn("--cpus=2 --memory=8g", builder)
        self.assertIn("--cidfile \"$CID_FILE\"", builder)
        self.assertNotIn("--gpus", builder)
        self.assertIn("--pull=never", builder)

    def test_wheel_hashes_and_cmake_identity_come_from_committed_lock(self):
        fetcher = (ROOT / "scripts/fetch_cuda_sm120_inputs.py").read_text(encoding="utf-8")
        package_lock = (ROOT / "runtime_sidecars/cuda-sm120-package-lock.json").read_text(encoding="utf-8")
        self.assertIn("cmake_archive_url", package_lock)
        self.assertIn("cmake_archive_size_bytes", package_lock)
        self.assertIn("source_url", package_lock)
        self.assertIn("sha256", fetcher)
        self.assertIn("TIMEOUT_SECONDS = 45", fetcher)


if __name__ == "__main__":
    unittest.main()
