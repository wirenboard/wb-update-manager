import os
import shutil
import subprocess
import sys
import textwrap
from contextlib import ExitStack, contextmanager
from pathlib import Path

from .common import (
    CONFIRM_STEPS_ARGNAME,
    NO_PRELIMINARY_UPDATE_ARGNAME,
    RETCODE_FAULT,
    RETCODE_NO_TARGET,
    RETCODE_OK,
    RETCODE_USER_ABORT,
    UPDATE_DEBIAN_RELEASE_ARGNAME,
    SystemState,
    UserAbortException,
    _cleanup_apt_cached_lists,
    generate_system_config,
    logger,
    release_exists,
    run_cmd,
    user_confirm,
)
from .tools import (
    apt_autoremove,
    apt_install,
    apt_mark_hold,
    apt_mark_unhold,
    apt_purge,
    apt_update,
    apt_upgrade,
    systemd_enable,
    systemd_mask,
    systemd_restart,
    systemd_unmask,
)


def _free_space_mb(path):
    stat = os.statvfs(path)
    return stat.f_bavail * stat.f_bsize / 1024 / 1024


def enough_free_space():
    # these values were checked using binary search on configuration with all standard software
    # with different volumes on / and /var (not fully correct, but still representative).
    # minimal working solution was 125 MB for root and 300 MB for /var.
    # I add a little bit of extra requirement on root to be on a safe side.
    min_cache_free_space_mb = 300
    min_system_free_space_mb = 150

    if _free_space_mb("/var/cache/apt/archives") < min_cache_free_space_mb:
        logger.error("Need at least %d MB of free space for apt cache (/mnt/data)", min_cache_free_space_mb)
        return False

    if _free_space_mb("/usr/bin") < min_system_free_space_mb:
        logger.error("Need at least %d MB of free space in root partition", min_system_free_space_mb)
        return False

    return True


TEMP_APT_PREFERENCE_FOR_TOOL = "/etc/apt/preferences.d/001wb-update-tool-stretch"


def create_temp_apt_policy_for_tool():
    logger.info("Creating temp apt preference to keep wb-update-manager from stretch")
    with open(TEMP_APT_PREFERENCE_FOR_TOOL, "w", encoding="utf-8") as f:
        f.write(
            textwrap.dedent(
                """
                Package: *wb-update-manager
                Pin: release o=wirenboard,l=*stretch*
                Pin-Priority: 800

                Package: *wb-update-manager
                Pin: release o=wirenboard,l=*bullseye*
                Pin-Priority: 10"""
            ).strip()
        )


def remove_temp_apt_policy_for_tool():
    logger.info("Removing temp apt preference to keep wb-update-manager from stretch")
    os.remove(TEMP_APT_PREFERENCE_FOR_TOOL)


@contextmanager
def temp_apt_policy_for_tool():
    create_temp_apt_policy_for_tool()
    try:
        yield
    finally:
        remove_temp_apt_policy_for_tool()


def touch_tool_update_done_flag():
    Path("/run/wb-release-tool-updated").touch()


def upgrade_and_maybe_switch_tool(assume_yes, log_filename=None):
    user_confirm(
        textwrap.dedent(
            """
            Now the system will be updated using Apt without changing the release.

            It is required to get latest state possible
            to make release change process more controllable.

            Make sure you have all your data backed up."""
        ).strip(),
        assume_yes,
    )

    # create flag which allows wb-update-manager to finish upgrade
    touch_tool_update_done_flag()

    with temp_apt_policy_for_tool():
        logger.info("Try to install new wb-update-manager version")
        apt_update()
        apt_install("wb-update-manager", assume_yes=assume_yes)

    logger.info("Starting (possibly updated) update utility as new process")
    args = sys.argv + [NO_PRELIMINARY_UPDATE_ARGNAME]

    # preserve update log filename from the first stage
    if log_filename:
        args += [LOG_FILENAME_ARGNAME, log_filename]

    # close log handlers in this instance to make it free for second one
    for handler in logger.handlers:
        handler.close()

    os.execvp(args[0], args)

    logger.fatal("Should not be here after execvp!")
    return 1


TEMP_UPGRADE_SOURCES_LIST = "/etc/apt/sources.list.d/000wb-bullseye-upgrade.list"
TEMP_UPGRADE_APT_PREFERENCES = "/etc/apt/preferences.d/000wb-bullseye-upgrade"


