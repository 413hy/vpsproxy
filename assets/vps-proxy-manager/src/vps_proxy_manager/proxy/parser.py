from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import yaml

SUPPORTED_SCHEMES = {"vless", "vmess", "trojan", "ss", "hysteria2"}


@dataclass(frozen=True)
class ProxyNodeSpec:
    name: str
    protocol: str
    server: str
    port: int
    link: str
    params: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)

    @property
    def fingerprint(self) -> str:
        stable = json.dumps(
            {
                "protocol": self.protocol,
                "server": self.server,
                "port": self.port,
                "params": self.params,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(stable.encode("utf-8")).hexdigest()


class ParseError(ValueError):
    pass


def _b64decode_padded(value: str) -> bytes:
    value = value.strip()
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def parse_node_link(link: str) -> ProxyNodeSpec:
    link = link.strip()
    parsed = urlparse(link)
    scheme = parsed.scheme.lower()
    if scheme not in SUPPORTED_SCHEMES:
        raise ParseError(f"unsupported proxy scheme: {scheme or 'empty'}")
    if scheme == "vless":
        return parse_vless(link)
    if scheme == "vmess":
        return parse_vmess(link)
    if scheme == "trojan":
        return parse_trojan(link)
    if not parsed.hostname or not parsed.port:
        raise ParseError("proxy link missing server or port")
    return ProxyNodeSpec(
        name=unquote(parsed.fragment) or f"{scheme}-{parsed.hostname}:{parsed.port}",
        protocol=scheme,
        server=parsed.hostname,
        port=parsed.port,
        link=link,
        params={key: values[-1] for key, values in parse_qs(parsed.query).items()},
    )


def parse_node_blob(blob: str) -> ProxyNodeSpec:
    blob = blob.strip()
    if any(blob.startswith(f"{scheme}://") for scheme in SUPPORTED_SCHEMES):
        return parse_node_link(blob)
    try:
        data = json.loads(blob)
    except json.JSONDecodeError as exc:
        raise ParseError("stored node is neither a proxy link nor JSON") from exc
    if not isinstance(data, dict):
        raise ParseError("stored node JSON must be an object")
    if "server_port" in data:
        node = _spec_from_singbox_outbound(data)
    else:
        node = _spec_from_clash_proxy(data)
    if node is None:
        raise ParseError("stored node JSON is not a supported proxy node")
    return node


def parse_vless(link: str) -> ProxyNodeSpec:
    parsed = urlparse(link)
    if not parsed.username or not parsed.hostname or not parsed.port:
        raise ParseError("VLESS link missing uuid, server, or port")
    query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
    if query.get("security") == "reality":
        required = ["sni", "pbk"]
        missing = [key for key in required if not query.get(key)]
        if missing:
            raise ParseError(f"Reality parameters incomplete: {', '.join(missing)}")
    name = unquote(parsed.fragment) or f"vless-{parsed.hostname}:{parsed.port}"
    tags = [part for part in re.split(r"[-_\s]+", name) if part]
    return ProxyNodeSpec(
        name=name,
        protocol="vless",
        server=parsed.hostname,
        port=parsed.port,
        link=link,
        tags=tags[:8],
        params={
            "uuid": parsed.username,
            "flow": query.get("flow", ""),
            "security": query.get("security", ""),
            "sni": query.get("sni", ""),
            "fp": query.get("fp", ""),
            "pbk": query.get("pbk", ""),
            "sid": query.get("sid", ""),
            "type": query.get("type", "tcp"),
            "headerType": query.get("headerType", ""),
            "encryption": query.get("encryption", "none"),
        },
    )


def parse_trojan(link: str) -> ProxyNodeSpec:
    parsed = urlparse(link)
    if not parsed.username or not parsed.hostname or not parsed.port:
        raise ParseError("Trojan link missing password, server, or port")
    query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
    return ProxyNodeSpec(
        name=unquote(parsed.fragment) or f"trojan-{parsed.hostname}:{parsed.port}",
        protocol="trojan",
        server=parsed.hostname,
        port=parsed.port,
        link=link,
        params={
            "password": unquote(parsed.username),
            "sni": query.get("sni") or query.get("peer") or "",
            "security": query.get("security", "tls"),
        },
    )


def parse_vmess(link: str) -> ProxyNodeSpec:
    raw = link.removeprefix("vmess://")
    try:
        data = json.loads(_b64decode_padded(raw))
    except Exception as exc:
        raise ParseError("invalid vmess base64 json") from exc
    server = str(data.get("add") or "")
    port = int(data.get("port") or 0)
    if not server or not port:
        raise ParseError("VMess link missing server or port")
    return ProxyNodeSpec(
        name=str(data.get("ps") or f"vmess-{server}:{port}"),
        protocol="vmess",
        server=server,
        port=port,
        link=link,
        params=data,
    )


def parse_subscription_text(text: str) -> list[ProxyNodeSpec]:
    text = text.strip().lstrip("\ufeff")
    if not text:
        raise ParseError("subscription is empty")
    parsers = [_parse_plain_links, _parse_base64_links, _parse_clash_yaml, _parse_singbox_json]
    errors: list[str] = []
    for parser in parsers:
        try:
            nodes = parser(text)
            if nodes:
                return _dedupe(nodes)
        except Exception as exc:
            errors.append(str(exc))
    raise ParseError("subscription format is not recognized: " + "; ".join(errors[:3]))


def _parse_plain_links(text: str) -> list[ProxyNodeSpec]:
    nodes: list[ProxyNodeSpec] = []
    for line in text.splitlines():
        line = line.strip()
        if any(line.startswith(f"{scheme}://") for scheme in SUPPORTED_SCHEMES):
            nodes.append(parse_node_link(line))
    return nodes


def _parse_base64_links(text: str) -> list[ProxyNodeSpec]:
    decoded = _b64decode_padded(re.sub(r"\s+", "", text)).decode("utf-8", errors="replace")
    return _parse_plain_links(decoded)


def _parse_clash_yaml(text: str) -> list[ProxyNodeSpec]:
    data = yaml.safe_load(text)
    if not isinstance(data, dict) or not isinstance(data.get("proxies"), list):
        return []
    nodes: list[ProxyNodeSpec] = []
    for item in data["proxies"]:
        if not isinstance(item, dict):
            continue
        node = _spec_from_clash_proxy(item)
        if node:
            nodes.append(node)
    return nodes


def _parse_singbox_json(text: str) -> list[ProxyNodeSpec]:
    data = json.loads(text)
    outbounds = data.get("outbounds") if isinstance(data, dict) else None
    if not isinstance(outbounds, list):
        return []
    nodes: list[ProxyNodeSpec] = []
    for item in outbounds:
        if not isinstance(item, dict):
            continue
        node = _spec_from_singbox_outbound(item)
        if node:
            nodes.append(node)
    return nodes


def _spec_from_clash_proxy(item: dict[str, Any]) -> ProxyNodeSpec | None:
    typ = str(item.get("type", "")).lower()
    server = str(item.get("server", ""))
    port = int(item.get("port") or 0)
    if typ not in SUPPORTED_SCHEMES or not server or not port:
        return None
    name = str(item.get("name") or f"{typ}-{server}:{port}")
    params = dict(item)
    if typ == "vless":
        params.setdefault("uuid", item.get("uuid"))
        params.setdefault("security", item.get("network") or item.get("tls") or "")
        params.setdefault("sni", item.get("servername") or item.get("sni") or "")
        reality_opts = item.get("reality-opts")
        if isinstance(reality_opts, dict):
            params.setdefault("security", "reality")
            params.setdefault("pbk", reality_opts.get("public-key") or reality_opts.get("public_key"))
            params.setdefault("sid", reality_opts.get("short-id") or reality_opts.get("short_id") or "")
        params.setdefault("fp", item.get("client-fingerprint") or item.get("fingerprint") or "chrome")
        params.setdefault("type", item.get("network") or "tcp")
    if typ == "trojan":
        params.setdefault("password", item.get("password"))
        params.setdefault("sni", item.get("sni") or item.get("servername") or "")
    return ProxyNodeSpec(
        name=name,
        protocol=typ,
        server=server,
        port=port,
        link=json.dumps(item, ensure_ascii=False, sort_keys=True),
        params=params,
        tags=[name],
    )


def _spec_from_singbox_outbound(item: dict[str, Any]) -> ProxyNodeSpec | None:
    typ = str(item.get("type", "")).lower()
    if typ not in SUPPORTED_SCHEMES:
        return None
    server = str(item.get("server", ""))
    port = int(item.get("server_port") or 0)
    if not server or not port:
        return None
    return ProxyNodeSpec(
        name=str(item.get("tag") or f"{typ}-{server}:{port}"),
        protocol=typ,
        server=server,
        port=port,
        link=json.dumps(item, ensure_ascii=False, sort_keys=True),
        params=item,
    )


def _dedupe(nodes: list[ProxyNodeSpec]) -> list[ProxyNodeSpec]:
    seen: set[str] = set()
    unique: list[ProxyNodeSpec] = []
    for node in nodes:
        if node.fingerprint not in seen:
            seen.add(node.fingerprint)
            unique.append(node)
    return unique
