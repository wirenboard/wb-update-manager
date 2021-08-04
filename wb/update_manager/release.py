"""
This package contains the library to manage with Wirenboard release data on board
and provides the main for the wb-release tool which switches release branches.
"""

import subprocess
import sys
import os
import logging
import argparse
import atexit
import textwrap
from collections import namedtuple
from urllib.request import urlopen
from urllib.error import HTTPError

ReleaseInfo = namedtuple('ReleaseInfo', 'release_name suite target repo_prefix')
SystemState = namedtuple('SystemState', 'suite target repo_prefix')

WB_ORIGIN = 'wirenboard'
WB_RELEASE_FILENAME = '/usr/lib/wb-release'
WB_SOURCES_LIST_FILENAME = '/etc/apt/sources.list.d/wirenboard.list'
WB_RELEASE_APT_PREFERENCES_FILENAME = '/etc/apt/preferences.d/20wb-release'
WB_TEMP_UPGRADE_PREFERENCES_FILENAME = '/etc/apt/preferences.d/00wb-release-upgrade-temp'
DEFAULT_REPO_URL = 'http://deb.wirenboard.com/'

RETCODE_OK = 0
RETCODE_USER_ABORT = 1
RETCODE_FAULT = 2
RETCODE_NO_TARGET = 3

logging.basicConfig(format='%(asctime)s %(name)s %(levelname)s: %(message)s')
logger = logging.getLogger('wb-release')
logger.setLevel(logging.INFO)


class NoSuiteInfoError(Exception):
    pass


class ImpossibleUpdateError(Exception):
    pass


class UserAbortException(Exception):
    pass


def user_confirm(text, assume_yes=False):
    print('\n' + text + '\n')

    if assume_yes:
        return

    while True:
        result = input('Are you sure you want to continue? (y/n): ').lower()
        if not result:
            continue
        if result == 'y':
            return
        else:
            raise UserAbortException


def read_wb_release_file(filename):
    d = {}
    with open(filename) as f:
        for line in f:
            line = line.strip()
            if line[0] != '#':
                key, value = line.split('=', maxsplit=1)
                d[key.lower()] = value.strip('"').strip('\'')

    return ReleaseInfo(**d)


def get_current_state(filename=WB_RELEASE_FILENAME) -> SystemState:
    release_info = read_wb_release_file(filename)
    return SystemState(release_info.suite, release_info.target, release_info.repo_prefix)


def get_target_state(old_state: SystemState, reset_url=False, prefix=None, target_release=None) -> SystemState:
    if reset_url and prefix:
        raise ImpossibleUpdateError('both --prefix and --reset-url are set')

    if reset_url:
        new_prefix = ''
    elif prefix:
        new_prefix = prefix
    else:
        new_prefix = old_state.repo_prefix

    if target_release:
        new_suite = target_release
    else:
        new_suite = old_state.suite

    new_prefix = new_prefix.strip(' /')

    return SystemState(new_suite, old_state.target, new_prefix)


def make_full_repo_url(state: SystemState, base_url=DEFAULT_REPO_URL):
    base_url = base_url.strip(' /')
    prefix = ('/' + state.repo_prefix).rstrip(' /')
    return base_url + prefix + '/' + state.target


def generate_sources_list(state: SystemState, base_url=DEFAULT_REPO_URL, filename=WB_SOURCES_LIST_FILENAME):
    suite = state.suite
    full_repo_url = make_full_repo_url(state, base_url)

    with open(filename, 'w') as f:
        f.write(textwrap.dedent("""
                # This file is automatically generated by wb-release.
                # DO NOT EDIT THIS FILE!
                #
                # If you want to switch to testing, use command
                #   wb-release -t testing
                deb {full_repo_url} {suite} main""").format(full_repo_url=full_repo_url, suite=suite).strip())


