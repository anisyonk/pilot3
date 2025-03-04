#!/usr/bin/env python
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
#
# Authors:
# - Mario Lassnig, mario.lassnig@cern.ch, 2016-2017
# - Daniel Drizhuk, d.drizhuk@gmail.com, 2017
# - Paul Nilsson, paul.nilsson@cern.ch, 2017-2022

from __future__ import print_function  # Python 2 (2to3 complains about this)
from __future__ import absolute_import

import argparse
import logging
import os
import sys
import threading
import time
from os import getcwd, chdir, environ
from os.path import exists, join
from shutil import rmtree

from pilot.common.errorcodes import ErrorCodes
from pilot.common.exception import PilotException
from pilot.info import infosys
from pilot.util.auxiliary import pilot_version_banner, shell_exit_code
from pilot.util.constants import SUCCESS, FAILURE, ERRNO_NOJOBS, PILOT_START_TIME, PILOT_END_TIME, get_pilot_version, \
    SERVER_UPDATE_NOT_DONE, PILOT_MULTIJOB_START_TIME
from pilot.util.filehandling import get_pilot_work_dir, mkdirs, establish_logging
from pilot.util.harvester import is_harvester_mode
from pilot.util.https import get_panda_server, https_setup, send_update
from pilot.util.timing import add_to_pilot_timing

errors = ErrorCodes()


def main():
    """
    Main function of PanDA Pilot 2.
    Prepare for and execute the requested workflow.

    :return: exit code (int).
    """

    # get the logger
    logger = logging.getLogger(__name__)

    # print the pilot version
    pilot_version_banner()

    # define threading events
    args.graceful_stop = threading.Event()
    args.abort_job = threading.Event()
    args.job_aborted = threading.Event()

    # define useful variables
    args.retrieve_next_job = True  # go ahead and download a new job
    args.signal = None  # to store any incoming signals
    args.signal_counter = 0  # keep track of number of received kill signal (suicide counter)
    args.kill_time = 0  # keep track of when first kill signal arrived

    # perform https setup
    if args.use_https:
        https_setup(args, get_pilot_version())

    # let the server know that the worker has started
    if args.update_server:
        send_worker_status('started', args.queue, args.url, args.port, logger)

    # initialize InfoService
    try:
        infosys.init(args.queue)
        # check if queue is ACTIVE
        if infosys.queuedata.state != 'ACTIVE':
            logger.critical('specified queue is NOT ACTIVE: %s -- aborting', infosys.queuedata.name)
            return errors.PANDAQUEUENOTACTIVE
    except PilotException as error:
        logger.fatal(error)
        return error.get_error_code()

    # set the site name for rucio  ## is it really used?
    environ['PILOT_RUCIO_SITENAME'] = infosys.queuedata.site

    # store the site name as set with a pilot option
    environ['PILOT_SITENAME'] = infosys.queuedata.resource  #args.site  # TODO: replace with singleton

    # set requested workflow
    logger.info('pilot arguments: %s', str(args))
    workflow = __import__('pilot.workflow.%s' % args.workflow, globals(), locals(), [args.workflow], 0)

    # execute workflow
    try:
        exitcode = workflow.run(args)
    except Exception as exc:
        logger.fatal('main pilot function caught exception: %s', exc)
        exitcode = None

    # let the server know that the worker has finished
    if args.update_server:
        send_worker_status('finished', args.queue, args.url, args.port, logger)

    return exitcode


def str2bool(_var):
    """ Helper function to convert string to bool """

    if isinstance(_var, bool):
        return _var
    if _var.lower() in ('yes', 'true', 't', 'y', '1'):
        return True
    elif _var.lower() in ('no', 'false', 'f', 'n', '0'):
        return False
    else:
        raise argparse.ArgumentTypeError('Boolean value expected.')


