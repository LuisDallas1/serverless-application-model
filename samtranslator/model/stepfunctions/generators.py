import json
from dataclasses import dataclass
from copy import deepcopy
from typing import Any

from samtranslator.metrics.method_decorator import cw_timer
from samtranslator.model.exceptions import InvalidEventException, InvalidResourceException
from samtranslator.model.iam import IAMRole, IAMRolePolicies
from samtranslator.model.intrinsics import fnJoin, is_intrinsic
from samtranslator.model.resource_policies import ResourcePolicies
from samtranslator.model.role_utils import construct_role_for_resource
from samtranslator.model.s3_utils.uri_parser import parse_s3_uri
from samtranslator.model.stepfunctions.resources import (
    StepFunctionsStateMachine,
    StepFunctionsStateMachineAlias,
    StepFunctionsStateMachineVersion,
)
from samtranslator.model.tags.resource_tagging import get_tag_list
from samtranslator.model.xray_utils import get_xray_managed_policy_name
from samtranslator.utils.cfn_dynamic_references import is_dynamic_reference


@dataclass
class StateMachineConfig:
    logical_id: Any = None
    depends_on: Any = None
    managed_policy_map: Any = None
    intrinsics_resolver: Any = None
    definition: Any = None
    definition_uri: Any = None
    logging: Any = None
    name: Any = None
    policies: Any = None
    permissions_boundary: Any = None
    definition_substitutions: Any = None
    role: Any = None
    role_path: Any = None
    state_machine_type: Any = None
    tracing: Any = None
    events: Any = None
    event_resources: Any = None
    event_resolver: Any = None
    tags: Any = None
    resource_attributes: Any = None
    passthrough_resource_attributes: Any = None
    get_managed_policy_map: Any = None
    auto_publish_alias: Any = None
    deployment_preference: Any = None
    use_alias_as_event_target: Any = None


