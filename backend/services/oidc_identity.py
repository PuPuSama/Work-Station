from __future__ import annotations

import hmac
import os
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx
import jwt
from jwt import PyJWK
from jwt.exceptions import PyJWTError

from services.external_identity import VerifiedExternalIdentity


OIDC_DISCOVERY_SUFFIX = "/.well-known/openid-configuration"
OIDC_ALLOWED_ALGORITHMS = ("RS256",)
OIDC_MAX_DOCUMENT_BYTES = 1024 * 1024
OIDC_MAX_KEYS = 100


class OidcConfigurationError(ValueError):
    """OIDC configuration is incomplete or unsafe."""


class OidcProviderUnavailable(RuntimeError):
    """The configured provider cannot currently complete a request."""


class OidcVerificationError(PermissionError):
    """An ID Token did not satisfy the local OIDC trust policy."""


def _url(
    value: str,
    *,
    field_name: str,
    allow_query: bool,
) -> str:
    normalized = value.strip()
    parsed = urlsplit(normalized)
    try:
        port = parsed.port
    except ValueError as exc:
        raise OidcConfigurationError(
            f"{field_name} must be an absolute HTTPS URL"
        ) from exc
    loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    allowed_schemes = {"http", "https"} if loopback else {"https"}
    if (
        parsed.scheme not in allowed_schemes
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (not allow_query and parsed.query)
        or port is not None
        and not 0 < port < 65536
    ):
        raise OidcConfigurationError(
            f"{field_name} must be an absolute HTTPS URL"
        )
    return normalized


def _required_text(
    value: str,
    *,
    field_name: str,
    max_length: int,
) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        raise OidcConfigurationError(f"{field_name} is invalid")
    return normalized


def _post_login_path(value: str) -> str:
    normalized = value.strip() or "/"
    parsed = urlsplit(normalized)
    if (
        not normalized.startswith("/")
        or normalized.startswith("//")
        or parsed.scheme
        or parsed.netloc
        or parsed.fragment
    ):
        raise OidcConfigurationError(
            "ARTICLE_AGENT_OIDC_POST_LOGIN_PATH must be a local path"
        )
    return normalized


@dataclass(frozen=True, slots=True)
class OidcProviderSettings:
    """Provider-neutral OIDC relying-party configuration."""

    issuer: str
    client_id: str
    client_secret: str = field(repr=False)
    redirect_uri: str
    post_login_path: str = "/"
    request_timeout_seconds: float = 10.0
    cache_seconds: int = 10 * 60
    state_seconds: int = 10 * 60
    session_seconds: int = 12 * 60 * 60
    clock_skew_seconds: int = 60
    max_id_token_age_seconds: int = 15 * 60

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "issuer",
            _url(
                self.issuer,
                field_name="ARTICLE_AGENT_OIDC_ISSUER",
                allow_query=False,
            ),
        )
        object.__setattr__(
            self,
            "client_id",
            _required_text(
                self.client_id,
                field_name="ARTICLE_AGENT_OIDC_CLIENT_ID",
                max_length=512,
            ),
        )
        object.__setattr__(
            self,
            "client_secret",
            _required_text(
                self.client_secret,
                field_name="ARTICLE_AGENT_OIDC_CLIENT_SECRET",
                max_length=4096,
            ),
        )
        object.__setattr__(
            self,
            "redirect_uri",
            _url(
                self.redirect_uri,
                field_name="ARTICLE_AGENT_OIDC_REDIRECT_URI",
                allow_query=True,
            ),
        )
        object.__setattr__(
            self,
            "post_login_path",
            _post_login_path(self.post_login_path),
        )
        if not 1 <= self.request_timeout_seconds <= 60:
            raise OidcConfigurationError(
                "OIDC request timeout must be between 1 and 60 seconds"
            )
        for name, value, maximum in (
            ("cache", self.cache_seconds, 24 * 60 * 60),
            ("state", self.state_seconds, 30 * 60),
            ("session", self.session_seconds, 24 * 60 * 60),
            ("clock skew", self.clock_skew_seconds, 5 * 60),
            ("ID Token age", self.max_id_token_age_seconds, 60 * 60),
        ):
            if value <= 0 or value > maximum:
                raise OidcConfigurationError(
                    f"OIDC {name} lifetime is invalid"
                )

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> OidcProviderSettings | None:
        source = os.environ if environment is None else environment
        names = (
            "ARTICLE_AGENT_OIDC_ISSUER",
            "ARTICLE_AGENT_OIDC_CLIENT_ID",
            "ARTICLE_AGENT_OIDC_CLIENT_SECRET",
            "ARTICLE_AGENT_OIDC_REDIRECT_URI",
        )
        values = {name: source.get(name, "").strip() for name in names}
        if not any(values.values()):
            return None
        if not all(values.values()):
            raise OidcConfigurationError(
                "OIDC provider configuration is incomplete"
            )
        return cls(
            issuer=values["ARTICLE_AGENT_OIDC_ISSUER"],
            client_id=values["ARTICLE_AGENT_OIDC_CLIENT_ID"],
            client_secret=values["ARTICLE_AGENT_OIDC_CLIENT_SECRET"],
            redirect_uri=values["ARTICLE_AGENT_OIDC_REDIRECT_URI"],
            post_login_path=source.get(
                "ARTICLE_AGENT_OIDC_POST_LOGIN_PATH",
                "/",
            ),
        )

    @property
    def discovery_url(self) -> str:
        return f"{self.issuer.rstrip('/')}{OIDC_DISCOVERY_SUFFIX}"


