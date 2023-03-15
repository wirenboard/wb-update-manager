import functools
import logging
from contextlib import ExitStack
from unittest.mock import MagicMock, call, patch

import pytest

from wb.update_manager import bullseye, common, tools

STATE_STRETCH = common.SystemState("testing", "wb7/stretch", "", True)
STATE_BULLSEYE = common.SystemState("testing", "wb7/bullseye", "", True)


def patch_all_systemish(func):
    @functools.wraps(func)
    def patched(*args, **kwargs):
        with ExitStack() as stack:
            functions = (
                (tools, "run_cmd"),
                (common, "run_cmd"),
                (bullseye, "run_cmd"),
                (bullseye, "generate_system_config"),
                (common, "generate_sources_list"),
                (common, "generate_release_apt_preferences"),
                (bullseye, "_cleanup_apt_cached_lists"),
                (bullseye, "create_temp_apt_configs"),
                (bullseye, "remove_temp_apt_configs"),
                (bullseye, "create_temp_apt_policy_for_tool"),
                (bullseye, "remove_temp_apt_policy_for_tool"),
                (bullseye, "touch_tool_update_done_flag"),
                (bullseye, "touch_system_update_done_flag"),
                (bullseye, "upgrade_and_maybe_switch_tool"),
                (bullseye, "set_global_progress_flag"),
                (bullseye, "install_progress_banner"),
                (bullseye, "release_exists"),
                (bullseye, "enough_free_space"),
            )
            for obj, function in functions:
                mock_name = function + "_mock"
                if mock_name in kwargs:
                    stack.enter_context(patch.object(obj, function, new=kwargs[mock_name]))
                else:
                    kwargs[mock_name] = stack.enter_context(patch.object(obj, function))

            kwargs["release_exists_mock"].side_effect = (True,)
            kwargs["enough_free_space_mock"].side_effect = (True,)

            return func(*args, **kwargs)

    return patched


def run_usual_upgrade(log_filename=None, assume_yes=True, no_preliminary_update=True, confirm_steps=False):
    return bullseye.upgrade_new_debian_release(
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


def fail_on_nth_call(call_n, throw=None, return_value=None):
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
def num_ususal_run_cmd_calls(run_cmd_mock=None, **kwargs):
    run_usual_upgrade()
    return len(run_cmd_mock.mock_calls)


@pytest.mark.parametrize("successful_cmds", range(num_ususal_run_cmd_calls()))
@patch_all_systemish
def test_packages_always_become_unhold(successful_cmds, run_cmd_mock=None, **_):
    collector = MockCalledItemCollector()

    run_cmd_mock.side_effect = fail_on_nth_call(successful_cmds)

    with patch.multiple(
        "wb.update_manager.bullseye", apt_mark_hold=collector.adder, apt_mark_unhold=collector.remover
    ):
        run_usual_upgrade()

    assert len(collector.collected) == 0


@pytest.mark.parametrize("successful_cmds", range(num_ususal_run_cmd_calls()))
@patch_all_systemish
def test_services_always_become_unmasked(successful_cmds, run_cmd_mock=None, **_):
    collector = MockCalledItemCollector()

    run_cmd_mock.side_effect = fail_on_nth_call(successful_cmds)

    with patch.multiple(
        "wb.update_manager.bullseye", systemd_mask=collector.adder, systemd_unmask=collector.remover
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


@patch_all_systemish
def test_fail_on_low_free_space(enough_free_space_mock=None, run_cmd_mock=None, **_):
    enough_free_space_mock.side_effect = (False,)

    assert run_usual_upgrade() == 1
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
def test_no_release_exist(release_exists_mock=None, run_cmd_mock=None, **_):
    release_exists_mock.side_effect = (False,)

    assert run_usual_upgrade() == common.RETCODE_NO_TARGET
    run_cmd_mock.assert_not_called()


@patch_all_systemish
def test_apply_system_config_without_error(
    generate_system_config_mock=None, _cleanup_apt_cached_lists_mock=None, **_
):
    with bullseye.apply_new_system_config(STATE_STRETCH, STATE_BULLSEYE):
        pass

    generate_system_config_mock.assert_called_once_with(STATE_BULLSEYE)
    _cleanup_apt_cached_lists_mock.assert_not_called()


@patch_all_systemish
def test_apply_system_config_with_error(
    generate_system_config_mock=None, _cleanup_apt_cached_lists_mock=None, **_
):
    with pytest.raises(Exception):
        with bullseye.apply_new_system_config(STATE_STRETCH, STATE_BULLSEYE):
            raise Exception()

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
    with patch.multiple("wb.update_manager.bullseye", dpkg_reconfigure=dpkg_reconfigure_mock):
        run_usual_upgrade()

    dpkg_reconfigure_mock.assert_called_once_with("wb-configs")
