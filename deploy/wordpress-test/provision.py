"""Run on myserver in the isolated test directory, never in Work-Station.

Uses Docker Compose only for this test site. Credentials stay outside web volumes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import subprocess

ROOT = Path(__file__).resolve().parent
URL = "https://43.154.92.36"
COMPOSE = ["sudo", "-n", "docker", "compose", "-f", str(ROOT / "compose.yml")]


def private_write(path: Path, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(content)


def main() -> None:
    os.chdir(ROOT)
    os.umask(0o077)
    env = ROOT / ".env"
    if not env.exists():
        private_write(env, f"WP_URL={URL}\nWP_DB_PASSWORD={secrets.token_hex(32)}\n"
                      f"WP_DB_ROOT_PASSWORD={secrets.token_hex(32)}\n")
    credentials_path = ROOT / "credentials.json"
    if credentials_path.exists():
        credentials = json.loads(credentials_path.read_text())
    else:
        credentials = dict(url=URL, username="article_agent_publisher",
                           web_password=secrets.token_urlsafe(24),
                           admin_username="article_agent_admin",
                           admin_password=secrets.token_urlsafe(24))
        private_write(credentials_path, json.dumps(credentials, indent=2))
    subprocess.run([*COMPOSE, "up", "-d", "db", "wordpress", "--wait",
                    "--wait-timeout", "180"], check=True)
    result = subprocess.run(
        [*COMPOSE, "run", "--rm", "-T", "wpcli", "eval-file", "/opt/bootstrap.php",
         "--skip-wordpress"], input=json.dumps(credentials), text=True, capture_output=True)
    if result.returncode:
        # Do not echo subprocess output: bootstrap handles private credential input.
        raise SystemExit("WordPress bootstrap failed; credential output withheld.")
    saved = json.loads(result.stdout)
    if not saved.get("application_password"):
        raise SystemExit("WordPress bootstrap did not return an application password.")
    private_write(credentials_path, json.dumps(saved, indent=2))
    print(f"Test site initialized: {URL}; credentials: {credentials_path}")


if __name__ == "__main__":
    main()
