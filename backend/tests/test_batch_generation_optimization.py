from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from models import AICheck
from services.ai_rate_policy import apply_batch_high_ai_rate_skip
from services.job_queue import JobCancelled, JobConflict, is_retryable_error
from services.server_generation_checks import (
    checkpoint_generated_copy, finish_generation_checks, resume_generated_copy,
)
from services.server_humanize_generation import (
    HumanizeGenerationInvalid, LlmServerHumanizeProvider,
)
from storage import RevisionConflictError, content_hash
from test_m7_server_humanize_generation import (
    ARTICLE, StubLlm, article_with_word_count, prompt, task,
)


class BatchGenerationOptimizationTests(unittest.TestCase):
    def test_threshold_is_strict_and_requires_current_measurement(self):
        for score, expected in [(0, False), (20, False), (40, False),
                                (40.1, True), (100, True), (101, False),
                                (None, False), (float("nan"), False)]:
            with self.subTest(score=score):
                value = task()
                value.initial_ai_check = AICheck(score=score, article_hash=value.initial_article_hash)
                self.assertEqual(apply_batch_high_ai_rate_skip(value), expected)
                if expected:
                    self.assertEqual(value.humanized_article, value.initial_article)
                    self.assertFalse(value.final_ai_check.confirmed)
                    self.assertTrue(value.final_ai_check.deferred)
                    self.assertTrue(apply_batch_high_ai_rate_skip(value))
        value = task()
        value.initial_ai_check = AICheck(score=90, article_hash=content_hash("older copy"))
        self.assertFalse(apply_batch_high_ai_rate_skip(value))

    def test_single_pass_never_corrects_rejected_content(self):
        source = article_with_word_count(1100)
        for candidate in [source, "", "invented content", article_with_word_count(1500)]:
            with self.subTest(candidate_words=len(candidate.split())):
                llm = StubLlm(candidate)
                provider = LlmServerHumanizeProvider(object(), llm=llm)
                if candidate == source:
                    self.assertEqual(provider.generate(task(source), source_article=source,
                        prompt_snapshot=prompt(), single_pass=True), source)
                else:
                    with self.assertRaises(HumanizeGenerationInvalid) as caught:
                        provider.generate(task(source), source_article=source,
                            prompt_snapshot=prompt(), single_pass=True)
                    self.assertFalse(is_retryable_error(caught.exception))
                self.assertEqual(len(llm.calls), 1)

    def prepared(self):
        value = task()
        job = dict(id="job-a", operation="article", source_revision=value.revision,
                   organization_id="org-a", project_id="project-a", requested_by_user_id="user-a")
        checkpoint_generated_copy(value, job)
        value.revision += 1  # durable CAS writer increments the saved revision
        return value, job

    def test_checkpoint_rejects_other_jobs_and_user_edits(self):
        value, job = self.prepared()
        self.assertTrue(resume_generated_copy(value, job))
        self.assertFalse(resume_generated_copy(value, {**job, "id": "other-job"}))
        for changed in [value.model_copy(update={"revision": value.revision + 1}),
                        value.model_copy(update={"initial_article": "user edit"})]:
            with self.assertRaises(JobConflict):
                resume_generated_copy(changed, job)

    def run_checks(self, value, job, **kwargs):
        return finish_generation_checks(value, job, engine=object(), audit=None,
            ai_rate=kwargs.get("ai_rate"), knowledge_coverage=kwargs.get("coverage"),
            cancelled=kwargs.get("cancelled", lambda: False))

    @patch("services.server_generation_checks.ProjectAccessService")
    @patch("services.server_generation_checks.PostgresAuditedTaskWriter")
    def test_checks_overlap_and_completed_recovery_is_idempotent(self, writer, access):
        value, job = self.prepared()
        rendezvous = threading.Barrier(2, timeout=3)
        def detect(_text):
            rendezvous.wait()
            return SimpleNamespace(ai_percentage=55, report="Test detection")
        def coverage(copy, **_kwargs):
            rendezvous.wait()
            copy.initial_article = "must never overwrite saved copy"
            copy.knowledge_coverage.status = "available"
        def save(record, **kwargs):
            self.assertEqual(kwargs["expected_revision"], 1)
            record.revision += 1
            return record
        writer.return_value.put.side_effect = save
        self.assertEqual(self.run_checks(value, job,
            ai_rate=SimpleNamespace(ready=True, detect=detect),
            coverage=SimpleNamespace(evaluate_task=coverage)), 2)
        self.assertEqual(value.initial_article, ARTICLE.strip())
        self.assertEqual(value.initial_ai_check.score, 55)
        self.assertEqual(value.knowledge_coverage.status, "available")
        self.assertEqual(self.run_checks(value, job), 2)
        self.assertEqual(writer.return_value.put.call_count, 1)

    @patch("services.server_generation_checks.ProjectAccessService")
    @patch("services.server_generation_checks.PostgresAuditedTaskWriter")
    def test_failed_optional_checks_retain_copy_and_report_unavailable(self, writer, access):
        value, job = self.prepared()
        def fail(*_args, **_kwargs):
            raise RuntimeError("private provider detail")
        def save(record, **_kwargs):
            record.revision += 1
            return record
        writer.return_value.put.side_effect = save
        self.run_checks(value, job, ai_rate=SimpleNamespace(ready=True, detect=fail),
                        coverage=SimpleNamespace(evaluate_task=fail))
        self.assertEqual(value.initial_article, ARTICLE.strip())
        self.assertIsNone(value.initial_ai_check.score)
        self.assertEqual(value.knowledge_coverage.status, "unavailable")
        self.assertNotIn("private", value.model_dump_json())

    @patch("services.server_generation_checks.ProjectAccessService")
    @patch("services.server_generation_checks.PostgresAuditedTaskWriter")
    def test_cancel_and_conflict_never_overwrite_saved_copy(self, writer, access):
        value, job = self.prepared()
        with self.assertRaises(JobCancelled):
            self.run_checks(value, job, cancelled=lambda: True)
        writer.return_value.put.assert_not_called()
        writer.return_value.put.side_effect = RevisionConflictError("task-a", 1, 2)
        with self.assertRaises(JobConflict):
            self.run_checks(value, job)


if __name__ == "__main__":
    unittest.main()
