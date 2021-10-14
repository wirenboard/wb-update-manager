import pytest
import io
import os
import tempfile
import argparse
import urllib.request
from urllib.error import HTTPError
from subprocess import CalledProcessError
from types import SimpleNamespace
from wb.update_manager import release


DATA_PATH = os.path.join(os.path.dirname(__file__), 'data')


def read_file_ignore_comments(filename):
    ret = ''
    with open(filename) as f:
        for line in f:
            ret += line.split('#', 1)[0].strip() + '\n'

    return ret.strip()


class TestUserConfirm:
    @pytest.mark.parametrize('str_input', ['y', 'yes', 'Y', '\n  y'])
    def test_yes(self, str_input, monkeypatch):
        monkeypatch.setattr('sys.stdin', io.StringIO(str_input))
        try:
            release.user_confirm('Hello World')
        except release.UserAbortException:
            pytest.fail('unexpected UserAbortException')

    def test_default_yes(self):
        try:
            release.user_confirm('Hello World', assume_yes=True)
        except release.UserAbortException:
            pytest.fail('unexpected UserAbortException')

    @pytest.mark.parametrize('str_input', ['n', 'no', 'wtf', '123'])
    def test_no(self, str_input, monkeypatch):
        monkeypatch.setattr('sys.stdin', io.StringIO(str_input))

        with pytest.raises(release.UserAbortException):
            release.user_confirm('Hello World')


class TestWbReleaseReader:
    @pytest.mark.parametrize('filename,result', [
        ('wb-release.1.txt', release.ReleaseInfo('staging.01830', 'testing', 'wb6/stretch', '')),
        ('wb-release.2.txt', release.ReleaseInfo('wb-2108', 'stable', 'wb5/stretch', 'git/my/path'))
    ])
    def test_read(self, filename, result):
        assert release.read_wb_release_file(os.path.join(DATA_PATH, filename)) == result

    @pytest.mark.parametrize('filename', ['wb-release.err.1.txt', 'wb-release.err.2.txt'])
    def test_error(self, filename):
        with pytest.raises(Exception):
            release.read_wb_release_file(os.path.join(DATA_PATH, filename))


class TestSystemStateReader:
    @pytest.mark.parametrize('filename,result', [
        ('wb-release.1.txt', release.SystemState('testing', 'wb6/stretch', '')),
        ('wb-release.2.txt', release.SystemState('stable', 'wb5/stretch', 'git/my/path'))
    ])
    def test_read(self, filename, result):
        assert release.get_current_state(os.path.join(DATA_PATH, filename)) == result

    @pytest.mark.parametrize('filename', ['wb-release.err.1.txt', 'wb-release.err.2.txt'])
    def test_error(self, filename):
        with pytest.raises(Exception):
            release.get_current_state(os.path.join(DATA_PATH, filename))


class TestTargetStateGenerator:
    def test_impossible_update(self):
        with pytest.raises(release.ImpossibleUpdateError):
            release.get_target_state(release.SystemState('some', 'thing', 'here'), reset_url=True, prefix='new/prefix')

    def test_change_prefix(self):
        old_state = release.SystemState('testing', 'wb6/stretch', '')
        new_state = release.SystemState('testing', 'wb6/stretch', 'new/prefix')

        assert new_state == release.get_target_state(old_state, prefix='new/prefix')

    def test_reset_prefix(self):
        old_state = release.SystemState('testing', 'wb6/stretch', 'old/prefix')
        new_state = release.SystemState('testing', 'wb6/stretch', '')

        assert new_state == release.get_target_state(old_state, reset_url=True)

    def test_change_release(self):
        old_state = release.SystemState('testing', 'wb6/stretch', '')
        new_state = release.SystemState('stable', 'wb6/stretch', '')

        assert new_state == release.get_target_state(old_state, target_release='stable')

    def test_change_release_keep_prefix(self):
        old_state = release.SystemState('testing', 'wb6/stretch', 'my/prefix')
        new_state = release.SystemState('stable', 'wb6/stretch', 'my/prefix')

        assert new_state == release.get_target_state(old_state, target_release='stable')