def get_args():
    """
    Return the args from the arg parser.

    :return: args (arg parser object).
    """

    arg_parser = argparse.ArgumentParser()

    # pilot log creation
    arg_parser.add_argument('--no-pilot-log',
                            dest='nopilotlog',
                            action='store_true',
                            default=False,
                            help='Do not write the pilot log to file')

    # pilot work directory
    arg_parser.add_argument('-a', '--workdir',
                            dest='workdir',
                            default="",
                            help='Pilot work directory')

    # debug option to enable more log messages
    arg_parser.add_argument('-d', '--debug',
                            dest='debug',
                            action='store_true',
                            default=False,
                            help='Enable debug mode for logging messages')

    # the choices must match in name the python module in pilot/workflow/
    arg_parser.add_argument('-w', '--workflow',
                            dest='workflow',
                            default='generic',
                            choices=['generic', 'generic_hpc',
                                     'production', 'production_hpc',
                                     'analysis', 'analysis_hpc',
                                     'eventservice_hpc', 'stager', 'payload_stageout'],
                            help='Pilot workflow (default: generic)')

    # graciously stop pilot process after hard limit
    arg_parser.add_argument('-l', '--lifetime',
                            dest='lifetime',
                            default=324000,
                            required=False,
                            type=int,
                            help='Pilot lifetime seconds (default: 324000 s)')

    # set the appropriate site, resource and queue
    arg_parser.add_argument('-q', '--queue',
                            dest='queue',
                            required=True,
                            help='MANDATORY: queue name (e.g., AGLT2_TEST-condor)')
    arg_parser.add_argument('-r', '--resource',
                            dest='resource',
                            required=False,  # From v 2.2.0 the resource name is internally set
                            help='OBSOLETE: resource name (e.g., AGLT2_TEST)')
    arg_parser.add_argument('-s', '--site',
                            dest='site',
                            required=False,  # From v 2.2.1 the site name is internally set
                            help='OBSOLETE: site name (e.g., AGLT2_TEST)')

    # graciously stop pilot process after hard limit
    arg_parser.add_argument('-j', '--joblabel',
                            dest='job_label',
                            default='ptest',
                            help='Job prod/source label (default: ptest)')

    # pilot version tag; PR or RC
    arg_parser.add_argument('-i', '--versiontag',
                            dest='version_tag',
                            default='PR',
                            help='Version tag (default: PR, optional: RC)')

    arg_parser.add_argument('-z', '--updateserver',
                            dest='update_server',
                            action='store_false',
                            default=True,
                            help='Disable server updates')

    arg_parser.add_argument('-t', '--noproxyverification',
                            dest='verify_proxy',
                            action='store_false',
                            default=True,
                            help='Disable proxy verification')

    arg_parser.add_argument('-u', '--verifypayloadproxy',
                            dest='verify_payload_proxy',
                            action='store_false',
                            default=True,
                            help='Disable payload proxy verification')

    # graciously stop pilot process after hard limit
    arg_parser.add_argument('-v', '--getjobrequests',
                            dest='getjob_requests',
                            default=2,
                            required=False,
                            type=int,
                            help='Number of getjob requests')

    arg_parser.add_argument('-x', '--getjobfailures',
                            dest='getjob_failures',
                            default=5,
                            required=False,
                            type=int,
                            help='Maximum number of getjob request failures in Harvester mode')

    # SSL certificates
    arg_parser.add_argument('--cacert',
                            dest='cacert',
                            default=None,
                            help='CA certificate to use with HTTPS calls to server, commonly X509 proxy',
                            metavar='path/to/your/certificate')
    arg_parser.add_argument('--capath',
                            dest='capath',
                            default=None,
                            help='CA certificates path',
                            metavar='path/to/certificates/')

    # Server URLs and ports
    arg_parser.add_argument('--url',
                            dest='url',
                            default='',  # the proper default is stored in default.cfg
                            help='PanDA server URL')
    arg_parser.add_argument('-p', '--port',
                            dest='port',
                            default=25443,
                            help='PanDA server port')
    arg_parser.add_argument('--queuedata-url',
                            dest='queuedata_url',
                            default='',
                            help='Queuedata server URL')
    arg_parser.add_argument('--storagedata-url',
                            dest='storagedata_url',
                            default='',
                            help='URL for downloading DDM end points data')
    arg_parser.add_argument('--rucio-host',
                            dest='rucio_host',
                            default='',
                            help='URL for the Rucio host (optional)')

    # Country group
    arg_parser.add_argument('--country-group',
                            dest='country_group',
                            default='',
                            help='Country group option for getjob request')

    # Working group
    arg_parser.add_argument('--working-group',
                            dest='working_group',
                            default='',
                            help='Working group option for getjob request')

    # Allow other country
    arg_parser.add_argument('--allow-other-country',
                            dest='allow_other_country',
                            type=str2bool,
                            default=False,
                            help='Is the resource allowed to be used outside the privileged group?')

    # Allow same user
    arg_parser.add_argument('--allow-same-user',
                            dest='allow_same_user',
                            type=str2bool,
                            default=True,
                            help='Multi-jobs will only come from same taskID (and thus same user)')

    # Experiment
    arg_parser.add_argument('--pilot-user',
                            dest='pilot_user',
                            default='generic',
                            required=True,
                            help='Pilot user (e.g. name of experiment corresponding to pilot plug-in)')

    # Harvester specific options (if any of the following options are used, args.harvester will be set to True)
    arg_parser.add_argument('--harvester-workdir',
                            dest='harvester_workdir',
                            default='',
                            help='Harvester work directory')
    arg_parser.add_argument('--harvester-datadir',
                            dest='harvester_datadir',
                            default='',
                            help='Harvester data directory')
    arg_parser.add_argument('--harvester-eventstatusdump',
                            dest='harvester_eventstatusdump',
                            default='',
                            help='Harvester event status dump json file containing processing status')
    arg_parser.add_argument('--harvester-workerattributes',
                            dest='harvester_workerattributes',
                            default='',
                            help='Harvester worker attributes json file containing job status')
    arg_parser.add_argument('--harvester-submit-mode',
                            dest='harvester_submitmode',
                            default='PULL',
                            help='Harvester submit mode (PUSH or PULL [default])')
    arg_parser.add_argument('--resource-type',
                            dest='resource_type',
                            default='',
                            type=str,
                            choices=['SCORE', 'MCORE', 'SCORE_HIMEM', 'MCORE_HIMEM'],
                            help='Resource type; MCORE, SCORE, SCORE_HIMEM or MCORE_HIMEM')
    arg_parser.add_argument('--use-https',
                            dest='use_https',
                            type=str2bool,
                            default=True,
                            help='Use HTTPS protocol for communications with server')
    arg_parser.add_argument('--cleanup',
                            dest='cleanup',
                            type=str2bool,
                            default=True,
                            help='Cleanup work directory after pilot has finished')
    arg_parser.add_argument('--use-realtime-logging',
                            dest='use_realtime_logging',
                            action='store_true',
                            default=False,
                            help='Use near real-time logging, default=False')
    arg_parser.add_argument('--realtime-logging-server',
                            dest='realtime_logging_server',
                            default=None,
                            help='Realtime logging server [type:host:port]; e.g. logstash:12.23.34.45:80')
    # arg_parser.add_argument('--realtime-logfiles',
    #                         dest='realtime_logfiles',
    #                         default=None,
    #                         help='The log filenames to be sent to the realtime logging server')
    arg_parser.add_argument('--realtime-logname',
                            dest='realtime_logname',
                            default=None,
                            help='The log name in the realtime logging server')

    # Harvester and Nordugrid specific options
    arg_parser.add_argument('--input-dir',
                            dest='input_dir',
                            default='',
                            help='Input directory')
    arg_parser.add_argument('--input-destination-dir',
                            dest='input_destination_dir',
                            default='',
                            help='Input destination directory')
    arg_parser.add_argument('--output-dir',
                            dest='output_dir',
                            default='',
                            help='Output directory')
    arg_parser.add_argument('--job-type',
                            dest='jobtype',
                            default='',
                            help='Job type (managed, user)')
    arg_parser.add_argument('--use-rucio-traces',
                            dest='use_rucio_traces',
                            type=str2bool,
                            default=True,
                            help='Use rucio traces')

    # HPC options
    arg_parser.add_argument('--hpc-resource',
                            dest='hpc_resource',
                            default='',
                            help='Name of the HPC (e.g. Titan)')
    arg_parser.add_argument('--hpc-mode',
                            dest='hpc_mode',
                            default='manytoone',
                            help='HPC mode (manytoone, jumbojobs)')
    arg_parser.add_argument('--es-executor-type',
                            dest='executor_type',
                            default='generic',
                            help='Event service executor type (generic, raythena)')

    return arg_parser.parse_args()


