"""
This package contains the library to manage with Wirenboard release data on board
and provides the main for the wb-release tool which switches release branches.
"""
import re
import subprocess
import sys
import os
import logging
import argparse
import atexit
import textwrap
import errno
import shutil
from collections import namedtuple
import urllib.request
from urllib.error import HTTPError
from systemd import journal
from pathlib import Path

ReleaseInfo = namedtuple('ReleaseInfo', 'release_name suite target repo_prefix')
SystemState = namedtuple('SystemState', 'suite target repo_prefix consistent')

WB_ORIGIN = 'wirenboard'
WB_RELEASE_FILENAME = '/usr/lib/wb-release'
WB_SOURCES_LIST_FILENAME = '/etc/apt/sources.list.d/wirenboard.list'
WB_RELEASE_APT_PREFERENCES_FILENAME = '/etc/apt/preferences.d/20wb-release'
WB_TEMP_UPGRADE_PREFERENCES_FILENAME = '/etc/apt/preferences.d/00wb-release-upgrade-temp'
DEFAULT_REPO_URL = 'http://deb.wirenboard.com/'
DEFAULT_LOG_FILENAME = '/var/log/wb-release/update_{datetime}.log'

RETCODE_OK = 0
RETCODE_USER_ABORT = 1
RETCODE_FAULT = 2
RETCODE_NO_TARGET = 3
RETCODE_EINVAL = errno.EINVAL

APT_SOURCE_LIST_PATH = '/etc/apt/sources.list.d/'
APT_SOURCE_LIST_OLD_PATH = '/etc/apt/sources.list.d.old/'
APT_SOURCE_LIST_FILES = ['stretch-backports.list', 'debian-upstream.list']
TEMP_DEBIAN_UPSTREAM_LIST = APT_SOURCE_LIST_PATH + 'debian-upstream-bullseye-temp.list'

logger = logging.getLogger('wb-release')


def configure_logger(log_filename=None, no_journald_log=False):
    logger.setLevel(logging.DEBUG)

    # apt-get reconfigures pty somehow, so CR symbol becomes necessary in stdout,
    # so it is added to logging format string here
    # logging.basicConfig(format='%(asctime)s %(name)s %(levelname)s: %(message)s\r')
    fmt = logging.Formatter(fmt='%(asctime)s %(message)s\r',
                            datefmt='%H:%M:%S')

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(fmt)
    stdout_handler.setLevel(logging.INFO)
    logger.addHandler(stdout_handler)

    if log_filename:
        file_fmt = logging.Formatter('%(asctime)s %(name)s %(levelname)s: %(message)s')
        os.makedirs(os.path.dirname(log_filename), exist_ok=True)
        file_handler = logging.FileHandler(log_filename)
        file_handler.setFormatter(file_fmt)
        file_handler.setLevel(logging.DEBUG)
        logger.addHandler(file_handler)

        logger.info('Update log is written to {}'.format(log_filename))

    if not no_journald_log:
        journald_handler = journal.JournalHandler(SYSLOG_IDENTIFIER='wb-release')
        journald_handler.setLevel(logging.INFO)
        logger.addHandler(journald_handler)

        logger.info('journald logging enabled')


class NoSuiteInfoError(Exception):
    pass


class ImpossibleUpdateError(Exception):
    pass


class UserAbortException(Exception):
    pass


def user_confirm(text=None, assume_yes=False):
    if text:
        print('\n' + text + '\n')

    if assume_yes:
        return

    while True:
        result = input('Are you sure you want to continue? (y/n): ').lower().strip()
        if not result:
            continue
        if result[0] == 'y':
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


def read_apt_sources_list_suite(filename) -> str:
    if os.path.exists(filename):
        with open(filename) as f:
            for line in f:
                line = line.partition('#')[0].rstrip()
                if line.startswith('deb http'):
                    return line.split(' ', maxsplit=4)[2]

    raise NoSuiteInfoError()


def get_current_state(filename=WB_RELEASE_FILENAME, sources_filename=WB_SOURCES_LIST_FILENAME) -> SystemState:
    release_info = read_wb_release_file(filename)

    try:
        sources_list_suite = read_apt_sources_list_suite(sources_filename)
        consistent = (release_info.suite == sources_list_suite)
    except NoSuiteInfoError:
        consistent = False

    return SystemState(release_info.suite, release_info.target, release_info.repo_prefix, consistent)


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

    return SystemState(new_suite, old_state.target, new_prefix, consistent=True)


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


def _cleanup_tmp_apt_preferences(filename=WB_TEMP_UPGRADE_PREFERENCES_FILENAME):
    logger.info('Cleaning up temp apt preferences {}'.format(filename))
    os.remove(filename)


def _cleanup_apt_cached_lists(cache_dir='/var/lib/apt/lists'):
    if os.path.exists(cache_dir):
        for path in Path(cache_dir).resolve().glob('**/*'):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(str(path))


