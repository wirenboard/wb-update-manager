from collections import namedtuple
from contextlib import ExitStack
from unittest.mock import call, mock_open, patch

import pytest

from wb.update_manager import common, release_upgrade

FakeStatvfs = namedtuple("FakeStatvfs", "f_bavail f_bsize")


def vfs_megabytes(mbs):
    return FakeStatvfs(mbs * 1024 * 1024 / 512, 512)


@pytest.mark.parametrize(
    "target, partition_exists, free_space_mb, expected_calls",
    [
        (
            "wb6/bullseye",
            True,
            (280, 350),
            [call("/var/cache/apt/archives"), call("/usr/bin")],
        ),
        (
            "wb7/bullseye",
            True,
            (280, 350),
            [call("/var/cache/apt/archives"), call("/usr/bin")],
        ),
        (
            "wb8/bullseye",
            True,
            (310, 450),
            [call("/var/cache/apt/archives"), call("/usr/bin")],
        ),
        ("wb6/bullseye", False, (670,), [call("/usr/bin")]),
        ("wb7/bullseye", False, (670,), [call("/usr/bin")]),
        ("wb8/bullseye", False, (750,), [call("/usr/bin")]),
    ],
)
def test_enough_free_space_check(target, partition_exists, free_space_mb, expected_calls):
    state = common.SystemState("testing", target, "", True)

    with patch("os.path.exists", return_value=partition_exists) as exists_mock, patch(
        "os.statvfs"
    ) as statvfs_mock:
        statvfs_mock.side_effect = tuple(vfs_megabytes(value) for value in free_space_mb)

        assert release_upgrade.enough_free_space(state)
        exists_mock.assert_called_once_with("/dev/mmcblk0p6")
        assert statvfs_mock.call_args_list == expected_calls


@pytest.mark.parametrize(
    "target, partition_exists, free_space_mb, expected_calls",
    [
        ("wb6/bullseye", True, (279,), [call("/var/cache/apt/archives")]),
        (
            "wb7/bullseye",
            True,
            (280, 349),
            [call("/var/cache/apt/archives"), call("/usr/bin")],
        ),
        ("wb8/bullseye", True, (309,), [call("/var/cache/apt/archives")]),
        (
            "wb8/bullseye",
            True,
            (310, 449),
            [call("/var/cache/apt/archives"), call("/usr/bin")],
        ),
        ("wb6/bullseye", False, (669,), [call("/usr/bin")]),
        ("wb8/bullseye", False, (749,), [call("/usr/bin")]),
    ],
)
def test_no_free_space_check(target, partition_exists, free_space_mb, expected_calls):
    state = common.SystemState("testing", target, "", True)

    with patch("os.path.exists", return_value=partition_exists), patch(
        "os.statvfs"
    ) as statvfs_mock:
        statvfs_mock.side_effect = tuple(vfs_megabytes(value) for value in free_space_mb)

        assert not release_upgrade.enough_free_space(state)
        assert statvfs_mock.call_args_list == expected_calls


def test_temp_apt_policy_for_tool_cleanup():
    with ExitStack() as patches:
        my_open = patches.enter_context(patch("builtins.open", new=mock_open()))
        my_remove = patches.enter_context(patch("os.remove"))

        with release_upgrade.temp_apt_policy_for_tool():
            my_open.assert_called_once()
            my_open().write.assert_called()

        filename = my_open.mock_calls[0].args[0]
        my_remove.assert_called_once_with(filename)


def test_temp_apt_configs_cleanup():
    with ExitStack() as patches:
        my_open = patches.enter_context(patch("builtins.open", new=mock_open()))
        my_remove = patches.enter_context(patch("os.remove"))
        my_cleanup = patches.enter_context(patch.object(release_upgrade, "_cleanup_apt_cached_lists"))

        with release_upgrade.make_temp_apt_configs():
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
        my_create = patches.enter_context(patch.object(release_upgrade, "create_temp_apt_configs"))
        my_remove = patches.enter_context(patch.object(release_upgrade, "remove_temp_apt_configs"))
        my_cleanup = patches.enter_context(patch.object(release_upgrade, "_cleanup_apt_cached_lists"))

        with pytest.raises(Exception):
            with release_upgrade.make_temp_apt_configs():
                raise Exception()  # pylint: disable=broad-exception-raised

        my_create.assert_called_once()
        my_remove.assert_called_once()
        my_cleanup.assert_called_once()
