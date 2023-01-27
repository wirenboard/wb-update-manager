"""
This package contains the library to manage with Wirenboard release data on board
and provides the main for the wb-release tool which switches release branches.
"""
import argparse
import atexit
import logging
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from systemd import journal

from .bullseye import upgrade_new_debian_release
from .common import (
    CONFIRM_STEPS_ARGNAME,
    LOG_FILENAME_ARGNAME,
    NO_PRELIMINARY_UPDATE_ARGNAME,
    RETCODE_EINVAL,
    RETCODE_FAULT,
    RETCODE_NO_TARGET,
    RETCODE_OK,
    RETCODE_USER_ABORT,
    UPDATE_DEBIAN_RELEASE_ARGNAME,
    WB_ORIGIN,
    WB_RELEASE_FILENAME,
    WB_SOURCES_LIST_FILENAME,
    ReleaseInfo,
    SystemState,
    UserAbortException,
    _cleanup_apt_cached_lists,
    generate_system_config,
    logger,
    release_exists,
    run_apt,
    run_cmd,
    user_confirm,
)

WB_TEMP_UPGRADE_PREFERENCES_FILENAME = "/etc/apt/preferences.d/00wb-release-upgrade-temp"
DEFAULT_LOG_FILENAME = "/var/log/wb-release/update_{datetime}.log"


def configure_logger(log_filename=None, no_journald_log=False):
    logger.setLevel(logging.DEBUG)

    # apt-get reconfigures pty somehow, so CR symbol becomes necessary in stdout,
    # so it is added to logging format string here
    # logging.basicConfig(format='%(asctime)s %(name)s %(levelname)s: %(message)s\r')
    fmt = logging.Formatter(fmt="%(asctime)s %(message)s\r", datefmt="%H:%M:%S")

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(fmt)
    stdout_handler.setLevel(logging.INFO)
    logger.addHandler(stdout_handler)

    if log_filename:
        file_fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s: %(message)s")
        os.makedirs(os.path.dirname(log_filename), exist_ok=True)
        file_handler = logging.FileHandler(log_filename)
        file_handler.setFormatter(file_fmt)
        file_handler.setLevel(logging.DEBUG)
        logger.addHandler(file_handler)

        logger.info("Update log is written to {}".format(log_filename))

    if not no_journald_log:
        journald_handler = journal.JournalHandler(SYSLOG_IDENTIFIER="wb-release")
        journald_handler.setLevel(logging.INFO)
        logger.addHandler(journald_handler)

        logger.info("journald logging enabled")


class NoSuiteInfoError(Exception):
    pass


class ImpossibleUpdateError(Exception):
    pass


def read_wb_release_file(filename):
    d = {}
    with open(filename) as f:
        for line in f:
            line = line.strip()
            if line[0] != "#":
                key, value = line.split("=", maxsplit=1)
                d[key.lower()] = value.strip('"').strip("'")

    return ReleaseInfo(**d)


def read_apt_sources_list_suite(filename) -> str:
    if os.path.exists(filename):
        with open(filename) as f:
            for line in f:
                line = line.partition("#")[0].rstrip()
                if line.startswith("deb http"):
                    return line.split(" ", maxsplit=4)[2]

    raise NoSuiteInfoError()


def get_current_state(filename=WB_RELEASE_FILENAME, sources_filename=WB_SOURCES_LIST_FILENAME) -> SystemState:
    release_info = read_wb_release_file(filename)

    try:
        sources_list_suite = read_apt_sources_list_suite(sources_filename)
        consistent = release_info.suite == sources_list_suite
    except NoSuiteInfoError:
        consistent = False

    return SystemState(release_info.suite, release_info.target, release_info.repo_prefix, consistent)


def get_target_state(
    old_state: SystemState, reset_url=False, prefix=None, target_release=None
) -> SystemState:
    if reset_url and prefix:
        raise ImpossibleUpdateError("both --prefix and --reset-url are set")

    if reset_url:
        new_prefix = ""
    elif prefix:
        new_prefix = prefix
    else:
        new_prefix = old_state.repo_prefix

    if target_release:
        new_suite = target_release
    else:
        new_suite = old_state.suite

    new_prefix = new_prefix.strip(" /")

    return SystemState(new_suite, old_state.target, new_prefix, consistent=True)