def create_temp_apt_configs():
    logger.info("Creating temp sources list for Bullseye on %s", TEMP_UPGRADE_SOURCES_LIST)
    with open(TEMP_UPGRADE_SOURCES_LIST, "w", encoding="utf-8") as f:
        f.write(
            textwrap.dedent(
                """
                deb http://deb.debian.org/debian bullseye main
                deb http://deb.debian.org/debian bullseye-updates main
                deb http://deb.debian.org/debian bullseye-backports main
                deb http://security.debian.org/debian-security bullseye-security main"""
            ).strip()
        )

    logger.info("Creating temp apt preferences for Bullseye on %s", TEMP_UPGRADE_APT_PREFERENCES)
    with open(TEMP_UPGRADE_APT_PREFERENCES, "w", encoding="utf-8") as f:
        f.write(
            textwrap.dedent(
                """
                Package: *
                Pin: release o=Debian,n=stretch*
                Pin-Priority: -1

                Package: *
                Pin: release o=Debian,n=bullseye*
                Pin-Priority: 600"""
            ).strip()
        )


def remove_temp_apt_configs():
    logger.info("Cleaning up temp apt configs for bullseye transition")
    os.remove(TEMP_UPGRADE_SOURCES_LIST)
    os.remove(TEMP_UPGRADE_APT_PREFERENCES)


@contextmanager
def make_temp_apt_configs():
    try:
        create_temp_apt_configs()
        yield
    except Exception:
        _cleanup_apt_cached_lists()
        raise
    finally:
        remove_temp_apt_configs()


@contextmanager
def mask_services(*services):
    try:
        logger.info("Masking services %s", str(services))
        systemd_mask(*services)
        yield
    finally:
        logger.info("Unmasking services %s", str(services))
        systemd_unmask(*services)


@contextmanager
def hold_packages(*packages):
    try:
        logger.info("Setting packages %s on hold", str(packages))
        apt_mark_hold(*packages)
        yield
    finally:
        logger.info("Unholding packages %s", str(packages))
        apt_mark_unhold(*packages)


def ensure_new_openssh(assume_yes):
    apt_update()
    logger.info("Updating openssh-server first to make Wiren Board available during update")
    with mask_services("ssh.service"):
        apt_install("openssh-server", assume_yes=assume_yes)

    logger.info("Restarting ssh.service to maintain connectivity")
    systemd_restart("ssh.service")


def ensure_python2_deps(assume_yes):
    apt_update()
    logger.info("Installing python-is-python2 for correct dependency resolving")
    apt_install("python-is-python2", assume_yes=assume_yes)


def mark_python2_for_cleanup():
    logger.info("Mark python-is-python2 as automatically installed to remove it in future")
    run_cmd("apt-mark", "auto", "python-is-python2")


def main_upgrade(assume_yes):
    # these services will be masked (preventing restart during update)
    # and then enabled or restarted manually
    services_to_restart = ("nginx.service", "mosquitto.service", "wb-mqtt-mbgate.service")
    services_to_enable = ("hostapd.service", "wb-configs.service", "wb-configs-early.service")
    services_to_mask = ("nginx.service", "mosquitto.service", "hostapd.service", "wb-mqtt-mbgate.service")

    apt_update()

    if not assume_yes:
        logger.info("Simulating upgrade")
        run_cmd("apt", "dist-upgrade", "-s", "-V")
        user_confirm(assume_yes=False)

    with mask_services(*services_to_mask):
        logger.info("Performing actual upgrade")
        apt_upgrade(dist=True, assume_yes=True)  # this step is confirmed in simulating above

        logger.info("Performing actual upgrade - second stage (e2fsprogs update)")
        logger.debug("Updating packages list, may be outdated after long update procedure")
        apt_update()
        apt_upgrade(dist=True, assume_yes=assume_yes)

    systemd_enable(*services_to_enable)
    systemd_restart(*services_to_restart)


def purge_old_wb_configs(assume_yes):
    logger.info("Purging wb-configs-stretch to remove old sources.list")
    apt_purge("wb-configs-stretch", assume_yes=assume_yes)


def touch_system_update_done_flag():
    logger.debug("Creating a flag for banner message to remind about reboot")
    Path("/run/wb-debian-release-updated").touch()