def _restore_system_config(original_state):
    logger.info('Restoring original system state')
    generate_system_config(original_state)

    logger.info('Cleaning apt cache')
    _cleanup_apt_cached_lists()


def generate_system_config(state):
    logger.info('Generating {} for {}'.format(WB_SOURCES_LIST_FILENAME, state))
    generate_sources_list(state, filename=WB_SOURCES_LIST_FILENAME)

    logger.info('Generating {} for {}'.format(WB_RELEASE_APT_PREFERENCES_FILENAME, state))
    generate_release_apt_preferences(state, filename=WB_RELEASE_APT_PREFERENCES_FILENAME)


def update_first_stage(assume_yes=False, log_filename=None):
    user_confirm(textwrap.dedent("""
                 Now the system will be updated using Apt without changing the release.

                 It is required to get latest state possible
                 to make release change process more controllable.

                 Make sure you have all your data backed up.""").strip(), assume_yes)

    logger.info('Performing upgrade on the current release')
    run_system_update(assume_yes)

    logger.info('Starting (possibly updated) update utility as new process')
    args = sys.argv + ['--no-preliminary-update']

    # preserve update log filename from the first stage
    if log_filename:
        args += ['--log-filename', log_filename]

    # close log handlers in this instance to make it free for second one
    for h in logger.handlers:
        h.close()

    res = subprocess.run(args, check=True)
    return res.returncode


def update_second_stage(state: SystemState, old_state: SystemState, assume_yes=False):
    if state != old_state:
        user_confirm(textwrap.dedent("""
                     Now the release will be switched to {}, prefix "{}".

                     During update, the sources and preferences files will be changed,
                     then apt-get dist-upgrade action will start. Some packages may be downgraded.

                     This process is potentially dangerous and may break your software.

                     STOP RIGHT THERE IF THIS IS A PRODUCTION SYSTEM!""").format(
            state.suite, state.repo_prefix).strip(),
                     assume_yes)

        logger.info('Setting target release to {}, prefix "{}"'.format(state.suite, state.repo_prefix))
        generate_system_config(state)
        atexit.register(_restore_system_config, old_state)
    else:
        user_confirm(textwrap.dedent("""
                    Now system packages will be reinstalled to their release versions. Some packages may be downgraded.

                    This process is potentially dangerous and may break your software.

                    Make sure you have some time to fix system if any."""), assume_yes)

    logger.info('Temporary setting apt preferences to force install release packages')
    generate_tmp_apt_preferences(state, filename=WB_TEMP_UPGRADE_PREFERENCES_FILENAME)
    atexit.register(_cleanup_tmp_apt_preferences, WB_TEMP_UPGRADE_PREFERENCES_FILENAME)

    logger.info('Updating system')
    run_system_update(assume_yes)

    atexit.unregister(_restore_system_config)

    logger.info('Cleaning up old packages')
    run_apt('autoremove', assume_yes=True)

    logger.info('Restarting wb-rules to show actual release info in MQTT')
    try:
        run_cmd('invoke-rc.d', 'wb-rules', 'restart')
    except subprocess.CalledProcessError:
        pass

    logger.info('Update done! Please reboot the system')


def release_exists(state: SystemState):
    full_url = make_full_repo_url(state) + '/dists/{}/Release'.format(state.suite)
    logger.info('Accessing {}...'.format(full_url))

    try:
        resp = urllib.request.urlopen(full_url, timeout=10.0)
        logger.info('Response code {}'.format(resp.getcode()))
    except HTTPError as e:
        if e.code >= 400 and e.code < 500:
            logger.info('Response code {}'.format(e.code))
            return False
        else:
            raise
    else:
        return True


def update_system(target_state: SystemState, old_state: SystemState,
                  second_stage=False, assume_yes=False, log_filename=None):
    try:
        if second_stage:
            return update_second_stage(target_state, old_state, assume_yes=assume_yes)
        else:
            return update_first_stage(assume_yes=assume_yes, log_filename=log_filename)

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
    finally:
        if log_filename:
            logger.info('Update log is saved in {}'.format(log_filename))


def print_banner():
    info = read_wb_release_file(WB_RELEASE_FILENAME)

    print('Wirenboard release {release_name} (as {suite}), target {target}'.format(**info._asdict()))

    if info.repo_prefix:
        print('This is a DEVELOPMENT release ({}), don\'t use in production!'.format(info.repo_prefix))

    print('\nYou can get this info in scripts from {}.'.format(WB_RELEASE_FILENAME))