def generate_tmp_apt_preferences(
    target_state: SystemState, origin=WB_ORIGIN, filename=WB_TEMP_UPGRADE_PREFERENCES_FILENAME
):
    with open(filename, "w") as f:
        f.write(
            textwrap.dedent(
                """
                # This file is automatically generated by wb-release.
                # DO NOT EDIT THIS FILE!
                Package: *
                Pin: release o={origin}, a={suite}
                Pin-Priority: 1010

                Package: *
                Pin: release o=wirenboard
                Pin-Priority: -10"""
            )
            .format(origin=origin, suite=target_state.suite)
            .strip()
        )


def _cleanup_tmp_apt_preferences(filename=WB_TEMP_UPGRADE_PREFERENCES_FILENAME):
    logger.info("Cleaning up temp apt preferences {}".format(filename))
    os.remove(filename)


def _restore_system_config(original_state):
    logger.info("Restoring original system state")
    generate_system_config(original_state)

    logger.info("Cleaning apt cache")
    _cleanup_apt_cached_lists()


def update_first_stage(assume_yes=False, log_filename=None):
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

    # create flag which allows old wb-update-manager to finish upgrade (from less than 1.2.5~upgrade5).
    # This is required only for bullseye-transitional version of wb-update-manager.
    Path("/run/wb-release-tool-updated").touch()

    logger.info("Performing upgrade on the current release")
    run_system_update(assume_yes)

    logger.info("Starting (possibly updated) update utility as new process")
    args = sys.argv + [NO_PRELIMINARY_UPDATE_ARGNAME]

    # preserve update log filename from the first stage
    if log_filename:
        args += [LOG_FILENAME_ARGNAME, log_filename]

    # close log handlers in this instance to make it free for second one
    for h in logger.handlers:
        h.close()

    res = subprocess.run(args, check=True)
    return res.returncode


def update_second_stage(state: SystemState, old_state: SystemState, assume_yes=False):
    if state != old_state:
        user_confirm(
            textwrap.dedent(
                """
                     Now the release will be switched to {}, prefix "{}".

                     During update, the sources and preferences files will be changed,
                     then apt-get dist-upgrade action will start. Some packages may be downgraded.

                     This process is potentially dangerous and may break your software.

                     STOP RIGHT THERE IF THIS IS A PRODUCTION SYSTEM!"""
            )
            .format(state.suite, state.repo_prefix)
            .strip(),
            assume_yes,
        )

        logger.info('Setting target release to {}, prefix "{}"'.format(state.suite, state.repo_prefix))
        generate_system_config(state)
        atexit.register(_restore_system_config, old_state)
    else:
        user_confirm(
            textwrap.dedent(
                """
                    Now system packages will be reinstalled to their release versions. Some packages may be downgraded.

                    This process is potentially dangerous and may break your software.

                    Make sure you have some time to fix system if any."""
            ),
            assume_yes,
        )

    logger.info("Temporary setting apt preferences to force install release packages")
    generate_tmp_apt_preferences(state, filename=WB_TEMP_UPGRADE_PREFERENCES_FILENAME)
    atexit.register(_cleanup_tmp_apt_preferences, WB_TEMP_UPGRADE_PREFERENCES_FILENAME)

    logger.info("Updating system")
    run_system_update(assume_yes)

    atexit.unregister(_restore_system_config)

    logger.info("Cleaning up old packages")
    run_apt("autoremove", assume_yes=True)

    logger.info("Restarting wb-rules to show actual release info in MQTT")
    try:
        run_cmd("invoke-rc.d", "wb-rules", "restart")
    except subprocess.CalledProcessError:
        pass

    logger.info("Update done! Please reboot the system")


def update_system(
    target_state: SystemState, old_state: SystemState, second_stage=False, assume_yes=False, log_filename=None
):
    try:
        if second_stage:
            return update_second_stage(target_state, old_state, assume_yes=assume_yes)
        else:
            return update_first_stage(assume_yes=assume_yes, log_filename=log_filename)

    except UserAbortException:
        logger.info("Aborted by user")
        return RETCODE_USER_ABORT
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        return RETCODE_USER_ABORT
    except subprocess.CalledProcessError as e:
        logger.error("\nThe subprocess {} has failed with status {}".format(e.cmd, e.returncode))
        return e.returncode
    except Exception:
        logger.exception("Something went wrong, check output and try again")
        return RETCODE_FAULT
    finally:
        if log_filename:
            logger.info("Update log is saved in {}".format(log_filename))


