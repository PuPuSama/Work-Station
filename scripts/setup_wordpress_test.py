"""Provision the disposable WordPress template used by integration tests."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ["docker", "compose", "-f", str(ROOT / "docker-compose.wordpress-test.yml")]
STATE = ROOT / "outputs" / "wordpress-test"
URL = "http://127.0.0.1:8088"
ADMIN = "article_agent_admin"
ADMIN_PASSWORD = "article_agent_admin_local_only"
EDITOR = "article_agent_publisher"


def run(*args: str) -> str:
    result = subprocess.run(
        [*COMPOSE, "run", "--rm", "wpcli", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def main() -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [*COMPOSE, "up", "-d", "db", "wordpress", "--wait", "--wait-timeout", "120"],
        cwd=ROOT,
        check=True,
    )
    installed = subprocess.run(
        [*COMPOSE, "run", "--rm", "wpcli", "core", "is-installed"],
        cwd=ROOT,
        capture_output=True,
    ).returncode == 0
    if not installed:
        run(
            "core", "install", f"--url={URL}", "--title=Article Agent Template",
            f"--admin_user={ADMIN}", f"--admin_password={ADMIN_PASSWORD}",
            "--admin_email=article-agent@example.invalid", "--skip-email",
        )
    run("option", "update", "blogdescription", "Article Agent publishing sandbox")
    run("option", "update", "permalink_structure", "/%postname%/")
    run("theme", "activate", "article-agent-template")
    user_id = subprocess.run(
        [*COMPOSE, "run", "--rm", "wpcli", "user", "get", EDITOR, "--field=ID"],
        cwd=ROOT, text=True, capture_output=True,
    )
    if user_id.returncode != 0:
        run("user", "create", EDITOR, "article-agent-publisher@example.invalid", "--role=editor", f"--user_pass={ADMIN_PASSWORD}")
    else:
        # Keep the disposable test account deterministic after a persisted-volume restart.
        run("user", "update", EDITOR, f"--user_pass={ADMIN_PASSWORD}")
    credential_path = STATE / "credentials.json"
    previous_credentials = {}
    if credential_path.is_file():
        try:
            previous_credentials = json.loads(credential_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous_credentials = {}
    application_password = str(previous_credentials.get("application_password", "")).strip()
    if not application_password:
        application_password = run(
            "user", "application-password", "create", EDITOR, "article-agent-api", "--porcelain"
        ).splitlines()[-1].strip()
    sample_id = subprocess.run(
        [*COMPOSE, "run", "--rm", "wpcli", "post", "list", "--post_type=page", "--name=template-home", "--field=ID"],
        cwd=ROOT, text=True, capture_output=True,
    ).stdout.strip()
    if not sample_id:
        run("post", "create", "--post_type=page", "--post_status=publish", "--post_name=template-home",
            "--post_title=Template Home", "--post_content=This is the Article Agent WordPress test template.")
    credentials = {
        "url": URL,
        "username": EDITOR,
        "web_password": ADMIN_PASSWORD,
        "application_password": application_password,
        "created_at": previous_credentials.get("created_at", time.time()),
    }
    (STATE / "credentials.json").write_text(json.dumps(credentials, indent=2), encoding="utf-8")
    print(f"WordPress template ready at {URL}")
    print(f"Credentials written to {STATE / 'credentials.json'}")


if __name__ == "__main__":
    main()