@dataclass(frozen=True, slots=True)
class OidcDiscoveryDocument:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    token_endpoint_auth_methods_supported: tuple[str, ...]
    id_token_signing_alg_values_supported: tuple[str, ...]

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
        *,
        expected_issuer: str,
    ) -> OidcDiscoveryDocument:
        try:
            issuer = _url(
                str(value["issuer"]),
                field_name="OIDC discovery issuer",
                allow_query=False,
            )
            authorization_endpoint = _url(
                str(value["authorization_endpoint"]),
                field_name="OIDC authorization endpoint",
                allow_query=True,
            )
            token_endpoint = _url(
                str(value["token_endpoint"]),
                field_name="OIDC token endpoint",
                allow_query=True,
            )
            jwks_uri = _url(
                str(value["jwks_uri"]),
                field_name="OIDC JWKS endpoint",
                allow_query=True,
            )
        except (KeyError, TypeError, OidcConfigurationError) as exc:
            raise OidcProviderUnavailable(
                "identity provider metadata is invalid"
            ) from exc
        if issuer != expected_issuer:
            raise OidcProviderUnavailable(
                "identity provider metadata is invalid"
            )

        raw_auth_methods = value.get(
            "token_endpoint_auth_methods_supported",
            ["client_secret_basic"],
        )
        raw_algorithms = value.get(
            "id_token_signing_alg_values_supported",
            [],
        )
        if (
            not isinstance(raw_auth_methods, list)
            or not all(isinstance(item, str) for item in raw_auth_methods)
            or not isinstance(raw_algorithms, list)
            or not all(isinstance(item, str) for item in raw_algorithms)
        ):
            raise OidcProviderUnavailable(
                "identity provider metadata is invalid"
            )
        auth_methods = tuple(dict.fromkeys(raw_auth_methods))
        algorithms = tuple(dict.fromkeys(raw_algorithms))
        if "client_secret_basic" not in auth_methods or not set(
            OIDC_ALLOWED_ALGORITHMS
        ).intersection(algorithms):
            raise OidcProviderUnavailable(
                "identity provider metadata is unsupported"
            )
        return cls(
            issuer=issuer,
            authorization_endpoint=authorization_endpoint,
            token_endpoint=token_endpoint,
            jwks_uri=jwks_uri,
            token_endpoint_auth_methods_supported=auth_methods,
            id_token_signing_alg_values_supported=algorithms,
        )


