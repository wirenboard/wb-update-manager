"""
This package contains the library to manage with Wirenboard release data on board
and provides the main for the wb-release tool which switches release branches.
"""

import argparse
import atexit
import errno
import logging
import os
import shutil
import subprocess
import sys
import textwrap
import urllib.request
from collections import namedtuple
from urllib.error import HTTPError

from systemd import journal

ReleaseInfo = namedtuple("ReleaseInfo", "release_name suite target repo_prefix")
SystemState = namedtuple("SystemState", "suite target repo_prefix consistent")

WB_ORIGIN = "wirenboard"
WB_RELEASE_FILENAME = "/usr/lib/wb-release"
WB_SOURCES_LIST_FILENAME = "/etc/apt/sources.list.d/wirenboard.list"
WB_RELEASE_APT_PREFERENCES_FILENAME = "/etc/apt/preferences.d/20wb-release"
WB_TEMP_UPGRADE_PREFERENCES_FILENAME = "/etc/apt/preferences.d/00wb-release-upgrade-temp"
DEFAULT_REPO_URL = "http://deb.wirenboard.com/"
DEFAULT_LOG_FILENAME = "/var/log/wb-release/update_{datetime}.log"

RETCODE_OK = 0
RETCODE_USER_ABORT = 1
RETCODE_FAULT = 2
RETCODE_NO_TARGET = 3
RETCODE_EINVAL = errno.EINVAL

logger = logging.getLogger("wb-release")


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

        logger.info("Update log is written to %s", log_filename)

    if not no_journald_log:
        journald_handler = journal.JournalHandler(SYSLOG_IDENTIFIER="wb-release")
        journald_handler.setLevel(logging.INFO)
        logger.addHandler(journald_handler)

        logger.info("journald logging enabled")


class NoSuiteInfoError(Exception):
    pass


class ImpossibleUpdateError(Exception):
    pass


class UserAbortException(Exception):
    pass


def user_confirm(text=None, assume_yes=False):
    if text:
        print("\n" + text + "\n")

    if assume_yes:
        return

    while True:
        result = input("Are you sure you want to continue? (y/n): ").lower().strip()
        if not result:
            continue
        if result[0] == "y":
            return

        raise UserAbortException