def print_banner():
    info = read_wb_release_file(WB_RELEASE_FILENAME)

    print("Wirenboard release {release_name} (as {suite}), target {target}".format(**info._asdict()))

    if info.repo_prefix:
        print("This is a DEVELOPMENT release ({}), don't use in production!".format(info.repo_prefix))

    print("\nYou can get this info in scripts from {}.".format(WB_RELEASE_FILENAME))


def run_system_update(assume_yes=False):
    run_apt("update", assume_yes=assume_yes)

    if not assume_yes:
        logger.info("Simulating upgrade")
        run_apt("dist-upgrade", "-s", "-V", assume_yes=False)
        user_confirm(assume_yes=assume_yes)

    logger.info("Performing actual upgrade")
    run_apt("dist-upgrade", assume_yes=True)


def route(args, argv):
    if len(argv[1:]) == 0 or args.version:
        print_banner()
        return RETCODE_OK

    configure_logger(args.log_filename, args.no_journald_log)

    current_state = get_current_state()
    second_stage = args.second_stage

    if args.update_debian_release:
        return upgrade_new_debian_release(
            current_state,
            log_filename=args.log_filename,
            assume_yes=args.yes,
            confirm_steps=args.confirm_steps,
            no_preliminary_update=args.second_stage,
        )

    if args.regenerate:
        return generate_system_config(current_state)

    if args.reset_packages:
        if args.reset_url or args.prefix or args.target_release:
            logger.error("--reset-packages flag can't be used on release change, abort")
            return RETCODE_EINVAL

        # skip preliminary update if we are just resetting packages
        target_state = current_state
        second_stage = True
    else:
        target_state = get_target_state(
            current_state, reset_url=args.reset_url, prefix=args.prefix, target_release=args.target_release
        )

        if target_state == current_state:
            logger.info("Target and current releases are the same, nothing to do")
            return RETCODE_OK

    if not release_exists(target_state):
        logger.error("Target state does not exist: {}".format(target_state))
        return RETCODE_NO_TARGET

    return update_system(
        target_state,
        current_state,
        second_stage=second_stage,
        assume_yes=args.yes,
        log_filename=args.log_filename,
    )


def main(argv=sys.argv):
    parser = argparse.ArgumentParser(
        description="The tool to manage Wirenboard software releases",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """
                                     By default, wb-release shows current release info (like -v flag).
                                     This tool should be used with extra care on production installations."""
        ),
    )

    parser.add_argument(
        "-r", "--regenerate", action="store_true", help="regenerate factory sources.list and exit"
    )
    parser.add_argument(
        "-t",
        "--target-release",
        type=str,
        default=None,
        help="upgrade release to a new target (stable or testing)",
    )
    parser.add_argument("-v", "--version", action="store_true", help="print version info and exit")
    parser.add_argument("-y", "--yes", action="store_true", help='auto "yes" to all questions')
    parser.add_argument(
        "-p", "--reset-packages", action="store_true", help="reset all packages to release versions and exit"
    )
    parser.add_argument("-l", LOG_FILENAME_ARGNAME, type=str, default=None, help="path to output log file")
    parser.add_argument("--no-journald-log", action="store_true", help="disable journald logging")

    url_group = parser.add_mutually_exclusive_group()
    url_group.add_argument(
        "--reset-url", action="store_true", help="reset repository URL to default Wirenboard one"
    )
    url_group.add_argument("--prefix", type=str, default=None, help="override repository URL prefix")

    parser.add_argument(
        NO_PRELIMINARY_UPDATE_ARGNAME,
        dest="second_stage",
        action="store_true",
        help="skip upgrade before switching (not recommended)",
    )

    parser.add_argument(
        UPDATE_DEBIAN_RELEASE_ARGNAME,
        dest="update_debian_release",
        action="store_true",
        help="update Debian release to bullseye",
    )
    parser.add_argument(
        CONFIRM_STEPS_ARGNAME,
        dest="confirm_steps",
        action="store_true",
        help="ask for confirmation on each step (for Debian release update)",
    )

    args = parser.parse_args(argv[1:])

    return route(args, argv)


if __name__ == "__main__":
    sys.exit(main())