class TestReleaseExistsChecker:
    def patch(self, mocker, side_effect=None):
        self.state = release.SystemState('testing', 'wb6/stretch', 'my/prefix')
        self.url = "http://deb.wirenboard.com/my/prefix/wb6/stretch/dists/testing/Release"

        ret = SimpleNamespace(getcode=lambda: 200)
        mocker.patch.object(urllib.request, 'urlopen', side_effect=side_effect, return_value=ret)

    def test_exist(self, mocker):
        self.patch(mocker)

        assert release.release_exists(self.state)
        urllib.request.urlopen.assert_called_once_with(self.url, timeout=10.0)

    def test_not_exist(self, mocker):
        self.patch(mocker, HTTPError("url", code=404, msg="NotFound", hdrs=None, fp=None))

        assert not release.release_exists(self.state)
        urllib.request.urlopen.assert_called_once_with(self.url, timeout=10.0)

    def test_fail(self, mocker):
        exc = HTTPError("url", code=500, msg="ServerError", hdrs=None, fp=None)
        self.patch(mocker, exc)

        with pytest.raises(HTTPError) as exc_info:
            release.release_exists(self.state)
            assert exc_info.value == exc

        urllib.request.urlopen.assert_called_once_with(self.url, timeout=10.0)


class TestAptRunner:
    def patch(self, mocker, side_effect=None):
        self.env = {'ENV1': 'hello'}
        mocker.patch.object(release, 'run_cmd', side_effect=side_effect)
        mocker.patch('os.environ.copy', return_value=self.env)

    def test_no_assume_yes(self, mocker):
        self.patch(mocker)
        release.run_apt('update')
        release.run_cmd.assert_called_once_with('apt-get', 'update', env=self.env)

    def test_assume_yes(self, mocker):
        self.patch(mocker)
        release.run_apt('update', assume_yes=True)

        argv = ['apt-get', 'update', '--yes', '--allow-downgrades',
                '-o', 'Dpkg::Options::=--force-confdef',
                '-o', 'Dpkg::Options::=--force-confold']
        env = self.env
        env['DEBIAN_FRONTEND'] = 'noninteractive'

        release.run_cmd.assert_called_once_with(*argv, env=env)

    def test_user_abort(self, mocker):
        self.patch(mocker, side_effect=CalledProcessError(1, cmd='apt-get'))
        with pytest.raises(release.UserAbortException):
            release.run_apt('update')
        release.run_cmd.assert_called_once_with('apt-get', 'update', env=self.env)

    def test_failure(self, mocker):
        exc = CalledProcessError(42, cmd='apt-get')
        self.patch(mocker, side_effect=exc)
        with pytest.raises(CalledProcessError) as exc_info:
            release.run_apt('update')
            assert exc_info.value == exc
        release.run_cmd.assert_called_once_with('apt-get', 'update', env=self.env)


@pytest.mark.parametrize('state,result', [
    (release.SystemState('testing', 'wb6/stretch', ''),
     'deb http://deb.wirenboard.com/wb6/stretch testing main'),
    (release.SystemState('wb-2108', 'wb6/stretch', 'my/prefix'),
     'deb http://deb.wirenboard.com/my/prefix/wb6/stretch wb-2108 main'),
    (release.SystemState('staging', 'all', ''),
     'deb http://deb.wirenboard.com/all staging main')
])
def test_sources_list_generator(state, result):
    with tempfile.NamedTemporaryFile() as f:
        release.generate_sources_list(state, filename=f.name)
        assert read_file_ignore_comments(f.name) == result


