from __future__ import annotations

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
    cidrs = config["route"]["rules"][1]["ip_cidr"]
    assert "193.218.200.147/32" in cidrs
    assert "203.0.113.10/32" in cidrs
    assert config["route"]["final"] == "proxy"


def test_speedtest_config_uses_mixed_local_inbound() -> None:
    node = parse_node_link(VLESS_REALITY)
    config = build_speedtest_config(node, 18080)
    inbound = config["inbounds"][0]
    assert inbound["type"] == "mixed"
    assert inbound["listen"] == "127.0.0.1"
