# Copyright The Cloud Custodian Authors.
# SPDX-License-Identifier: Apache-2.0
import copy
import logging
import json
import os

from c7n.mu import CloudWatchEventSource, LambdaFunction, LambdaManager, PythonPackageArchive, SQSSubscription


log = logging.getLogger("custodian-mailer")

entry_source = """\
import logging

from c7n_mailer import handle

logger = logging.getLogger('custodian.mailer')
log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
logging.basicConfig(level=logging.INFO, format=log_format)
logging.getLogger('botocore').setLevel(logging.WARNING)

def dispatch(event, context):
    # TODO: send event to function
    return handle.start_c7n_mailer(logger)
"""

CORE_DEPS = [
    # core deps
    "jinja2",
    "markupsafe",
    "yaml",
    "ldap3",
    "pyasn1",
    "redis",
    "jmespath",
    # for other dependencies
    "pkg_resources",
    # transport datadog - recursive deps
    "datadog",
    "decorator",
    # requests (recursive deps), needed by datadog, slackclient, splunk
    "requests",
    "urllib3",
    "idna",
    "charset_normalizer",
    "certifi",
    # used by splunk mailer transport
    "jsonpointer",
    "jsonpatch",
    # sendgrid dependencies
    "sendgrid",
    "python_http_client",
    "ellipticcurve",
]


def get_archive(config):
    deps = ["c7n_mailer"] + list(CORE_DEPS)
    archive = PythonPackageArchive(modules=deps)

    for d in set(config["templates_folders"]):
        if not os.path.exists(d):
            continue
        for t in [f for f in os.listdir(d) if os.path.splitext(f)[1] == ".j2"]:
            with open(os.path.join(d, t)) as fh:
                archive.add_contents("msg-templates/%s" % t, fh.read())

    function_config = copy.deepcopy(config)
    function_config["templates_folders"] = ["msg-templates/"]
    archive.add_contents("config.json", json.dumps(function_config))
    archive.add_contents("periodic.py", entry_source)

    archive.close()
    return archive


def provision(config, session_factory):
    events = []
    if config.get("lambda_trigger", "periodic") == 'periodic':
        events.append(CloudWatchEventSource(
            {"type": "periodic", "schedule": config.get("lambda_schedule", "rate(5 minutes)")},
            session_factory,
        ))
    elif config.get("lambda_trigger", "periodic") == 'sqs':
        events.append(SQSSubscription(
            session_factory,
            queue_arns=[config.get("queue_url")],
            # TODO: batch size
        ))

    # SQSSubscription
    func_config = dict(
        name=config.get("lambda_name", "cloud-custodian-mailer"),
        description=config.get("lambda_description", "Cloud Custodian Mailer"),
        tags=config.get("lambda_tags", {}),
        handler="periodic.dispatch",
        runtime=config["runtime"],
        memory_size=config["memory"],
        timeout=config["timeout"],
        role=config["role"],
        subnets=config["subnets"],
        security_groups=config["security_groups"],
        dead_letter_config=config.get("dead_letter_config", {}),
        events=events,
    )

    archive = get_archive(config)
    func = LambdaFunction(func_config, archive)
    log.info("Provisioning mailer lambda %s" % (session_factory().region_name))
    manager = LambdaManager(session_factory)
    manager.publish(func)
