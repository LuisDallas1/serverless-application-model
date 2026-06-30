"""
AWS::Serverless::CapacityProvider resource transformer
"""

from dataclasses import dataclass, field
from typing import Any

from samtranslator.metrics.method_decorator import cw_timer
from samtranslator.model import Resource
from samtranslator.model.capacity_provider.resources import LambdaCapacityProvider
from samtranslator.model.iam import IAMRole, IAMRolePolicies
from samtranslator.model.intrinsics import fnGetAtt
from samtranslator.model.resource_policies import ResourcePolicies
from samtranslator.model.role_utils.role_constructor import construct_role_for_resource
from samtranslator.model.tags.resource_tagging import get_tag_list
from samtranslator.translator.arn_generator import ArnGenerator


@dataclass
class _ComputeConfig:
    instance_requirements: dict = field(default_factory=dict)
    scaling_config: dict = field(default_factory=dict)


@dataclass
class _SecurityConfig:
    operator_role: Any = None
    kms_key_arn: Any = None


@dataclass
class _TagConfig:
    tags: Any = None
    managed_resource_tags: Any = None


@dataclass
class _CfnResourceConfig:
    depends_on: Any = None
    resource_attributes: Any = None
    passthrough_resource_attributes: Any = None


@dataclass
class _CapacityProviderProperties:
    capacity_provider_name: Any = None
    vpc_config: dict = field(default_factory=dict)
    compute: _ComputeConfig = field(default_factory=_ComputeConfig)
    security: _SecurityConfig = field(default_factory=_SecurityConfig)
    tags_config: _TagConfig = field(default_factory=_TagConfig)
    cfn: _CfnResourceConfig = field(default_factory=_CfnResourceConfig)


