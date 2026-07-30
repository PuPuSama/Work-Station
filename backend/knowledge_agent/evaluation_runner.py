from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from .database import create_knowledge_engine
from .embedding import OpenAICompatibleEmbeddingProvider
from .evaluation import (
    RetrievalEvaluationReport,
    evaluate_retriever,
    load_evaluation_cases,
)
from .hybrid_retriever import BasicHybridRetriever
from .settings import load_knowledge_agent_settings


def dataset_summary(path: Path) -> dict[str, object]:
    """Return annotation readiness without exposing customer evidence text."""

    cases = load_evaluation_cases(path, approved_only=False)
    approved = sum(case.annotation_status == "approved" for case in cases)
    refusals = sum(
        case.annotation_status == "approved" and case.expects_refusal
        for case in cases
    )
    return {
        "dataset": path.name,
        "case_count": len(cases),
        "approved_count": approved,
        "pending_count": len(cases) - approved,
        "approved_refusal_count": refusals,
        "runnable_case_count": approved,
        "fully_approved": approved == len(cases),
    }


def write_evaluation_report(
    path: Path,
    report: RetrievalEvaluationReport,
) -> None:
    """Atomically publish one explicit evaluation artifact."""

    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(report.to_json() + "\n", encoding="utf-8")
    temporary.replace(destination)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect a Knowledge Agent JSONL dataset or run the Basic Hybrid "
            "baseline against approved cases."
        )
    )
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--inspect-only", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--minimum-score", type=float)
    return parser


def _run_basic_hybrid(
    *,
    cases_path: Path,
    output_path: Path,
    k: int,
    minimum_score: float | None,
) -> RetrievalEvaluationReport:
    settings = load_knowledge_agent_settings(
        enabled=True,
        require_ready=True,
    )
    assert settings.database_url is not None
    engine = create_knowledge_engine(settings.database_url)
    provider = OpenAICompatibleEmbeddingProvider.from_settings(settings)
    try:
        report = evaluate_retriever(
            retriever_name="basic-hybrid",
            retriever=BasicHybridRetriever(engine, provider),
            cases=load_evaluation_cases(cases_path),
            k=k,
            minimum_score=minimum_score,
            metadata={
                "dataset": cases_path.name,
                "embedding_model": settings.embedding_model,
                "embedding_dimensions": settings.embedding_dimensions,
            },
        )
        write_evaluation_report(output_path, report)
        return report
    finally:
        provider.close()
        engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    cases_path = arguments.cases.expanduser().resolve()
    if arguments.inspect_only:
        print(json.dumps(dataset_summary(cases_path), ensure_ascii=False))
        return 0
    if arguments.output is None:
        parser.error("--output is required unless --inspect-only is used")

    try:
        report = _run_basic_hybrid(
            cases_path=cases_path,
            output_path=arguments.output,
            k=arguments.k,
            minimum_score=arguments.minimum_score,
        )
    except Exception as exc:
        parser.exit(
            2,
            f"evaluation failed ({type(exc).__name__}); no report was written\n",
        )
    print(
        json.dumps(
            {
                "retriever": report.retriever_name,
                "case_count": report.metrics.case_count,
                "k": report.k,
                "output": str(arguments.output),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "dataset_summary",
    "main",
    "write_evaluation_report",
]