class OidcProviderClient:
    """Fetch validated provider metadata/JWKS and exchange one PKCE code."""

    def __init__(
        self,
        settings: OidcProviderSettings,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.settings = settings
        self._client = client or httpx.Client(
            timeout=settings.request_timeout_seconds,
            follow_redirects=False,
            headers={"Accept": "application/json"},
        )
        self._owns_client = client is None
        self._lock = threading.Lock()
        self._discovery: tuple[float, OidcDiscoveryDocument] | None = None
        self._jwks: tuple[float, tuple[PyJWK, ...]] | None = None

    @staticmethod
    def _json(response: httpx.Response) -> Mapping[str, object]:
        if (
            response.status_code < 200
            or response.status_code >= 300
            or len(response.content) > OIDC_MAX_DOCUMENT_BYTES
        ):
            raise OidcProviderUnavailable(
                "identity provider request failed"
            )
        try:
            value = response.json()
        except (ValueError, TypeError) as exc:
            raise OidcProviderUnavailable(
                "identity provider response is invalid"
            ) from exc
        if not isinstance(value, dict):
            raise OidcProviderUnavailable(
                "identity provider response is invalid"
            )
        return value

    def _get_json(self, url: str) -> Mapping[str, object]:
        try:
            response = self._client.get(
                url,
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise OidcProviderUnavailable(
                "identity provider request failed"
            ) from exc
        return self._json(response)

    def discovery(
        self,
        *,
        force_refresh: bool = False,
    ) -> OidcDiscoveryDocument:
        current = time.monotonic()
        with self._lock:
            cached = self._discovery
            if (
                not force_refresh
                and cached is not None
                and current - cached[0] < self.settings.cache_seconds
            ):
                return cached[1]
        document = OidcDiscoveryDocument.from_mapping(
            self._get_json(self.settings.discovery_url),
            expected_issuer=self.settings.issuer,
        )
        with self._lock:
            self._discovery = (time.monotonic(), document)
        return document

    def _load_keys(
        self,
        *,
        force_refresh: bool = False,
    ) -> tuple[PyJWK, ...]:
        current = time.monotonic()
        with self._lock:
            cached = self._jwks
            if (
                not force_refresh
                and cached is not None
                and current - cached[0] < self.settings.cache_seconds
            ):
                return cached[1]
        document = self.discovery(force_refresh=force_refresh)
        raw = self._get_json(document.jwks_uri)
        values = raw.get("keys")
        if (
            not isinstance(values, list)
            or not values
            or len(values) > OIDC_MAX_KEYS
            or not all(isinstance(item, dict) for item in values)
        ):
            raise OidcProviderUnavailable(
                "identity provider keys are invalid"
            )
        keys: list[PyJWK] = []
        try:
            for value in values:
                key = PyJWK.from_dict(value)
                if (
                    key.key_id
                    and key.key_type == "RSA"
                    and key.public_key_use in {None, "sig"}
                    and key.algorithm_name in OIDC_ALLOWED_ALGORITHMS
                ):
                    keys.append(key)
        except (PyJWTError, ValueError, TypeError) as exc:
            raise OidcProviderUnavailable(
                "identity provider keys are invalid"
            ) from exc
        if not keys:
            raise OidcProviderUnavailable(
                "identity provider keys are invalid"
            )
        result = tuple(keys)
        with self._lock:
            self._jwks = (time.monotonic(), result)
        return result

    def signing_key(self, key_id: str) -> PyJWK:
        normalized = key_id.strip()
        if not normalized or len(normalized) > 512:
            raise OidcVerificationError(
                "OIDC identity verification failed"
            )
        for force_refresh in (False, True):
            matches = [
                key
                for key in self._load_keys(
                    force_refresh=force_refresh
                )
                if key.key_id == normalized
            ]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                break
        raise OidcVerificationError(
            "OIDC identity verification failed"
        )

    def exchange_code(
        self,
        *,
        code: str,
        code_verifier: str,
    ) -> str:
        normalized_code = code.strip()
        if not normalized_code or len(normalized_code) > 4096:
            raise OidcVerificationError(
                "OIDC identity verification failed"
            )
        document = self.discovery()
        try:
            response = self._client.post(
                document.token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "code": normalized_code,
                    "redirect_uri": self.settings.redirect_uri,
                    "client_id": self.settings.client_id,
                    "code_verifier": code_verifier,
                },
                auth=httpx.BasicAuth(
                    self.settings.client_id,
                    self.settings.client_secret,
                ),
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise OidcProviderUnavailable(
                "identity provider request failed"
            ) from exc
        value = self._json(response)
        id_token = value.get("id_token")
        if not isinstance(id_token, str) or not id_token.strip():
            raise OidcVerificationError(
                "OIDC identity verification failed"
            )
        return id_token.strip()

    def check_ready(self) -> None:
        """Verify exact discovery metadata and at least one RS256 key."""

        self._load_keys(force_refresh=True)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


class OidcIdTokenVerifier:
    """Validate an OIDC ID Token and return only issuer/subject identity."""

    def __init__(
        self,
        settings: OidcProviderSettings,
        provider: OidcProviderClient,
    ) -> None:
        self._settings = settings
        self._provider = provider

    def verify(
        self,
        id_token: str,
        *,
        expected_nonce: str,
        now: int | None = None,
    ) -> VerifiedExternalIdentity:
        token = id_token.strip()
        nonce = expected_nonce.strip()
        if (
            not token
            or len(token) > 64 * 1024
            or not nonce
            or len(nonce) > 512
        ):
            raise OidcVerificationError(
                "OIDC identity verification failed"
            )
        try:
            header = jwt.get_unverified_header(token)
            if (
                not isinstance(header, dict)
                or header.get("alg") not in OIDC_ALLOWED_ALGORITHMS
                or not isinstance(header.get("kid"), str)
            ):
                raise TypeError
            key = self._provider.signing_key(header["kid"])
            claims = jwt.decode(
                token,
                key.key,
                algorithms=list(OIDC_ALLOWED_ALGORITHMS),
                audience=self._settings.client_id,
                issuer=self._settings.issuer,
                leeway=self._settings.clock_skew_seconds,
                options={
                    "require": [
                        "iss",
                        "sub",
                        "aud",
                        "exp",
                        "iat",
                        "nonce",
                    ]
                },
            )
            token_nonce = claims["nonce"]
            issued_at = claims["iat"]
            audience = claims["aud"]
            authorized_party = claims.get("azp")
            subject = claims["sub"]
            if (
                not isinstance(token_nonce, str)
                or not hmac.compare_digest(token_nonce, nonce)
                or isinstance(issued_at, bool)
                or not isinstance(issued_at, int | float)
                or not isinstance(subject, str)
                or not subject.strip()
            ):
                raise TypeError
            current = int(time.time() if now is None else now)
            issued = int(issued_at)
            if (
                issued > current + self._settings.clock_skew_seconds
                or current - issued
                > self._settings.max_id_token_age_seconds
            ):
                raise TypeError
            if isinstance(audience, str):
                audiences = (audience,)
            elif isinstance(audience, list) and all(
                isinstance(item, str) for item in audience
            ):
                audiences = tuple(audience)
            else:
                raise TypeError
            if (
                audiences != (self._settings.client_id,)
                or (
                    authorized_party is not None
                    and authorized_party != self._settings.client_id
                )
            ):
                raise TypeError
            return VerifiedExternalIdentity(
                issuer=str(claims["iss"]),
                subject=subject,
            )
        except (
            KeyError,
            TypeError,
            ValueError,
            PyJWTError,
        ) as exc:
            raise OidcVerificationError(
                "OIDC identity verification failed"
            ) from exc


__all__ = [
    "OIDC_ALLOWED_ALGORITHMS",
    "OidcConfigurationError",
    "OidcDiscoveryDocument",
    "OidcIdTokenVerifier",
    "OidcProviderClient",
    "OidcProviderSettings",
    "OidcProviderUnavailable",
    "OidcVerificationError",
]
