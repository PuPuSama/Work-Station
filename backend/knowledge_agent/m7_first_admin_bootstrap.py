from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from typing import Mapping, Sequence
from urllib.parse import parse_qs, quote, urlsplit

from dotenv import load_dotenv

from knowledge_agent.database import create_knowledge_engine
from services.first_admin_bootstrap import (
    FirstAdminBootstrapError,
    FirstAdminBootstrapRequest,
    FirstAdminBootstrapService,
)
from services.oidc_identity import OidcProviderSettings


ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT_DIR / "data"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create the first Organization administrator and a one-time "
            "verified-identity invitation."
        )
    )
    parser.add_argument("--organization-id", default="qewitfastener")
    parser.add_argument("--organization-name", default="Qewit Fastener")
    parser.add_argument("--user-id", default="admin")
    parser.add_argument("--display-name", default="Administrator")
    parser.add_argument("--team-id", default="content")
    parser.add_argument("--team-name", default="Content Team")
    parser.add_argument("--project-id", default="qewitfastener.com")
    parser.add_argument("--expires-in-hours", type=int, default=24)
    parser.add_argument(
        "--frontend-base-url",
        default="http://127.0.0.1:3000",
    )
    return parser


def _frontend_base_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlsplit(normalized)
    loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        parsed.scheme not in ({"http", "https"} if loopback else {"https"})
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(
            "frontend_base_url must be HTTPS or an HTTP(S) loopback origin"
        )
    return normalized


def _invitation_path(organization_id: str) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = (DATA_DIR / f"first-admin-invitation-{organization_id}.txt").resolve()
    data_root = DATA_DIR.resolve()
    if not path.is_relative_to(data_root):
        raise ValueError("invitation output must stay inside the data directory")
    return path


def _token_from_url(value: str, expected_base_url: str) -> str:
    parsed = urlsplit(value.strip())
    expected = urlsplit(expected_base_url)
    if (
        parsed.scheme != expected.scheme
        or parsed.netloc != expected.netloc
        or parsed.path != "/accept-invite"
        or parsed.query
    ):
        raise ValueError("existing invitation file is invalid")
    values = parse_qs(parsed.fragment, strict_parsing=True)
    token_values = values.get("token", [])
    if len(token_values) != 1 or not token_values[0].strip():
        raise ValueError("existing invitation file is invalid")
    return token_values[0].strip()


def _load_or_create_invitation(
    *,
    path: Path,
    frontend_base_url: str,
    service: FirstAdminBootstrapService,
) -> tuple[str, str]:
    if path.exists():
        invitation_url = path.read_text(encoding="utf-8").strip()
        return invitation_url, _token_from_url(
            invitation_url,
            frontend_base_url,
        )
    token = service.create_token()
    invitation_url = (
        f"{frontend_base_url}/accept-invite#token={quote(token, safe='')}"
    )
    with path.open("x", encoding="utf-8", newline="\n") as output:
        output.write(f"{invitation_url}\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return invitation_url, token


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    if environment is None:
        load_dotenv(ROOT_DIR / ".env")
        load_dotenv(ROOT_DIR / "backend" / ".env")
        source: Mapping[str, str] = os.environ
    else:
        source = environment
    database_url = source.get("ARTICLE_AGENT_DATABASE_URL", "").strip()
    if not database_url:
        raise ValueError("ARTICLE_AGENT_DATABASE_URL is required")
    oidc = OidcProviderSettings.from_environment(source)
    if oidc is None:
        raise ValueError("OIDC provider configuration is required")
    frontend_base_url = _frontend_base_url(args.frontend_base_url)
    engine = create_knowledge_engine(database_url)
    try:
        service = FirstAdminBootstrapService(engine)
        output_path = _invitation_path(args.organization_id)
        _, invitation_token = _load_or_create_invitation(
            path=output_path,
            frontend_base_url=frontend_base_url,
            service=service,
        )
        result = service.bootstrap(
            FirstAdminBootstrapRequest(
                organization_id=args.organization_id,
                organization_name=args.organization_name,
                user_id=args.user_id,
                display_name=args.display_name,
                team_id=args.team_id,
                team_name=args.team_name,
                project_id=args.project_id,
                issuer=oidc.issuer,
                invitation_token=invitation_token,
                expires_in_hours=args.expires_in_hours,
            )
        )
    finally:
        engine.dispose()
    print(
        " ".join(
            (
                "FIRST_ADMIN_BOOTSTRAP_READY",
                f"state={result.state}",
                f"organization={result.organization_id}",
                f"user={result.user_id}",
                f"team={result.team_id}",
                f"project={result.project_id}",
                f"invitation_file={output_path}",
            )
        )
    )
    return 0


def cli() -> None:
    try:
        raise SystemExit(main())
    except (FirstAdminBootstrapError, OSError, ValueError):
        print("FIRST_ADMIN_BOOTSTRAP_FAILED", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    cli()
