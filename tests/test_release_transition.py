import functools
import subprocess
from contextlib import ExitStack
from unittest.mock import MagicMock, call, patch

import pytest

from wb.update_manager import common, release_upgrade, tools

STATE_STRETCH = common.SystemState("testing", "wb7/stretch", "", True)
STATE_BULLSEYE = common.SystemState("testing", "wb7/bullseye", "", True)


def patch_all_systemish(func):
    @functools.wraps(func)
    def patched(*args, **kwargs):
        with ExitStack() as stack:
            functions = (
                (tools, "run_cmd"),
                (common, "run_cmd"),
                (release_upgrade, "run_cmd"),
                (release_upgrade, "generate_system_config"),
                (common, "generate_sources_list"),
                (common, "generate_release_apt_preferences"),
                (release_upgrade, "_cleanup_apt_cached_lists"),
                (release_upgrade, "create_temp_apt_configs"),
                (release_upgrade, "remove_temp_apt_configs"),
                (release_upgrade, "create_temp_apt_policy_for_tool"),
                (release_upgrade, "remove_temp_apt_policy_for_tool"),
                (release_upgrade, "touch_system_update_done_flag"),
                (release_upgrade, "upgrade_and_maybe_switch_tool"),
                (release_upgrade, "get_global_progress_flag"),
                (release_upgrade, "set_global_progress_flag"),
                (release_upgrade, "install_progress_banner"),
                (release_upgrade, "release_exists"),
                (release_upgrade, "enough_free_space"),
                (release_upgrade, "system_dirs_permissions_ok"),
                (release_upgrade, "bootstrap_libc"),
                (release_upgrade, "save_config_files_snapshot"),
                (release_upgrade, "discard_unused_config_files_snapshot"),
                (release_upgrade, "purge_packages_removed_by_upgrade"),
            )
            for obj, function in functions:
                mock_name = function + "_mock"
                if mock_name in kwargs:
                    stack.enter_context(patch.object(obj, function, new=kwargs[mock_name]))
                else:
                    kwargs[mock_name] = stack.enter_context(patch.object(obj, function))
                if function == "run_cmd":
                    kwargs[mock_name].return_value = (0, "")

            kwargs["release_exists_mock"].side_effect = (True,)
            kwargs["enough_free_space_mock"].side_effect = (True,)
            kwargs["system_dirs_permissions_ok_mock"].return_value = True
            kwargs["get_global_progress_flag_mock"].side_effect = ("",)

            return func(*args, **kwargs)

    return patched


def run_usual_upgrade(log_filename=None, assume_yes=True, no_preliminary_update=True, confirm_steps=False):
    return release_upgrade.upgrade_new_debian_release(
        STATE_STRETCH,
        log_filename=log_filename,
        assume_yes=assume_yes,
        no_preliminary_update=no_preliminary_update,
        confirm_steps=confirm_steps,
    )


class MockCalledItemCollector:
    def __init__(self):
        self.collected = []

    def adder(self, *args, **_):
        self.collected += list(args)

    def remover(self, *args, **_):
        for obj in args:
            self.collected.remove(obj)


def fail_on_nth_call(call_n, throw=None, return_value=(0, "")):
    if not throw:
        throw = KeyboardInterrupt()

    for _ in range(call_n - 1):
        yield return_value

    yield throw

    while True:
        yield return_value


@patch_all_systemish
def test_selftest_patcher(**_):
    assert tools.run_cmd is common.run_cmd
    assert tools.run_apt is common.run_apt


@functools.cache
@patch_all_systemish
def num_ususal_run_cmd_calls(run_cmd_mock=None, **_kwargs):
    run_usual_upgrade()
    return len(run_cmd_mock.mock_calls)


@pytest.mark.parametrize("successful_cmds", range(num_ususal_run_cmd_calls()))
@patch_all_systemish
def test_packages_always_become_unhold(successful_cmds, run_cmd_mock=None, **_):
    collector = MockCalledItemCollector()

    run_cmd_mock.side_effect = fail_on_nth_call(successful_cmds)

    with patch.multiple(
        "wb.update_manager.release_upgrade", apt_mark_hold=collector.adder, apt_mark_unhold=collector.remover
    ):
        run_usual_upgrade()

    assert len(collector.collected) == 0