def read_wb_release_file(filename):
    ret = {}
    with open(filename, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line[0] != "#":
                key, value = line.split("=", maxsplit=1)
                ret[key.lower()] = value.strip('"').strip("'")

    return ReleaseInfo(**ret)


def read_apt_sources_list_suite(filename) -> str:
    if os.path.exists(filename):
        with open(filename, encoding="utf-8") as f:
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


def make_full_repo_url(state: SystemState, base_url=DEFAULT_REPO_URL):
    base_url = base_url.strip(" /")
    prefix = ("/" + state.repo_prefix).rstrip(" /")
    return base_url + prefix + "/" + state.target


def generate_sources_list(state: SystemState, base_url=DEFAULT_REPO_URL, filename=WB_SOURCES_LIST_FILENAME):
    suite = state.suite
    full_repo_url = make_full_repo_url(state, base_url)

    with open(filename, "w", encoding="utf-8") as f:
        content = (
            textwrap.dedent(
                """
            # This file is automatically generated by wb-release.
            # DO NOT EDIT THIS FILE!
            #
            # If you want to switch to testing, use command
            #   wb-release -t testing
            deb {full_repo_url} {suite} main"""
            )
            .format(full_repo_url=full_repo_url, suite=suite)
            .strip()
        )
        print(content, file=f)


def generate_release_apt_preferences(
    state: SystemState, origin=WB_ORIGIN, filename=WB_RELEASE_APT_PREFERENCES_FILENAME
):
    with open(filename, "w", encoding="utf-8") as f:
        f.write(
            textwrap.dedent(
                """
                # This file is automatically generated by wb-release.
                # DO NOT EDIT THIS FILE!
                #
                # If you want to switch to testing, use command
                #   wb-release -t testing
                Package: *
                Pin: release o={origin}, a={suite}
                Pin-Priority: 990"""
            )
            .format(origin=origin, suite=state.suite)
            .strip()
        )


def generate_tmp_apt_preferences(
    target_state: SystemState, origin=WB_ORIGIN, filename=WB_TEMP_UPGRADE_PREFERENCES_FILENAME
):
    with open(filename, "w", encoding="utf-8") as f:
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
    logger.info("Cleaning up temp apt preferences %s", filename)
    os.remove(filename)


def _cleanup_apt_cached_lists(cache_dir="/var/lib/apt/lists"):
    if os.path.exists(cache_dir):
        shutil.rmtree(cache_dir)


def _restore_system_config(original_state):
    logger.info("Restoring original system state")
    generate_system_config(original_state)

    logger.info("Cleaning apt cache")
    _cleanup_apt_cached_lists()


def generate_system_config(state):
    logger.info("Generating %s for %s", WB_SOURCES_LIST_FILENAME, state)
    generate_sources_list(state, filename=WB_SOURCES_LIST_FILENAME)

    logger.info("Generating %s for %s", WB_RELEASE_APT_PREFERENCES_FILENAME, state)
    generate_release_apt_preferences(state, filename=WB_RELEASE_APT_PREFERENCES_FILENAME)


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

    logger.info("Performing upgrade on the current release")
    run_system_update(assume_yes)

    logger.info("Starting (possibly updated) update utility as new process")
    args = sys.argv + ["--no-preliminary-update"]

    # preserve update log filename from the first stage
    if log_filename:
        args += ["--log-filename", log_filename]

    # close log handlers in this instance to make it free for second one
    for handler in logger.handlers:
        handler.close()

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

        logger.info('Setting target release to %s, prefix "%s"', state.suite, state.repo_prefix)
        generate_system_config(state)
        atexit.register(_restore_system_config, old_state)
    else:
        user_confirm(
            textwrap.dedent(
                """
                Now system packages will be reinstalled to their release versions.
                Some packages may be downgraded.

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


def release_exists(state: SystemState):
    full_url = make_full_repo_url(state) + "/dists/{}/Release".format(state.suite)
    logger.info("Accessing %s...", full_url)

    try:
        with urllib.request.urlopen(full_url, timeout=10.0) as resp:
            logger.info("Response code %d", resp.getcode())
    except HTTPError as e:
        if e.code >= 400 and e.code < 500:
            logger.info("Response code %d", e.code)
            return False
        raise
    else:
        return True


def update_system(
    target_state: SystemState, old_state: SystemState, second_stage=False, assume_yes=False, log_filename=None
):
    try:
        if second_stage:
            return update_second_stage(target_state, old_state, assume_yes=assume_yes)
        return update_first_stage(assume_yes=assume_yes, log_filename=log_filename)

    except UserAbortException:
        logger.info("Aborted by user")
        return RETCODE_USER_ABORT
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        return RETCODE_USER_ABORT
    except subprocess.CalledProcessError as e:
        logger.error("\nThe subprocess %s has failed with status %d", e.cmd, e.returncode)
        return e.returncode
    except Exception:
        logger.exception("Something went wrong, check output and try again")
        return RETCODE_FAULT
    finally:
        if log_filename:
            logger.info("Update log is saved in %s", log_filename)


def print_banner():
    info = read_wb_release_file(WB_RELEASE_FILENAME)

    print("Wirenboard release {release_name} (as {suite}), target {target}".format(**info._asdict()))

    if info.repo_prefix:
        print("This is a DEVELOPMENT release ({}), don't use in production!".format(info.repo_prefix))

    print("\nYou can get this info in scripts from {}.".format(WB_RELEASE_FILENAME))


def run_apt(*cmd, assume_yes=False):
    args = ["apt-get", "-q"] + list(cmd)
    env = os.environ.copy()

    env["DEBIAN_FRONTEND"] = "noninteractive"
    args += [
        "-o",
        "Dpkg::Options::=--force-confdef",
        "-o",
        "Dpkg::Options::=--force-confold",
        "--allow-downgrades",
    ]

    if assume_yes:
        args += ["--yes"]

    try:
        run_cmd(*args, env=env, log_suffix="apt.{}".format(cmd[0]))
    except subprocess.CalledProcessError as e:
        if e.returncode == 1:
            raise UserAbortException() from e
        raise


def run_cmd(*args, env=None, log_suffix=None):
    if not log_suffix:
        log_suffix = args[0]

    logger.debug('Starting cmd: "%s"', " ".join(list(args)))
    logger.debug('Environment: "%s"', env)

    proc_logger = logger.getChild(log_suffix)

    with subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as proc:
        with proc.stdout:
            for line in iter(proc.stdout.readline, b""):
                proc_logger.info(line.decode().rstrip().rsplit("\r", 1)[-1])

        retcode = proc.wait()
        if retcode != 0:
            raise subprocess.CalledProcessError(retcode, args)


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
        logger.error("Target state does not exist: %s", target_state)
        return RETCODE_NO_TARGET

    return update_system(
        target_state,
        current_state,
        second_stage=second_stage,
        assume_yes=args.yes,
        log_filename=args.log_filename,
    )


def main(argv=None):
    if argv is None:
        argv = sys.argv

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
        "-r",
        "--regenerate",
        action="store_true",
        help="regenerate factory sources.list and exit",
    )
    parser.add_argument(
        "-t",
        "--target-release",
        type=str,
        default=None,
        help="upgrade release to a new target (stable or testing)",
    )
    parser.add_argument(
        "-v",
        "--version",
        action="store_true",
        help="print version info and exit",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help='auto "yes" to all questions',
    )
    parser.add_argument(
        "-p",
        "--reset-packages",
        action="store_true",
        help="reset all packages to release versions and exit",
    )
    parser.add_argument(
        "-l",
        "--log-filename",
        type=str,
        default=None,
        help="path to output log file",
    )
    parser.add_argument(
        "--no-journald-log",
        action="store_true",
        help="disable journald logging",
    )

    url_group = parser.add_mutually_exclusive_group()
    url_group.add_argument(
        "--reset-url",
        action="store_true",
        help="reset repository URL to default Wirenboard one",
    )
    url_group.add_argument(
        "--prefix",
        type=str,
        default=None,
        help="override repository URL prefix",
    )

    parser.add_argument(
        "--no-preliminary-update",
        dest="second_stage",
        action="store_true",
        help="skip upgrade before switching (not recommended)",
    )

    args = parser.parse_args(argv[1:])

    return route(args, argv)


if __name__ == "__main__":
    sys.exit(main())
