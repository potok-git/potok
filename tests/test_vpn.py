import struct
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from potok.config import combined_vpn_domains
from potok.vpn import (
    VpnError,
    VpnManager,
    build_a_response,
    dns_servers_from,
    encode_dns_query,
    endpoint_host,
    merged_routes,
    name_uses_vpn,
    parse_dns_a,
    question_name,
    redact_secrets,
    route_edits,
    route_is_already_gone,
    runtime_wireguard_text,
)


def _manager(tmp_path: Path) -> VpnManager:
    return VpnManager("wg0", tmp_path / "wg0.conf", timeout_seconds=5)


def _ok(code: int = 0, stderr: str = "") -> CompletedProcess[str]:
    return CompletedProcess(args=["wg"], returncode=code, stdout="", stderr=stderr)


def test_up_skips_when_already_active(tmp_path: Path) -> None:
    conf = tmp_path / "wg0.conf"
    conf.write_text("[Interface]\n", encoding="utf-8")
    manager = _manager(tmp_path)

    with patch.object(manager, "_run", return_value=_ok()) as run:
        manager.up()

    run.assert_called_once_with(["wg", "show", "wg0"])


def test_up_calls_wg_quick(tmp_path: Path) -> None:
    conf = tmp_path / "wg0.conf"
    conf.write_text("[Interface]\n", encoding="utf-8")
    manager = _manager(tmp_path)
    answers = [_ok(1), _ok(), _ok()]

    with patch.object(manager, "_run", side_effect=answers) as run:
        manager.up()

    command = run.call_args_list[1].args[0]
    assert command[:2] == ["wg-quick", "up"]
    assert "Table = off" in Path(command[2]).read_text(encoding="utf-8")


def test_up_missing_config(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    with pytest.raises(VpnError, match="не найден"):
        manager.up()


def test_up_failure_hides_private_key(tmp_path: Path) -> None:
    conf = tmp_path / "wg0.conf"
    conf.write_text("[Interface]\n", encoding="utf-8")
    manager = _manager(tmp_path)
    leaked = _ok(code=1, stderr="PrivateKey = secretvalue\nUnable to modify interface")
    answers = [_ok(1), leaked]

    with patch.object(manager, "_run", side_effect=answers):
        with pytest.raises(VpnError, match="секрет скрыт") as exc:
            manager.up()

    assert "secretvalue" not in str(exc.value)


def test_down_when_already_down(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    with patch.object(manager, "_run", return_value=_ok(1)) as run:
        manager.down()
    run.assert_called_once_with(["wg", "show", "wg0"])


def test_missing_wg_binary(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    with (
        patch("potok.vpn.subprocess.run", side_effect=FileNotFoundError),
        pytest.raises(VpnError, match="не найдена"),
    ):
        manager.is_up()


def test_probe_leaves_tunnel_up(tmp_path: Path) -> None:
    conf = tmp_path / "wg0.conf"
    conf.write_text("[Interface]\n", encoding="utf-8")
    manager = _manager(tmp_path)

    def ensure(self: VpnManager) -> str:
        del self
        return "Туннель wg0 включён и остаётся включённым."

    with (
        patch.object(VpnManager, "ensure", ensure),
        patch.object(VpnManager, "is_up", lambda self: True),
    ):
        text = manager.probe()

    assert "остаётся включённым" in text


def test_runtime_config_does_not_capture_all_traffic() -> None:
    source = """
[Interface]
PrivateKey = secret
Address = 10.0.0.2/32
DNS = 9.9.9.9

[Peer]
PublicKey = pub
AllowedIPs = 10.0.0.0/24
Endpoint = 203.0.113.8:51820
"""
    text = runtime_wireguard_text(source)
    assert "Table = off" in text
    assert "DNS" not in text
    assert "AllowedIPs = 0.0.0.0/0" in text
    assert "secret" in text
    assert endpoint_host(source) == "203.0.113.8"
    assert dns_servers_from(source) == ("9.9.9.9",)


def test_required_domains_stay_when_extra_is_added() -> None:
    names = combined_vpn_domains(("kinozal.tv",))
    assert names[0] == "api.telegram.org"
    assert "rutracker.org" in names
    assert "kinozal.tv" in names
    assert names.count("kinozal.tv") == 1


def test_route_edits_add_and_remove() -> None:
    commands = route_edits({"1.1.1.1"}, {"8.8.8.8"}, "wg0")
    assert commands[0] == ["ip", "route", "replace", "8.8.8.8/32", "dev", "wg0"]
    assert commands[1] == ["ip", "route", "del", "1.1.1.1/32", "dev", "wg0"]


def test_failed_dns_keeps_old_routes() -> None:
    kept = merged_routes({"1.1.1.1"}, {"8.8.8.8"}, keep_current=True)
    assert kept == {"1.1.1.1", "8.8.8.8"}
    replaced = merged_routes({"1.1.1.1"}, {"8.8.8.8"}, keep_current=False)
    assert replaced == {"8.8.8.8"}


def test_missing_route_on_delete_is_not_a_failure() -> None:
    command = ["ip", "route", "del", "1.1.1.1/32", "dev", "wg0"]
    assert route_is_already_gone(command, "RTNETLINK answers: No such process")
    assert not route_is_already_gone(
        ["ip", "route", "replace", "1.1.1.1/32", "dev", "wg0"],
        "RTNETLINK answers: No such process",
    )


def test_vpn_name_matches_subdomain_only() -> None:
    domains = ("api.telegram.org", "rutracker.org")
    assert name_uses_vpn("api.telegram.org", domains)
    assert name_uses_vpn("www.rutracker.org", domains)
    assert not name_uses_vpn("web.telegram.org", domains)


def test_dns_answer_roundtrip() -> None:
    query = encode_dns_query("rutracker.org", 9)
    reply = build_a_response(query, ["5.6.7.8"])
    assert question_name(reply) == "rutracker.org"
    assert parse_dns_a(reply) == ["5.6.7.8"]


def test_parse_dns_a_record() -> None:
    query = encode_dns_query("example.com", 7)
    header = struct.pack(">HHHHHH", 7, 0x8180, 1, 1, 0, 0)
    answer = b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 60, 4) + bytes((1, 2, 3, 4))
    assert parse_dns_a(header + query[12:] + answer) == ["1.2.3.4"]


def test_redact_secrets() -> None:
    text = "line\nPrivateKey = abc\nPresharedKey=zzz\nok"
    assert "abc" not in redact_secrets(text)
    assert "zzz" not in redact_secrets(text)
    assert "ok" in redact_secrets(text)
