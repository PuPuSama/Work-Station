from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent.contracts import (  # noqa: E402
    KnowledgeChunk,
    RetrievalHit,
    RetrievalProvenance,
)
from knowledge_agent.evaluation import (  # noqa: E402
    EvidenceImprovementObservation,
    RetrievalEvaluationCase,
    evaluate_evidence_improvement,
    evaluate_retriever,
    load_evaluation_cases,
)
from knowledge_agent.evaluation_runner import (  # noqa: E402
    dataset_summary,
    main,
    write_evaluation_report,
)


def hit(
    source_id: str,
    *,
    source_kind: str,
    url: str,
    score: float,
) -> RetrievalHit:
    snapshot_id = f"{source_id}-snapshot"
    return RetrievalHit(
        chunk=KnowledgeChunk(
            project_id="example.com",
            chunk_id=f"{snapshot_id}:0",
            source_id=source_id,
            snapshot_id=snapshot_id,
            text=f"Evidence from {source_id}",
        ),
        score=score,
        provenance=RetrievalProvenance(
            project_id="example.com",
            source_id=source_id,
            snapshot_id=snapshot_id,
            display_name=source_id,
            source_kind=source_kind,  # type: ignore[arg-type]
            trust_tier="reference_material",
            public_source=True,
            canonical_url=url,
        ),
    )


class _Retriever:
    def __init__(self, results):
        self.results = results
        self.queries = []

    def retrieve(self, query):
        self.queries.append(query)
        return self.results.get(query.text, ())


class _Clock:
    def __init__(self, values):
        self.values = iter(values)

    def __call__(self):
        return next(self.values)