def generate_release_apt_preferences(state: SystemState, origin=WB_ORIGIN,
                                     filename=WB_RELEASE_APT_PREFERENCES_FILENAME):
    with open(filename, 'w') as f:
        f.write(textwrap.dedent("""
                # This file is automatically generated by wb-release.
                # DO NOT EDIT THIS FILE!
                #
                # If you want to switch to testing, use command
                #   wb-release -t testing
                Package: *
                Pin: release o={origin}, a={suite}
                Pin-Priority: 990""").format(origin=origin, suite=state.suite).strip())


def generate_tmp_apt_preferences(target_state: SystemState, origin=WB_ORIGIN,
                                 filename=WB_TEMP_UPGRADE_PREFERENCES_FILENAME):
    with open(filename, 'w') as f:
        f.write(textwrap.dedent("""
                # This file is automatically generated by wb-release.
                # DO NOT EDIT THIS FILE!
                Package: *
                Pin: release o={origin}, a={suite}
                Pin-Priority: 1010

                Package: *
                Pin: release o=wirenboard
                Pin-Priority: -10""").format(origin=origin, suite=target_state.suite).strip())


def cleanup_tmp_apt_preferences(filename=WB_TEMP_UPGRADE_PREFERENCES_FILENAME):
    logger.info('Cleaning up temp apt preferences {}'.format(filename))
    os.remove(filename)


def restore_system_config(original_state):
    logger.info('Restoring original system state')
    generate_system_config(original_state)


def generate_system_config(state):
    logger.info('Generating {} for {}'.format(WB_SOURCES_LIST_FILENAME, state))
    generate_sources_list(state, filename=WB_SOURCES_LIST_FILENAME)

    logger.info('Generating {} for {}'.format(WB_RELEASE_APT_PREFERENCES_FILENAME, state))
    generate_release_apt_preferences(state, filename=WB_RELEASE_APT_PREFERENCES_FILENAME)


def update_first_stage(assume_yes=False):
    user_confirm(textwrap.dedent("""
                 Now the system will be updated using Apt without changing the release.

                 It is required to get latest state possible
                 to make release change process more controllable.

                 Make sure you have all your data backed up.""").strip(), assume_yes)

    logger.info('Performing upgrade on the current release')
    _system_update(assume_yes)

    logger.info('Starting (possibly updated) update utility as new process')
    args = sys.argv + ['--no-preliminary-update']

    if assume_yes:
        args += ['--yes']

    _run_cmd(*args)


def update_second_stage(state: SystemState, old_state: SystemState, assume_yes=False):
    user_confirm(textwrap.dedent("""
                 Now the release will be switched to {}, prefix "{}".

                 During update, the sources and preferences files will be changed,
                 then apt-get dist-upgrade action will start. Some packages may be downgraded.

                 This process is potentially dangerous and may break your software.

                 STOP RIGHT THERE IF THIS IS A PRODUCTION SYSTEM!""").format(state.suite, state.repo_prefix).strip(),
                 assume_yes)

    logger.info('Setting target release to {}, prefix "{}"'.format(state.suite, state.repo_prefix))
    generate_system_config(state)
    atexit.register(restore_system_config, old_state)

    logger.info('Temporary setting apt preferences to force install release packages')
    generate_tmp_apt_preferences(state, filename=WB_TEMP_UPGRADE_PREFERENCES_FILENAME)
    atexit.register(cleanup_tmp_apt_preferences, WB_TEMP_UPGRADE_PREFERENCES_FILENAME)

    logger.info('Updating system')
    _system_update(assume_yes)

    logger.info('Cleaning up old packages')
    _run_apt('autoremove', assume_yes)

    atexit.unregister(restore_system_config)

    logger.info('Restarting wb-rules to show actual release info in MQTT')
    try:
        _run_cmd('invoke-rc.d', 'wb-rules', 'restart')
    except subprocess.CalledProcessError:
        pass

    logger.info('Update done! Please reboot the system')


def release_exists(state: SystemState):
    full_url = make_full_repo_url(state) + '/dists/{}/Release'.format(state.suite)
    logger.info('Accessing {}...'.format(full_url))

    try:
        resp = urlopen(full_url, timeout=10.0)
        logger.info('Response code {}'.format(resp.getcode()))
    except HTTPError as e:
        if e.code >= 400 and e.code < 500:
            logger.info('Response code {}'.format(e.code))
            return False
        else:
            raise
    else:
        return True


