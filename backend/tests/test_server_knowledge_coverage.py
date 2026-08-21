from __future__ import annotations

import unittest

from models import KnowledgeCoverageCheck, TaskRecord
from server_project_http import ProjectKnowledgeCoverageDetailResponse
from services.server_knowledge_coverage import (
    CoverageEvidenceChunk,
    SentenceSupportDecision,
    ServerKnowledgeCoverageService,
    extract_article_sentences,
    sentence_content_hash,
)


ARTICLE = """img.hero.webp

# Industrial Enclosure Guide

An IP65 enclosure limits dust ingress and resists water jets during normal operation. Buyers should still confirm cable-entry sealing for the installed environment.

## Selection

### Compare the application

- Confirm the mounting location before approving the enclosure.

| Item | Requirement |
| --- | --- |
| Ingress rating | IP65 for exposed production areas |

## FAQ

### What should a buyer confirm before ordering?

Confirm the final cable-entry layout in the approved drawing before production.
"""


class FakeContext:
    def __init__(self, chunks):
        self.chunks = tuple(chunks)

    def load(self, **_kwargs):
        return self.chunks


class FakeProvider:
    ready = True

    def __init__(self, decisions):
        self.decisions = tuple(decisions)

    def evaluate_for_organization(self, **_kwargs):
        return self.decisions


class FakeLinks:
    def __init__(self):
        self.saved = []

    def save_evidence_link(self, link):
        self.saved.append(link)


class FakeDetails:
    def __init__(self, rows):
        self.rows = tuple(rows)

    def load(self, **_kwargs):
        return self.rows


def task(article: str = ARTICLE) -> TaskRecord:
    return TaskRecord(
        id="task-a",
        week_folder="server",
        customer="example.com",
        topic_index=7,
        topic="Industrial enclosures",
        selected_title="Industrial Enclosure Guide",
        outline="# Industrial Enclosure Guide\n\n## Selection",
        task_dir="/server/task-a",
        article=article,
        initial_article=article,
        created_at="2026-08-21T00:00:00+00:00",
        updated_at="2026-08-21T00:00:00+00:00",
    )


class SentenceExtractionTests(unittest.TestCase):
    def test_excludes_index_headings_questions_and_short_fragments(self) -> None:
        sentences = extract_article_sentences(ARTICLE)
        text = "\n".join(item.text for item in sentences)

        self.assertNotIn("img.hero.webp", text)
        self.assertNotIn("Industrial Enclosure Guide", text)
        self.assertNotIn("What should a buyer confirm", text)
        self.assertIn("An IP65 enclosure limits dust ingress", text)
        self.assertIn("Confirm the mounting location", text)
        self.assertIn("IP65 for exposed production areas", text)
        self.assertIn("Confirm the final cable-entry layout", text)
        self.assertEqual(len(sentences), 5)

    def test_unchanged_sentence_keeps_identity_when_sibling_sentence_changes(self) -> None:
        before = extract_article_sentences(
            "A stable product sentence has enough visible English words. "
            "This second sentence also has enough visible words."
        )
        after = extract_article_sentences(
            "A stable product sentence has enough visible English words. "
            "This replacement sentence has different visible words now."
        )

        self.assertEqual(before[0].sentence_id, after[0].sentence_id)
        self.assertEqual(before[0].sentence_hash, after[0].sentence_hash)
        self.assertNotEqual(before[0].paragraph_hash, after[0].paragraph_hash)
        self.assertNotEqual(sentence_content_hash(before), sentence_content_hash(after))


