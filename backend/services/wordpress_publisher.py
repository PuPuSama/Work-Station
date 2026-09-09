"""Small, draft-only WordPress REST publisher for Server article delivery."""
from __future__ import annotations

import html
import ipaddress
import json
import os
import re
import socket
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx


class WordPressPublisherError(ValueError):
    """A safe, user-facing publishing error without response-body leakage."""

    def __init__(self, message: str, *, retryable: bool = False, status_code: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


_PROJECT_CREDENTIALS_ENV = "ARTICLE_AGENT_WORDPRESS_PROJECT_CREDENTIALS"
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class WordPressSettings:
    base_url: str
    username: str
    app_password: str = field(repr=False)
    timeout_seconds: float = 30.0

    @classmethod
    def from_environment(cls) -> "WordPressSettings":
        base_url = os.getenv("ARTICLE_AGENT_WORDPRESS_URL", "").strip().rstrip("/")
        username = os.getenv("ARTICLE_AGENT_WORDPRESS_USERNAME", "").strip()
        app_password = os.getenv("ARTICLE_AGENT_WORDPRESS_APP_PASSWORD", "").strip()
        if not base_url or not username or not app_password:
            raise WordPressPublisherError(
                "WordPress 尚未配置，请设置站点地址、发布账号和应用密码。"
            )
        _validate_wordpress_base_url(base_url)
        return cls(base_url=base_url, username=username, app_password=app_password)

    @classmethod
    def from_environment_for_url(
        cls,
        base_url: str,
        *,
        project_id: str | None = None,
        credentials: tuple[str, str] | None = None,
    ) -> "WordPressSettings":
        """Use saved project credentials, mapped env credentials, or a default."""
        username, app_password = (
            credentials
            if credentials is not None
            else _resolve_wordpress_credentials(project_id)
        )
        if not str(username or "").strip() or not str(app_password or "").strip():
            raise WordPressPublisherError(
                "WordPress 尚未配置，请设置站点地址、发布账号和应用密码。"
            )
        fallback_url = os.getenv("ARTICLE_AGENT_WORDPRESS_URL", "").strip().rstrip("/")
        normalized = resolve_wordpress_base_url(base_url) or fallback_url
        if not normalized:
            raise WordPressPublisherError("WordPress 尚未配置站点地址。")
        _validate_wordpress_base_url(normalized)
        return cls(
            base_url=normalized,
            username=username,
            app_password=app_password,
        )


def _resolve_wordpress_credentials(project_id: str | None) -> tuple[str, str]:
    """Resolve credentials without persisting or returning secret values to clients.

    A non-empty project mapping is fail-closed: every project that uses WordPress
    must have its own pair of environment-variable references. The legacy global
    variables remain supported when the mapping is unset.
    """
    mapping_raw = os.getenv(_PROJECT_CREDENTIALS_ENV, "").strip()
    if mapping_raw:
        if not project_id or not str(project_id).strip():
            raise WordPressPublisherError(
                "WordPress 未指定项目，无法解析项目级发布凭据。"
            )
        try:
            mapping = json.loads(mapping_raw)
        except (TypeError, ValueError) as exc:
            raise WordPressPublisherError(
                "WordPress 项目凭据映射格式无效。"
            ) from exc
        if not isinstance(mapping, dict):
            raise WordPressPublisherError("WordPress 项目凭据映射格式无效。")
        entry = mapping.get(str(project_id).strip())
        if not isinstance(entry, dict):
            raise WordPressPublisherError("WordPress 尚未配置当前项目的发布凭据。")
        username_env = str(entry.get("username_env") or "").strip()
        password_env = str(entry.get("app_password_env") or "").strip()
        if not _ENV_NAME_RE.fullmatch(username_env) or not _ENV_NAME_RE.fullmatch(password_env):
            raise WordPressPublisherError("WordPress 项目凭据引用格式无效。")
        username = os.getenv(username_env, "").strip()
        app_password = os.getenv(password_env, "").strip()
        if not username or not app_password:
            raise WordPressPublisherError("WordPress 当前项目的发布凭据未配置完整。")
        return username, app_password

    username = os.getenv("ARTICLE_AGENT_WORDPRESS_USERNAME", "").strip()
    app_password = os.getenv("ARTICLE_AGENT_WORDPRESS_APP_PASSWORD", "").strip()
    if not username or not app_password:
        raise WordPressPublisherError(
            "WordPress 尚未配置，请设置站点地址、发布账号和应用密码。"
        )
    return username, app_password


def _validate_wordpress_base_url(base_url: str) -> None:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise WordPressPublisherError("WordPress 地址必须是 http 或 https URL。")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise WordPressPublisherError("WordPress 地址不能包含账号、密码、查询参数或片段。")
    if parsed.scheme != "https":
        host = (parsed.hostname or "").casefold()
        if os.getenv("ARTICLE_AGENT_LOCAL_DEBUG") != "1" or host not in {
            "127.0.0.1", "localhost", "::1"
        }:
            raise WordPressPublisherError("生产 WordPress 连接必须使用 HTTPS。")
    host = parsed.hostname or ""
    try:
        resolved = {
            ipaddress.ip_address(info[4][0])
            for info in socket.getaddrinfo(
                host,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        }
    except (OSError, ValueError):
        raise WordPressPublisherError("WordPress 地址无法解析，请检查站点地址。") from None
    local_debug = os.getenv("ARTICLE_AGENT_LOCAL_DEBUG") == "1"
    if any(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        for address in resolved
    ) and not (local_debug and all(address.is_loopback for address in resolved)):
        raise WordPressPublisherError("WordPress 地址不能指向本机或私有网络。")


def validate_wordpress_base_url(base_url: str) -> str:
    normalized = resolve_wordpress_base_url(base_url)
    if not normalized:
        return ""
    _validate_wordpress_base_url(normalized)
    return normalized


def resolve_wordpress_base_url(base_url: str) -> str:
    """Normalize one project URL without applying the deployment fallback."""
    return str(base_url or "").strip().rstrip("/")


def configured_wordpress_base_url(project_url: str = "") -> str:
    """Return the project URL or deployment fallback in a comparable form."""
    return resolve_wordpress_base_url(project_url) or resolve_wordpress_base_url(
        os.getenv("ARTICLE_AGENT_WORDPRESS_URL", "")
    )


def _safe_filename(value: str, fallback: str = "article-image.webp") -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip(".-")
    if not name.lower().endswith(".webp"):
        name = f"{name or fallback.rsplit('.', 1)[0]}.webp"
    return name[:180]


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").casefold()).strip("-")
    return (f"article-agent-{slug}" if slug else "article-agent-draft")[:180]


def wordpress_slug(value: str) -> str:
    return _safe_slug(value)


def _inline_markdown(value: str, image_urls: Mapping[str, str]) -> str:
    tokens: list[str] = []

    def stash(fragment: str) -> str:
        tokens.append(fragment)
        return f"\x00TOKEN{len(tokens) - 1}\x00"

    def image_replacement(match: re.Match[str]) -> str:
        alt = html.escape(match.group(1).strip(), quote=True)
        target = match.group(2).strip()
        url = image_urls.get(target) or image_urls.get(target.rsplit("/", 1)[-1])
        if not url:
            if re.match(r"https?://", target, re.IGNORECASE):
                url = target
            else:
                raise WordPressPublisherError("正文引用了尚未准备好的图片。")
        return stash(f'<img src="{html.escape(url, quote=True)}" alt="{alt}" loading="lazy" />')

    value = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", image_replacement, value)
    escaped = html.escape(value, quote=False)

    def link_replacement(match: re.Match[str]) -> str:
        href = match.group(2).strip()
        if not re.match(r"https?://", href, re.IGNORECASE):
            return match.group(1)
        return stash(
            f'<a href="{html.escape(href, quote=True)}" rel="noopener noreferrer">'
            f"{match.group(1)}</a>"
        )

    escaped = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", link_replacement, escaped, flags=re.IGNORECASE)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    for index, token in enumerate(tokens):
        escaped = escaped.replace(f"\x00TOKEN{index}\x00", token)
    return escaped


def markdown_to_wordpress_html(markdown: str, image_urls: Mapping[str, str] | None = None) -> str:
    """Convert the supported article Markdown subset into safe WordPress HTML."""
    images = dict(image_urls or {})
    lines = str(markdown or "").replace("\r\n", "\n").split("\n")
    blocks: list[str] = []
    paragraph: list[str] = []
    index = 0

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(f"<p>{_inline_markdown(' '.join(paragraph).strip(), images)}</p>")
            paragraph.clear()

    while index < len(lines):
        line = lines[index].strip()
        if not line:
            flush_paragraph()
            index += 1
            continue
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*#*$", line)
        if heading:
            flush_paragraph()
            level = len(heading.group(1))
            blocks.append(f"<h{level}>{_inline_markdown(heading.group(2), images)}</h{level}>")
            index += 1
            continue
        if re.fullmatch(r"(?:---+|\*\s*\*\s*\*)", line):
            flush_paragraph()
            blocks.append("<hr />")
            index += 1
            continue
        table_separator = index + 1 < len(lines) and re.match(
            r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$", lines[index + 1]
        )
        if "|" in line and table_separator:
            flush_paragraph()

            def cells(raw: str) -> list[str]:
                raw = raw.strip().strip("|")
                return [part.strip() for part in raw.split("|")]

            headers = cells(line)
            table = ["<table><thead><tr>"]
            table.extend(f"<th>{_inline_markdown(cell, images)}</th>" for cell in headers)
            table.append("</tr></thead><tbody>")
            index += 2
            while index < len(lines) and lines[index].strip() and "|" in lines[index]:
                table.append("<tr>")
                table.extend(f"<td>{_inline_markdown(cell, images)}</td>" for cell in cells(lines[index]))
                table.append("</tr>")
                index += 1
            table.append("</tbody></table>")
            blocks.append("".join(table))
            continue
        list_match = re.match(r"^([-*]|\d+[.)])\s+(.+)$", line)
        if list_match:
            flush_paragraph()
            ordered = list_match.group(1)[0].isdigit()
            tag = "ol" if ordered else "ul"
            items: list[str] = []
            while index < len(lines):
                item = re.match(r"^([-*]|\d+[.)])\s+(.+)$", lines[index].strip())
                if not item or item.group(1)[0].isdigit() != ordered:
                    break
                items.append(f"<li>{_inline_markdown(item.group(2), images)}</li>")
                index += 1
            blocks.append(f"<{tag}>{''.join(items)}</{tag}>")
            continue
        marker = line.rsplit("/", 1)[-1]
        if marker in images and re.match(r"^(?:img\.)?[^\s]+\.(?:webp|png|jpg|jpeg)$", marker, re.IGNORECASE):
            flush_paragraph()
            blocks.append(
                f'<figure><img src="{html.escape(images[marker], quote=True)}" alt="Article image" loading="lazy" /></figure>'
            )
            index += 1
            continue
        paragraph.append(line)
        index += 1
    flush_paragraph()
    return "\n".join(blocks)


class WordPressPublisher:
    def __init__(self, settings: WordPressSettings, client: httpx.Client | None = None):
        self.settings = settings
        self._client = client or httpx.Client(
            auth=httpx.BasicAuth(settings.username, settings.app_password),
            timeout=settings.timeout_seconds,
            follow_redirects=False,
        )
        self._owns_client = client is None
        self._api = f"{settings.base_url}/wp-json/wp/v2"

    @classmethod
    def from_environment(cls) -> "WordPressPublisher":
        return cls(WordPressSettings.from_environment())

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._client.request(method, f"{self._api}{path}", **kwargs)
        except httpx.HTTPError as exc:
            raise WordPressPublisherError("WordPress 暂时无法连接，请稍后重试。", retryable=True) from exc
        if response.is_redirect or response.status_code in {301, 308}:
            raise WordPressPublisherError("WordPress 返回了不允许的重定向，请检查站点地址。")
        if response.status_code == 429 or response.status_code >= 500:
            raise WordPressPublisherError("WordPress 暂时不可用，请稍后重试。", retryable=True, status_code=response.status_code)
        if response.status_code >= 400:
            message = "WordPress 拒绝了请求，请检查账号权限或文章数据。"
            if response.status_code in {401, 403}:
                message = "WordPress 账号没有创建草稿或上传媒体的权限。"
            elif response.status_code == 409:
                message = "WordPress 中存在相同标识但内容不同的文章，请人工处理。"
            raise WordPressPublisherError(message, status_code=response.status_code)
        if len(response.content) > 2 * 1024 * 1024:
            raise WordPressPublisherError("WordPress 返回的数据过大，已停止处理。")
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise WordPressPublisherError("WordPress 返回了无法识别的数据。") from exc

    def upload_media(self, data: bytes, filename: str, alt_text: str = "Article image") -> tuple[int, str]:
        payload = self._request(
            "POST",
            "/media",
            content=bytes(data),
            headers={
                "Content-Type": "image/webp",
                "Content-Disposition": f'attachment; filename="{_safe_filename(filename)}"',
            },
        )
        try:
            media_id = int(payload["id"])
            source_url = str(payload.get("source_url") or payload.get("guid", {}).get("rendered") or "")
        except (KeyError, TypeError, ValueError) as exc:
            raise WordPressPublisherError("WordPress 媒体响应缺少必要信息。") from exc
        if not source_url:
            raise WordPressPublisherError("WordPress 未返回媒体地址。")
        if alt_text:
            try:
                self._request("POST", f"/media/{media_id}", json={"alt_text": alt_text[:200]})
            except WordPressPublisherError:
                # Alt text is helpful but must not turn a successfully uploaded
                # immutable media object into a failed article delivery.
                pass
        return media_id, source_url

    def media_source(self, media_id: int) -> str:
        payload = self._request("GET", f"/media/{int(media_id)}")
        try:
            source_url = str(payload.get("source_url") or payload.get("guid", {}).get("rendered") or "")
        except AttributeError as exc:
            raise WordPressPublisherError("WordPress 媒体响应缺少地址。") from exc
        if not source_url:
            raise WordPressPublisherError("WordPress 未返回媒体地址。")
        return source_url

    def test_connection(self) -> dict[str, str]:
        """Verify the configured Application Password can read the account."""
        payload = self._request("GET", "/users/me")
        try:
            user_id = str(int(payload["id"]))
            name = str(payload.get("name") or payload.get("slug") or "")
        except (KeyError, TypeError, ValueError) as exc:
            raise WordPressPublisherError("WordPress 账号响应缺少必要信息。") from exc
        return {"user_id": user_id, "name": name}

    def create_or_get_draft(
        self,
        *,
        title: str,
        content: str,
        slug: str,
        source_hash: str,
        task_id: str,
        excerpt: str = "",
        featured_media: int | None = None,
        meta: Mapping[str, str] | None = None,
    ) -> tuple[str, int, str]:
        existing = self._request("GET", "/posts", params={"slug": slug, "status": "any", "context": "edit", "per_page": 1})
        if isinstance(existing, list) and existing:
            post = existing[0]
            post_meta = post.get("meta") or {}
            if str(post_meta.get("article_agent_source_hash", "")) == source_hash and str(
                post_meta.get("article_agent_task_id", "")
            ) == task_id:
                return "already_exists", int(post["id"]), str(post.get("link") or "")
            raise WordPressPublisherError("WordPress 中已有同标识但来源版本不同的文章，请人工处理。", status_code=409)
        body: dict[str, Any] = {
            "status": "draft",
            "title": title,
            "content": content,
            "slug": slug,
            "excerpt": excerpt,
            "meta": dict(meta or {}) | {
                "article_agent_source_hash": source_hash,
                "article_agent_task_id": task_id,
            },
        }
        if featured_media:
            body["featured_media"] = featured_media
        try:
            post = self._request("POST", "/posts", json=body)
        except WordPressPublisherError as exc:
            if exc.status_code == 400:
                # A concurrent request may have won the deterministic slug.
                existing = self._request("GET", "/posts", params={"slug": slug, "status": "any", "context": "edit", "per_page": 1})
                if isinstance(existing, list) and existing:
                    candidate = existing[0]
                    post_meta = candidate.get("meta") or {}
                    if str(post_meta.get("article_agent_source_hash", "")) == source_hash and str(
                        post_meta.get("article_agent_task_id", "")
                    ) == task_id:
                        return "already_exists", int(candidate["id"]), str(candidate.get("link") or "")
            raise exc
        try:
            return "draft_created", int(post["id"]), str(post.get("link") or "")
        except (KeyError, TypeError, ValueError) as exc:
            raise WordPressPublisherError("WordPress 草稿响应缺少必要信息。") from exc


__all__ = [
    "WordPressPublisher",
    "WordPressPublisherError",
    "WordPressSettings",
    "markdown_to_wordpress_html",
    "validate_wordpress_base_url",
    "wordpress_slug",
]
