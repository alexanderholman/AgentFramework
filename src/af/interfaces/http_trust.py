"""Trusted HTTP host and origin parsing helpers for AF interface boundaries."""

from __future__ import annotations

import re
from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urlsplit

from af.framework.errors import InvalidProviderConfiguration

_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_DEFAULT_ORIGIN_PORTS = {"http": 80, "https": 443}


@dataclass(frozen=True, slots=True)
class ParsedHost:
    host: str
    port: int | None = None


@dataclass(frozen=True, slots=True)
class ParsedOrigin:
    scheme: str
    host: str
    port: int | None = None


def parse_host_value(value: str, *, error_message: str) -> ParsedHost:
    normalized = value.strip()
    if not normalized or any(
        character in normalized for character in ("@", "/", "\\", "?", "#", "*")
    ):
        raise InvalidProviderConfiguration(error_message)
    if normalized.startswith("["):
        end = normalized.find("]")
        if end <= 0:
            raise InvalidProviderConfiguration(error_message)
        host = _canonicalize_host(normalized[1:end], error_message=error_message)
        if ":" not in host:
            raise InvalidProviderConfiguration(error_message)
        remainder = normalized[end + 1 :]
        if not remainder:
            return ParsedHost(host)
        if not remainder.startswith(":"):
            raise InvalidProviderConfiguration(error_message)
        return ParsedHost(host, _parse_port(remainder[1:], error_message=error_message))
    colon_count = normalized.count(":")
    if colon_count == 0:
        return ParsedHost(_canonicalize_host(normalized, error_message=error_message))
    if colon_count == 1:
        host_part, port_part = normalized.rsplit(":", 1)
        if not host_part:
            raise InvalidProviderConfiguration(error_message)
        return ParsedHost(
            _canonicalize_host(host_part, error_message=error_message),
            _parse_port(port_part, error_message=error_message),
        )
    host = _canonicalize_host(normalized, error_message=error_message)
    if ":" not in host:
        raise InvalidProviderConfiguration(error_message)
    return ParsedHost(host)


def parse_origin_value(value: str, *, error_message: str) -> ParsedOrigin:
    if (
        value != value.strip()
        or not value
        or "*" in value
        or any(character.isspace() or _is_control_char(character) for character in value)
    ):
        raise InvalidProviderConfiguration(error_message)
    scheme_separator = value.find("://")
    if scheme_separator <= 0:
        raise InvalidProviderConfiguration(error_message)
    authority = value[scheme_separator + 3 :]
    if (
        not authority
        or authority.endswith(":")
        or any(delimiter in authority for delimiter in ("/", "?", "#"))
    ):
        raise InvalidProviderConfiguration(error_message)
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise InvalidProviderConfiguration(error_message) from None
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise InvalidProviderConfiguration(error_message)
    try:
        port = parsed.port
    except ValueError:
        raise InvalidProviderConfiguration(error_message) from None
    host = _canonicalize_host(parsed.hostname, error_message=error_message)
    if port is not None and _is_default_origin_port(parsed.scheme, port):
        port = None
    return ParsedOrigin(parsed.scheme, host, port)


def format_host(parsed: ParsedHost) -> str:
    host = f"[{parsed.host}]" if ":" in parsed.host else parsed.host
    if parsed.port is None:
        return host
    return f"{host}:{parsed.port}"


def format_origin(parsed: ParsedOrigin) -> str:
    host = f"[{parsed.host}]" if ":" in parsed.host else parsed.host
    if parsed.port is None:
        return f"{parsed.scheme}://{host}"
    return f"{parsed.scheme}://{host}:{parsed.port}"


def host_matches(allowed: ParsedHost, actual: ParsedHost) -> bool:
    return actual.host == allowed.host and (allowed.port is None or actual.port == allowed.port)


def origin_matches(allowed: ParsedOrigin, actual: ParsedOrigin) -> bool:
    return (
        actual.scheme == allowed.scheme
        and actual.host == allowed.host
        and actual.port == allowed.port
    )


def _canonicalize_host(value: str, *, error_message: str) -> str:
    normalized = value.strip().lower()
    if not normalized or any(character.isspace() for character in normalized):
        raise InvalidProviderConfiguration(error_message)
    try:
        return ip_address(normalized).compressed
    except ValueError:
        labels = normalized.split(".")
        if (
            normalized.endswith(".")
            or len(normalized) > 253
            or any(not label or not _HOST_LABEL.fullmatch(label) for label in labels)
        ):
            raise InvalidProviderConfiguration(error_message) from None
        return normalized


def _parse_port(value: str, *, error_message: str) -> int:
    if not value or not value.isascii() or not value.isdigit():
        raise InvalidProviderConfiguration(error_message)
    parsed = int(value)
    if not 1 <= parsed <= 65_535:
        raise InvalidProviderConfiguration(error_message)
    return parsed


def _is_default_origin_port(scheme: str, port: int) -> bool:
    return _DEFAULT_ORIGIN_PORTS.get(scheme) == port


def _is_control_char(character: str) -> bool:
    """Reject ASCII control bytes (0x00-0x1F and DEL 0x7F) in trusted-origin input."""
    codepoint = ord(character)
    return codepoint < 0x20 or codepoint == 0x7F