class CapacityProviderGenerator:
    """
    Generator for Lambda Capacity Provider resources
    """

    def __init__(self, logical_id: str, config: _CapacityProviderProperties | None = None) -> None:
        """
        Initialize a CapacityProviderGenerator

        :param logical_id: Logical ID of the SAM Capacity Provider resource
        :param config: Configuration parameters for the capacity provider
        """
        self.logical_id = logical_id
        self.config = config or _CapacityProviderProperties()

    @cw_timer(prefix="Generator", name="CapacityProvider")
    def to_cloudformation(self) -> list[Resource]:
        """
        Transform the capacity provider configuration to CloudFormation resources

        :returns: list of CloudFormation resources
        """
        resources: list[Resource] = []

        # Create IAM roles if not provided;
        if not self.config.security.operator_role:
            # 1. Generate one and pass arn to capacity provider resource
            operator_iam_role: IAMRole = self._create_operator_role()
            resources.append(operator_iam_role)
            # 2. Pass ARN to capacity provider resource via self.config.security.operator_role
            self.config.security.operator_role = fnGetAtt(operator_iam_role.logical_id, "Arn")

        # Create the Lambda CapacityProvider resource
        capacity_provider = self._create_capacity_provider()
        resources.append(capacity_provider)

        return resources

    def _create_capacity_provider(self) -> LambdaCapacityProvider:
        """
        Create a Lambda CapacityProvider resource
        """
        capacity_provider = LambdaCapacityProvider(
            self.logical_id, depends_on=self.config.cfn.depends_on, attributes=self.config.cfn.resource_attributes
        )

        # Set the CapacityProviderName if provided
        if self.config.capacity_provider_name:
            capacity_provider.CapacityProviderName = self.config.capacity_provider_name

        # Clean up VpcConfig to remove None values for optional fields
        if self.config.vpc_config:
            vpc_config = {"SubnetIds": self.config.vpc_config["SubnetIds"]}
            if "SecurityGroupIds" in self.config.vpc_config and self.config.vpc_config["SecurityGroupIds"] is not None:
                vpc_config["SecurityGroupIds"] = self.config.vpc_config["SecurityGroupIds"]

            capacity_provider.VpcConfig = vpc_config

        # Set the OperatorRole if provided (will be updated later if role is auto-generated)
        if self.config.security.operator_role:
            capacity_provider.PermissionsConfig = {"CapacityProviderOperatorRoleArn": self.config.security.operator_role}

        # Set the Tags - always add SAM tag, plus any user-provided tags
        capacity_provider.Tags = self._transform_tags(self.config.tags_config.tags)

        # Set the InstanceRequirements if provided
        if self.config.compute.instance_requirements:
            capacity_provider.InstanceRequirements = self._transform_instance_requirements()

        # Set the ScalingConfig if provided
        if self.config.compute.scaling_config:
            capacity_provider.CapacityProviderScalingConfig = self._transform_scaling_config()

        # Set the KmsKeyArn if provided
        if self.config.security.kms_key_arn:
            capacity_provider.KmsKeyArn = self.config.security.kms_key_arn

        # Set PropagateTags from ManagedResourceTags if provided
        if self.config.tags_config.managed_resource_tags:
            capacity_provider.PropagateTags = self._transform_managed_resource_tags()

        # Pass through resource attributes
        if self.config.cfn.passthrough_resource_attributes:
            for attr_name, attr_value in self.config.cfn.passthrough_resource_attributes.items():
                capacity_provider.set_resource_attribute(attr_name, attr_value)

        return capacity_provider

    def _ensure_permissions_config(self, capacity_provider: LambdaCapacityProvider) -> None:
        """
        Ensure that the PermissionsConfig dictionary exists on the capacity provider
        """
        # Using getattr to avoid mypy unreachable statement error
        # This is because mypy thinks PermissionsConfig can never be None based on type definitions
        if getattr(capacity_provider, "PermissionsConfig", None) is None:
            capacity_provider.PermissionsConfig = {}

    def _transform_instance_requirements(self) -> dict[str, Any]:
        """
        Transform the SAM InstanceRequirements to CloudFormation format
        """
        instance_requirements = {}

        if self.config.compute.instance_requirements.get("Architectures") is not None:
            instance_requirements["Architectures"] = self.config.compute.instance_requirements["Architectures"]

        if self.config.compute.instance_requirements.get("AllowedTypes") is not None:
            instance_requirements["AllowedInstanceTypes"] = self.config.compute.instance_requirements["AllowedTypes"]

        if self.config.compute.instance_requirements.get("ExcludedTypes") is not None:
            instance_requirements["ExcludedInstanceTypes"] = self.config.compute.instance_requirements["ExcludedTypes"]

        return instance_requirements

    def _transform_scaling_config(self) -> dict[str, Any]:
        """
        Transform the SAM ScalingConfig to CloudFormation format
        """
        scaling_config = {}

        if self.config.compute.scaling_config.get("MaxVCpuCount") is not None:
            scaling_config["MaxVCpuCount"] = self.config.compute.scaling_config["MaxVCpuCount"]

        # Handle AverageCPUUtilization structure
        if self.config.compute.scaling_config.get("AverageCPUUtilization") is not None:
            scaling_config["ScalingMode"] = "Manual"
            scaling_policies = []

            scaling_policies.append(
                {
                    "PredefinedMetricType": "LambdaCapacityProviderAverageCPUUtilization",
                    "TargetValue": self.config.compute.scaling_config["AverageCPUUtilization"],
                }
            )

            scaling_config["ScalingPolicies"] = scaling_policies
        else:
            # Default to Auto scaling mode if no AverageCPUUtilization specified
            scaling_config["ScalingMode"] = "Auto"

        return scaling_config

    def _transform_tags(self, additional_tags: dict[str, Any] | None = None) -> list[dict[str, str]]:
        """
        Helper function to generate tags with automatic SAM tag

        :param additional_tags: Optional additional tags to include
        :returns: list of tag dictionaries for CloudFormation
        """
        tags_dict = additional_tags.copy() if additional_tags else {}
        tags_dict["lambda:createdBy"] = "SAM"
        return get_tag_list(tags_dict)

    def _create_operator_role(self) -> IAMRole:
        """
        Create an IAM role for the Lambda capacity provider operator
        """

        role_logical_id = f"{self.logical_id}OperatorRole"

        # Use the IAM utility to create the assume role policy for Lambda
        assume_role_policy_document = IAMRolePolicies.lambda_assume_role_policy()

        # Create the SAM tag using the helper method
        tags = self._transform_tags()

        # Get the managed policy ARN with the correct partition
        managed_policy_arns = [ArnGenerator.generate_aws_managed_policy_arn("AWSLambdaManagedEC2ResourceOperator")]

        # Use the role constructor utility
        operator_role = construct_role_for_resource(
            resource_logical_id=self.logical_id,
            attributes=self.config.cfn.passthrough_resource_attributes,
            managed_policy_map=None,
            assume_role_policy_document=assume_role_policy_document,
            resource_policies=ResourcePolicies({}),  # Empty resource policies
            managed_policy_arns=managed_policy_arns,
            tags=tags,
        )

        # Override the logical ID to match the expected format
        operator_role.logical_id = role_logical_id

        return operator_role

    def _transform_managed_resource_tags(self) -> dict[str, Any]:
        """
        Transform SAM ManagedResourceTags to CFN PropagateTags format.
        """
        tags: dict[str, Any] = self.config.tags_config.managed_resource_tags or {}

        if "Tags" in tags:
            return {"Mode": "Explicit", "ExplicitTags": get_tag_list(tags["Tags"])}

        if "Propagate" in tags:
            return {"Mode": "CapacityProvider" if tags["Propagate"] else "None"}

        return {}
