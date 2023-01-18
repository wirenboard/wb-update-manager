"""
New tools (created on Bullseye upgrade)
"""

from .common import run_apt, run_cmd


def apt_update():
    run_apt("update")


def apt_purge(*packages, assume_yes=False):
    run_apt("purge", *packages, assume_yes=assume_yes)


def apt_install(*packages, assume_yes=False):
    run_apt("install", *packages, assume_yes=assume_yes)


def apt_upgrade(dist=True, assume_yes=False):
    cmd = "dist-upgrade" if dist else "upgrade"
    run_apt(cmd, assume_yes=assume_yes)


def apt_hold(*packages):
    run_cmd("apt-mark", "hold", *packages, log_suffix="apt-mark")


def apt_unhold(*packages):
    run_cmd("apt-mark", "unhold", *packages, log_suffix="apt-mark")


def apt_autoremove(assume_yes=False):
    run_apt("autoremove", assume_yes=assume_yes)


def systemd_mask(*services):
    for service in services:
        run_cmd("systemctl", "mask", service, log_suffix="systemctl")


def systemd_unmask(*services):
    for service in services:
        run_cmd("systemctl", "unmask", service, log_suffix="systemctl")


def systemd_restart(*services):
    for service in services:
        run_cmd("systemctl", "restart", service, log_suffix="systemctl")
