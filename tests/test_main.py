import os
from pathlib import Path

from potok.__main__ import load_local_env, main
from potok.vpn import VpnManager


def test_demo_keeps_split_tunnel(tmp_path: Path, monkeypatch, capsys) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        """
vpn:
  interface: wg0
  config_path: /etc/potok/wg0.conf
routing:
  via_vpn: [rss, metadata]
  via_direct: [downloads]
logging:
  level: INFO
""",
        encoding="utf-8",
    )
    calls: list[str] = []

    def ensure(self: VpnManager) -> str:
        calls.append("ensure")
        return "готово"

    monkeypatch.setattr(VpnManager, "ensure", ensure)
    monkeypatch.setattr(VpnManager, "is_up", lambda self: True)

    code = main(["demo", "--config", str(config)])

    assert code == 0
    assert calls == ["ensure"]
    output = capsys.readouterr().out
    assert "через VPN" in output
    assert "прямому каналу" in output
    assert "api.telegram.org" in output


def test_load_local_env_does_not_override(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "POTOK_TELEGRAM_TOKEN=from-file\nPOTOK_FEED_USER=reader\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("POTOK_TELEGRAM_TOKEN", "already-set")
    monkeypatch.delenv("POTOK_FEED_USER", raising=False)

    load_local_env(env_file)

    assert os.environ["POTOK_TELEGRAM_TOKEN"] == "already-set"
    assert os.environ["POTOK_FEED_USER"] == "reader"


def test_watch_starts_without_telegram(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        """
vpn:
  interface: wg0
  config_path: /etc/potok/wg0.conf
routing:
  via_vpn: [rss]
  via_direct: [downloads]
""",
        encoding="utf-8",
    )
    started: list[Path | None] = []

    async def fake_watch(_config, *, config_path):
        started.append(config_path)

    monkeypatch.setattr("potok.bot.run_watch", fake_watch)
    assert main(["watch", "--config", str(config)]) == 0
    assert started == [config]
