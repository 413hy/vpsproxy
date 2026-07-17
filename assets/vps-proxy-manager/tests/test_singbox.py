from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_parser import VLESS_REALITY
from vps_proxy_manager.proxy.parser import parse_node_link
from vps_proxy_manager.proxy.singbox import (
    build_speedtest_config,
    build_tun_config,
    node_to_outbound,
)


def test_vless_reality_outbound() -> None:
    node = parse_node_link(VLESS_REALITY)
    outbound = node_to_outbound(node)
    assert outbound["type"] == "vless"
    assert outbound["flow"] == "xtls-rprx-vision"
    assert outbound["tls"]["reality"]["enabled"] is True
    assert outbound["tls"]["utls"]["fingerprint"] == "chrome"


def test_tun_config_bypasses_management_and_proxy_ip() -> None:
    node = parse_node_link(VLESS_REALITY)
    config = build_tun_config(node, management_source_ip="203.0.113.10", ssh_port=22)
    cidrs = next(rule["ip_cidr"] for rule in config["route"]["rules"] if "ip_cidr" in rule)
    assert "193.218.200.147/32" in cidrs
    assert "203.0.113.10/32" in cidrs
    assert config["route"]["final"] == "proxy"
    assert "203.0.113.10/32" in config["inbounds"][0]["route_exclude_address"]
    local_dns = next(server for server in config["dns"]["servers"] if server["tag"] == "local-dns")
    assert "detour" not in local_dns


def test_speedtest_config_uses_mixed_local_inbound() -> None:
    node = parse_node_link(VLESS_REALITY)
    config = build_speedtest_config(node, 18080)
    inbound = config["inbounds"][0]
    assert inbound["type"] == "mixed"
    assert inbound["listen"] == "127.0.0.1"


def test_singbox_native_outbound_is_preserved() -> None:
    node = parse_node_link(VLESS_REALITY)
    native = {
        "type": "vless",
        "tag": "original",
        "server": node.server,
        "server_port": node.port,
        "uuid": node.params["uuid"],
        "tls": {
            "enabled": True,
            "server_name": "apple.com",
            "reality": {"enabled": True, "public_key": "abc", "short_id": "1"},
        },
    }
    from vps_proxy_manager.proxy.parser import parse_node_blob

    preserved = node_to_outbound(parse_node_blob(json.dumps(native)), "proxy")
    assert preserved["tag"] == "proxy"
    assert preserved["tls"]["reality"]["enabled"] is True


def test_shadowsocks_and_hysteria2_outbounds() -> None:
    from vps_proxy_manager.proxy.parser import parse_node_link

    ss = parse_node_link("ss://YWVzLTI1Ni1nY206dGVzdC1wYXNzd29yZA@example.com:8388#ss")
    ss_outbound = node_to_outbound(ss)
    assert ss_outbound["type"] == "shadowsocks"
    assert ss_outbound["method"] == "aes-256-gcm"

    hy2 = parse_node_link("hysteria2://test-password@example.com:443?sni=cdn.example.com#hy2")
    hy2_outbound = node_to_outbound(hy2)
    assert hy2_outbound["type"] == "hysteria2"
    assert hy2_outbound["tls"]["server_name"] == "cdn.example.com"


def test_clash_vmess_boolean_tls_is_preserved() -> None:
    from vps_proxy_manager.proxy.parser import parse_subscription_text

    node = parse_subscription_text(
        """
proxies:
  - name: vmess-tls
    type: vmess
    server: example.com
    port: 443
    uuid: 11111111-1111-4111-8111-111111111111
    tls: true
    sni: cdn.example.com
"""
    )[0]
    outbound = node_to_outbound(node)
    assert outbound["tls"] == {"enabled": True, "server_name": "cdn.example.com"}


@pytest.mark.parametrize("kind", ["tun", "speedtest"])
def test_generated_config_passes_installed_singbox_check(tmp_path: Path, kind: str) -> None:
    binary = shutil.which("sing-box")
    if not binary:
        pytest.skip("sing-box is not installed")
    node = parse_node_link(VLESS_REALITY)
    config = (
        build_tun_config(node, management_source_ip="203.0.113.10", ssh_port=22)
        if kind == "tun"
        else build_speedtest_config(node, 18080)
    )
    path = tmp_path / f"{kind}.json"
    path.write_text(json.dumps(config))
    result = subprocess.run(
        [binary, "check", "-c", str(path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
