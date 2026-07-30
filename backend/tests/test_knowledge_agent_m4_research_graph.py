from __future__ import annotations

import sys
import os
import unittest
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres import PostgresSaver


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent.research_graph import (  # noqa: E402
    BoundedResearchGraph,
    CandidateIngestionResult,
    ResearchCandidate,
    ResearchGraphRequest,
    ScopeEvidenceObservation,
    new_research_thread_id,
)
from knowledge_agent.checkpoint_setup import psycopg_connection_url  # noqa: E402


class FakePlans:
    def __init__(self, scope_ids: tuple[str, ...] = ("scope-1",)) -> None:
        self.scope_ids_value = scope_ids
        self.calls: list[dict[str, object]] = []

    def scope_ids(self, **kwargs) -> tuple[str, ...]:
        self.calls.append(dict(kwargs))
        return self.scope_ids_value


class SequencedEvidence:
    def __init__(
        self,
        observations: tuple[ScopeEvidenceObservation, ...],
        *,
        failures: int = 0,
    ) -> None:
        self.observations = observations
        self.failures = failures
        self.calls: list[str] = []

    def retrieve_scope(
        self,
        *,
        scope_id: str,
        **_kwargs,
    ) -> ScopeEvidenceObservation:
        self.calls.append(scope_id)
        if self.failures:
            self.failures -= 1
            raise RuntimeError("temporary retrieval failure")
        index = min(len(self.calls) - 1, len(self.observations) - 1)
        return self.observations[index]


class FakeDiscovery:
    def __init__(
        self,
        candidates: tuple[ResearchCandidate, ...] = (),
    ) -> None:
        self.candidates = candidates
        self.calls: list[dict[str, object]] = []

    def discover(self, **kwargs) -> tuple[ResearchCandidate, ...]:
        self.calls.append(dict(kwargs))
        return self.candidates


class FakeIngestion:
    def __init__(
        self,
        result: CandidateIngestionResult = CandidateIngestionResult(),
    ) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def ingest(self, **kwargs) -> CandidateIngestionResult:
        self.calls.append(dict(kwargs))
        return self.result


def observation(
    suffix: str,
    sufficiency: str,
) -> ScopeEvidenceObservation:
    return ScopeEvidenceObservation(
        evidence_pack_id=f"pack-{suffix}",
        sufficiency=sufficiency,  # type: ignore[arg-type]
        gap_reasons=(
            () if sufficiency == "sufficient" else ("not enough evidence",)
        ),
        chunk_ids=(f"snapshot:{suffix}",),
    )


def request(thread_id: str, *, max_rounds: int = 2) -> ResearchGraphRequest:
    return ResearchGraphRequest(
        organization_id="single-tenant",
        project_id="project-a",
        article_id="article-1",
        outline_version=3,
        retrieval_plan_id="plan-1",
        thread_id=thread_id,
        max_gap_fill_rounds=max_rounds,
        max_discovery_queries=max_rounds,
    )