class KnowledgeCoverageServiceTests(unittest.TestCase):
    hard_fact_chunk = CoverageEvidenceChunk(
        chunk_id="snapshot-hard:0",
        text="The enclosure is rated IP65 for exposed production areas.",
        heading_path=("Ingress protection",),
        source_kind="product_detail",
        trust_tier="hard_fact",
        public_source=True,
        canonical_url="https://example.com/enclosure",
    )
    reference_chunk = CoverageEvidenceChunk(
        chunk_id="snapshot-reference:0",
        text="Confirm mounting and cable-entry layout before production.",
        heading_path=("Buyer checks",),
        source_kind="knowledge_page",
        trust_tier="reference_material",
        public_source=False,
        canonical_url=None,
    )

    def test_persists_sentence_links_and_reports_hard_fact_coverage(self) -> None:
        record = task()
        sentences = extract_article_sentences(ARTICLE)
        decisions = []
        for sentence in sentences:
            hard_fact = "IP65" in sentence.text
            decisions.append(
                SentenceSupportDecision(
                    sentence_id=sentence.sentence_id,
                    supported=True,
                    chunk_ids=(
                        self.hard_fact_chunk.chunk_id
                        if hard_fact
                        else self.reference_chunk.chunk_id,
                    ),
                    support_type="direct" if hard_fact else "paraphrase",
                    hard_fact=hard_fact,
                )
            )
        links = FakeLinks()
        service = ServerKnowledgeCoverageService(
            object(),  # type: ignore[arg-type]
            provider=FakeProvider(decisions),
            context=FakeContext((self.hard_fact_chunk, self.reference_chunk)),  # type: ignore[arg-type]
            links=links,  # type: ignore[arg-type]
        )

        report = service.evaluate_task(
            record,
            organization_id="org-a",
            user_id="user-a",
            project_id="example.com",
        )

        self.assertEqual(report.status, "available")
        self.assertEqual(report.eligible_sentences, len(sentences))
        self.assertEqual(report.supported_sentences, len(sentences))
        self.assertEqual(report.sentence_coverage, 1.0)
        self.assertGreater(report.hard_fact_sentences, 0)
        self.assertEqual(report.hard_fact_coverage, 1.0)
        self.assertEqual(report.evidence_link_count, len(sentences))
        self.assertTrue(
            all(link.support_scope == "sentence" for link in links.saved)
        )
        self.assertTrue(
            all(link.metadata.get("sentence_hash") for link in links.saved)
        )

    def test_hard_fact_does_not_accept_reference_material(self) -> None:
        article = (
            "# Title\n\nThe enclosure has an IP65 rating for exposed production areas."
        )
        record = task(article)
        sentence = extract_article_sentences(article)[0]
        service = ServerKnowledgeCoverageService(
            object(),  # type: ignore[arg-type]
            provider=FakeProvider(
                (
                    SentenceSupportDecision(
                        sentence_id=sentence.sentence_id,
                        supported=True,
                        chunk_ids=(self.reference_chunk.chunk_id,),
                        hard_fact=True,
                    ),
                )
            ),
            context=FakeContext((self.reference_chunk,)),  # type: ignore[arg-type]
            links=FakeLinks(),  # type: ignore[arg-type]
        )

        report = service.evaluate_task(
            record,
            organization_id="org-a",
            user_id="user-a",
            project_id="example.com",
        )

        self.assertEqual(report.supported_sentences, 0)
        self.assertEqual(report.hard_fact_sentences, 1)
        self.assertEqual(report.hard_fact_coverage, 0.0)

    def test_missing_evidence_pack_is_unavailable_not_zero_percent(self) -> None:
        record = task()
        service = ServerKnowledgeCoverageService(
            object(),  # type: ignore[arg-type]
            provider=FakeProvider(()),
            context=FakeContext(()),  # type: ignore[arg-type]
            links=FakeLinks(),  # type: ignore[arg-type]
        )

        report = service.evaluate_task(
            record,
            organization_id="org-a",
            user_id="user-a",
            project_id="example.com",
        )

        self.assertEqual(report.status, "unavailable")
        self.assertIn("Evidence Pack", report.message)

    def test_detail_maps_supported_sentences_to_source_links(self) -> None:
        article = (
            "# Title\n\nBrief note. "
            "The enclosure has an IP65 rating for exposed production areas."
        )
        record = task(article)
        sentence = extract_article_sentences(article)[0]
        coverage_hash = sentence_content_hash((sentence,))
        record.knowledge_coverage = KnowledgeCoverageCheck(
            status="available",
            eligible_sentences=1,
            supported_sentences=1,
            sentence_coverage=1.0,
            hard_fact_sentences=1,
            supported_hard_fact_sentences=1,
            hard_fact_coverage=1.0,
            evidence_link_count=1,
            content_hash=coverage_hash,
            checked_at="2026-08-21T00:00:00+00:00",
        )
        service = ServerKnowledgeCoverageService(
            object(),  # type: ignore[arg-type]
            provider=FakeProvider(()),
            context=FakeContext(()),  # type: ignore[arg-type]
            links=FakeLinks(),  # type: ignore[arg-type]
            details=FakeDetails(
                (
                    {
                        "evidence_link_id": "coverage-link-a",
                        "sentence_id": sentence.sentence_id,
                        "chunk_id": self.hard_fact_chunk.chunk_id,
                        "claim_type": "hard_fact",
                        "support_type": "direct",
                        "metadata": {
                            "coverage_content_hash": coverage_hash,
                            "sentence_hash": sentence.sentence_hash,
                        },
                        "source_id": "source-a",
                        "snapshot_id": "snapshot-a",
                        "heading_path": ["Ingress protection"],
                        "text": self.hard_fact_chunk.text,
                        "display_name": "Official enclosure page",
                        "source_kind": "product_detail",
                        "trust_tier": "hard_fact",
                        "public_source": True,
                        "canonical_url": self.hard_fact_chunk.canonical_url,
                    },
                )
            ),  # type: ignore[arg-type]
        )

        detail = service.read_detail(record, project_id="example.com")
        sentences = [
            item
            for paragraph in detail.paragraphs
            for item in paragraph.sentences
        ]

        self.assertEqual(detail.status, "available")
        self.assertEqual(len(sentences), 2)
        self.assertFalse(sentences[0].eligible)
        self.assertFalse(sentences[0].supported)
        self.assertTrue(sentences[1].supported)
        self.assertTrue(sentences[1].hard_fact)
        self.assertEqual(
            sentences[1].evidence[0].canonical_url,
            "https://example.com/enclosure",
        )
        response = ProjectKnowledgeCoverageDetailResponse.model_validate(detail)
        self.assertEqual(response.paragraphs[0].sentences[1].evidence[0].source_id, "source-a")

    def test_detail_rejects_links_for_stale_article_content(self) -> None:
        record = task()
        record.knowledge_coverage = KnowledgeCoverageCheck(
            status="available",
            content_hash="0" * 64,
            checked_at="2026-08-21T00:00:00+00:00",
        )
        details = FakeDetails(({"unexpected": "row"},))
        service = ServerKnowledgeCoverageService(
            object(),  # type: ignore[arg-type]
            provider=FakeProvider(()),
            context=FakeContext(()),  # type: ignore[arg-type]
            links=FakeLinks(),  # type: ignore[arg-type]
            details=details,  # type: ignore[arg-type]
        )

        detail = service.read_detail(record, project_id="example.com")

        self.assertEqual(detail.status, "stale")
        sentence_details = [
            item
            for paragraph in detail.paragraphs
            for item in paragraph.sentences
        ]
        self.assertEqual(
            sum(1 for item in sentence_details if item.eligible),
            len(extract_article_sentences(ARTICLE)),
        )
        self.assertFalse(any(item.supported for item in sentence_details))


if __name__ == "__main__":
    unittest.main()
