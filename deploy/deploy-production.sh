#!/usr/bin/env bash
set -Eeuo pipefail

target_commit="${1:?target commit is required}"
repository="/home/ubuntu/Work-Station"
release_root="/home/ubuntu/.cache/article-agent-deploy"
release_directory="$release_root/$target_commit"
workspace="$repository/workspace"
environment_file="$repository/.env"
compose_project="article"
replacement_started=0
docker_command=(sudo -n env "ARTICLE_WORKSPACE_PATH=$workspace" "ARTICLE_ENV_FILE=$environment_file" docker)

export ARTICLE_WORKSPACE_PATH="$workspace"
export ARTICLE_ENV_FILE="$environment_file"

cleanup_release() {
  cd "$repository"
  if git worktree list --porcelain | grep -Fqx "worktree $release_directory"; then
    git worktree remove --force "$release_directory"
  fi
  git worktree prune
}

upgrade_database() {
  "${docker_command[@]}" compose -p "$compose_project" run \
    -T \
    --rm \
    --no-deps \
    backend \
    python -m alembic -c /app/backend/alembic.ini upgrade head
  "${docker_command[@]}" compose -p "$compose_project" run \
    -T \
    --rm \
    --no-deps \
    backend \
    python -m knowledge_agent.checkpoint_setup
}

rollback() {
  status=$?
  trap - ERR
  echo "Deployment failed with status $status." >&2
  if [[ "$replacement_started" == "1" ]]; then
    "${docker_command[@]}" compose -p "$compose_project" ps || true
    "${docker_command[@]}" compose -p "$compose_project" logs \
      --no-color \
      --tail 200 \
      backend || true
    echo "Rebuilding and restoring the previously checked-out release." >&2
    cd "$repository"
    "${docker_command[@]}" compose -p "$compose_project" build
    "${docker_command[@]}" compose -p "$compose_project" up \
      -d \
      --remove-orphans \
      --wait \
      --wait-timeout 180 || true
  fi
  cleanup_release || true
  exit "$status"
}
trap rollback ERR

for command in git docker python3 sudo; do
  command -v "$command" >/dev/null || {
    echo "Required command is missing: $command" >&2
    exit 1
  }
done
"${docker_command[@]}" compose version >/dev/null

cd "$repository"
test -f docker-compose.yml
test -f "$environment_file" || {
  echo "Missing server environment file: $environment_file" >&2
  exit 1
}
test -d "$workspace" || {
  echo "Missing persistent workspace: $workspace" >&2
  exit 1
}

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "Server checkout has tracked local changes; refusing to overwrite them." >&2
  git status --short >&2
  exit 1
fi

git fetch --prune origin main
git cat-file -e "$target_commit^{commit}"
current_commit="$(git rev-parse HEAD)"

if [[ "$current_commit" == "$target_commit" ]]; then
  echo "Commit $target_commit is already checked out; verifying the deployment."
  "${docker_command[@]}" compose -p "$compose_project" build --pull
  upgrade_database
  "${docker_command[@]}" compose -p "$compose_project" up \
    -d \
    --remove-orphans \
    --wait \
    --wait-timeout 180
  "${docker_command[@]}" compose -p "$compose_project" ps
  exit 0
fi

if git merge-base --is-ancestor "$target_commit" "$current_commit"; then
  echo "Commit $target_commit has already been superseded by $current_commit; nothing to deploy."
  exit 0
fi

if ! git merge-base --is-ancestor "$current_commit" "$target_commit"; then
  echo "Target commit is not a fast-forward from the server checkout." >&2
  exit 1
fi

mkdir -p "$release_root"
cleanup_release
git worktree add --detach "$release_directory" "$target_commit"

cd "$release_directory"
"${docker_command[@]}" compose -p "$compose_project" config --quiet
"${docker_command[@]}" compose -p "$compose_project" build --pull
upgrade_database
replacement_started=1
"${docker_command[@]}" compose -p "$compose_project" up \
  -d \
  --remove-orphans \
  --wait \
  --wait-timeout 180

cd "$repository"
git merge --ff-only "$target_commit"
replacement_started=0
trap - ERR
cleanup_release || echo "Warning: release worktree cleanup failed." >&2
"${docker_command[@]}" compose -p "$compose_project" ps
echo "Successfully deployed $target_commit."