def create_main_work_dir():
    """
    Create and return the pilot's main work directory.
    The function also sets args.mainworkdir and cd's into this directory.
    Note: args, used in this function, is defined in outer scope.

    :return: exit code (int), main work directory (string).
    """

    exitcode = 0

    if args.workdir != "":
        _mainworkdir = get_pilot_work_dir(args.workdir)
        try:
            # create the main PanDA Pilot work directory
            mkdirs(_mainworkdir)
        except PilotException as error:
            # print to stderr since logging has not been established yet
            print('failed to create workdir at %s -- aborting: %s' % (_mainworkdir, error), file=sys.stderr)
            exitcode = shell_exit_code(error._errorCode)
    else:
        _mainworkdir = getcwd()

    args.mainworkdir = _mainworkdir
    chdir(_mainworkdir)

    return exitcode, _mainworkdir


def set_environment_variables():
    """
    Set environment variables. To be replaced with singleton implementation.
    This function sets PILOT_WORK_DIR, PILOT_HOME, PILOT_SITENAME, PILOT_USER and PILOT_VERSION and others.
    Note: args and mainworkdir, used in this function, are defined in outer scope.

    :return:
    """

    # working directory as set with a pilot option (e.g. ..)
    environ['PILOT_WORK_DIR'] = args.workdir  # TODO: replace with singleton

    # main work directory (e.g. /scratch/PanDA_Pilot3_3908_1537173670)
    environ['PILOT_HOME'] = mainworkdir  # TODO: replace with singleton

    # pilot source directory (e.g. /cluster/home/usatlas1/gram_scratch_hHq4Ns/condorg_oqmHdWxz)
    environ['PILOT_SOURCE_DIR'] = args.sourcedir  # TODO: replace with singleton

    # set the pilot user (e.g. ATLAS)
    environ['PILOT_USER'] = args.pilot_user  # TODO: replace with singleton

    # internal pilot state
    environ['PILOT_JOB_STATE'] = 'startup'  # TODO: replace with singleton

    # set the pilot version
    environ['PILOT_VERSION'] = get_pilot_version()

    # set the default wrap-up/finish instruction
    environ['PILOT_WRAP_UP'] = 'NORMAL'

    # proxy verifications
    environ['PILOT_PROXY_VERIFICATION'] = '%s' % args.verify_proxy
    environ['PILOT_PAYLOAD_PROXY_VERIFICATION'] = '%s' % args.verify_payload_proxy

    # keep track of the server updates, if any
    environ['SERVER_UPDATE'] = SERVER_UPDATE_NOT_DONE

    # set the (HPC) resource name (if set in options)
    environ['PILOT_RESOURCE_NAME'] = args.hpc_resource

    # allow for the possibility of turning off rucio traces
    environ['PILOT_USE_RUCIO_TRACES'] = str(args.use_rucio_traces)

    # event service executor type
    environ['PILOT_ES_EXECUTOR_TYPE'] = args.executor_type

    if args.output_dir:
        environ['PILOT_OUTPUT_DIR'] = args.output_dir

    # keep track of the server urls
    environ['PANDA_SERVER_URL'] = get_panda_server(args.url, args.port)
    environ['QUEUEDATA_SERVER_URL'] = '%s' % args.queuedata_url
    environ['STORAGEDATA_SERVER_URL'] = '%s' % args.storagedata_url