@pytest.mark.parametrize("successful_cmds", range(num_ususal_run_cmd_calls()))
@patch_all_systemish
def test_services_always_become_unmasked(successful_cmds, run_cmd_mock=None, **_):
    collector = MockCalledItemCollector()

    run_cmd_mock.side_effect = fail_on_nth_call(successful_cmds)

    with patch.multiple(
        "wb.update_manager.release_upgrade", systemd_mask=collector.adder, systemd_unmask=collector.remover
    ):
        run_usual_upgrade()

    assert len(collector.collected) == 0


@pytest.mark.parametrize("successful_cmds", range(num_ususal_run_cmd_calls() - 1))
@patch_all_systemish
def test_keyboard_interrupts(successful_cmds, run_cmd_mock=None, **_):
    run_cmd_mock.side_effect = fail_on_nth_call(successful_cmds, throw=KeyboardInterrupt())

    assert run_usual_upgrade() == common.RETCODE_USER_ABORT


@patch_all_systemish
def test_all_clear(run_cmd_mock=None, **_):
    assert run_usual_upgrade() == common.RETCODE_OK
    assert len(run_cmd_mock.mock_calls) == num_ususal_run_cmd_calls()


@patch_all_systemish
def test_cleared_global_progress_flag(set_global_progress_flag_mock=None, **_):
    run_usual_upgrade()

    set_global_progress_flag_mock.assert_any_call("progress")
    set_global_progress_flag_mock.assert_called_with(None)


@patch_all_systemish
def test_set_update_done_flag(touch_system_update_done_flag_mock=None, **_):
    run_usual_upgrade()

    touch_system_update_done_flag_mock.assert_called()


@patch_all_systemish
def test_banner_installed(install_progress_banner_mock=None, **_):
    run_usual_upgrade()

    install_progress_banner_mock.assert_called()


RESTART_WB_RULES_CALL = call("/usr/sbin/invoke-rc.d", "wb-rules", "restart", raise_on_error=False)


@patch_all_systemish
def test_wb_rules_restarted_after_upgrade(run_cmd_mock=None, **_):
    assert run_usual_upgrade() == common.RETCODE_OK
    assert run_cmd_mock.mock_calls[-1] == RESTART_WB_RULES_CALL


@patch_all_systemish
def test_no_wb_rules_restart_on_failed_upgrade(run_cmd_mock=None, **_):
    with patch.object(
        release_upgrade, "actual_upgrade", side_effect=subprocess.CalledProcessError(100, "apt-get")
    ):
        assert run_usual_upgrade() == 100

    assert RESTART_WB_RULES_CALL not in run_cmd_mock.mock_calls


@patch_all_systemish
def test_fail_on_low_free_space(enough_free_space_mock=None, run_cmd_mock=None, **_):
    enough_free_space_mock.side_effect = (False,)

    assert run_usual_upgrade() == 1
    run_cmd_mock.assert_not_called()


@patch_all_systemish
def test_no_free_space_check_after_interrupted_transition(
    get_global_progress_flag_mock=None, enough_free_space_mock=None, run_cmd_mock=None, **_
):
    get_global_progress_flag_mock.side_effect = ("error",)
    enough_free_space_mock.side_effect = (False,)

    run_usual_upgrade(no_preliminary_update=False)

    enough_free_space_mock.assert_not_called()
    run_cmd_mock.assert_called()


@pytest.mark.parametrize("progress_flag", ["", "initialize", "progress", "error"])
@patch_all_systemish
def test_fail_on_world_writable_system_dirs(
    progress_flag,
    system_dirs_permissions_ok_mock=None,
    get_global_progress_flag_mock=None,
    set_global_progress_flag_mock=None,
    upgrade_and_maybe_switch_tool_mock=None,
    run_cmd_mock=None,
    **_,
):
    get_global_progress_flag_mock.side_effect = (progress_flag,)
    system_dirs_permissions_ok_mock.return_value = False

    assert run_usual_upgrade(no_preliminary_update=False) == common.RETCODE_FAULT
    upgrade_and_maybe_switch_tool_mock.assert_not_called()
    set_global_progress_flag_mock.assert_not_called()
    run_cmd_mock.assert_not_called()