def update_tool_on_new_system(assume_yes):
    logger.info("Updating wb-update-manager tool")
    apt_update()
    apt_install("wb-update-manager", assume_yes=assume_yes)


def update_release_info():
    apt_update()
    apt_install("wb-release-info", assume_yes=True)


@contextmanager
def apply_new_system_config(current_state, new_state):
    generate_system_config(new_state)
    try:
        yield  # perform update
    except Exception:
        logger.info("Restoring original system state")
        generate_system_config(current_state)

        logger.info("Cleaning up apt cache (to make manual apt calls safe from now)")
        _cleanup_apt_cached_lists()
        raise
    else:
        logger.debug("new system config has done well, keeping this config and apt cache")


def make_new_state(state: SystemState) -> SystemState:
    controller_version = state.target.split("/", maxsplit=1)[0]
    return state._replace(target=(controller_version + "/bullseye"))


def set_global_progress_flag(value: str):
    flag = "/var/lib/wb-debian-release-update-in-progress"

    if value:
        with open(flag, "w", encoding="utf-8") as f:
            f.write(value)
    else:
        logger.debug("Removing system release update flag")
        os.remove(flag)


def install_progress_banner():
    logger.info("Copying restart-required motd message from current wb-update-manager version")
    shutil.copy("/usr/share/wb-update-manager/99-wb-debian-release-updated", "/etc/update-motd.d/")


def actual_upgrade(current_state, new_state, assume_yes=False):
    user_confirm(
        textwrap.dedent(
            """
            Now the release will be switched to {}, prefix "{}", target "{}".

            During update, the sources and preferences files will be changed,
            then apt-get dist-upgrade action will start.

            This process is potentially dangerous and may break your software.

            To control process on each step, use this command with {} flag.

            STOP RIGHT THERE IF THIS IS A PRODUCTION SYSTEM!"""
        )
        .format(new_state.suite, new_state.repo_prefix, new_state.target, CONFIRM_STEPS_ARGNAME)
        .strip(),
        assume_yes,
    )

    with ExitStack() as stack:
        # these operation order is not important
        stack.enter_context(apply_new_system_config(current_state, new_state))
        stack.enter_context(make_temp_apt_configs())
        stack.enter_context(hold_packages("wb-update-manager", "wb-release-info"))

        ensure_new_openssh(assume_yes)
        ensure_python2_deps(assume_yes)

        main_upgrade(assume_yes)

    mark_python2_for_cleanup()
    purge_old_wb_configs(assume_yes)
    apt_autoremove(assume_yes)

    update_release_info()
    update_tool_on_new_system(assume_yes)

    touch_system_update_done_flag()


def upgrade_new_debian_release(
    state: SystemState, log_filename, assume_yes=False, confirm_steps=False, no_preliminary_update=False
):
    # how to do it only on fresh start? check state of 'initialize'
    if not enough_free_space():
        return 1

    try:
        if not no_preliminary_update:
            # this may break if apt failed during processes, so should be able to skip it somehow?
            # or enforce tool installation from stretch inside
            upgrade_and_maybe_switch_tool(assume_yes, log_filename=log_filename)
            return 0  # should never be here

        new_state = make_new_state(state)
        if not release_exists(new_state):
            logger.error("Target state does not exist: %s", new_state)
            return RETCODE_NO_TARGET

        retcode = RETCODE_OK
        install_progress_banner()

        print("============ Update debian release to bullseye ============")
        set_global_progress_flag("progress")

        actual_upgrade(state, new_state, assume_yes=not confirm_steps)

        logger.info("Done! Please reboot system")
        set_global_progress_flag(None)

    except UserAbortException:
        logger.info("Aborted by user")
        retcode = RETCODE_USER_ABORT

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        retcode = RETCODE_USER_ABORT

    except subprocess.CalledProcessError as e:
        logger.error("\nThe subprocess %s has failed with status %d", str(e.cmd), e.returncode)
        retcode = e.returncode

    except Exception:  # pylint: disable=broad-except
        logger.exception("Something went wrong, check output and try again")
        retcode = RETCODE_FAULT

    finally:
        if log_filename:
            logger.info("Update log is saved in %s", log_filename)

    if retcode != RETCODE_OK:
        logger.info("Try running wb-release %s again to continue transition", UPDATE_DEBIAN_RELEASE_ARGNAME)
        set_global_progress_flag("error")

    return retcode