def wrap_up():
    """
    Perform cleanup and terminate logging.
    Note: args and mainworkdir, used in this function, are defined in outer scope.

    :return: exit code (int).
    """

    exitcode = 0

    # cleanup pilot workdir if created
    if args.sourcedir != mainworkdir and args.cleanup:
        chdir(args.sourcedir)
        try:
            rmtree(mainworkdir)
        except Exception as exc:
            logging.warning("failed to remove %s: %s", mainworkdir, exc)
        else:
            logging.info("removed %s", mainworkdir)

    # in Harvester mode, create a kill_worker file that will instruct Harvester that the pilot has finished
    if args.harvester:
        from pilot.util.harvester import kill_worker
        kill_worker()

    try:
        exitcode = trace.pilot['error_code']
    except Exception:
        exitcode = trace
    else:
        logging.info('traces error code: %d', exitcode)
        if trace.pilot['nr_jobs'] <= 1:
            if exitcode != 0:
                logging.info('an exit code was already set: %d (will be converted to a standard shell code)', exitcode)
        elif trace.pilot['nr_jobs'] > 0:
            if trace.pilot['nr_jobs'] == 1:
                logging.getLogger(__name__).info('pilot has finished (%d job was processed)', trace.pilot['nr_jobs'])
            else:
                logging.getLogger(__name__).info('pilot has finished (%d jobs were processed)', trace.pilot['nr_jobs'])
            exitcode = SUCCESS
        elif trace.pilot['state'] == FAILURE:
            logging.critical('pilot workflow failure -- aborting')
        elif trace.pilot['state'] == ERRNO_NOJOBS:
            logging.critical('pilot did not process any events -- aborting')
            exitcode = ERRNO_NOJOBS
    logging.info('pilot has finished')
    logging.shutdown()

    return shell_exit_code(exitcode)