@pytest.mark.parametrize('state,result', [
    (release.SystemState('testing', 'wb6/stretch', ''),
     "Package: *\nPin: release o=wirenboard, a=testing\nPin-Priority: 990"),
    (release.SystemState('wb-2108', 'wb6/stretch', 'my/prefix'),
     "Package: *\nPin: release o=wirenboard, a=wb-2108\nPin-Priority: 990")
])
def test_release_apt_preferences_generator(state, result):
    with tempfile.NamedTemporaryFile() as f:
        release.generate_release_apt_preferences(state, filename=f.name)
        assert read_file_ignore_comments(f.name) == result


@pytest.mark.parametrize('state,result', [
    (release.SystemState('testing', 'wb6/stretch', ''),
     "Package: *\nPin: release o=wirenboard, a=testing\nPin-Priority: 1010"),
    (release.SystemState('wb-2108', 'wb6/stretch', 'my/prefix'),
     "Package: *\nPin: release o=wirenboard, a=wb-2108\nPin-Priority: 1010")
])
def test_tmp_apt_preferences_generator(state, result):
    with tempfile.NamedTemporaryFile() as f:
        release.generate_tmp_apt_preferences(state, filename=f.name)
        assert read_file_ignore_comments(f.name) == \
            result + '\n\nPackage: *\nPin: release o=wirenboard\nPin-Priority: -10'


def test_generate_system_config(mocker):
    mocker.patch.object(release, 'generate_sources_list')
    mocker.patch.object(release, 'generate_release_apt_preferences')
    state = release.SystemState('testing', 'wb6/stretch', 'my/prefix')

    release.generate_system_config(state)

    release.generate_sources_list.assert_called_once_with(
        state, filename=release.WB_SOURCES_LIST_FILENAME)
    release.generate_release_apt_preferences.assert_called_once_with(
        state, filename=release.WB_RELEASE_APT_PREFERENCES_FILENAME)


@pytest.mark.parametrize('assume_yes', [True, False])
def test_system_update(mocker, assume_yes):
    mocker.patch.object(release, 'run_apt')
    release.system_update(assume_yes)
    release.run_apt.assert_has_calls([
        mocker.call('update', assume_yes),
        mocker.call('dist-upgrade', assume_yes)
    ], any_order=False)