@patch_all_systemish
def test_call_tool_upgrade_on_flag(upgrade_and_maybe_switch_tool_mock=None, run_cmd_mock=None, **_):
    run_usual_upgrade(no_preliminary_update=False)

    upgrade_and_maybe_switch_tool_mock.assert_called_once()
    run_cmd_mock.assert_not_called()


@patch_all_systemish
def test_no_tool_upgrade_without_flag(upgrade_and_maybe_switch_tool_mock=None, **_):
    run_usual_upgrade()
    upgrade_and_maybe_switch_tool_mock.assert_not_called()


@patch_all_systemish
def test_no_tool_upgrade_after_interrupted_transition(
    get_global_progress_flag_mock=None, upgrade_and_maybe_switch_tool_mock=None, run_cmd_mock=None, **_
):
    get_global_progress_flag_mock.side_effect = ("error",)

    run_usual_upgrade(no_preliminary_update=False)

    upgrade_and_maybe_switch_tool_mock.assert_not_called()
    run_cmd_mock.assert_called()


@patch_all_systemish
def test_no_release_exist(release_exists_mock=None, run_cmd_mock=None, **_):
    release_exists_mock.side_effect = (False,)

    assert run_usual_upgrade() == common.RETCODE_NO_TARGET
    run_cmd_mock.assert_not_called()


@patch_all_systemish
def test_apply_system_config_without_error(
    generate_system_config_mock=None, _cleanup_apt_cached_lists_mock=None, **_
):
    with release_upgrade.apply_new_system_config(STATE_STRETCH, STATE_BULLSEYE):
        pass

    generate_system_config_mock.assert_called_once_with(STATE_BULLSEYE)
    _cleanup_apt_cached_lists_mock.assert_not_called()


@patch_all_systemish
def test_apply_system_config_with_error(
    generate_system_config_mock=None, _cleanup_apt_cached_lists_mock=None, **_
):
    with pytest.raises(Exception):
        with release_upgrade.apply_new_system_config(STATE_STRETCH, STATE_BULLSEYE):
            raise Exception()  # pylint: disable=broad-exception-raised

    generate_system_config_mock.assert_has_calls(
        [
            call(STATE_BULLSEYE),
            call(STATE_STRETCH),
        ]
    )
    _cleanup_apt_cached_lists_mock.assert_called_once()


@patch_all_systemish
def test_wb_configs_reconfigured_after_purge_of_stretch(**_):
    dpkg_reconfigure_mock = MagicMock()
    with patch.multiple("wb.update_manager.release_upgrade", dpkg_reconfigure=dpkg_reconfigure_mock):
        run_usual_upgrade()

    dpkg_reconfigure_mock.assert_called_once_with("wb-configs")


def test_skip_keyring_bootstrap_when_version_is_fresh_enough():
    version_check_mock = MagicMock(return_value=True)
    write_sources_mock = MagicMock()
    apt_update_mock = MagicMock()
    apt_install_mock = MagicMock()
    cleanup_mock = MagicMock()

    with patch.multiple(
        "wb.update_manager.release_upgrade",
        is_debian_archive_keyring_recent_enough=version_check_mock,
        _write_temp_upgrade_sources=write_sources_mock,
        apt_update=apt_update_mock,
        apt_install=apt_install_mock,
        _cleanup_apt_cached_lists=cleanup_mock,
    ):
        release_upgrade.bootstrap_debian_archive_keyring(assume_yes=True)

    write_sources_mock.assert_not_called()
    apt_update_mock.assert_not_called()
    apt_install_mock.assert_not_called()
    cleanup_mock.assert_not_called()


def test_keyring_bootstrap_runs_when_version_is_old():
    version_check_mock = MagicMock(return_value=False)
    write_sources_mock = MagicMock()
    apt_update_mock = MagicMock()
    apt_install_mock = MagicMock()
    cleanup_mock = MagicMock()

    with patch.multiple(
        "wb.update_manager.release_upgrade",
        is_debian_archive_keyring_recent_enough=version_check_mock,
        _write_temp_upgrade_sources=write_sources_mock,
        apt_update=apt_update_mock,
        apt_install=apt_install_mock,
        _cleanup_apt_cached_lists=cleanup_mock,
    ), patch("wb.update_manager.release_upgrade.os.remove") as remove_mock:
        release_upgrade.bootstrap_debian_archive_keyring(assume_yes=True)

    write_sources_mock.assert_called_once_with(release_upgrade.TEMP_BOOTSTRAP_SOURCES_LIST, trusted=True)
    apt_update_mock.assert_called_once()
    apt_install_mock.assert_called_once_with("debian-archive-keyring", assume_yes=True)
    remove_mock.assert_called_once_with(release_upgrade.TEMP_BOOTSTRAP_SOURCES_LIST)
    cleanup_mock.assert_called_once()