def run_apt(*cmd, assume_yes=False):
    args = ['apt-get', '-q'] + list(cmd)
    env = os.environ.copy()

    env['DEBIAN_FRONTEND'] = 'noninteractive'
    args += ['-o', 'Dpkg::Options::=--force-confdef',
             '-o', 'Dpkg::Options::=--force-confold',
             '--allow-downgrades']

    if assume_yes:
        args += ['--yes']

    try:
        run_cmd(*args, env=env, log_suffix='apt.{}'.format(cmd[0]))
    except subprocess.CalledProcessError as e:
        if e.returncode == 1:
            raise UserAbortException()
        else:
            raise


def run_cmd(*args, env=None, log_suffix=None):
    if not log_suffix:
        log_suffix = args[0]

    logger.debug('Starting cmd: "{}"'.format(' '.join(list(args))))
    logger.debug('Environment: "{}"'.format(env))

    proc_logger = logger.getChild(log_suffix)

    proc = subprocess.Popen(args,
                            env=env,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)

    with proc.stdout:
        for line in iter(proc.stdout.readline, b''):
            proc_logger.info(line.decode().rstrip().rsplit('\r', 1)[-1])

    retcode = proc.wait()
    if retcode != 0:
        raise subprocess.CalledProcessError(retcode, args)


def run_system_update(assume_yes=False):
    run_apt('update', assume_yes=assume_yes)

    if not assume_yes:
        logger.info('Simulating upgrade')
        run_apt('dist-upgrade', '-s', '-V', assume_yes=False)
        user_confirm(assume_yes=assume_yes)

    logger.info('Performing actual upgrade')
    run_apt('dist-upgrade', assume_yes=True)


def prepare_debian_sources_lists():
    if not os.path.exists(APT_SOURCE_LIST_OLD_PATH):
        os.mkdir(APT_SOURCE_LIST_OLD_PATH)

    for file in APT_SOURCE_LIST_FILES:
        if os.path.exists(APT_SOURCE_LIST_PATH + file):
            os.rename(APT_SOURCE_LIST_PATH + file,
                      APT_SOURCE_LIST_OLD_PATH + file)

    with open(TEMP_DEBIAN_UPSTREAM_LIST, "w") as f:
        f.write(textwrap.dedent("""
                    deb http://deb.debian.org/debian bullseye main
                    deb http://deb.debian.org/debian bullseye-updates main
                    deb http://deb.debian.org/debian bullseye-backports main
                    deb http://security.debian.org/debian-security bullseye-security main""").strip())


def clean_debian_sources_lists():
    if os.path.exists(APT_SOURCE_LIST_OLD_PATH):
        shutil.rmtree(os.path.realpath(APT_SOURCE_LIST_OLD_PATH))


def restore_debian_sources_lists():
    for file in APT_SOURCE_LIST_FILES:
        if os.path.exists(APT_SOURCE_LIST_OLD_PATH + file):
            os.rename(APT_SOURCE_LIST_OLD_PATH + file,
                      APT_SOURCE_LIST_PATH + file)
    clean_debian_sources_lists()


def remove_tmp_debian_upstream():
    logger.info('Removing temp debian-upstream sources list file')
    os.remove(TEMP_DEBIAN_UPSTREAM_LIST)


def _restore_watchdog():
    logger.info('Starting watchdog service')
    run_cmd('systemctl', 'start', 'watchdog.service')


