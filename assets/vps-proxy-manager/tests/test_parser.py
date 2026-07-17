from __future__ import annotations

import base64
import json

from vps_proxy_manager.proxy.parser import parse_node_blob, parse_node_link, parse_subscription_text

VLESS_REALITY = (
    "vless://5d80eab0-0345-46db-a106-ff59f56d70e4@193.218.200.147:29630"
    "?encryption=none&flow=xtls-rprx-vision&security=reality&sni=apple.com&fp=chrome"
    "&pbk=gBm1oMCY9MRGSgfVPP-w-v5EtfUQRwnB9MDXDqyYrA0&sid=1e8af15f&type=tcp"
    "&headerType=none#vl-reality-vision-bot"
)


def test_parse_vless_reality_example() -> None:
    node = parse_node_link(VLESS_REALITY)
    assert node.name == "vl-reality-vision-bot"
    assert node.protocol == "vless"
    assert node.server == "193.218.200.147"
    assert node.port == 29630
    assert node.params["security"] == "reality"
    assert node.params["sni"] == "apple.com"
    assert node.params["flow"] == "xtls-rprx-vision"
    assert node.params["fp"] == "chrome"


def test_plain_subscription() -> None:
    nodes = parse_subscription_text(VLESS_REALITY + "\n")
    assert len(nodes) == 1
    assert nodes[0].protocol == "vless"


def test_base64_subscription() -> None:
    encoded = base64.b64encode((VLESS_REALITY + "\n").encode()).decode()
    nodes = parse_subscription_text(encoded)
    assert len(nodes) == 1
    assert nodes[0].name == "vl-reality-vision-bot"


def test_clash_yaml_subscription() -> None:
    text = """
proxies:
  - name: clash-vless
    type: vless
    server: example.com
    port: 443
    uuid: 11111111-1111-4111-8111-111111111111
"""
    nodes = parse_subscription_text(text)
    assert nodes[0].name == "clash-vless"
    assert nodes[0].server == "example.com"
    stored = parse_node_blob(nodes[0].link)
    assert stored.name == "clash-vless"
    assert stored.params["uuid"] == "11111111-1111-4111-8111-111111111111"


def test_singbox_json_subscription() -> None:
    text = json.dumps(
        {
            "outbounds": [
                {
                    "type": "vless",
                    "tag": "sb-vless",
                    "server": "example.org",
                    "server_port": 8443,
                    "uuid": "11111111-1111-4111-8111-111111111111",
                }
            ]
        }
    )
    nodes = parse_subscription_text(text)
    assert nodes[0].name == "sb-vless"
    stored = parse_node_blob(nodes[0].link)
    assert stored.server == "example.org"


def test_parse_trojan_link_password() -> None:
    node = parse_node_link("trojan://secret@example.com:443?sni=example.com#trojan")
    assert node.protocol == "trojan"
    assert node.params["password"] == "secret"  # noqa: S105
    assert node.params["sni"] == "example.com"


def test_parse_sip002_shadowsocks_link() -> None:
    credentials = base64.urlsafe_b64encode(b"aes-256-gcm:test-password").decode().rstrip("=")
    node = parse_node_link(f"ss://{credentials}@example.com:8388#ss-node")
    assert node.protocol == "ss"
    assert node.server == "example.com"
    assert node.port == 8388
    assert node.params["method"] == "aes-256-gcm"
    assert node.params["password"] == "test-password"  # noqa: S105


def test_parse_hysteria2_link() -> None:
    node = parse_node_link(
        "hysteria2://test-password@example.com:443?sni=cdn.example.com&insecure=1#hy2"
    )
    assert node.protocol == "hysteria2"
    assert node.params["password"] == "test-password"  # noqa: S105
    assert node.params["sni"] == "cdn.example.com"
    assert node.params["insecure"] is True


def test_parse_hy2_alias() -> None:
    node = parse_node_link("hy2://test-password@example.com:443?sni=cdn.example.com#alias")
    assert node.protocol == "hysteria2"
    assert node.name == "alias"


def test_clash_vless_reality_overrides_network_security() -> None:
    text = """
proxies:
  - name: reality
    type: vless
    server: example.com
    port: 443
    uuid: 11111111-1111-4111-8111-111111111111
    network: tcp
    servername: apple.com
    reality-opts:
      public-key: public-key
      short-id: abcdef
"""
    node = parse_subscription_text(text)[0]
    assert node.params["security"] == "reality"
    assert node.params["pbk"] == "public-key"


def test_clash_shadowsocks_plugin_options_are_normalized() -> None:
    node = parse_subscription_text(
        """
proxies:
  - name: ss-plugin
    type: ss
    server: example.com
    port: 8388
    cipher: aes-256-gcm
    password: test-password
    plugin: v2ray-plugin
    plugin-opts:
      mode: websocket
      tls: true
      host: cdn.example.com
"""
    )[0]
    assert node.params["plugin_opts"] == "mode=websocket;tls;host=cdn.example.com"