def test_keyring_version_check_uses_dpkg_comparison():
    with patch("wb.update_manager.release_upgrade.subprocess.run") as run_mock:
        run_mock.side_effect = [
            subprocess.CompletedProcess(args=[], returncode=0, stdout="2025.1\n"),
            subprocess.CompletedProcess(args=[], returncode=0),
        ]

        assert release_upgrade.is_debian_archive_keyring_recent_enough()

    run_mock.assert_has_calls(
        [
            call(
                ["dpkg-query", "--showformat=${Version}", "--show", "debian-archive-keyring"],
                check=True,
                text=True,
                capture_output=True,
            ),
            call(["dpkg", "--compare-versions", "2025.1", "ge", "2025.1"], check=False),
        ]
    )


def test_breaks_packages_are_not_installed_when_upgrade_is_declined(caplog):
    apt_install_mock = MagicMock()
    caplog.set_level("INFO", logger=release_upgrade.logger.name)

    with patch.multiple(
        "wb.update_manager.release_upgrade",
        apt_update=MagicMock(),
        apt_install=apt_install_mock,
        run_cmd=MagicMock(),
        find_breaks_held_back_packages=MagicMock(return_value=["tmux"]),
        bootstrap_libc=MagicMock(),
        user_confirm=MagicMock(side_effect=common.UserAbortException),
    ):
        with pytest.raises(common.UserAbortException):
            release_upgrade.main_upgrade(assume_yes=False)

    assert "The following packages must be upgraded explicitly before the dist-upgrade: tmux" in caplog.text
    assert call("tmux", assume_yes=True) not in apt_install_mock.call_args_list


def test_breaks_resolution_and_actual_upgrade_are_confirmed_separately():
    calls = []

    def apt_upgrade(*_, **kwargs):
        calls.append("simulate" if kwargs.get("dry_run") else "upgrade")
        return 0, ""

    with patch.multiple(
        "wb.update_manager.release_upgrade",
        apt_update=MagicMock(),
        apt_install=MagicMock(),
        apt_upgrade=MagicMock(side_effect=apt_upgrade),
        run_cmd=MagicMock(),
        find_breaks_held_back_packages=MagicMock(return_value=["tmux"]),
        resolve_breaks_held_back_packages=MagicMock(side_effect=lambda _: calls.append("resolve")),
        bootstrap_libc=MagicMock(side_effect=lambda: calls.append("bootstrap-libc")),
        user_confirm=MagicMock(side_effect=lambda **_: calls.append("confirm")),
        mask_services=MagicMock(return_value=MagicMock(__enter__=MagicMock(), __exit__=MagicMock())),
        upgrade_curl_from_backports=MagicMock(),
        systemd_enable=MagicMock(),
        systemd_restart=MagicMock(),
    ):
        release_upgrade.main_upgrade(assume_yes=False)

    assert calls[:6] == [
        "confirm",
        "resolve",
        "simulate",
        "confirm",
        "bootstrap-libc",
        "upgrade",
    ]


def run_bootstrap_libc(dpkg_query_stdout):
    query_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=dpkg_query_stdout)
    apt_install_mock = MagicMock()

    with patch("wb.update_manager.release_upgrade.subprocess.run") as run_mock, patch.object(
        release_upgrade, "apt_install", apt_install_mock
    ):
        run_mock.return_value = query_result
        release_upgrade.bootstrap_libc()

    run_mock.assert_called_once_with(
        ["dpkg-query", "--showformat=${db:Status-Status} ${Architecture}\n", "--show", "libc6"],
        check=True,
        text=True,
        capture_output=True,
    )
    return apt_install_mock