class BoundedResearchGraphTests(unittest.TestCase):
    def _graph(
        self,
        *,
        plans: FakePlans | None = None,
        evidence: SequencedEvidence,
        discovery: FakeDiscovery | None = None,
        ingestion: FakeIngestion | None = None,
    ) -> BoundedResearchGraph:
        return BoundedResearchGraph(
            plans=plans or FakePlans(),
            evidence=evidence,
            discovery=discovery or FakeDiscovery(),
            ingestion=ingestion or FakeIngestion(),
            checkpointer=InMemorySaver(),
        )

    def test_sufficient_scopes_complete_without_discovery(self) -> None:
        plans = FakePlans(("scope-1", "scope-2"))
        evidence = SequencedEvidence(
            (
                observation("one", "sufficient"),
                observation("two", "sufficient"),
            )
        )
        discovery = FakeDiscovery()
        graph = self._graph(
            plans=plans,
            evidence=evidence,
            discovery=discovery,
        )
        thread_id = new_research_thread_id("project-a", "article-1", 3)

        result = graph.start(request(thread_id))

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["evidence_pack_ids"], ["pack-one", "pack-two"])
        self.assertEqual(evidence.calls, ["scope-1", "scope-2"])
        self.assertEqual(discovery.calls, [])
        self.assertGreater(len(graph.history(thread_id)), 4)

    def test_missing_evidence_stops_after_two_gap_fill_rounds(self) -> None:
        evidence = SequencedEvidence((observation("weak", "weak"),))
        discovery = FakeDiscovery()
        ingestion = FakeIngestion()
        graph = self._graph(
            evidence=evidence,
            discovery=discovery,
            ingestion=ingestion,
        )

        result = graph.start(request("thread-two-rounds"))

        self.assertEqual(result["status"], "completed_with_warnings")
        self.assertEqual(result["gap_fill_round"], 0)
        self.assertEqual(len(discovery.calls), 2)
        self.assertEqual(len(ingestion.calls), 2)
        self.assertEqual(
            [call["round_number"] for call in discovery.calls],
            [1, 2],
        )
        self.assertIn("after 2 gap-fill rounds", result["warnings"][0])

    def test_ambiguous_candidate_interrupts_and_resumes_same_thread(self) -> None:
        candidate = ResearchCandidate(
            candidate_id="candidate-1",
            url="https://project-a.example.test/page",
            page_type="knowledge_page",
            needs_review=True,
            evidence={"reason": "ambiguous page type"},
        )
        evidence = SequencedEvidence(
            (
                observation("before", "weak"),
                observation("after", "sufficient"),
            )
        )
        ingestion = FakeIngestion(
            CandidateIngestionResult(published_source_ids=("source-1",))
        )
        graph = self._graph(
            evidence=evidence,
            discovery=FakeDiscovery((candidate,)),
            ingestion=ingestion,
        )

        interrupted = graph.start(request("thread-review"))

        self.assertEqual(interrupted["status"], "waiting_for_review")
        self.assertIn("__interrupt__", interrupted)
        snapshot = graph.state("thread-review")
        self.assertEqual(snapshot["current_node"], "discover_official_sources")

        completed = graph.resume(
            "thread-review",
            approved_urls=(candidate.url,),
        )

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["published_source_ids"], ["source-1"])
        self.assertEqual(
            ingestion.calls[0]["approved_urls"],
            [candidate.url],
        )
        self.assertEqual(ingestion.calls[0]["round_number"], 1)

    def test_unknown_review_url_is_rejected(self) -> None:
        candidate = ResearchCandidate(
            candidate_id="candidate-1",
            url="https://project-a.example.test/page",
            page_type="knowledge_page",
            needs_review=True,
            evidence={},
        )
        graph = self._graph(
            evidence=SequencedEvidence((observation("before", "weak"),)),
            discovery=FakeDiscovery((candidate,)),
        )
        graph.start(request("thread-invalid-review"))

        with self.assertRaisesRegex(ValueError, "unknown candidates"):
            graph.resume(
                "thread-invalid-review",
                approved_urls=("https://evil.example/page",),
            )

    def test_transient_retrieval_failure_retries_node_only(self) -> None:
        plans = FakePlans()
        evidence = SequencedEvidence(
            (observation("ok", "sufficient"),),
            failures=2,
        )
        graph = self._graph(plans=plans, evidence=evidence)

        result = graph.start(request("thread-retry"))

        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(plans.calls), 1)
        self.assertEqual(len(evidence.calls), 3)


class ResearchGraphContractTests(unittest.TestCase):
    def test_thread_ids_are_unique_and_do_not_expose_business_identity(self) -> None:
        first = new_research_thread_id("secret-project", "article-1", 2)
        second = new_research_thread_id("secret-project", "article-1", 2)

        self.assertNotEqual(first, second)
        self.assertNotIn("secret-project", first)
        self.assertTrue(first.startswith("rg_"))

    def test_request_enforces_bounded_rounds(self) -> None:
        with self.assertRaisesRegex(ValueError, "between 0 and 2"):
            request("thread", max_rounds=3)


@unittest.skipUnless(
    os.environ.get("ARTICLE_AGENT_DATABASE_URL"),
    "ARTICLE_AGENT_DATABASE_URL is required for PostgreSQL checkpoint tests",
)
class PostgresResearchCheckpointTests(unittest.TestCase):
    def test_interrupt_resumes_after_recreating_graph_process_boundary(self) -> None:
        os.environ["LANGGRAPH_STRICT_MSGPACK"] = "true"
        database_url = psycopg_connection_url(
            os.environ["ARTICLE_AGENT_DATABASE_URL"]
        )
        thread_id = new_research_thread_id("project-a", "article-1", 3)
        candidate = ResearchCandidate(
            candidate_id="candidate-1",
            url="https://project-a.example.test/page",
            page_type="knowledge_page",
            needs_review=True,
            evidence={"reason": "operator review required"},
        )

        with PostgresSaver.from_conn_string(database_url) as first_saver:
            first_graph = BoundedResearchGraph(
                plans=FakePlans(),
                evidence=SequencedEvidence((observation("before", "weak"),)),
                discovery=FakeDiscovery((candidate,)),
                ingestion=FakeIngestion(),
                checkpointer=first_saver,
            )
            interrupted = first_graph.start(request(thread_id))
            self.assertEqual(interrupted["status"], "waiting_for_review")

        resumed_evidence = SequencedEvidence(
            (observation("after", "sufficient"),)
        )
        resumed_ingestion = FakeIngestion(
            CandidateIngestionResult(published_source_ids=("source-1",))
        )
        with PostgresSaver.from_conn_string(database_url) as second_saver:
            second_graph = BoundedResearchGraph(
                plans=FakePlans(),
                evidence=resumed_evidence,
                discovery=FakeDiscovery(),
                ingestion=resumed_ingestion,
                checkpointer=second_saver,
            )
            completed = second_graph.resume(
                thread_id,
                approved_urls=(candidate.url,),
            )
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["evidence_pack_ids"], ["pack-after"])
            self.assertEqual(completed["published_source_ids"], ["source-1"])
            self.assertGreater(len(second_graph.history(thread_id)), 5)
            second_saver.delete_thread(thread_id)


if __name__ == "__main__":
    unittest.main()
