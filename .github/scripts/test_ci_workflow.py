#!/usr/bin/env python3
"""Static guards of the CI speed pack (#140): the pieces of deploy.yml that make «skip the tests» and «reuse the image» safe stay wired together.
Text checks only (no YAML library on the runner)."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = (ROOT / ".github" / "workflows" / "deploy.yml").read_text()
TEST_JOB = "test"                      # the job that runs the suite (the matrix)
SHARDS = 8                             # 0 = a single job without a matrix


def job(name):
    """The text of one top-level job."""
    m = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)", WORKFLOW, re.S | re.M)
    assert m, f"job {name} not found"
    return m.group(1)


def run_lines(text):
    """Lines of the shell blocks (``run: |``) only."""
    out, inside, indent = [], False, 0
    for line in text.splitlines():
        stripped = line.strip()
        if inside:
            if stripped and len(line) - len(line.lstrip()) <= indent:
                inside = False
            else:
                out.append(line)
                continue
        m = re.match(r"^(\s*)(- )?run: [|>]", line)
        if m:
            inside, indent = True, len(m.group(1)) + (2 if m.group(2) else 0)
        elif re.match(r"^\s*(- )?run: ", line):
            out.append(line)
    return out


class GateWiring(unittest.TestCase):
    def test_the_gate_reads_the_runs_and_nothing_more(self):
        gate = job("gate")
        self.assertRegex(gate, r"actions: read")
        self.assertRegex(gate, r"contents: read")
        self.assertIn("ci_gate.py gate", gate)
        self.assertIn("--ref-name \"${{ github.ref_name }}\"", gate)
        self.assertIn("--sha \"${{ github.sha }}\"", gate)
        self.assertRegex(gate, r"outputs:\n\s+skip_tests: \$\{\{ steps\.g\.outputs\.skip_tests \}\}")

    def test_the_gate_expects_as_many_test_jobs_as_the_workflow_has(self):
        expected = SHARDS or 1
        self.assertIn(f"--expect-tests {expected}", job("gate"))
        if SHARDS:
            matrix = re.search(r"shard: \[([0-9, ]+)\]", job(TEST_JOB)).group(1)
            self.assertEqual([int(x) for x in matrix.split(",")], list(range(1, SHARDS + 1)))
            self.assertIn(f"/{SHARDS})", job(TEST_JOB))                                   # the job name
            self.assertIn(f"ci_shard.py --check {SHARDS}", job(TEST_JOB))
            self.assertIn(f"ci_shard.py ${{{{ matrix.shard }}}} {SHARDS}", job(TEST_JOB))

    def test_the_tests_are_skipped_only_on_the_word_of_the_gate_and_run_when_it_is_silent(self):
        text = job(TEST_JOB)
        self.assertRegex(text, r"needs: gate")
        condition = re.search(r"^    if: (.*)$", text, re.M).group(1)
        self.assertIn("needs.gate.outputs.skip_tests != 'true'", condition)
        self.assertIn("!cancelled()", condition)                                           # a failed gate still lets them run

    def test_the_deploy_waits_for_the_verdict_which_knows_the_gate(self):
        ok = job("tests-ok")
        self.assertRegex(ok, rf"needs: \[gate, guards, {TEST_JOB}\]")
        self.assertRegex(ok, r"if: \$\{\{ always\(\) \}\}")
        self.assertIn("ci_gate.py verdict", ok)
        for part in ("needs.gate.result", "needs.gate.outputs.skip_tests", f"needs.{TEST_JOB}.result", "needs.guards.result"):
            self.assertIn(part, ok)
        self.assertRegex(job("deploy"), r"needs: tests-ok")

    def test_the_deploy_guard_tests_run_in_their_own_job_whatever_the_gate_says(self):
        guards = job("guards")
        self.assertNotIn("needs:", guards)
        self.assertNotIn("if:", guards)
        self.assertIn("test_*.py", guards)

    def test_text_from_the_api_never_reaches_a_shell_line(self):
        """The reason holds branch names and states from the API: it goes through env, never into a script."""
        for name in ("gate", "tests-ok", "deploy"):
            for line in run_lines(job(name)):
                self.assertNotIn("gate.outputs.reason", line)

    def test_the_deploy_still_runs_only_for_main_and_develop(self):
        deploy = job("deploy")
        self.assertIn("(github.ref_name == 'main' || github.ref_name == 'develop')", deploy)
        # 01.11: the test jobs can be SKIPPED by the gate; a job downstream of a skipped one is skipped too unless its condition calls a
        # status function - the first prod skip deployed nothing. The deploy states !cancelled() and the verdict of tests-ok explicitly.
        self.assertIn("!cancelled()", deploy)
        self.assertIn("needs.tests-ok.result == 'success'", deploy)


class HangGuard(unittest.TestCase):
    """28.11: a shard ran 45+ minutes in silence (the default limit of a job is 6 hours). A job that hangs must stop by itself and a test that hangs must name itself."""

    def test_a_test_job_has_a_time_limit_far_below_the_default_six_hours(self):
        limit = int(re.search(r"^    timeout-minutes: (\d+)$", job(TEST_JOB), re.M).group(1))
        self.assertLessEqual(limit, 60)

    def test_a_test_that_hangs_prints_its_stacks_before_the_job_is_stopped(self):
        text = job(TEST_JOB)
        limit = int(re.search(r"^    timeout-minutes: (\d+)$", text, re.M).group(1))
        seconds = int(re.search(r"-o faulthandler_timeout=(\d+)", text).group(1))
        self.assertLess(seconds, limit * 60)                                      # the stacks come BEFORE the stop

    def test_the_heaviest_file_has_its_own_measured_weight_so_that_it_stands_in_a_shard_alone(self):
        import json
        known = json.loads((ROOT / ".github" / "ci_test_durations.json").read_text())
        self.assertGreater(known["tests/test_pay_tails_property.py"], 1000)


class ReuseWiring(unittest.TestCase):
    def test_the_build_step_decides_between_reuse_and_build_before_up(self):
        deploy = job("deploy")
        find = deploy.index('"$release_tool" find')
        build = deploy.index('build "$service"')
        up = deploy.index("up -d --no-build --no-deps")
        self.assertLess(find, build)
        self.assertLess(build, up)
        self.assertIn('--archive "$archive_sha"', deploy)
        self.assertIn('--sha "$sha"', deploy)

    def test_the_labels_that_a_later_reuse_needs_are_written_at_every_build(self):
        for label in ("org.opencontainers.image.revision", "io.algolab.source.archive.sha256", "io.algolab.build.fingerprint", "io.algolab.service"):
            self.assertIn(f'"{label}"', job("deploy"))

    def test_the_guards_after_the_start_are_untouched(self):
        deploy = job("deploy")
        for step in ("Verify container is healthy", "Verify deployed revision", "Export deployed release manifest",
                     "Include source archive and rollback identity", "Verify site responds"):
            self.assertIn(f"- name: {step}", deploy)
        self.assertIn("rollback_tag=", deploy)
        self.assertIn("git merge-base --is-ancestor", deploy)
        self.assertIn("Refusing deployment from a modified server checkout", deploy)


if __name__ == "__main__":
    unittest.main()