def test_libc_bootstrap_upgrades_wb8_architectures_together():
    apt_install_mock = run_bootstrap_libc("installed arm64\ninstalled armhf\n")

    apt_install_mock.assert_called_once_with("libc6:arm64", "libc6:armhf", assume_yes=True)


def test_libc_bootstrap_upgrades_native_libc_on_armhf():
    apt_install_mock = run_bootstrap_libc("installed armhf\n")

    apt_install_mock.assert_called_once_with("libc6:armhf", assume_yes=True)


def test_libc_bootstrap_ignores_not_installed_architectures():
    apt_install_mock = run_bootstrap_libc("installed armhf\nconfig-files arm64\n")

    apt_install_mock.assert_called_once_with("libc6:armhf", assume_yes=True)


def test_libc_bootstrap_is_skipped_when_libc_is_not_installed():
    apt_install_mock = run_bootstrap_libc("not-installed armhf\n")

    apt_install_mock.assert_not_called()


@patch_all_systemish
def test_failed_index_download_stops_before_dist_upgrade(
    run_cmd_mock=None, generate_system_config_mock=None, _cleanup_apt_cached_lists_mock=None, **_
):
    def fail_on_apt_update(*args, **_kwargs):
        if args[:3] == ("apt-get", "-q", "update"):
            raise subprocess.CalledProcessError(100, args)
        return (0, "")

    run_cmd_mock.side_effect = fail_on_apt_update

    with patch.object(release_upgrade, "bootstrap_debian_archive_keyring"):
        assert run_usual_upgrade() == 100

    assert not any(c.args[:3] == ("apt-get", "-q", "dist-upgrade") for c in run_cmd_mock.mock_calls)
    assert generate_system_config_mock.mock_calls[-1] == call(STATE_STRETCH)
    _cleanup_apt_cached_lists_mock.assert_called()


@patch_all_systemish
def test_packages_removed_by_upgrade_are_purged(
    save_config_files_snapshot_mock=None,
    discard_unused_config_files_snapshot_mock=None,
    purge_packages_removed_by_upgrade_mock=None,
    **_,
):
    with patch.object(release_upgrade, "bootstrap_debian_archive_keyring"):
        assert run_usual_upgrade() == 0

    save_config_files_snapshot_mock.assert_called_once_with()
    discard_unused_config_files_snapshot_mock.assert_not_called()
    purge_packages_removed_by_upgrade_mock.assert_called_once_with(True)


@patch_all_systemish
def test_purge_follows_confirm_steps(purge_packages_removed_by_upgrade_mock=None, **_):
    with patch.object(release_upgrade, "user_confirm"), patch.object(
        release_upgrade, "bootstrap_debian_archive_keyring"
    ):
        assert run_usual_upgrade(confirm_steps=True) == 0

    purge_packages_removed_by_upgrade_mock.assert_called_once_with(False)


@patch_all_systemish
def test_packages_removed_by_autoremove_are_not_purged(
    run_cmd_mock=None, purge_packages_removed_by_upgrade_mock=None, **_
):
    def is_autoremove(c):
        return c.args[:3] == ("apt-get", "-q", "autoremove")

    autoremove_calls_before_purge = []
    purge_packages_removed_by_upgrade_mock.side_effect = lambda _: autoremove_calls_before_purge.extend(
        filter(is_autoremove, run_cmd_mock.mock_calls)
    )
    with patch.object(release_upgrade, "bootstrap_debian_archive_keyring"):
        assert run_usual_upgrade() == 0

    purge_packages_removed_by_upgrade_mock.assert_called_once()
    assert not autoremove_calls_before_purge
    assert any(is_autoremove(c) for c in run_cmd_mock.mock_calls)


@patch_all_systemish
def test_unused_snapshot_is_discarded_on_failed_upgrade(
    discard_unused_config_files_snapshot_mock=None, purge_packages_removed_by_upgrade_mock=None, **_
):
    with patch.object(release_upgrade, "main_upgrade", side_effect=KeyboardInterrupt), patch.object(
        release_upgrade, "bootstrap_debian_archive_keyring"
    ):
        assert run_usual_upgrade() == common.RETCODE_USER_ABORT

    discard_unused_config_files_snapshot_mock.assert_called_once_with()
    purge_packages_removed_by_upgrade_mock.assert_not_called()


