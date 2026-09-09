"""Run the real Server UI with an isolated, automatically signed-in test user.

Usage: backend/.venv/Scripts/python.exe scripts/local_debug.py
Ctrl+C stops the two child servers. Docker volumes are retained for the next run.
"""
from __future__ import annotations

import os
import json
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "outputs" / "local-debug"
DATABASE = "postgresql+psycopg://article_debug:article_debug_local@127.0.0.1:55438/article_agent_debug"


def environment() -> dict[str, str]:
    # Do not inherit application credentials or let dotenv fall back to the repo.
    env = {k: v for k, v in os.environ.items() if not k.startswith((
        "ARTICLE_", "APP_", "LLM_", "OPENAI_", "ANTHROPIC_", "TAVILY_",
        "ZEROGPT_", "OIDC_", "AWS_", "NEXT_PUBLIC_",
    ))}
    STATE.mkdir(parents=True, exist_ok=True)
    empty_env = STATE / "empty.env"
    empty_env.write_text("# Intentionally empty local-debug environment.\n", encoding="utf-8")
    secret_path = STATE / "session.key"
    if not secret_path.exists():
        with secret_path.open("x", encoding="utf-8") as secret_file:
            secret_file.write(secrets.token_urlsafe(48))
    session_secret = secret_path.read_text(encoding="utf-8").strip()
    if len(session_secret) < 32:
        raise RuntimeError("The local debug session key is invalid.")
    env.update({
        "PYTHONPATH": str(ROOT / "backend"),
        "PYTHONUNBUFFERED": "1",
        "ARTICLE_AGENT_LOCAL_DEBUG": "1",
        "ARTICLE_AGENT_ROOT": str(ROOT),
        "ARTICLE_AGENT_CONFIG": str(ROOT / "config.local-debug.yaml"),
        "ARTICLE_AGENT_ENV_FILE": str(empty_env),
        "ARTICLE_AGENT_DATABASE_URL": DATABASE,
        "ARTICLE_AGENT_SERVER_SESSION_SECRET": session_secret,
        # Reuse the ignored, per-environment session key for local encryption.
        # Production must provide its own stable secret through the same setting.
        "ARTICLE_AGENT_WORDPRESS_CREDENTIALS_KEY": session_secret,
        "ARTICLE_AGENT_OBJECT_STORE_ENDPOINT": "http://127.0.0.1:59018",
        "ARTICLE_AGENT_OBJECT_STORE_BUCKET": "article-agent-debug",
        "ARTICLE_AGENT_OBJECT_STORE_ACCESS_KEY": "article_debug",
        "ARTICLE_AGENT_OBJECT_STORE_SECRET_KEY": "article_debug_local",
        "ARTICLE_AGENT_OBJECT_STORE_SSE": "none",
        "ARTICLE_AGENT_API_PROXY_TARGET": "http://127.0.0.1:8108",
        "APP_PASSWORD": "",
        "APP_SESSION_SECRET": "",
        "APP_COOKIE_SECURE": "false",
        "LLM_API_KEY": "",
        "OPENAI_API_KEY": "",
        "TAVILY_API_KEY": "",
        "ZEROGPT_API_KEY": "",
        "NODE_ENV": "development",
        "NEXT_TELEMETRY_DISABLED": "1",
    })
    wordpress_credentials = ROOT / "outputs" / "wordpress-test" / "credentials.json"
    if wordpress_credentials.is_file():
        try:
            credentials = json.loads(wordpress_credentials.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            credentials = {}
        for key, field in (
            ("ARTICLE_AGENT_WORDPRESS_URL", "url"),
            ("ARTICLE_AGENT_WORDPRESS_USERNAME", "username"),
            ("ARTICLE_AGENT_WORDPRESS_APP_PASSWORD", "application_password"),
        ):
            value = str(credentials.get(field, "")).strip()
            if value:
                env[key] = value
    return env


def prepare(env: dict[str, str]) -> None:
    from local_debug_server import validate_environment
    validate_environment(env)
    for port in (8108, 3108):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                raise RuntimeError(f"Debug port {port} is occupied; leave the existing service intact.") from None
    subprocess.run(["docker", "compose", "-f", str(ROOT / "docker-compose.debug.yml"),
                    "up", "-d", "--wait", "--wait-timeout", "60"],
                   cwd=ROOT, env=env, check=True)
    subprocess.run([sys.executable, "-m", "alembic", "-c", "backend/alembic.ini", "upgrade", "head"],
                   cwd=ROOT, env=env, check=True)
    subprocess.run([sys.executable, "-m", "knowledge_agent.checkpoint_setup"],
                   cwd=ROOT, env=env, check=True)
    # Apply the same environment before importing application configuration.
    os.environ.clear()
    os.environ.update(env)
    sys.path.insert(0, str(ROOT / "backend"))
    import sqlalchemy as sa
    from sqlalchemy.dialects.postgresql import insert
    from knowledge_agent.schema import projects
    from server_schema import organizations, workspace_users, project_ownership
    engine = sa.create_engine(DATABASE)
    try:
        with engine.begin() as connection:
            for table, values in (
                (organizations, dict(organization_id="local-debug-org", name="Local Debug")),
                (workspace_users, dict(organization_id="local-debug-org", user_id="local-debug-user",
                                      display_name="Local Debug User", organization_role="org_admin")),
                (projects, dict(project_id="local-debug.example.test", customer_name="Local Debug Project",
                                official_domain="local-debug.example.test")),
                (project_ownership, dict(project_id="local-debug.example.test", organization_id="local-debug-org",
                                         owner_user_id="local-debug-user")),
            ):
                connection.execute(insert(table).values(**values).on_conflict_do_nothing())
    finally:
        engine.dispose()
    import boto3
    client = boto3.client("s3", endpoint_url=env["ARTICLE_AGENT_OBJECT_STORE_ENDPOINT"],
                          aws_access_key_id="article_debug", aws_secret_access_key="article_debug_local",
                          region_name="us-east-1")
    try:
        for attempt in range(30):
            try:
                client.create_bucket(Bucket="article-agent-debug")
                break
            except client.exceptions.BucketAlreadyOwnedByYou:
                break
            except Exception:
                if attempt == 29:
                    raise
                time.sleep(1)
    finally:
        client.close()


def main() -> None:
    env = environment()
    prepare(env)
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Node.js is required for the local frontend.")
    commands = (
        ([sys.executable, "-m", "uvicorn", "local_debug_server:create_app", "--factory",
          "--app-dir", "scripts", "--host", "127.0.0.1", "--port", "8108", "--no-proxy-headers"], ROOT),
        ([node, "node_modules/next/dist/bin/next", "dev", "--hostname", "127.0.0.1", "--port", "3108"], ROOT / "frontend"),
    )
    children = []
    try:
        for (command, cwd), label in zip(commands, ("backend", "frontend")):
            with (STATE / f"{label}.log").open("w", encoding="utf-8") as log:
                children.append(subprocess.Popen(command, cwd=cwd, env=env, stdout=log,
                    stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
        print("Local debug: http://127.0.0.1:3108 (automatic test account)", flush=True)
        print("Isolated test data only; model keys are disabled. Logs: outputs/local-debug/", flush=True)
        while all(child.poll() is None for child in children):
            time.sleep(1)
        raise RuntimeError("A debug server exited; inspect its local log.")
    except KeyboardInterrupt:
        print("Stopping local debug servers; test data is retained.", flush=True)
    finally:
        for child in reversed(children):
            if child.poll() is None:
                if os.name == "nt":
                    # Next dev has a worker process; terminate only our live child tree.
                    subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW, check=False)
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()


if __name__ == "__main__":
    main()