def update_system(target_state: SystemState, old_state: SystemState, second_stage=False, assume_yes=False):
    try:
        if second_stage:
            return update_second_stage(target_state, old_state, assume_yes)
        else:
            return update_first_stage(assume_yes)

    except UserAbortException:
        logger.info('Aborted by user')
        return RETCODE_USER_ABORT
    except KeyboardInterrupt:
        logger.info('Interrupted by user')
        return RETCODE_USER_ABORT
    except subprocess.CalledProcessError as e:
        logger.error('\nThe subprocess {} has failed with status {}'.format(e.cmd, e.returncode))
        return e.returncode
    except Exception:
        logger.exception('Something went wrong, check output and try again')
        return RETCODE_FAULT


def print_banner():
    info = read_wb_release_file(WB_RELEASE_FILENAME)

    print('Wirenboard release {release_name} (as {suite}), target {target}'.format(**info._asdict()))

    if info.repo_prefix:
        print('This is a DEVELOPMENT release ({}), don\'t use in production!'.format(info.repo_prefix))

    print('\nYou can get this info in scripts from {}.'.format(WB_RELEASE_FILENAME))


def _run_apt(cmd, assume_yes=False):
    args = ['apt-get', cmd]
    env = os.environ.copy()

    if assume_yes:
        args += ['--yes', '--allow-downgrades', '-o', 'Dpkg::Options::=--force-confdef',
                 '-o', 'Dpkg::Options::=--force-confold']
        env['DEBIAN_FRONTEND'] = 'noninteractive'

    try:
        _run_cmd(*args, env=env)
    except subprocess.CalledProcessError as e:
        if e.returncode == 1:
            raise UserAbortException()
        else:
            raise


def _run_cmd(*args, env=None):
    subprocess.run(args, env=env, check=True)


def _system_update(assume_yes=False):
    _run_apt('update', assume_yes)
    _run_apt('dist-upgrade', assume_yes)


def route(args, argv):
    if len(argv[1:]) == 0 or args.version:
        print_banner()
        return RETCODE_OK

    current_state = get_current_state()

    if args.regenerate:
        return generate_system_config(current_state)

    target_state = get_target_state(current_state,
                                    reset_url=args.reset_url,
                                    prefix=args.prefix,
                                    target_release=args.target_release)

    if target_state == current_state:
        logger.info('Target and current releases are the same, nothing to do')
        return RETCODE_OK

    if not release_exists(target_state):
        logger.error('Target state does not exist: {}'.format(target_state))
        return RETCODE_NO_TARGET

    return update_system(target_state, current_state, second_stage=args.second_stage, assume_yes=args.yes)


def main(argv=sys.argv):
    parser = argparse.ArgumentParser(description='The tool to manage Wirenboard software releases',
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog=textwrap.dedent('''
                                     By default, wb-release shows current release info (like -v flag).
                                     This tool should be used with extra care on production installations.'''))

    parser.add_argument('-r', '--regenerate', action='store_true', help='regenerate factory sources.list and exit')
    parser.add_argument('-t', '--target-release', type=str, default=None,
                        help='upgrade release to a new target (stable or testing)')
    parser.add_argument('-v', '--version', action='store_true', help='print version info and exit')
    parser.add_argument('-y', '--yes', action='store_true', help='auto "yes" to all questions')

    url_group = parser.add_mutually_exclusive_group()
    url_group.add_argument('--reset-url', action='store_true', help='reset repository URL to default Wirenboard one')
    url_group.add_argument('--prefix', type=str, default=None, help='override repository URL prefix')

    parser.add_argument('--no-preliminary-update', dest='second_stage', action='store_true',
                        help='skip upgrade before switching (not recommended)')

    args = parser.parse_args(argv[1:])

    return route(args, argv)


if __name__ == '__main__':
    sys.exit(main())