class TestRoute:
    def patch(self, mocker, system_state=release.SystemState('testing', 'wb6/stretch', ''), release_exists=True):
        # possible actions
        mocker.patch.object(release, 'update_system', return_value=release.RETCODE_OK)
        mocker.patch.object(release, 'generate_system_config', return_value=release.RETCODE_OK)
        mocker.patch.object(release, 'print_banner')

        # additional info sources
        mocker.patch.object(release, 'get_current_state', return_value=system_state)
        mocker.patch.object(release, 'release_exists', return_value=release_exists)

    def make_args(self, **kwargs):
        new_kwargs = {
            'regenerate': False,
            'target_release': None,
            'version': False,
            'yes': False,
            'reset_packages': False,
            'reset_url': False,
            'prefix': None,
            'second_stage': False}
        new_kwargs.update(**kwargs)
        return SimpleNamespace(**new_kwargs)

    def test_print_banner_empty(self, mocker):
        self.patch(mocker)
        assert release.RETCODE_OK == release.route(args=self.make_args(), argv=['test'])
        release.print_banner.assert_called_once_with()

    def test_print_banner_version(self, mocker):
        self.patch(mocker)
        assert release.RETCODE_OK == release.route(args=self.make_args(version=True), argv=['test', '-v'])
        release.print_banner.assert_called_once_with()

    def test_reset_packages_route(self, mocker):
        state = release.SystemState('testing', 'wb6/stretch', '')
        self.patch(mocker, state)
        assert release.RETCODE_OK == release.route(args=self.make_args(reset_packages=True), argv=['test', '-p'])
        release.update_system.assert_called_once_with(state, state, second_stage=True, assume_yes=False)

    def test_reset_packages_conflict(self, mocker):
        self.patch(mocker)
        assert release.RETCODE_EINVAL == release.route(args=self.make_args(
            reset_packages=True, reset_url=True), argv=['test', '-p'])
        assert release.RETCODE_EINVAL == release.route(args=self.make_args(
            reset_packages=True, target_release='new_release'), argv=['test', '-p'])
        assert release.RETCODE_EINVAL == release.route(args=self.make_args(
            reset_packages=True, prefix='new/prefix'), argv=['test', '-p'])

    def test_regenerate_config(self, mocker):
        state = release.SystemState('testing', 'wb6/stretch', '')
        self.patch(mocker, state)
        assert release.RETCODE_OK == release.route(args=self.make_args(regenerate=True), argv=['test', '-r'])
        release.generate_system_config.assert_called_once_with(state)

    def test_same_state(self, mocker):
        state = release.SystemState('testing', 'wb6/stretch', '')
        self.patch(mocker, state)
        assert release.RETCODE_OK == release.route(args=self.make_args(target_release='testing'), argv=['test', '-t'])

    def test_new_state_exists(self, mocker):
        state = release.SystemState('testing', 'wb6/stretch', '')
        new_state = release.SystemState('stable', 'wb6/stretch', '')
        self.patch(mocker, state)
        assert release.RETCODE_OK == release.route(args=self.make_args(target_release='stable'), argv=['test', '-t'])
        release.release_exists.assert_called_once_with(new_state)
        release.update_system.assert_called_once_with(new_state, state, second_stage=False, assume_yes=False)

    def test_new_state_not_exist(self, mocker):
        state = release.SystemState('testing', 'wb6/stretch', '')
        new_state = release.SystemState('stable', 'wb6/stretch', '')
        self.patch(mocker, state, release_exists=False)
        assert release.RETCODE_NO_TARGET == release.route(
            args=self.make_args(target_release='stable'), argv=['test', '-t'])
        release.release_exists.assert_called_once_with(new_state)
        release.update_system.assert_not_called()


class TestArgParser:
    def patch(self, mocker, return_value=release.RETCODE_OK):
        args = {
            'regenerate': False,
            'target_release': None,
            'version': False,
            'yes': False,
            'reset_packages': False,
            'reset_url': False,
            'prefix': None,
            'second_stage': False
        }
        self.default_args = argparse.Namespace(**args)
        mocker.patch.object(release, 'route', return_value=return_value)

    def test_no_args(self, mocker):
        self.patch(mocker)
        argv = ['wb-release']
        release.main(argv)
        release.route.assert_called_once_with(self.default_args, argv)

    def test_target(self, mocker):
        self.patch(mocker)
        argv = ['wb-release', '-t', 'testing']
        release.main(argv)

        args = vars(self.default_args)
        args['target_release'] = 'testing'

        release.route.assert_called_once_with(argparse.Namespace(**args), argv)


class TestUpdate:
    def patch(self, mocker, raise_exc=None, return_value=release.RETCODE_OK):
        self.old_state = release.SystemState('testing', 'wb6/stretch', '')
        self.new_state = release.SystemState('stable', 'wb6/stretch', '')

        mocker.patch.object(release, 'update_first_stage', return_value=return_value, side_effect=raise_exc)
        mocker.patch.object(release, 'update_second_stage', return_value=return_value, side_effect=raise_exc)

    def test_first_stage(self, mocker):
        self.patch(mocker)

        assert release.RETCODE_OK == release.update_system(self.new_state, self.old_state, second_stage=False)
        release.update_first_stage.assert_called_once_with(assume_yes=False)
        release.update_second_stage.assert_not_called()

    @pytest.mark.parametrize('exception,retcode', [
        (release.UserAbortException, release.RETCODE_USER_ABORT),
        (CalledProcessError(returncode=42, cmd='test'), 42),
        (KeyboardInterrupt, release.RETCODE_USER_ABORT),
        (Exception, release.RETCODE_FAULT)
    ])
    @pytest.mark.parametrize('second_stage', [True, False])
    def test_exceptions(self, mocker, exception, retcode, second_stage):
        self.patch(mocker, raise_exc=exception)

        assert retcode == release.update_system(self.new_state, self.old_state, second_stage=second_stage)
        if second_stage:
            release.update_second_stage.assert_called_once_with(self.new_state, self.old_state, assume_yes=False)
            release.update_first_stage.assert_not_called()
        else:
            release.update_first_stage.assert_called_once_with(assume_yes=False)
            release.update_second_stage.assert_not_called()


