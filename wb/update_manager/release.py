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
from urllib.parse import urljoin

ReleaseInfo = namedtuple('ReleaseInfo', 'release_name suite target repo_prefix')
RepoInfo = namedtuple('RepoInfo', 'url suite')

WB_RELEASE_FILENAME = '/usr/lib/wb-release'
WB_SOURCES_LIST_FILENAME = '/etc/apt/sources.list.d/wirenboard.list'
WB_TEMP_UPGRADE_PREFERENCES_FILENAME = '/etc/apt/preferences.d/00-wb-release-upgrade-temp'
DEFAULT_REPO_URL = 'http://deb.wirenboard.com/'

logging.basicConfig(format='%(asctime)s %(name)s %(levelname)s: %(message)s')
logger = logging.getLogger('updater')
logger.setLevel(logging.INFO)

class NoSuiteInfoError(Exception):
    pass

class ImpossibleUpdateError(Exception):
    pass

class UserAbortException(Exception):
    pass

def user_confirm(text, force=False):
    print('\n' + text + '\n')

    if not force:
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


def get_wb_release_info():
    if not hasattr(get_wb_release_info, '_cached'):
        get_wb_release_info._cached = read_wb_release_file(WB_RELEASE_FILENAME)
    return get_wb_release_info._cached


def get_wb_repo_info(filename=WB_SOURCES_LIST_FILENAME):
    target = get_wb_release_info().target

    with open(filename) as f:
        for line in f:
            line = line.strip()
            if line[0] != '#':
                # format: 'deb http://example.com/path/to/repo suite main'
                line = line.split(' ')
                full_repo_url = line[1]
                suite = line[2]

                if not full_repo_url.endswith(target):
                    logger.warning('No current target suffix in repository URL, skipping entry')
                    continue

                url = full_repo_url[:-len(target)]
                return RepoInfo(url, suite)

    raise NoSuiteInfoError(filename)


def generate_sources_list(suite, url=DEFAULT_REPO_URL, filename=WB_SOURCES_LIST_FILENAME):
    if url[-1] != '/':
        url += '/'

    full_repo_url = url + get_wb_release_info().target

    with open(filename, 'w') as f:
        f.write("""# This file is automatically generated by wb-release-updater.
# DO NOT EDIT THIS FILE!
#
# If you want to switch to testing, use command
#   wb-release-updater -t testing
deb {full_repo_url} {suite} main""".format(full_repo_url=full_repo_url, suite=suite))


def add_tmp_apt_preferences(target, filename=WB_TEMP_UPGRADE_PREFERENCES_FILENAME):
    with open(filename, 'w') as f:
        f.write("""Package: *
Pin: release o=wirenboard, a={target}
Pin-Priority: 1010""".format(target=target))


def cleanup_tmp_apt_preferences(filename=WB_TEMP_UPGRADE_PREFERENCES_FILENAME):
    logger.info('Cleaning up temp apt preferences {}'.format(filename))
    os.remove(filename)

def regenerate_sources_list(target=None, url=None):
    release_info = get_wb_release_info()

    if not target:
        target = release_info.suite
    if not url:
        url = urljoin(DEFAULT_REPO_URL, release_info.repo_prefix)

    logger.info('Generating {} for suite {}'.format(WB_SOURCES_LIST_FILENAME, target))
    generate_sources_list(target, url=url)


def update_first_stage(target, force):
    user_confirm("""Now the system will be updated using Apt.

It is required to get latest state possible
to make release change process more controllable.

Make sure you have all your data backed up.""", force)

    logger.info('Performing upgrade on the current release')
    _system_update(force)

    logger.info('Starting (possibly updated) update utility as new process')
    args = ['python3', '-m', __name__, '-t', target, '--no-preliminary-update']
    if force:
        args += ['-f']
    _run_cmd(*args)