class StateMachineGenerator:
    _SAM_KEY = "stateMachine:createdBy"
    _SAM_VALUE = "SAM"
    _SUBSTITUTION_NAME_TEMPLATE = "definition_substitution_%s"
    _SUBSTITUTION_KEY_TEMPLATE = "${definition_substitution_%s}"
    SFN_INVALID_PROPERTY_BOTH_ROLE_POLICY = (
        "Specify either 'Role' or 'Policies' (but not both at the same time) or neither of them"
    )

    def __init__(self, config: StateMachineConfig) -> None:
        """
        Constructs an State Machine Generator class that generates a State Machine resource

        :param config: Configuration parameters for the State Machine resource
        """
        self.config = config
        self.state_machine = StepFunctionsStateMachine(
            config.logical_id, depends_on=config.depends_on, attributes=config.resource_attributes
        )
        self.substitution_counter = 1

    @cw_timer(prefix="Generator", name="StateMachine")
    def to_cloudformation(self):  # type: ignore[no-untyped-def]
        """
        Constructs and returns the State Machine resource and any additional resources associated with it.

        :returns: a list of resources including the State Machine resource.
        :rtype: list
        """
        resources: list[Any] = [self.state_machine]

        if self.config.definition_substitutions:
            self.state_machine.DefinitionSubstitutions = self.config.definition_substitutions

        if self.config.definition and self.config.definition_uri:
            raise InvalidResourceException(
                self.config.logical_id, "Specify either 'Definition' or 'DefinitionUri' property and not both."
            )
        if self.config.definition:
            processed_definition = deepcopy(self.config.definition)
            substitutions = self._replace_dynamic_values_with_substitutions(processed_definition)
            if len(substitutions) > 0:
                if self.state_machine.DefinitionSubstitutions:
                    self.state_machine.DefinitionSubstitutions.update(substitutions)
                else:
                    self.state_machine.DefinitionSubstitutions = substitutions
            self.state_machine.DefinitionString = self._build_definition_string(processed_definition)
        elif self.config.definition_uri:
            self.state_machine.DefinitionS3Location = self._construct_definition_uri()
        else:
            raise InvalidResourceException(
                self.config.logical_id, "Either 'Definition' or 'DefinitionUri' property must be specified."
            )

        if self.config.role and self.config.policies:
            raise InvalidResourceException(self.config.logical_id, self.SFN_INVALID_PROPERTY_BOTH_ROLE_POLICY)
        if self.config.role:
            self.state_machine.RoleArn = self.config.role
        else:
            if not self.config.policies:
                self.config.policies = []
            execution_role = self._construct_role()
            self.state_machine.RoleArn = execution_role.get_runtime_attr("arn")
            resources.append(execution_role)

        self.state_machine.StateMachineName = self.config.name
        self.state_machine.StateMachineType = self.config.state_machine_type
        self.state_machine.LoggingConfiguration = self.config.logging
        self.state_machine.TracingConfiguration = self.config.tracing
        self.state_machine.Tags = self._construct_tag_list()

        managed_traffic_shifting_resources = self._generate_managed_traffic_shifting_resources()
        resources.extend(managed_traffic_shifting_resources)

        event_resources = self._generate_event_resources()
        resources.extend(event_resources)

        return resources

    def _construct_definition_uri(self) -> dict[str, Any]:
        if isinstance(self.config.definition_uri, dict):
            if not self.config.definition_uri.get("Bucket", None) or not self.config.definition_uri.get("Key", None):
                raise InvalidResourceException(
                    self.config.logical_id, "'DefinitionUri' requires Bucket and Key properties to be specified."
                )
            s3_pointer = self.config.definition_uri
        else:
            parsed_s3_pointer = parse_s3_uri(self.config.definition_uri)
            if parsed_s3_pointer is None:
                raise InvalidResourceException(
                    self.config.logical_id,
                    "'DefinitionUri' is not a valid S3 Uri of the form "
                    "'s3://bucket/key' with optional versionId query parameter.",
                )
            s3_pointer = parsed_s3_pointer

        definition_s3 = {"Bucket": s3_pointer["Bucket"], "Key": s3_pointer["Key"]}
        if "Version" in s3_pointer:
            definition_s3["Version"] = s3_pointer["Version"]
        return definition_s3

    def _build_definition_string(self, definition_dict):  # type: ignore[no-untyped-def]
        definition_lines = json.dumps(definition_dict, sort_keys=True, indent=4, separators=(",", ": ")).split("\n")
        return fnJoin("\n", definition_lines)

    def _construct_role(self) -> IAMRole:
        policies = self.config.policies[:]
        if self.config.tracing and self.config.tracing.get("Enabled") is True:
            policies.append(get_xray_managed_policy_name())

        state_machine_policies = ResourcePolicies(
            {"Policies": policies},
            policy_template_processor=None,
        )

        return construct_role_for_resource(
            resource_logical_id=self.config.logical_id,
            role_path=self.config.role_path,
            attributes=self.config.passthrough_resource_attributes,
            managed_policy_map=self.config.managed_policy_map,
            assume_role_policy_document=IAMRolePolicies.stepfunctions_assume_role_policy(),
            resource_policies=state_machine_policies,
            tags=self._construct_tag_list(),
            permissions_boundary=self.config.permissions_boundary,
            get_managed_policy_map=self.config.get_managed_policy_map,
        )

    def _construct_tag_list(self) -> list[dict[str, Any]]:
        sam_tag = {self._SAM_KEY: self._SAM_VALUE}
        return get_tag_list(sam_tag) + get_tag_list(self.config.tags)

    def _construct_version(self) -> StepFunctionsStateMachineVersion:
        logical_id = f"{self.config.logical_id}Version"
        attributes = self.config.passthrough_resource_attributes.copy()

        if "DeletionPolicy" not in attributes:
            attributes["DeletionPolicy"] = "Retain"
        if "UpdateReplacePolicy" not in attributes:
            attributes["UpdateReplacePolicy"] = "Retain"

        state_machine_version = StepFunctionsStateMachineVersion(logical_id=logical_id, attributes=attributes)
        state_machine_version.StateMachineArn = self.state_machine.get_runtime_attr("arn")
        state_machine_version.StateMachineRevisionId = self.state_machine.get_runtime_attr("state_machine_revision_id")

        return state_machine_version

    def _construct_alias(self, version: StepFunctionsStateMachineVersion) -> StepFunctionsStateMachineAlias:
        logical_id = f"{self.config.logical_id}Alias{self.config.auto_publish_alias}"
        attributes = self.config.passthrough_resource_attributes

        state_machine_alias = StepFunctionsStateMachineAlias(logical_id=logical_id, attributes=attributes)
        state_machine_alias.Name = self.config.auto_publish_alias

        state_machine_version_arn = version.get_runtime_attr("arn")

        deployment_preference = {}
        if self.config.deployment_preference:
            deployment_preference = self.config.deployment_preference
        else:
            deployment_preference["Type"] = "ALL_AT_ONCE"

        deployment_preference["StateMachineVersionArn"] = state_machine_version_arn
        state_machine_alias.DeploymentPreference = deployment_preference

        self.state_machine_alias = state_machine_alias

        return state_machine_alias

    def _generate_managed_traffic_shifting_resources(
        self,
    ) -> list[Any]:
        if not self.config.auto_publish_alias and self.config.use_alias_as_event_target:
            raise InvalidResourceException(
                self.config.logical_id, "'UseAliasAsEventTarget' requires 'AutoPublishAlias' property to be specified."
            )
        if not self.config.auto_publish_alias and not self.config.deployment_preference:
            return []
        if not self.config.auto_publish_alias and self.config.deployment_preference:
            raise InvalidResourceException(
                self.config.logical_id, "'DeploymentPreference' requires 'AutoPublishAlias' property to be specified."
            )

        state_machine_version = self._construct_version()
        return [state_machine_version, self._construct_alias(state_machine_version)]

    def _generate_event_resources(self) -> list[dict[str, Any]]:
        resources = []
        if self.config.events:
            for logical_id, event_dict in self.config.events.items():
                kwargs = {
                    "intrinsics_resolver": self.config.intrinsics_resolver,
                    "permissions_boundary": self.config.permissions_boundary,
                }
                try:
                    eventsource = self.config.event_resolver.resolve_resource_type(event_dict).from_dict(
                        self.state_machine.logical_id + logical_id, event_dict, logical_id
                    )
                    for name, resource in self.config.event_resources[logical_id].items():
                        kwargs[name] = resource
                except (TypeError, AttributeError) as e:
                    raise InvalidEventException(logical_id, str(e)) from e
                target_resource = (
                    (self.state_machine_alias or self.state_machine)
                    if self.config.use_alias_as_event_target
                    else self.state_machine
                )
                resources += eventsource.to_cloudformation(resource=target_resource, **kwargs)

        return resources

    def _replace_dynamic_values_with_substitutions(self, _input):  # type: ignore[no-untyped-def]
        substitution_map = {}
        for path in self._get_paths_to_intrinsics(_input):  # type: ignore[no-untyped-call]
            location = _input
            for step in path[:-1]:
                location = location[step]
            sub_name, sub_key = self._generate_substitution()
            substitution_map[sub_name] = location[path[-1]]
            location[path[-1]] = sub_key
        return substitution_map

    def _get_paths_to_intrinsics(self, _input, path=None):  # type: ignore[no-untyped-def]
        if path is None:
            path = []
        dynamic_value_paths = []  # type: ignore[var-annotated]
        if isinstance(_input, dict):
            iterator = _input.items()
        elif isinstance(_input, list):
            iterator = enumerate(_input)  # type: ignore[assignment]
        else:
            return dynamic_value_paths

        for key, value in sorted(iterator, key=lambda item: item[0]):
            if is_intrinsic(value) or is_dynamic_reference(value):
                dynamic_value_paths.append([*path, key])
            elif isinstance(value, (dict, list)):
                dynamic_value_paths.extend(self._get_paths_to_intrinsics(value, [*path, key]))  # type: ignore[no-untyped-call]

        return dynamic_value_paths

    def _generate_substitution(self) -> tuple[str, str]:
        substitution_name = self._SUBSTITUTION_NAME_TEMPLATE % self.substitution_counter
        substitution_key = self._SUBSTITUTION_KEY_TEMPLATE % self.substitution_counter
        self.substitution_counter += 1
        return substitution_name, substitution_key
