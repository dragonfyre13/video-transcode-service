#!/usr/bin/env python

import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import yaml

# Base directory of video files to transcode
TC_ROOT = "/video_files"
# Base directory of config and log files
CONFIG_ROOT = "/config"
LOG_FILE = os.path.join(CONFIG_ROOT, 'transcoder.log')
CONFIG_FILE = os.path.join(CONFIG_ROOT, 'config.yaml')
LOG_TRACE = 5


class TranscodeError(RuntimeError):
    pass


class ChildProcessError(Exception):
    """
    This exception is raised when a child process returns a non-zero exit status.
    The exit status will be stored in the returncode attribute.
    The command will be stored in the command attribute
    The stdout of the command will be stored in stdout.
    If stdout and stderr were merged during execution, stdout and stderr
    attributes will both contain the merged output. Otherwise stdout
    and stderr attributes will contain the relevant output for each channel.
    """
    def __init__(self, returncode, command, stdout, stderr):
        self.returncode = returncode
        self.command = command
        self.stdout = stdout
        if stderr is None:
            self.stderr = stdout
        else:
            self.stderr = stderr

    def __str__(self):
        return "Command '%s' returned non-zero exit status %d" % (self.command, self.returncode)


class Transcoder(object):
    def __init__(self):
        self.running = False
        self.in_event_loop = False
        self.logger = self.setup_logging()
        self.current_proc = None
        self._default_handlers = {}
        self.config = self.get_config_dict()
        self._input_subdir = 'input'
        self._output_subdir = 'output'
        self._successful_originals_subdir = 'originals'
        self._failed_originals_subdir = 'failed'
        self._work_dir = 'work'
        # Used when processing a file. Horribly un-threadsafe, but I don't have a use for threads in this class.
        self._current_relpath = ''
        self._current_filename = ''
        self._option_dir = ''
        self._option_args = ''

    @property
    def input_loc(self):
        return os.path.join(TC_ROOT, self._option_dir, self._input_subdir,
                            self._current_relpath, self._current_filename)

    @property
    def output_loc(self):
        return os.path.join(TC_ROOT, self._option_dir, self._output_subdir,
                            self._current_relpath, self._current_filename)

    @property
    def output_mkv_loc(self):
        return os.path.join(TC_ROOT, self._option_dir, self._output_subdir,
                            self._current_relpath, os.path.splitext(self._current_filename)[0] + '.mkv')

    @property
    def failed_originals_loc(self):
        return os.path.join(TC_ROOT, self._option_dir, self._failed_originals_subdir,
                            self._current_relpath, self._current_filename)

    @property
    def successful_originals_loc(self):
        return os.path.join(TC_ROOT, self._option_dir, self._successful_originals_subdir,
                            self._current_relpath, self._current_filename)

    @property
    def simple_loc(self):
        return '"%s" in "%s"' % (os.path.join(self._current_relpath, self._current_filename),
                                 self._option_dir)

    @property
    def work_mkv_loc(self):
        return os.path.join(self.work_dir,
                            os.path.splitext(self._current_filename)[0] + '.mkv')

    @property
    def work_dir(self):
        # TODO: Consider making the work_dir available on a different docker mount point.
        return os.path.join(TC_ROOT, self._work_dir)

    @property
    def option_args(self):
        # An extra space at the start doesn't break anything with the args.
        return ' '.join([self.config['global_args'], self._option_args])

    def set_current_file_props(self, current_relpath='', current_filename=''):
        '''Set a filename and it's relative path to subdirs for the current file.'''
        if current_relpath or current_filename:
            self.logger.log(LOG_TRACE, 'Set file props to relpath=%r and filename=%r',
                            current_relpath, current_filename)
        self._current_relpath = current_relpath or ''
        self._current_filename = current_filename or ''

    def set_current_option_props(self, option_dir='', option_args=''):
        '''Set the current option details (the option directory within TC_ROOT and the transcode_video arguments)'''
        self._option_dir = option_dir or ''
        self._option_args = option_args or ''

    @staticmethod
    def setup_logging():
        '''Setup basic logging to the log file (DEBUG minimum) and stdout (INFO minimum)'''
        logger = logging.getLogger('transcoder')
        formatter = logging.Formatter('%(asctime)s %(levelname)8s: %(message)s')
        logger.setLevel(logging.DEBUG)
        filehandler = logging.FileHandler(LOG_FILE)
        filehandler.setLevel(logging.DEBUG)
        filehandler.setFormatter(formatter)
        logger.addHandler(filehandler)
        streamhandler = logging.StreamHandler(sys.stdout)
        streamhandler.setLevel(logging.INFO)
        streamhandler.setFormatter(formatter)
        logger.addHandler(streamhandler)
        logger.info('************ Started transcoder logging ************')
        return logger

    def reload_config(self):
        '''Reload the configuration from the yaml file, then check that the filesystem is still valid.'''
        config = self.get_config_dict()
        self._input_subdir = config['input_subdir'] or 'input'
        self._output_subdir = config['output_subdir'] or 'output'
        self._successful_originals_subdir = config['successful_originals_subdir'] or 'originals'
        self._failed_originals_subdir = config['failed_originals_subdir'] or 'failed'
        self._work_dir = config['work_dir'] or 'work'
        self.logger.log(LOG_TRACE, 'Loaded config including:\n_work_dir=%r\n_input_subdir=%r\n_output_subdir=%r\n'
                        '_failed_originals_subdir=%r\n_successful_originals_subdir=%r', self._input_subdir,
                        self._output_subdir, self._failed_originals_subdir, self._successful_originals_subdir)
        self.check_filesystem(config)
        return config

    @staticmethod
    def get_config_dict():
        '''Pulls the current config, while supplying some sane-ish defaults'''
        config = dict(
            write_waiting_threshold=30, min_free_mb=1000, require_english=False,
            work_dir='work', input_subdir='input', output_subdir='output',
            successful_originals_subdir='originals', failed_originals_subdir='failed',
            global_args='', conversion_options={'defaults': ''})
        with open(CONFIG_FILE) as f:
            config.update(yaml.load(f))
        return config

    def check_filesystem(self, config):
        '''Check that the filesystem and directories are setup as expected for the current config'''
        # First, let's remove this annoying .dvdcss directory that keeps popping into /config
        if os.path.exists(os.path.join(CONFIG_ROOT, '.dvdcss')):
            shutil.rmtree(os.path.join(CONFIG_ROOT, '.dvdcss'))
        check_paths = [self.work_dir]
        # Clear this, otherwise all sorts of junk is created.
        self.set_current_file_props()
        for option_dir in config['conversion_options'].keys():
            self.set_current_option_props(option_dir=option_dir)
            check_paths.extend([self.input_loc, self.output_loc,
                                self.failed_originals_loc, self.successful_originals_loc])
        # Either verify all paths exist or create them if they don't
        for path in check_paths:
            if not os.path.exists(path):
                self.logger.debug('Creating %r as it does not currently exist', path)
                try:
                    os.makedirs(path)
                except OSError as e:
                    msg = 'Cannot create directory "%s": %s' % (path, e.strerror)
                    raise IOError(msg)

    def setup_signal_handlers(self):
        "Setup graceful shutdown and cleanup when sent a signal"
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            self._default_handlers[sig] = signal.signal(sig, lambda signum, frame: self.stop())

    def stop(self):
        '''Stop processing, while guarding against multiple signals being sent before the first one finishes'''
        if not self.running:
            return
        self.running = False
        try:
            self.logger.info('Transcoder shutting down')
        except BaseException:
            # Don't die for the sake of logging...
            pass
        if self.current_proc:
            self.current_proc.terminate()
        # Restore the original signal handlers
        self.restore_signal_handlers()

    def restore_signal_handlers(self):
        "Restore the default handlers"
        for sig, handler in self._default_handlers.items():
            signal.signal(sig, handler)
        self._default_handlers = {}

    def execute(self, command, merge_stderr=True):
        '''A simple method to kick off an arbitrary child process'''
        args = shlex.split(command)
        stderr = subprocess.STDOUT if merge_stderr else subprocess.PIPE
        try:
            self.current_proc = subprocess.Popen(args=args, stdout=subprocess.PIPE, stderr=stderr)
            stdout, stderr = self.current_proc.communicate()
            if self.current_proc.returncode != 0:
                raise ChildProcessError(self.current_proc.returncode, command, stdout, stderr)
        finally:
            self.current_proc = None
        return stdout

    def run(self):
        '''
        Run the event loop. Looks for stuff as long as we're running.
        '''
        try:
            self.running = True
            self.setup_signal_handlers()

            while self.running:
                self.in_event_loop = True
                self.config = self.reload_config()
                if not self.check_for_input():
                    time.sleep(5)
            self.in_event_loop = False
        except BaseException as e:
            self.logger.error('Uncaught exception: %s', str(e), exc_info=True)
            raise

    def wait_free_space(self):
        fs_stat = os.statvfs(TC_ROOT)
        avail_size_mb = (fs_stat.f_frsize * fs_stat.f_bavail) / (1024 * 1024)
        if avail_size_mb > self.config['min_free_mb']:
            self.logger.debug('Free disk space: %i MB', avail_size_mb)
            return True
        else:
            self.logger.warning('Halting until minimum disk space is available. '
                                'Free disk space: %i MB', avail_size_mb)
            while avail_size_mb < self.config['min_free_mb']:
                time.sleep(self.config['write_waiting_threshold'])
                fs_stat = os.statvfs(TC_ROOT)
                avail_size_mb = (fs_stat.f_frsize * fs_stat.f_bavail) / (1024 * 1024)
            self.logger.debug('Free MB of filesystem: %i', avail_size_mb)
            return True

    def move_file(self, orig_loc, new_loc):
        self.logger.log(LOG_TRACE, 'Moving file from %r to %r', orig_loc, new_loc)
        if not os.path.exists(orig_loc):
            raise IOError('File to move does not exist: %s' % orig_loc)
        if not os.path.exists(os.path.dirname(new_loc)):
            self.logger.debug('Creating %r as it does not currently exist.', os.path.dirname(new_loc))
            try:
                os.makedirs(os.path.dirname(new_loc))
            except OSError as e:
                msg = 'Cannot create directory "%s": %s' % (os.path.dirname(new_loc), e.strerror)
                raise IOError(msg)
        return shutil.move(orig_loc, new_loc)

    def check_for_input(self):
        '''
        Look through each of the input directories (recursively) and transcode any files found.
        Non-media files found are simply moved to the output directory.
        Returns True if a media file was processed, False if no media file was found in any input directory.
        '''
        for option_dir, option_args in self.config['conversion_options'].items():
            self.set_current_option_props(option_dir=option_dir, option_args=option_args)
            self.set_current_file_props()
            option_input_dir = self.input_loc
            for dirpath, dirnames, filenames in os.walk(option_input_dir):
                if not filenames:
                    continue
                if dirpath.startswith(option_input_dir):
                    dirpath = dirpath[len(option_input_dir):]
                for filename in filenames:
                    if filename.startswith('.'):
                        continue
                    # This makes all the "X_loc" properties mean relevant things to the current file.
                    self.set_current_file_props(current_relpath=dirpath, current_filename=filename)
                    if (time.time() - os.stat(self.input_loc).st_mtime) <= self.config['write_waiting_threshold']:
                        continue
                    try:
                        self.scan_media(test_media_file=True)
                    except TranscodeError:
                        self.logger.debug('Moving non-media file %s to output directory.', self.simple_loc)
                        self.move_file(self.input_loc, self.output_loc)
                        continue
                    try:
                        self.wait_free_space()
                        self.process_input()
                    except TranscodeError as e:
                        if not self.running:
                            self.logger.info('Stopping file processing of %s due to early shutdown.', self.simple_loc)
                            return False
                        self.logger.error('TranscodeError processing %s: %s', self.simple_loc, str(e), exc_info=True)
                        self.move_file(self.input_loc, self.failed_originals_loc)
                        return True
                    except BaseException as e:
                        if not self.running:
                            self.logger.info('Stopping file processing of %s due to early shutdown.', self.simple_loc)
                            return False
                        self.logger.error('Unknown error while processing %s: %s',
                                          self.simple_loc, str(e), exc_info=True)
                        self.move_file(self.input_loc, self.failed_originals_loc)
                        return True
                    # move the source to the COMPLETED_DIRECTORY
                    self.move_file(self.input_loc, self.successful_originals_loc)
                    return True
        return False

    def process_input(self):
        self.logger.info('Found new input %s', self.simple_loc)
        self.logger.log(LOG_TRACE, 'File paths:\ninput = %r\noutput = %r\nsuccess = %r\nfailure = %r\n',
                        self.input_loc, self.output_loc, self.successful_originals_loc, self.failed_originals_loc)
        # transcode the video, including steps to parse the input meta info and determine crop dimensions
        self.transcode()

        self.logger.debug('Video %s transcoded successfully, now generating transcode stats.', self.simple_loc)
        try:
            with open(self.work_mkv_loc + '.transcode_stats', 'w') as f:
                for stat_type in 'rbst':
                    out = self.execute('query-handbrake-log %s "%s"' % (stat_type, self.work_mkv_loc + '.log'),
                                       merge_stderr=False)
                    if stat_type == 'r':
                        self.logger.debug('Encoding rate factor (relative quality, lower=better): %s', out.strip())
                    f.write(out.strip() + '\n')
        except ChildProcessError as e:
            # Not raising an error since transcode was successful, even if query-handbrake-log didn't work
            self.logger.warning('Generating handbrake stats failed for %s with code %i: %s',
                                self.simple_loc, e.returncode, e.stderr)
        # move the completed output to the output directory
        self.logger.info('Moving completed work for %s to output directory', self.simple_loc)
        self.move_file(self.work_mkv_loc, self.output_mkv_loc)
        self.move_file(self.work_mkv_loc + '.log', self.output_mkv_loc + '.log')
        self.move_file(self.work_mkv_loc + '.transcode_stats', self.output_mkv_loc + '.transcode_stats')

    def scan_media(self, test_media_file=False):
        '''Use handbrake to scan the media for metadata'''
        if not test_media_file:
            self.logger.debug('Scanning %s for metadata', self.simple_loc)
        command = 'HandBrakeCLI --scan --input "%s"' % self.input_loc
        try:
            out = self.execute(command)
        except ChildProcessError as e:
            if test_media_file:
                raise TranscodeError('Not a usable media file')
            if 'unrecognized file type' in e.stderr:
                self.logger.warning('Unknown media type (%i) for input %s',
                                    e.returncode, self.simple_loc)
                raise TranscodeError('Unknown media type')
            else:
                self.logger.warning('Unknown error for input %s with error code %i: %s',
                                    self.simple_loc, e.returncode, e.stderr)
                raise TranscodeError('Unknown metadata error')
        return out

    @staticmethod
    def non_zero_min(values):
        "Return the min value but always prefer non-zero values if they exist"
        if not values:
            raise TypeError('non_zero_min expected 1 arguments, got 0')
        non_zero_values = [i for i in values if i != 0]
        if non_zero_values:
            return min(non_zero_values)
        return 0

    def detect_crop(self):
        crop_re = r'[0-9]+:[0-9]+:[0-9]+:[0-9]+'
        self.logger.debug('Detecting crop for %s', self.simple_loc)
        command = 'detect-crop --values-only "%s"' % self.input_loc
        try:
            out = self.execute(command)
        except ChildProcessError as e:
            # when detect-crop detects discrepancies between handbrake and
            # mplayer, each crop is written out but detect-crop also returns
            # an error code. if this is the case, we don't want to error out.
            if re.findall(crop_re, e.stdout):
                self.logger.log(LOG_TRACE, 'Ignoring detect-crop discrepancy between handbrake and mplayer')
                out = e.stdout
            else:
                self.logger.debug('detect-crop failed for %s, proceeding with no crop. Error: %s',
                                  self.simple_loc, e.stdout)
                return '0:0:0:0'

        crops = re.findall(crop_re, out)
        if not crops:
            self.logger.debug('No crop found for %s, proceeding with no crop', self.simple_loc)
            return '0:0:0:0'
        # use the smallest crop for each edge. prefer non-zero values if they exist
        dimensions = zip(*[map(int, c.split(':')) for c in crops])
        crop = ':'.join(map(str, [self.non_zero_min(piece) for piece in dimensions]))
        self.logger.debug('Using crop "%s" for %s', crop, self.simple_loc)
        if not crop:
            raise TranscodeError('Error determining crop dimensions')
        return crop

    def transcode(self):
        # if these paths exist in the work directory, remove them first
        for workpath in (self.work_mkv_loc, self.work_mkv_loc + '.log'):
            if os.path.exists(workpath):
                self.logger.info('Removing old work output: "%s"', workpath)
                os.unlink(workpath)

        command = ' '.join([
            'transcode-video',
            '--crop %s' % self.detect_crop(),
            self.parse_audio_tracks(),
            self.option_args,
            '--output "%s"' % self.work_mkv_loc,
            '"%s"' % self.input_loc
        ])
        self.logger.info('Transcoding %s with command: %s', self.simple_loc, command)
        try:
            self.execute(command, merge_stderr=False)
        except ChildProcessError as e:
            if not self.running:
                self.logger.warning('Transcoding failed for %s due to early shut down.', self.simple_loc)
                raise TranscodeError('Transcoding halted')
            self.logger.warning('Transcoding failed for %s with code %i: %s', self.simple_loc, e.returncode, e.stderr)
            raise TranscodeError('Transcoding failed')
        self.logger.info('Transcoding completed for %s', self.simple_loc)

    def parse_audio_tracks(self):
        "Parse the meta info for audio tracks beyond the first one"

        # find all the audio streams and their optional language and title data
        meta = self.scan_media(test_media_file=False)
        streams = []
        stream_re = (r'(\s{4}Stream #[0-9]+[.:][0-9]+(?:\((?P<lang>[a-z]+)\))?: '
                     r'Audio:.*?\n)(?=(?:\s{4}Stream)|(?:[^\s]))')
        title_re = r'^\s{6}title\s+:\s(?P<title>[^\n]+)'
        default_re = r'(^(?!\s*title\s+:\s)[^\n]+\n)*'
        for stream, lang in re.findall(stream_re, meta, re.DOTALL | re.MULTILINE):
            #lang = lang = ''
            title, title_match = '', re.search(title_re, stream, re.MULTILINE)
            default = False
            if title_match:
                title = title_match.group(1)
            default = '(default)' in re.match(default_re, stream, re.MULTILINE).group(0)
            streams.append({'title': title, 'lang': lang, 'default': default})

        # find the audio track numbers
        tracks = []
        pos = meta.find('+ audio tracks:')
        track_re = r'^\s+\+\s(?P<track>[0-9]+),\s(?P<title>[^\(\n]*)'
        for line in meta[pos:].split('\n')[1:]:
            if line.startswith('  + subtitle tracks:'):
                break
            match = re.match(track_re, line)
            if match:
                tracks.append({'number': match.group(1), 'title': match.group(2)})

        # assuming there's an equal number of tracks and streams, we can
        # match up stream titles to tracks and have a nicer output
        use_stream_titles = len(streams) == len(tracks)
        additional_tracks = []

        for i, track in enumerate(tracks):
            title = ''
            if use_stream_titles:
                title = streams[i]['title']
                if streams[i]['default']:
                    self.logger.debug('Ignoring default audio track #%s with title: %s', track['number'], title)
                    continue
                # Only add extra streams that are english if the config require_english == True
                is_english_stream = str(streams[i]['lang']).lower() in ('english', 'eng', 'en', '')
                if self.config['require_english'] and not is_english_stream:
                    self.logger.debug('Ignoring extra non-english audio track #%s with title: %s',
                                      track['number'], title)
                    continue
            elif i == 0:
                # Unable to determine if the track is default due to mismatch between
                # streams count and tracks count. Presuming the first track is default.
                continue
            title = title or track['title']
            # remove any quotes in the title so we don't mess up the command
            title = title.replace('"', '')
            self.logger.debug('Adding audio track #%s with title: %s', track['number'], title)
            additional_tracks.append('--add-audio %s="%s"' % (track['number'], title))
        return ' '.join(additional_tracks)


if __name__ == '__main__':
    Transcoder().run()
