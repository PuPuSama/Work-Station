from __future__ import annotations

import hashlib
import os
import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent import (  # noqa: E402
    KnowledgeChunk,
    KnowledgeProject,
    KnowledgeSource,
    PostgresKnowledgeRepository,
    PostgresRetrievalPlanRepository,
    RetrievalPlan,
    RetrievalScope,
    SourceSnapshot,
    create_knowledge_engine,
)
from knowledge_agent.schema import (  # noqa: E402
    evidence_pack_hits,
    evidence_packs,
    knowledge_chunks,
    knowledge_sources,
    projects,
    research_graph_runs,
    retrieval_plans,
    retrieval_scopes,
    source_snapshots,
)
from models import TaskRecord  # noqa: E402
from services.job_queue import JobConflict  # noqa: E402
from services.server_article_generation import (  # noqa: E402
    _latest_completed_research_context,
    _validate_pinned_research_context,
)


NOW = datetime(2026, 8, 10, tzinfo=timezone.utc)


class ServerArticleEvidenceContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        database_url = os.environ.get("ARTICLE_AGENT_DATABASE_URL", "").strip()
        if not database_url:
            raise unittest.SkipTest("ARTICLE_AGENT_DATABASE_URL is not set")
        cls.engine = create_knowledge_engine(database_url)

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "engine"):
            cls.engine.dispose()

    def setUp(self) -> None:
        self.prefix = f"article-evidence-{uuid.uuid4().hex}"
        self.project_id = f"{self.prefix}.example.test"
        self.organization_id = f"{self.prefix}-org"
        self.task = TaskRecord(
            id=f"{self.prefix}-task",
            week_folder="server",
            customer=self.project_id,
            topic_index=7,
            topic="Evidence topic",
            status="outline_confirmed",
            selected_title="Evidence title",
            outline="## Facts\n\n### Detail A\n\n### Detail B\n\n## FAQ",
            task_dir=f"/server/{self.prefix}",
            created_at=NOW.isoformat(),
            updated_at=NOW.isoformat(),
        )
        knowledge = PostgresKnowledgeRepository(self.engine)
        knowledge.upsert_project(
            KnowledgeProject(
                project_id=self.project_id,
                customer_name="Evidence Project",
                official_domain=self.project_id,
            )
        )
        source_id = f"{self.prefix}-source"
        snapshot_id = f"{self.prefix}-snapshot"
        knowledge.upsert_source(
            KnowledgeSource(
                project_id=self.project_id,
                source_id=source_id,
                display_name="Evidence Source",
                source_kind="knowledge_page",
                trust_tier="hard_fact",
            )
        )
        self.chunk_ids = tuple(f"{snapshot_id}:{index}" for index in range(8))
        knowledge.store_snapshot(
            self.project_id,
            SourceSnapshot(
                project_id=self.project_id,
                source_id=source_id,
                snapshot_id=snapshot_id,
                content_hash=hashlib.sha256(snapshot_id.encode()).hexdigest(),
                fetched_at=NOW,
                parser_name="test",
                parser_version="1",
            ),
            tuple(
                KnowledgeChunk(
                    project_id=self.project_id,
                    source_id=source_id,
                    snapshot_id=snapshot_id,
                    chunk_id=chunk_id,
                    ordinal=index,
                    text=f"Evidence {index}",
                )
                for index, chunk_id in enumerate(self.chunk_ids)
            ),
        )
        self.plan_id = f"{self.prefix}-plan"
        self.pack_id = f"{self.prefix}-pack"
        outline_hash = hashlib.sha256(
            self.task.outline.strip().encode("utf-8")
        ).hexdigest()
        PostgresRetrievalPlanRepository(self.engine).save_retrieval_plan(
            RetrievalPlan(
                project_id=self.project_id,
                retrieval_plan_id=self.plan_id,
                article_id="topic_007",
                outline_version=1,
                scopes=(
                    RetrievalScope(
                        project_id=self.project_id,
                        retrieval_plan_id=self.plan_id,
                        scope_id=f"{self.prefix}-scope",
                        ordinal=0,
                        scope_type="h2_section",
                        scope_key="facts",
                        title="Facts",
                        query_variants=("facts",),
                    ),
                ),
                metadata={
                    "generated_from": "confirmed_task_outline",
                    "task_id": self.task.id,
                    "outline_hash": outline_hash,
                },
                created_at=NOW,
            )
        )
        self.thread_id = f"{self.prefix}-thread"
        with self.engine.begin() as connection:
            connection.execute(
                evidence_packs.insert().values(
                    project_id=self.project_id,
                    evidence_pack_id=self.pack_id,
                    retrieval_plan_id=self.plan_id,
                    scope_id=f"{self.prefix}-scope",
                    article_id="topic_007",
                    outline_version=1,
                    sufficiency="sufficient",
                    gap_reasons=[],
                    hard_fact_chunk_ids=list(self.chunk_ids),
                    public_citation_urls=[],
                    created_at=NOW,
                )
            )
            connection.execute(
                evidence_pack_hits.insert(),
                [
                    {
                        "project_id": self.project_id,
                        "evidence_pack_id": self.pack_id,
                        "chunk_id": chunk_id,
                        "rank": rank,
                        "score": 1 - rank / 100,
                    }
                    for rank, chunk_id in enumerate(self.chunk_ids, start=1)
                ],
            )
            connection.execute(
                research_graph_runs.insert().values(
                    project_id=self.project_id,
                    thread_id=self.thread_id,
                    organization_id=self.organization_id,
                    retrieval_plan_id=self.plan_id,
                    article_id="topic_007",
                    outline_version=1,
                    status="completed",
                    current_node="completed",
                    current_scope_id=None,
                    gap_fill_round=0,
                    max_gap_fill_rounds=0,
                    discovery_queries_used=0,
                    max_discovery_queries=0,
                    evidence_pack_ids=[self.pack_id],
                    warnings=[],
                    metadata={},
                    finished_at=NOW,
                )
            )

    def tearDown(self) -> None:
        with self.engine.begin() as connection:
            for table in (
                research_graph_runs,
                evidence_pack_hits,
                evidence_packs,
                retrieval_scopes,
                retrieval_plans,
                knowledge_chunks,
                source_snapshots,
                knowledge_sources,
                projects,
            ):
                connection.execute(
                    table.delete().where(table.c.project_id == self.project_id)
                )

    def test_latest_completed_run_supplies_bounded_pinned_evidence(self) -> None:
        context = _latest_completed_research_context(
            self.engine,
            organization_id=self.organization_id,
            project_id=self.project_id,
            task=self.task,
        )
        assert context is not None
        self.assertEqual(context.evidence_pack_ids, (self.pack_id,))
        self.assertEqual(context.chunk_ids, self.chunk_ids[:6])
        _validate_pinned_research_context(
            self.engine,
            organization_id=self.organization_id,
            project_id=self.project_id,
            task=self.task,
            thread_id=context.thread_id,
            retrieval_plan_id=context.retrieval_plan_id,
            evidence_pack_ids=context.evidence_pack_ids,
            chunk_ids=context.chunk_ids,
        )

    def test_changed_pinned_pack_identity_is_rejected(self) -> None:
        context = _latest_completed_research_context(
            self.engine,
            organization_id=self.organization_id,
            project_id=self.project_id,
            task=self.task,
        )
        assert context is not None
        with self.engine.begin() as connection:
            connection.execute(
                research_graph_runs.update()
                .where(
                    research_graph_runs.c.project_id == self.project_id,
                    research_graph_runs.c.thread_id == self.thread_id,
                )
                .values(evidence_pack_ids=[])
            )
        with self.assertRaisesRegex(JobConflict, "evidence context changed"):
            _validate_pinned_research_context(
                self.engine,
                organization_id=self.organization_id,
                project_id=self.project_id,
                task=self.task,
                thread_id=context.thread_id,
                retrieval_plan_id=context.retrieval_plan_id,
                evidence_pack_ids=context.evidence_pack_ids,
                chunk_ids=context.chunk_ids,
            )

    def test_outline_hash_mismatch_falls_back(self) -> None:
        changed = self.task.model_copy(update={"outline": self.task.outline + "\nchanged"})
        self.assertIsNone(
            _latest_completed_research_context(
                self.engine,
                organization_id=self.organization_id,
                project_id=self.project_id,
                task=changed,
            )
        )


if __name__ == "__main__":
    unittest.main()
