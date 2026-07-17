from __future__ import annotations

import ipaddress
from typing import Any

from vps_proxy_manager.proxy.parser import ProxyNodeSpec

PRIVATE_CIDRS = [
    "0.0.0.0/8",
    "10.0.0.0/8",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "224.0.0.0/4",
    "::1/128",
    "fc00::/7",
    "fe80::/10",
]


class ConfigError(ValueError):
    pass


def node_to_outbound(node: ProxyNodeSpec, tag: str = "proxy") -> dict[str, Any]:
    if node.params.get("type") == node.protocol and "server_port" in node.params:
        outbound = dict(node.params)
        outbound["tag"] = tag
        return outbound
    if node.protocol == "vless":
        return _vless_outbound(node, tag)
    if node.protocol == "vmess":
        return _vmess_outbound(node, tag)
    if node.protocol == "trojan":
        return {
            "type": "trojan",
            "tag": tag,
            "server": node.server,
            "server_port": node.port,
            "password": node.params.get("password", ""),
            "tls": {"enabled": True, "server_name": node.params.get("sni", node.server)},
        }
    if node.protocol == "ss":
        return {
            "type": "shadowsocks",
            "tag": tag,
            "server": node.server,
            "server_port": node.port,
            "method": node.params.get("method") or node.params.get("cipher"),
            "password": node.params.get("password", ""),
            **(
                {
                    "plugin": node.params["plugin"],
                    "plugin_opts": node.params.get("plugin_opts", ""),
                }
                if node.params.get("plugin")
                else {}
            ),
        }
    if node.protocol == "hysteria2":
        hysteria2_outbound: dict[str, Any] = {
            "type": "hysteria2",
            "tag": tag,
            "server": node.server,
            "server_port": node.port,
            "password": node.params.get("password", ""),
            "tls": {
                "enabled": True,
                "server_name": node.params.get("sni") or node.server,
                "insecure": bool(node.params.get("insecure", False)),
            },
        }
        if node.params.get("obfs"):
            hysteria2_outbound["obfs"] = {
                "type": node.params["obfs"],
                "password": node.params.get("obfs-password", ""),
            }
        for field in ["up_mbps", "down_mbps"]:
            if node.params.get(field):
                hysteria2_outbound[field] = int(node.params[field])
        return hysteria2_outbound
    raise ConfigError(f"protocol not supported by config generator: {node.protocol}")


def _vless_outbound(node: ProxyNodeSpec, tag: str) -> dict[str, Any]:
    params = node.params
    if params.get("security") == "reality" and not params.get("pbk"):
        raise ConfigError("Reality public key is required")
    outbound: dict[str, Any] = {
        "type": "vless",
        "tag": tag,
        "server": node.server,
        "server_port": node.port,
        "uuid": params.get("uuid"),
        "flow": params.get("flow") or None,
        "network": params.get("type") or "tcp",
        "packet_encoding": "xudp",
    }
    tls: dict[str, Any] = {}
    if params.get("security") in {"tls", "reality"}:
        tls = {
            "enabled": True,
            "server_name": params.get("sni") or node.server,
            "utls": {
                "enabled": True,
                "fingerprint": params.get("fp") or "chrome",
            },
        }
    if params.get("security") == "reality":
        tls["reality"] = {
            "enabled": True,
            "public_key": params.get("pbk"),
            "short_id": params.get("sid") or "",
        }
    if tls:
        outbound["tls"] = tls
    return {key: value for key, value in outbound.items() if value is not None}


def _vmess_outbound(node: ProxyNodeSpec, tag: str) -> dict[str, Any]:
    params = node.params
    security = params.get("security") or params.get("scy") or "auto"
    outbound: dict[str, Any] = {
        "type": "vmess",
        "tag": tag,
        "server": node.server,
        "server_port": node.port,
        "uuid": params.get("id") or params.get("uuid"),
        "security": security,
        "alter_id": int(params.get("aid") or params.get("alterId") or 0),
    }
    if params.get("tls") in {True, "true", "tls"}:
        outbound["tls"] = {"enabled": True, "server_name": params.get("sni") or node.server}
    return outbound


def build_tun_config(
    node: ProxyNodeSpec,
    *,
    management_source_ip: str | None,
    ssh_port: int,
    enable_ipv6: bool = True,
    auto_redirect: bool = True,
) -> dict[str, Any]:
    exclude = list(PRIVATE_CIDRS)
    for value in [node.server, management_source_ip]:
        if value:
            try:
                ip = ipaddress.ip_address(value)
                exclude.append(f"{ip}/32" if ip.version == 4 else f"{ip}/128")
            except ValueError:
                pass
    addresses = ["172.19.0.1/30"]
    if enable_ipv6:
        addresses.append("fdfe:dcba:9876::1/126")
    config: dict[str, Any] = {
        "log": {"level": "info", "timestamp": True},
        "dns": {
            "servers": [
                {"tag": "remote-dns", "type": "https", "server": "1.1.1.1", "detour": "proxy"},
                {"tag": "local-dns", "type": "udp", "server": "223.5.5.5"},
            ],
            "strategy": "prefer_ipv4",
            "final": "remote-dns",
        },
        "inbounds": [
            {
                "type": "tun",
                "tag": "tun-in",
                "address": addresses,
                "auto_route": True,
                "auto_redirect": auto_redirect,
                "route_exclude_address": exclude,
                "strict_route": True,
                "stack": "mixed",
            }
        ],
        "outbounds": [
            node_to_outbound(node, "proxy"),
            {"type": "direct", "tag": "direct"},
        ],
        "route": {
            "auto_detect_interface": True,
            "default_domain_resolver": "local-dns",
            "rules": [
                {"action": "sniff"},
                {"protocol": "dns", "action": "hijack-dns"},
                {"ip_cidr": exclude, "outbound": "direct"},
                {"port": ssh_port, "network": "tcp", "outbound": "direct"},
            ],
            "final": "proxy",
        },
    }
    return config


def build_speedtest_config(node: ProxyNodeSpec, listen_port: int) -> dict[str, Any]:
    return {
        "log": {"level": "error"},
        "dns": {
            "servers": [{"type": "local", "tag": "local-dns"}],
            "strategy": "prefer_ipv4",
        },
        "inbounds": [
            {
                "type": "mixed",
                "tag": "mixed-in",
                "listen": "127.0.0.1",
                "listen_port": listen_port,
            }
        ],
        "outbounds": [node_to_outbound(node, "proxy"), {"type": "direct", "tag": "direct"}],
        "route": {
            "default_domain_resolver": "local-dns",
            "rules": [{"action": "sniff"}],
            "final": "proxy",
        },
    }
