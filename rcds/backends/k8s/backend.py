import itertools
from pathlib import Path
from typing import Any, Dict, List

import yaml
from jinja2 import Environment, Template
from kubernetes import config  # type: ignore

import rcds
import rcds.backend
from rcds.util import load_any
from rcds.util.jsonschema import DefaultValidatingDraft7Validator

from .jinja import jinja_env
from .manifests import AnyManifest, sync_manifests

options_schema_validator = DefaultValidatingDraft7Validator(
    schema=load_any(Path(__file__).parent / "options.schema.yaml")
)


class ContainerBackend(rcds.backend.BackendContainerRuntime):
    _project: rcds.Project
    _options: Dict[str, Any]
    _namespace_template: Template
    _jinja_env: Environment

    def __init__(self, project: rcds.Project, options: Dict[str, Any]):
        self._project = project
        self._options = options

        # FIXME: validate options better
        if not options_schema_validator.is_valid(self._options):
            raise ValueError("Invalid options")

        self._namespace_template = Template(self._options["namespaceTemplate"])
        self._jinja_env = jinja_env.overlay()
        self._jinja_env.globals["options"] = self._options

        config.load_kube_config(context=self._options.get("kubeContext", None))

    def commit(self, delete: bool) -> bool:
        deployed_challs = filter(
            lambda c: c.config["deployed"], self._project.challenges.values()
        )
        # TODO: auto assignment of expose params
        manifests = list(
            itertools.chain.from_iterable(
                map(
                    lambda chall: self.gen_manifests_for_challenge(chall),
                    deployed_challs,
                )
            )
        )
        sync_manifests(manifests, delete)
        return True

    def get_namespace_for_challenge(self, challenge: rcds.Challenge) -> str:
        return self._namespace_template.render({"challenge": challenge.config})

    def gen_manifests_for_challenge(
        self, challenge: rcds.Challenge
    ) -> List[AnyManifest]:
        if "containers" not in challenge.config:
            return []

        manifests: List[Dict[str, Any]] = []

        def render_and_append(env: Environment, template: str) -> None:
            nonlocal manifests
            manifest = env.get_template(template).render().strip()
            manifests += filter(lambda x: x is not None, yaml.safe_load_all(manifest))

        challenge_env: Environment = self._jinja_env.overlay()
        challenge_env.globals["challenge"] = challenge.config
        challenge_env.globals["namespace"] = self.get_namespace_for_challenge(challenge)
        challenge_env.globals["hidden"] = (
            "false" if challenge.config["visible"] else "true"
        )

        instance_per_team = challenge.config["instancePerTeam"]

        render_and_append(challenge_env, "namespace.yaml")

        if instance_per_team:
            exposed_services = challenge.config.get("expose", dict())
            assert (
                len(exposed_services) == 1
            ), "Only one port should be exposed in instance-per-team mode"

            exposed_name, exposed_cfgs = list(exposed_services.items())[0]
            assert (
                len(exposed_cfgs) == 1
            ), "Only one port should be exposed in instance-per-team mode"
            exposed_cfg = exposed_cfgs[0]
            exposed_kind = "http" if "http" in exposed_cfg else "tcp"

            challenge_env.globals["exposed"] = {
                "name": exposed_name,
                "config": exposed_cfg,
                "kind": exposed_kind,
            }

            render_and_append(challenge_env, "challenge.yaml")

        else:

            render_and_append(challenge_env, "network-policy.yaml")

            for container_name, container_config in challenge.config[
                "containers"
            ].items():
                expose_config = challenge.config.get("expose", dict()).get(
                    container_name, None
                )

                container_env: Environment = challenge_env.overlay()
                container_env.globals["container"] = {
                    "name": container_name,
                    "config": container_config,
                }
                if expose_config is not None:
                    container_env.globals["container"]["expose"] = expose_config

                render_and_append(container_env, "deployment.yaml")
                render_and_append(container_env, "service.yaml")
                render_and_append(container_env, "ingressroute.yaml")
                render_and_append(container_env, "ingressroutetcp.yaml")

        return manifests


class BackendsInfo(rcds.backend.BackendsInfo):
    HAS_CONTAINER_RUNTIME = True

    def get_container_runtime(
        self, project: rcds.Project, options: Dict[str, Any]
    ) -> ContainerBackend:
        return ContainerBackend(project, options)


def get_info() -> BackendsInfo:
    return BackendsInfo()
