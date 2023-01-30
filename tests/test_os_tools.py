from collections import namedtuple
from contextlib import ExitStack
from unittest.mock import call, mock_open, patch

import pytest

from wb.update_manager import bullseye

FakeStatvfs = namedtuple("FakeStatvfs", "f_bavail f_bsize")


def vfs_megabytes(mbs):
    return FakeStatvfs(mbs * 1024 * 1024 / 512, 512)


def test_enough_free_space_check():
    with patch("os.statvfs") as statvfs_mock:
        statvfs_mock.side_effect = (vfs_megabytes(1024), vfs_megabytes(1024))

        assert bullseye.enough_free_space()
        statvfs_mock.assert_has_calls(
            [
                call("/var/cache/apt/archives"),
                call("/usr/bin"),
            ]
        )


def test_no_free_space_check():
    with patch("os.statvfs") as statvfs_mock:
        statvfs_mock.side_effect = (vfs_megabytes(10), vfs_megabytes(1024))
        assert not bullseye.enough_free_space()

        statvfs_mock.reset_mock()
        statvfs_mock.side_effect = (vfs_megabytes(1024), vfs_megabytes(10))
        assert not bullseye.enough_free_space()

        statvfs_mock.reset_mock()
        statvfs_mock.side_effect = (vfs_megabytes(10), vfs_megabytes(10))
        assert not bullseye.enough_free_space()


def test_temp_apt_policy_for_tool_cleanup():
    with ExitStack() as patches:
        my_open = patches.enter_context(patch("builtins.open", new=mock_open()))
        my_remove = patches.enter_context(patch("os.remove"))

        with bullseye.temp_apt_policy_for_tool():
            my_open.assert_called_once()
            my_open().write.assert_called()

        filename = my_open.mock_calls[0].args[0]
        my_remove.assert_called_once_with(filename)


def test_temp_apt_configs_cleanup():
    with ExitStack() as patches:
        my_open = patches.enter_context(patch("builtins.open", new=mock_open()))
        my_remove = patches.enter_context(patch("os.remove"))
        my_cleanup = patches.enter_context(patch.object(bullseye, "_cleanup_apt_cached_lists"))

        with bullseye.make_temp_apt_configs():
            my_open.assert_called()
            my_open().write.assert_called()

        filenames = []
        for call_entry in my_open.call_args_list:
            if len(call_entry.args) > 0:
                filenames.append(call_entry.args[0])

        assert len(filenames) >= 1
        for filename in filenames:
            my_remove.assert_any_call(filename)

        my_cleanup.assert_not_called()


def test_temp_apt_configs_clean_cache_on_error():
    with ExitStack() as patches:
        my_create = patches.enter_context(patch.object(bullseye, "create_temp_apt_configs"))
        my_remove = patches.enter_context(patch.object(bullseye, "remove_temp_apt_configs"))
        my_cleanup = patches.enter_context(patch.object(bullseye, "_cleanup_apt_cached_lists"))

        with pytest.raises(Exception):
            with bullseye.make_temp_apt_configs():
                raise Exception()

        my_create.assert_called_once()
        my_remove.assert_called_once()
        my_cleanup.assert_called_once()