def upgrade_new_debian_release(state: SystemState, log_filename, assume_yes=False, confirm_steps=False):
    # these services will be masked (preventing restart during upgrade)
    # and then restarted manually
    SERVICES_TO_RESTART = ('nginx.service', 'mosquitto.service', 'wb-mqtt-mbgate.service')
    MASKED_SERVICES = ('nginx.service', 'mosquitto.service', 'hostapd.service', 'wb-mqtt-mbgate.service')

    print('============ Upgrade debian release to bullseye ============')

    m = re.search('(.+)/.+', state.target)
    controller_version = m.group(1)

    try:
        user_confirm(textwrap.dedent("""
                         Now the system will be updated using Apt without changing the release.

                         It is required to get latest state possible
                         to make release change process more controllable.

                         Make sure you have all your data backed up.""").strip(), assume_yes)

        logger.info('Performing upgrade on the current release')
        run_system_update(assume_yes)
        new_state = state._replace(target=(controller_version + '/bullseye'))
        if not release_exists(new_state):
            logger.error('Target state does not exist: {}'.format(new_state))
            return RETCODE_NO_TARGET

        user_confirm(textwrap.dedent("""
                             Now the release will be switched to {}, prefix "{}", target "{}".

                             During update, the sources and preferences files will be changed,
                             then apt-get dist-upgrade action will start.

                             This process is potentially dangerous and may break your software.

                             To control process on each step, use this command with --confirm-steps flag.

                             STOP RIGHT THERE IF THIS IS A PRODUCTION SYSTEM!""").format(
            new_state.suite, new_state.repo_prefix, new_state.target).strip(),
                     assume_yes)

        logger.info('Setting target release to {}, prefix "{}", target "{}"'.format(new_state.suite,
                                                                                    new_state.repo_prefix,
                                                                                    new_state.target))
        prepare_debian_sources_lists()
        atexit.register(remove_tmp_debian_upstream)
        atexit.register(restore_debian_sources_lists)

        generate_system_config(new_state)
        atexit.register(_restore_system_config, state)

        run_apt('update', assume_yes=True)

        logger.info('Restoring old debian sources list after update (to properly remove old wb-configs)')
        atexit.unregister(restore_debian_sources_lists)
        restore_debian_sources_lists()

        logger.info('Updating openssh-server first to make Wiren Board available during upgrade')
        run_cmd('systemctl', 'mask', 'ssh.service')
        run_apt('install', 'openssh-server', assume_yes=not confirm_steps)
        run_cmd('systemctl', 'unmask', 'ssh.service')
        run_cmd('systemctl', 'restart', 'ssh.service')

        logger.info('Installing python-is-python2 for correct dependency resolving')
        run_apt('install', 'python-is-python2', assume_yes=not confirm_steps)

        logger.info('Masking services to prevent restart during upgrade')
        for service in MASKED_SERVICES:
            run_cmd('systemctl', 'mask', service)

        if confirm_steps:
            logger.info('Simulating upgrade')
            run_apt('dist-upgrade', '-s', '-V', assume_yes=False)
            user_confirm(assume_yes=False)

        logger.info('Performing actual upgrade')
        run_apt('dist-upgrade', assume_yes=True)  # this step is confirmed in simulating above

        logger.info('Performing actual upgrade - second stage (e2fsprogs update)')
        run_apt('dist-upgrade', assume_yes=not confirm_steps)

        logger.info('Purging wb-configs-stretch to remove old sources.list')
        run_apt('purge', 'wb-configs-stretch', assume_yes=not confirm_steps)

        atexit.unregister(_restore_system_config)
        clean_debian_sources_lists()

        logger.info('Enabling services masked during upgrade')
        for service in MASKED_SERVICES:
            run_cmd('systemctl', 'unmask', service)

        logger.info('Restarting updated services')
        for service in SERVICES_TO_RESTART:
            logger.info(' * %s...', service)
            run_cmd('systemctl', 'restart', service)

        logger.info('Mark python-is-python2 as automatically installed to remove it in future')
        run_cmd('apt-mark', 'auto', 'python-is-python2')

        logger.info('Cleaning up old packages')
        run_apt('autoremove', assume_yes=not confirm_steps)

        shutil.copy('/usr/share/wb-update-manager/99-wb-debian-release-updated', '/etc/update-motd.d/')
        open('/run/wb-debian-release-updated', 'w').close()

        logger.info('Done! Please reboot system')

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

    finally:
        if log_filename:
            logger.info('Update log is saved in {}'.format(log_filename))

    return RETCODE_OK


def route(args, argv):
    if len(argv[1:]) == 0 or args.version:
        print_banner()
        return RETCODE_OK

    configure_logger(args.log_filename, args.no_journald_log)

    current_state = get_current_state()
    second_stage = args.second_stage

    if args.update_debian_release:
        return upgrade_new_debian_release(current_state, log_filename=args.log_filename,
                                          assume_yes=args.yes, confirm_steps=args.confirm_steps)

    if args.regenerate:
        return generate_system_config(current_state)

    if args.reset_packages:
        if args.reset_url or args.prefix or args.target_release:
            logger.error('--reset-packages flag can\'t be used on release change, abort')
            return RETCODE_EINVAL

        # skip preliminary update if we are just resetting packages
        target_state = current_state
        second_stage = True
    else:
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

    return update_system(target_state, current_state,
                         second_stage=second_stage, assume_yes=args.yes, log_filename=args.log_filename)


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
    parser.add_argument('-p', '--reset-packages', action='store_true',
                        help='reset all packages to release versions and exit')
    parser.add_argument('-l', '--log-filename', type=str, default=None, help='path to output log file')
    parser.add_argument('--no-journald-log', action='store_true', help='disable journald logging')

    url_group = parser.add_mutually_exclusive_group()
    url_group.add_argument('--reset-url', action='store_true', help='reset repository URL to default Wirenboard one')
    url_group.add_argument('--prefix', type=str, default=None, help='override repository URL prefix')

    parser.add_argument('--no-preliminary-update', dest='second_stage', action='store_true',
                        help='skip upgrade before switching (not recommended)')

    parser.add_argument('--update-debian-release', action='store_true', help='update Debian release to bullseye')
    parser.add_argument('--confirm-steps', action='store_true', help='ask for confirmation on each step (for Debian release update)')

    args = parser.parse_args(argv[1:])

    return route(args, argv)


if __name__ == '__main__':
    sys.exit(main())