def mock_dpkg_query(dpkg_query_stdout):
    return patch(
        "wb.update_manager.release_upgrade.subprocess.run",
        return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout=dpkg_query_stdout),
    )


DPKG_QUERY_BEFORE_UPGRADE = "installed ntp\nconfig-files old-user-removed\ninstalled libfoo:armhf\n"
DPKG_QUERY_AFTER_UPGRADE = (
    "config-files ntp\nconfig-files old-user-removed\nconfig-files libfoo:armhf\ninstalled ntpsec\n"
)


def test_only_packages_removed_by_upgrade_are_purged(tmp_path):
    snapshot = tmp_path / "snapshot"
    apt_purge_mock = MagicMock()

    with patch.object(release_upgrade, "CONFIG_FILES_SNAPSHOT", str(snapshot)), patch.object(
        release_upgrade, "apt_purge", apt_purge_mock
    ):
        with mock_dpkg_query(DPKG_QUERY_BEFORE_UPGRADE) as run_mock:
            release_upgrade.save_config_files_snapshot()
        run_mock.assert_called_once_with(
            ["dpkg-query", "--showformat=${db:Status-Status} ${binary:Package}\n", "--show"],
            check=True,
            text=True,
            capture_output=True,
        )

        with mock_dpkg_query(DPKG_QUERY_AFTER_UPGRADE):
            release_upgrade.purge_packages_removed_by_upgrade(assume_yes=True)

    apt_purge_mock.assert_called_once_with("libfoo:armhf", "ntp", assume_yes=True)
    assert not snapshot.exists()
    assert list(tmp_path.iterdir()) == []


def test_snapshot_from_interrupted_upgrade_is_kept(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.write_text("old-user-removed\n")

    with patch.object(release_upgrade, "CONFIG_FILES_SNAPSHOT", str(snapshot)):
        # the interrupted upgrade has already removed ntp
        with mock_dpkg_query(DPKG_QUERY_AFTER_UPGRADE) as run_mock:
            release_upgrade.save_config_files_snapshot()

    run_mock.assert_not_called()
    assert snapshot.read_text() == "old-user-removed\n"


def test_nothing_is_purged_without_snapshot(tmp_path):
    apt_purge_mock = MagicMock()

    with patch.object(release_upgrade, "CONFIG_FILES_SNAPSHOT", str(tmp_path / "snapshot")), patch.object(
        release_upgrade, "apt_purge", apt_purge_mock
    ), mock_dpkg_query(DPKG_QUERY_AFTER_UPGRADE):
        release_upgrade.purge_packages_removed_by_upgrade()

    apt_purge_mock.assert_not_called()


@pytest.mark.parametrize(
    "purge_error", [subprocess.CalledProcessError(100, "apt-get"), common.UserAbortException()]
)
def test_failed_purge_does_not_fail_upgrade(tmp_path, purge_error):
    snapshot = tmp_path / "snapshot"
    snapshot.write_text("old-user-removed\n")
    apt_purge_mock = MagicMock(side_effect=purge_error)

    with patch.object(release_upgrade, "CONFIG_FILES_SNAPSHOT", str(snapshot)), patch.object(
        release_upgrade, "apt_purge", apt_purge_mock
    ), mock_dpkg_query(DPKG_QUERY_AFTER_UPGRADE):
        release_upgrade.purge_packages_removed_by_upgrade()

    apt_purge_mock.assert_called_once()
    assert not snapshot.exists()


def test_unused_snapshot_is_discarded(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.write_text("old-user-removed\n")

    with patch.object(release_upgrade, "CONFIG_FILES_SNAPSHOT", str(snapshot)), mock_dpkg_query(
        DPKG_QUERY_BEFORE_UPGRADE
    ):
        release_upgrade.discard_unused_config_files_snapshot()

    assert not snapshot.exists()


def test_snapshot_is_kept_when_upgrade_removed_packages(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.write_text("old-user-removed\n")

    with patch.object(release_upgrade, "CONFIG_FILES_SNAPSHOT", str(snapshot)), mock_dpkg_query(
        DPKG_QUERY_AFTER_UPGRADE
    ):
        release_upgrade.discard_unused_config_files_snapshot()

    assert snapshot.read_text() == "old-user-removed\n"