def get_pilot_source_dir():
    """
    Return the pilot source directory.

    :return: full path to pilot source directory.
    """

    cwd = getcwd()
    if exists(join(join(cwd, 'pilot3'), 'pilot.py')):  # in case wrapper has untarred src as pilot3 in init dir
        return join(cwd, 'pilot3')
    elif exists(join(cwd, 'pilot.py')):  # in case pilot gets launched from within the src dir
        return cwd
    else:
        # could throw error here, but logging is not setup yet - fail later
        return cwd


def send_worker_status(status, queue, url, port, logger):
    """
    Send worker info to the server to let it know that the worker has started
    Note: the function can fail, but if it does, it will be ignored.

    :param status: 'started' or 'finished' (string).
    :param queue: PanDA queue name (string).
    :param url: server url (string).
    :param port: server port (string).
    :param logger: logging object.
    :return:
    """

    # worker node structure to be sent to the server
    data = {}
    data['workerID'] = os.environ.get('HARVESTER_WORKER_ID', None)
    data['harvesterID'] = os.environ.get('HARVESTER_ID', None)
    data['status'] = status
    data['site'] = queue

    # attempt to send the worker info to the server
    if data['workerID'] and data['harvesterID']:
        send_update('updateWorkerPilotStatus', data, url, port)
    else:
        logger.warning('workerID/harvesterID not known, will not send worker status to server')


if __name__ == '__main__':
    """
    Main function of pilot module.
    """

    # get the args from the arg parser
    args = get_args()
    args.last_heartbeat = time.time()
    # args.rucio_host = 'https://voatlasrucio-server-prod.cern.ch:443'

    # Define and set the main harvester control boolean
    args.harvester = is_harvester_mode(args)

    # initialize the pilot timing dictionary
    args.timing = {}  # TODO: move to singleton?

    # initialize job status dictionary (e.g. used to keep track of log transfers)
    args.job_status = {}  # TODO: move to singleton or to job object directly?

    # store T0 time stamp
    add_to_pilot_timing('0', PILOT_START_TIME, time.time(), args)
    add_to_pilot_timing('1', PILOT_MULTIJOB_START_TIME, time.time(), args)

    # if requested by the wrapper via a pilot option, create the main pilot workdir and cd into it
    args.sourcedir = getcwd()  #get_pilot_source_dir()

    exit_code, mainworkdir = create_main_work_dir()
    if exit_code != 0:
        sys.exit(exit_code)

    # setup and establish standard logging
    establish_logging(debug=args.debug, nopilotlog=args.nopilotlog)

    # set environment variables (to be replaced with singleton implementation)
    set_environment_variables()

    # execute main function
    trace = main()

    # store final time stamp (cannot be placed later since the mainworkdir is about to be purged)
    add_to_pilot_timing('0', PILOT_END_TIME, time.time(), args, store=False)

    # perform cleanup and terminate logging
    exit_code = wrap_up()

    # the end.
    sys.exit(exit_code)