class KnowledgeAgentM6EvaluationTests(unittest.TestCase):
    def test_qewit_dataset_has_twenty_real_topics_and_one_approved_seed(
        self,
    ) -> None:
        path = (
            BACKEND_DIR.parent
            / "evaluation"
            / "knowledge-agent"
            / "qewitfastener"
            / "retrieval-cases.jsonl"
        )
        cases = load_evaluation_cases(path, approved_only=False)

        self.assertEqual(len(cases), 20)
        self.assertEqual(
            [case.case_id for case in cases if case.annotation_status == "approved"],
            ["topic_006"],
        )
        topic_006 = next(case for case in cases if case.case_id == "topic_006")
        self.assertEqual(
            topic_006.query,
            "What are some common mistakes to avoid when selecting and using "
            "woodscrews in woodworking projects?",
        )
        self.assertEqual(
            topic_006.expected_source_ids,
            ("web_91ba10a7f31afda8b8b14a46",),
        )

    def test_metrics_cover_recall_rank_kind_wrong_source_refusal_and_latency(
        self,
    ) -> None:
        retriever = _Retriever(
            {
                "product material": (
                    hit(
                        "category-source",
                        source_kind="product_category",
                        url="https://example.com/category",
                        score=0.95,
                    ),
                    hit(
                        "detail-source",
                        source_kind="product_detail",
                        url="https://example.com/product",
                        score=0.90,
                    ),
                ),
                "unsupported claim": (),
            }
        )
        cases = (
            RetrievalEvaluationCase(
                case_id="answerable",
                project_id="example.com",
                query="product material",
                expected_source_ids=("detail-source",),
                allowed_source_kinds=("product_detail",),
                forbidden_canonical_urls=("https://example.com/category",),
                annotation_status="approved",
            ),
            RetrievalEvaluationCase(
                case_id="refusal",
                project_id="example.com",
                query="unsupported claim",
                expects_refusal=True,
                annotation_status="approved",
            ),
        )

        report = evaluate_retriever(
            retriever_name="fake",
            retriever=retriever,  # type: ignore[arg-type]
            cases=cases,
            k=5,
            clock=_Clock((0.0, 0.01, 0.02, 0.04)),
        )

        metrics = report.metrics
        self.assertEqual(metrics.case_count, 2)
        self.assertEqual(metrics.recall_at_k, 1.0)
        self.assertEqual(metrics.mean_reciprocal_rank, 0.5)
        self.assertEqual(metrics.first_hit_source_kind_accuracy, 0.0)
        self.assertEqual(metrics.wrong_source_rate, 0.5)
        self.assertEqual(metrics.correct_refusal_rate, 1.0)
        self.assertAlmostEqual(metrics.latency_p50_ms, 10.0)
        self.assertAlmostEqual(metrics.latency_p95_ms, 20.0)
        self.assertEqual(
            [query.project_id for query in retriever.queries],
            ["example.com", "example.com"],
        )
        self.assertEqual(
            json.loads(report.to_json())["retriever_name"],
            "fake",
        )

    def test_gap_fill_improvement_tracks_evidence_cost_and_hard_facts(
        self,
    ) -> None:
        report = evaluate_evidence_improvement(
            (
                EvidenceImprovementObservation(
                    case_id="topic_006",
                    scope_id="product-fact",
                    before_sufficiency="missing",
                    after_sufficiency="sufficient",
                    before_hit_count=0,
                    after_hit_count=3,
                    gap_fill_attempts=1,
                    published_source_ids=("source-a",),
                    cost_usd=0.20,
                    before_hard_fact_coverage=0.0,
                    after_hard_fact_coverage=1.0,
                ),
                EvidenceImprovementObservation(
                    case_id="topic_006",
                    scope_id="faq",
                    before_sufficiency="sufficient",
                    after_sufficiency="sufficient",
                    before_hit_count=2,
                    after_hit_count=2,
                    gap_fill_attempts=0,
                    published_source_ids=(),
                    cost_usd=0.0,
                    before_hard_fact_coverage=1.0,
                    after_hard_fact_coverage=1.0,
                ),
            )
        )

        self.assertEqual(report.metrics.scope_count, 2)
        self.assertEqual(report.metrics.improved_scope_rate, 0.5)
        self.assertEqual(report.metrics.sufficient_before_rate, 0.5)
        self.assertEqual(report.metrics.sufficient_after_rate, 1.0)
        self.assertEqual(report.metrics.mean_hit_delta, 1.5)
        self.assertEqual(report.metrics.mean_hard_fact_coverage_before, 0.5)
        self.assertEqual(report.metrics.mean_hard_fact_coverage_after, 1.0)
        self.assertEqual(report.metrics.total_gap_fill_attempts, 1)
        self.assertEqual(report.metrics.published_source_count, 1)
        self.assertAlmostEqual(report.metrics.total_cost_usd, 0.20)

    def test_pending_cases_are_loaded_but_excluded_from_approved_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.jsonl"
            path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "case_id": "pending",
                                "project_id": "example.com",
                                "query": "Needs review",
                                "annotation_status": "pending",
                            }
                        ),
                        json.dumps(
                            {
                                "case_id": "approved",
                                "project_id": "example.com",
                                "query": "Known fact",
                                "expected_source_ids": ["source-1"],
                                "annotation_status": "approved",
                            }
                        ),
                    ]
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                [case.case_id for case in load_evaluation_cases(path)],
                ["approved"],
            )
            self.assertEqual(
                [
                    case.case_id
                    for case in load_evaluation_cases(
                        path,
                        approved_only=False,
                    )
                ],
                ["pending", "approved"],
            )

    def test_loader_rejects_coerced_json_field_types(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "case_id": "bad-types",
                        "project_id": "example.com",
                        "query": "Known fact",
                        "expects_refusal": "false",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "must be a boolean"):
                load_evaluation_cases(path)

    def test_approved_answerable_case_requires_ground_truth(self) -> None:
        with self.assertRaisesRegex(ValueError, "expected_source_ids"):
            RetrievalEvaluationCase(
                case_id="missing-ground-truth",
                project_id="example.com",
                query="Unannotated",
                annotation_status="approved",
            )

    def test_invalid_source_kind_and_forbidden_url_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported values"):
            RetrievalEvaluationCase(
                case_id="bad-kind",
                project_id="example.com",
                query="Facts",
                allowed_source_kinds=("social_media",),
            )
        with self.assertRaisesRegex(ValueError, "absolute HTTP"):
            RetrievalEvaluationCase(
                case_id="bad-url",
                project_id="example.com",
                query="Facts",
                forbidden_canonical_urls=("/blog/not-canonical",),
            )

    def test_dataset_summary_and_atomic_report_writer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases_path = root / "cases.jsonl"
            cases_path.write_text(
                "\n".join(
                    (
                        json.dumps(
                            {
                                "case_id": "pending",
                                "project_id": "example.com",
                                "query": "Needs review",
                            }
                        ),
                        json.dumps(
                            {
                                "case_id": "refusal",
                                "project_id": "example.com",
                                "query": "Unsupported",
                                "expects_refusal": True,
                                "annotation_status": "approved",
                            }
                        ),
                    )
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                dataset_summary(cases_path),
                {
                    "dataset": "cases.jsonl",
                    "case_count": 2,
                    "approved_count": 1,
                    "pending_count": 1,
                    "approved_refusal_count": 1,
                    "runnable_case_count": 1,
                    "fully_approved": False,
                },
            )

            report = evaluate_retriever(
                retriever_name="fake",
                retriever=_Retriever({"Unsupported": ()}),  # type: ignore[arg-type]
                cases=load_evaluation_cases(cases_path),
                clock=_Clock((0.0, 0.01)),
            )
            output = root / "reports" / "baseline.json"
            write_evaluation_report(output, report)
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8"))["retriever_name"],
                "fake",
            )
            self.assertFalse(output.with_suffix(".json.tmp").exists())

    def test_runner_failure_does_not_echo_provider_exception_text(self) -> None:
        secret = "provider-echoed-secret"
        errors = StringIO()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            with (
                patch(
                    "knowledge_agent.evaluation_runner._run_basic_hybrid",
                    side_effect=RuntimeError(f"failed with {secret}"),
                ),
                redirect_stderr(errors),
                self.assertRaises(SystemExit) as caught,
            ):
                main(
                    (
                        "--cases",
                        "unused.jsonl",
                        "--output",
                        str(output),
                    )
                )
        self.assertEqual(caught.exception.code, 2)
        self.assertNotIn(secret, errors.getvalue())
        self.assertIn("RuntimeError", errors.getvalue())
