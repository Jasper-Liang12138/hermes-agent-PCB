"""Delivery checks for PCB Agent SOUL.md installation."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_pcb_soul_has_managed_version_marker():
    soul_text = (REPO_ROOT / "SOUL.md").read_text(encoding="utf-8")

    assert soul_text.startswith("<!-- PCB_AGENT_SOUL_VERSION: 2026-06-24-v1 -->")
    assert "SOUL.md shapes voice, explanation, advice, and summaries only." in soul_text
    assert "It does not control workflow state" in soul_text


def test_delivery_install_upgrades_changed_soul_with_backup():
    install_text = (REPO_ROOT / ".github" / "delivery" / "install.bat").read_text(
        encoding="utf-8"
    )

    assert r'fc /B "%SCRIPT_DIR%SOUL.md" "%HERMES%\SOUL.md"' in install_text
    assert "SOUL.md.bak-!SOUL_TS!" in install_text
    assert "EnableDelayedExpansion" in install_text
    assert "PCB Agent SOUL.md upgraded" in install_text
    assert "SOUL.md unchanged" in install_text
    assert "SOUL.md exists, skipped" not in install_text


def test_package_delivery_requires_soul_md():
    package_text = (REPO_ROOT / "scripts" / "package-delivery-windows.ps1").read_text(
        encoding="utf-8"
    )

    assert 'throw "SOUL.md not found: $SoulSrc"' in package_text
    assert 'throw "SOUL.md was not copied to delivery output: $SoulOutput"' in package_text
    assert 'Write-Warn "SOUL.md not found' not in package_text
