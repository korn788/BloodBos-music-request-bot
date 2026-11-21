"""The Linux AppImage: it uses its own VLC, and it puts itself in the app menu."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

if not sys.platform.startswith("linux"):
    pytest.skip("Linux only", allow_module_level=True)

import app_paths  # noqa: E402
import desktop  # noqa: E402
import vlc_loader  # noqa: E402


@pytest.fixture
def clean_vlc_env(monkeypatch):
    for v in ("PYTHON_VLC_LIB_PATH", "PYTHON_VLC_MODULE_PATH", "VLC_PLUGIN_PATH"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(vlc_loader, "_extra", [])


def test_uses_the_vlc_it_carries(tmp_path, monkeypatch, clean_vlc_env):
    vlc_dir = tmp_path / "vlc"
    (vlc_dir / "plugins" / "codec").mkdir(parents=True)
    (vlc_dir / "certs").mkdir()
    for so in ("libvlc.so.5", "libvlccore.so.9"):
        (vlc_dir / so).write_bytes(b"")
    monkeypatch.setattr(app_paths, "bundled_vlc_dir", lambda: str(vlc_dir))
    assert vlc_loader._prepare() == str(vlc_dir)
    assert os.environ["PYTHON_VLC_LIB_PATH"] == str(vlc_dir / "libvlc.so.5")
    assert os.environ["VLC_PLUGIN_PATH"] == str(vlc_dir / "plugins")
    # https trusts the certificates it carries, whatever this Linux keeps where
    assert vlc_loader.instance_args() == [f"--gnutls-dir-trust={vlc_dir / 'certs'}"]


def test_other_installs_use_the_systems_vlc(monkeypatch, clean_vlc_env):
    monkeypatch.setattr(app_paths, "bundled_vlc_dir", lambda: None)
    assert vlc_loader._prepare() is None
    assert "PYTHON_VLC_LIB_PATH" not in os.environ and vlc_loader.instance_args() == []


@pytest.fixture
def home(tmp_path, monkeypatch):
    data = tmp_path / "share"
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "system"))
    monkeypatch.setattr(desktop.shutil, "which", lambda name: None)
    icon = tmp_path / "icon.png"
    icon.write_bytes(b"png")
    entry = data / "applications" / "bloodbos-radio.desktop"
    return tmp_path, entry, str(icon)


def image(tmp_path, monkeypatch, rel):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"AppImage")
    monkeypatch.setenv("APPIMAGE", str(p))
    return p


def test_adds_itself_to_the_app_menu_and_follows_a_move(home, monkeypatch):
    tmp, entry, icon = home
    first = image(tmp, monkeypatch, "Downloads/BLOODBOS Radio 100%.AppImage")
    assert desktop.integrate_appimage(icon) == "Added BLOODBOS Radio to your app menu"
    text = entry.read_text()
    assert f'Exec="{first.parent}/BLOODBOS Radio 100%%.AppImage"' in text
    assert f"TryExec={first}" in text and "Icon=bloodbos-radio" in text
    icon_file = tmp / "share/icons/hicolor/256x256/apps/bloodbos-radio.png"
    assert icon_file.read_bytes() == b"png"
    assert desktop.integrate_appimage(icon) == ""          # nothing to change

    moved = image(tmp, monkeypatch, "Apps/bloodbos.AppImage")
    first.unlink()
    assert desktop.integrate_appimage(icon) == "The app menu now opens this copy of BLOODBOS Radio"
    assert f'Exec="{moved}"' in entry.read_text()


def test_leaves_the_install_lines_menu_entry_alone(home, monkeypatch):
    tmp, entry, icon = home
    entry.parent.mkdir(parents=True)
    mine = "[Desktop Entry]\nName=BLOODBOS Radio\nExec=\"/home/x/.local/bin/bloodbos-radio\"\n"
    entry.write_text(mine)
    image(tmp, monkeypatch, "Downloads/BLOODBOS-Radio-x86_64.AppImage")
    assert desktop.integrate_appimage(icon) == ""
    assert entry.read_text() == mine


def test_leaves_the_deb_alone(home, monkeypatch):
    tmp, entry, icon = home
    system = tmp / "system" / "applications"
    system.mkdir(parents=True)
    (system / "bloodbos-radio.desktop").write_text("[Desktop Entry]\n")
    image(tmp, monkeypatch, "Downloads/BLOODBOS-Radio-x86_64.AppImage")
    assert desktop.integrate_appimage(icon) == "" and not entry.exists()


def test_not_an_appimage(home, monkeypatch):
    monkeypatch.delenv("APPIMAGE", raising=False)
    assert desktop.integrate_appimage(home[2]) == "" and not home[1].exists()