def update_second_stage(target, force, url):
    repo_info = get_wb_repo_info()
    current = repo_info.suite

    logger.debug('Current suite is {}'.format(current))
    logger.debug('Target suite is {}'.format(target))

    if current == target and url == repo_info.url:
        logger.info('Nothing to upgrade, already on {} and repos are the same'.format(current))
        return

    user_confirm("""WARNING! This script doesn't check if the update is valid yet.

It currently allows you to try any target release and URL.
Make sure you know what you're doing""")

    try:
        logger.info('Changing target release to {} from {}'.format(target, url))
        generate_sources_list(target, url)

        logger.info('Temporary setting apt preferences to force install release packages')
        add_tmp_apt_preferences(target, filename=WB_TEMP_UPGRADE_PREFERENCES_FILENAME)
        atexit.register(cleanup_tmp_apt_preferences, WB_TEMP_UPGRADE_PREFERENCES_FILENAME)

        logger.info('Updating system')
        _system_update(force)

        logger.info('Cleaning up old packages')
        _run_cmd('apt-get', 'autoremove')

        logger.info('Restarting wb-rules to show actual release info in MQTT')
        try:
            _run_cmd('invoke-rc.d', 'wb-rules', 'restart')
        except subprocess.CalledProcessError:
            pass

        logger.info('Update done! Please reboot the system')
        return
    except Exception:
        logger.exception('Update failed')
        logger.info('Run \'wb-release -r -t {} --url {}\' to restore lists, or use FIT image'.
                    format(current, repo_info.url))
        return 1

def update_system(target=None, second_stage=False, force=False, url=None, reset_url=False, **_):
    repo_info = get_wb_repo_info()

    if reset_url:
        url = DEFAULT_REPO_URL
    elif not url:
        url = repo_info.url

    if not target:
        target = repo_info.suite

    try:
        if second_stage:
            return update_second_stage(target, force, url)
        else:
            return update_first_stage(target, force)

    except UserAbortException:
        logger.info('Aborted by user')
        return 2
    except KeyboardInterrupt:
        logger.info('Interrupted by user')
        return 2
    except Exception:
        logger.exception('Something went wrong, check logs and try again')
        return 1

def print_banner():
    info = get_wb_release_info()

    print("Wirenboard release {release_name} (as {suite}), target {target}".format(**info._asdict()))

    if info.repo_prefix:
        print("This is a DEVELOPMENT release ({}), don't use in production!".format(info.repo_prefix))

    print("\nYou can get this info in scripts from {}.".format(WB_RELEASE_FILENAME))


def _run_cmd(*args):
    subprocess.run(args, check=True)


def _system_update(force=False):
    _run_cmd('apt-get', 'update')

    upgrade_cmd = ['apt-get', 'dist-upgrade']
    if force:
        upgrade_cmd += ['-y']

    _run_cmd(*upgrade_cmd)

def main(argv=sys.argv):
    parser = argparse.ArgumentParser(description='The tool to manage Wirenboard software releases',
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog=textwrap.dedent('''
                                     By default, wb-release shows current release info (like -v flag).
                                     This tool should be used with extra care on production installations.'''))

    parser.add_argument('-f', '--force', action='store_true', help='do not ask anything')
    parser.add_argument('-r', '--regenerate', action='store_true', help='regenerate factory sources.list and exit')
    parser.add_argument('-t', '--target', type=str, default=None, help='upgrade release to a new target')
    parser.add_argument('-v', '--version', action='store_true', help='print version info and exit')

    parser.add_argument('--reset-url', action='store_true', help='reset repository URL to default Wirenboard one')
    parser.add_argument('--url', type=str, default=None, help='override repository URL')
    parser.add_argument('--no-preliminary-update', action='store_true',
                        help='skip upgrade before switching (not recommended)')

    args = parser.parse_args(argv[1:])

    if args.regenerate:
        return regenerate_sources_list(args.target, DEFAULT_REPO_URL if args.reset_url else args.url)
    elif args.target:
        return update_system(**vars(args))
    else:
        return print_banner()

if __name__ == '__main__':
    sys.exit(main())