class TestUpdateStageBase:
    def patch(self, mocker, confirm=True):
        side_effect = None if confirm else release.UserAbortException
        mocker.patch.object(release, 'user_confirm', side_effect=side_effect)
        mocker.patch.object(release, 'system_update')
        mocker.patch.object(release, 'run_cmd')


class TestUpdateFirstStage(TestUpdateStageBase):
    def patch(self, mocker, argv=['wb-release'], confirm=True):
        super().patch(mocker, confirm=confirm)

        mocker.patch('sys.argv', argv)

    def test_no_confirm(self, mocker):
        self.patch(mocker, confirm=False)

        with pytest.raises(release.UserAbortException):
            release.update_first_stage(False)

        release.system_update.assert_not_called()

    @pytest.mark.parametrize('assume_yes', [True, False])
    def test_confirmed(self, mocker, assume_yes):
        argv = ['wb-release', '-t', 'testing', '--more-strange-args', 'and more']
        self.patch(mocker, argv=argv)

        release.update_first_stage(assume_yes)

        release.system_update.assert_called_once_with(assume_yes)
        release.run_cmd.assert_called_once_with(*(argv + ['--no-preliminary-update']))


class TestUpdateSecondStage(TestUpdateStageBase):
    def patch(self, mocker, confirm=True):
        super().patch(mocker, confirm=confirm)

        self.old_state = release.SystemState('testing', 'wb6/stretch', '')
        self.new_state = release.SystemState('stable', 'wb6/stretch', '')

        mocker.patch.object(release, 'generate_system_config')
        mocker.patch.object(release, 'generate_tmp_apt_preferences')
        mocker.patch.object(release, 'run_apt')
        mocker.patch('atexit.register')
        mocker.patch('atexit.unregister')

    def test_no_confirm(self, mocker):
        self.patch(mocker, confirm=False)

        with pytest.raises(release.UserAbortException):
            release.update_second_stage(self.new_state, self.old_state, assume_yes=False)

        release.system_update.assert_not_called()
        release.run_apt.assert_not_called()
        release.generate_system_config.assert_not_called()
        release.generate_tmp_apt_preferences.assert_not_called()

    @pytest.mark.parametrize('assume_yes', [True, False])
    def test_reset_packages(self, mocker, assume_yes):
        self.patch(mocker)

        release.update_second_stage(self.old_state, self.old_state, assume_yes)

        release.generate_system_config.assert_not_called()
        release.generate_tmp_apt_preferences.assert_called_once_with(
            self.old_state, filename=release.WB_TEMP_UPGRADE_PREFERENCES_FILENAME)
        release.system_update.assert_called_once_with(assume_yes)
        release.run_apt.assert_called_once_with('autoremove', assume_yes)

    @pytest.mark.parametrize('assume_yes', [True, False])
    def test_upgrade_release(self, mocker, assume_yes):
        self.patch(mocker)

        release.update_second_stage(self.new_state, self.old_state, assume_yes)

        release.generate_system_config.assert_called_once_with(self.new_state)
        release.generate_tmp_apt_preferences.assert_called_once_with(
            self.new_state, filename=release.WB_TEMP_UPGRADE_PREFERENCES_FILENAME)
        release.system_update.assert_called_once_with(assume_yes)
        release.run_apt.assert_called_once_with('autoremove', assume_yes)
