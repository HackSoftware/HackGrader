from __future__ import absolute_import
from celery import shared_task
from celery.utils.log import get_task_logger

from tester.models import TestRun, TestWithPlainText, RunResult, Language
from HackTester.settings import BASE_DIR, MEDIA_ROOT
from HackTester.settings import NPROC_SOFT_LIMIT, NPROC_HARD_LIMIT, \
        DOCKER_MEMORY_LIMIT, DOCKER_USER, DOCKER_IMAGE, \
        DOCKER_USER, DOCKER_TIME_LIMIT


import os
import json
import time
import shutil

from subprocess import CalledProcessError, check_output, STDOUT, TimeoutExpired

logger = get_task_logger(__name__)

FILE_EXTENSIONS = {l.name: l.extension for l in Language.objects.all()}

SANDBOX = 'sandbox/'
DOCKER_COMMAND = """docker run -d \
        -u {docker_user} \
        --ulimit nproc={nproc_soft_limit}:{nproc_hard_limit} \
        -m {docker_memory_limit} --memory-swap -1 \
        --net=none \
        -v {grader}:/grader -v {sandbox}:/grader/input \
        {docker_image} \
        /bin/bash --login -c 'python3 grader/start.py'"""
DOCKER_COMMAND = DOCKER_COMMAND \
        .format(**{"grader": os.path.join(BASE_DIR, "grader"),
                   "sandbox": os.path.join(BASE_DIR, SANDBOX),
                   "docker_user": DOCKER_USER,
                   "nproc_soft_limit": NPROC_SOFT_LIMIT,
                   "nproc_hard_limit": NPROC_HARD_LIMIT,
                   "docker_memory_limit": DOCKER_MEMORY_LIMIT,
                   "docker_image": DOCKER_IMAGE})


DOCKER_INSPECT_COMMAND = "docker inspect -f '{state}' {container_id}"
DOCKER_LOG_COMMAND = "docker logs {container_id}"
DOCKER_CLEAR_COMMAND = "docker rm {container_id}"


def move_file(where, what):
    media = os.path.dirname(os.path.abspath(MEDIA_ROOT))

    if what.startswith('/'):
        what = what[1:]

    src = os.path.join(media, what)
    dest = os.path.join(BASE_DIR, SANDBOX, where)

    logger.info(src)
    logger.info(dest)

    shutil.copyfile(src, dest)


def save_input(where, contents):
    path = os.path.join(BASE_DIR, SANDBOX, where)

    with open(path, mode='w', encoding='utf-8') as f:
        f.write(contents)


def get_output(logs):
    decoded_output = json.loads(logs)
    returncode = decoded_output["returncode"]
    output = decoded_output["output"]

    return (returncode, output)


def wait_while_docker_finishes(container_id):
    keys = {"container_id": container_id,
            "state": "{{.State.Running}}"}
    command = DOCKER_INSPECT_COMMAND.format(**keys)

    while True:
        result = check_output(['/bin/bash', '-c', command],
                              stderr=STDOUT).decode('utf-8').strip()

        logger.info("Checking if {} has finished: {}".format(container_id, result))
        if result == 'false':
            break

        time.sleep(1)


def run_code_in_docker():
    return check_output(['/bin/bash', '-c', DOCKER_COMMAND],
                        stderr=STDOUT,
                        timeout=DOCKER_TIME_LIMIT).decode('utf-8')


def get_docker_logs(container_id):
    command = DOCKER_LOG_COMMAND.format(**{"container_id": container_id})
    logger.info(command)

    return check_output(['/bin/bash', '-c', command],
                        stderr=STDOUT).decode('utf-8')


def docker_cleanup(container_id):
    keys = {"container_id": container_id}
    command = DOCKER_CLEAR_COMMAND.format(**keys)
    check_output(['/bin/bash', '-c', command],
                 stderr=STDOUT)


def get_result_status(returncode):
    if returncode == 0:
        return 'ok'

    return 'not_ok'


@shared_task
def grade_pending_run(run_id):
    if run_id is None:
        pending_task = TestRun.objects.filter(status='pending')\
                                      .order_by('-created_at')\
                                      .first()
    else:
        pending_task = TestRun.objects.filter(pk=run_id).first()

    if pending_task is None:
        return "No tasks to run right now."

    language = pending_task.language.name.lower()

    pending_task.status = 'running'
    pending_task.save()

    extension = FILE_EXTENSIONS[language]
    solution = 'solution{}'.format(extension)
    tests = 'tests{}'.format(extension)

    if pending_task.is_plain():
        save_input(solution, pending_task.testwithplaintext.solution_code)
        save_input(tests, pending_task.testwithplaintext.test_code)

    if pending_task.is_binary():
        move_file(solution, pending_task.testwithbinaryfile.solution.url)
        move_file(tests, pending_task.testwithbinaryfile.tests.url)

    data = {
        'language': language,
        'solution': solution,
        'tests': tests
    }

    if pending_task.extra_options is not None:
        for key, value in pending_task.extra_options.items():
            data[key] = value

    save_input('data.json', json.dumps(data))

    try:
        container_id = run_code_in_docker()
        wait_while_docker_finishes(container_id)

        logs = get_docker_logs(container_id)
        logger.info(logs)
        returncode, output = get_output(logs)

        status = 'done'
    except CalledProcessError as e:
        returncode = e.returncode
        output = repr(e)
        status = 'failed'
    except TimeoutExpired as e:
        returncode = 127
        output = repr(e)
        status = 'docker_time_limit_hit'

    if container_id:
        docker_cleanup(container_id)

    pending_task.status = status
    pending_task.save()

    run_result = RunResult()
    run_result.run = pending_task
    run_result.returncode = returncode
    run_result.status = get_result_status(returncode)
    run_result.output = output
    run_result.save()

    return run_result.id
