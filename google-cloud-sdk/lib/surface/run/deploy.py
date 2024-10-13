# -*- coding: utf-8 -*- #
# Copyright 2018 Google LLC. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Deploy a container to Cloud Run."""

import enum
import json
import os.path

from googlecloudsdk.api_lib.run import api_enabler
from googlecloudsdk.api_lib.run import k8s_object
from googlecloudsdk.api_lib.run import service as service_lib
from googlecloudsdk.api_lib.run import traffic
from googlecloudsdk.calliope import base
from googlecloudsdk.calliope import exceptions as c_exceptions
from googlecloudsdk.command_lib.artifacts import docker_util
from googlecloudsdk.command_lib.run import artifact_registry
from googlecloudsdk.command_lib.run import config_changes
from googlecloudsdk.command_lib.run import connection_context
from googlecloudsdk.command_lib.run import container_parser
from googlecloudsdk.command_lib.run import exceptions
from googlecloudsdk.command_lib.run import flags
from googlecloudsdk.command_lib.run import messages_util
from googlecloudsdk.command_lib.run import platforms
from googlecloudsdk.command_lib.run import pretty_print
from googlecloudsdk.command_lib.run import resource_args
from googlecloudsdk.command_lib.run import resource_change_validators
from googlecloudsdk.command_lib.run import serverless_operations
from googlecloudsdk.command_lib.run import stages
from googlecloudsdk.command_lib.run.integrations import run_apps_operations
from googlecloudsdk.command_lib.util.args import map_util
from googlecloudsdk.command_lib.util.concepts import concept_parsers
from googlecloudsdk.command_lib.util.concepts import presentation_specs
from googlecloudsdk.core import properties
from googlecloudsdk.core.console import console_io
from googlecloudsdk.core.console import progress_tracker


class BuildType(enum.Enum):
  DOCKERFILE = 'Dockerfile'
  BUILDPACKS = 'Buildpacks'


def ContainerArgGroup(release_track=base.ReleaseTrack.GA):
  """Returns an argument group with all per-container deploy args."""

  help_text = """
Container Flags

  The following flags apply to a single container. If the --container flag is specified these flags may only be
  specified after a --container flag. Otherwise they will apply to the primary ingress container.
"""
  group = base.ArgumentGroup(help=help_text)
  group.AddArgument(flags.SourceAndImageFlags())
  group.AddArgument(flags.PortArg())
  group.AddArgument(flags.Http2Flag())
  group.AddArgument(flags.MutexEnvVarsFlags())
  group.AddArgument(flags.MemoryFlag())
  group.AddArgument(flags.CpuFlag())
  group.AddArgument(flags.ArgsFlag())
  group.AddArgument(flags.SecretsFlags())
  group.AddArgument(flags.DependsOnFlag())
  group.AddArgument(flags.AddVolumeMountFlag())
  group.AddArgument(flags.RemoveVolumeMountFlag())
  group.AddArgument(flags.ClearVolumeMountsFlag())

  if release_track in [base.ReleaseTrack.ALPHA, base.ReleaseTrack.BETA]:
    group.AddArgument(flags.GpuFlag(hidden=False))
    group.AddArgument(flags.AddCommandAndFunctionFlag())
    group.AddArgument(flags.BaseImageArg())
    group.AddArgument(flags.AutomaticUpdatesFlag())
    group.AddArgument(flags.BuildServiceAccountMutexGroup())
    group.AddArgument(flags.BuildWorkerPoolMutexGroup())
    group.AddArgument(flags.MutexBuildEnvVarsFlags())
  else:
    group.AddArgument(flags.CommandFlag())
  if release_track == base.ReleaseTrack.ALPHA:
    group.AddArgument(flags.StartupProbeFlag())
    group.AddArgument(flags.LivenessProbeFlag())

  return group


@base.UniverseCompatible
@base.ReleaseTracks(base.ReleaseTrack.GA)
class Deploy(base.Command):
  """Create or update a Cloud Run service."""

  detailed_help = {
      'DESCRIPTION': """\
          Creates or updates a Cloud Run service.
          """,
      'EXAMPLES': """\
          To deploy a container to the service `my-backend` on Cloud Run:

              $ {command} my-backend --image=us-docker.pkg.dev/project/image

          You may also omit the service name. Then a prompt will be displayed
          with a suggested default value:

              $ {command} --image=us-docker.pkg.dev/project/image

          To deploy to Cloud Run on Kubernetes Engine, you need to specify a cluster:

              $ {command} --image=us-docker.pkg.dev/project/image --cluster=my-cluster
          """,
  }

  @classmethod
  def CommonArgs(cls, parser):
    # Flags specific to managed CR
    managed_group = flags.GetManagedArgGroup(parser)
    flags.AddAllowUnauthenticatedFlag(managed_group)
    flags.AddBinAuthzPolicyFlags(managed_group)
    flags.AddBinAuthzBreakglassFlag(managed_group)
    flags.AddCloudSQLFlags(managed_group)
    flags.AddCmekKeyFlag(managed_group)
    flags.AddCmekKeyRevocationActionTypeFlag(managed_group)
    flags.AddCpuThrottlingFlag(managed_group)
    flags.AddCustomAudiencesFlag(managed_group)
    flags.AddDescriptionFlag(managed_group)
    flags.AddEgressSettingsFlag(managed_group)
    flags.AddEncryptionKeyShutdownHoursFlag(managed_group)
    flags.AddRevisionSuffixArg(managed_group)
    flags.AddSandboxArg(managed_group)
    flags.AddSessionAffinityFlag(managed_group)
    flags.AddStartupCpuBoostFlag(managed_group)
    flags.AddVpcConnectorArgs(managed_group)
    flags.AddVpcNetworkGroupFlagsForUpdate(managed_group)
    flags.RemoveContainersFlag().AddToParser(managed_group)
    flags.AddVolumesFlags(managed_group, cls.ReleaseTrack())
    flags.AddServiceMinInstancesFlag(managed_group)
    flags.AddInvokerIamCheckFlag(managed_group)

    # Flags specific to connecting to a cluster
    cluster_group = flags.GetClusterArgGroup(parser)
    flags.AddEndpointVisibilityEnum(cluster_group)
    flags.AddConfigMapsFlags(cluster_group)

    # Flags not specific to any platform
    service_presentation = presentation_specs.ResourcePresentationSpec(
        'SERVICE',
        resource_args.GetServiceResourceSpec(prompt=True),
        'Service to deploy to.',
        required=True,
        prefixes=False,
    )
    flags.AddPlatformAndLocationFlags(parser)
    flags.AddConcurrencyFlag(parser)
    flags.AddTimeoutFlag(parser)
    flags.AddAsyncFlag(parser)
    flags.AddLabelsFlags(parser)
    flags.AddGeneralAnnotationFlags(parser)
    flags.AddMinInstancesFlag(parser)
    flags.AddMaxInstancesFlag(parser)
    flags.AddNoTrafficFlag(parser)
    flags.AddDeployTagFlag(parser)
    flags.AddServiceAccountFlag(parser)
    flags.AddClientNameAndVersionFlags(parser)
    flags.AddIngressFlag(parser)
    concept_parsers.ConceptParser([service_presentation]).AddToParser(parser)
    # No output by default, can be overridden by --format
    parser.display_info.AddFormat('none')

  @classmethod
  def Args(cls, parser):
    cls.CommonArgs(parser)
    container_args = ContainerArgGroup(cls.ReleaseTrack())
    container_parser.AddContainerFlags(parser, container_args)

  def _GetAllowUnauthRegions(self, args):
    """Returns regions to operate on SetIamPolicy for multi-region Services.

    Args:
      args: argparse.Namespace, Command line arguments

    Returns:
      None for single-region services, or a list of regions for multi-region
      services.
    """
    return None

  def GetAllowUnauth(self, args, operations, service_ref, service_exists):
    """Returns allow_unauth value for a service change.

    Args:
      args: argparse.Namespace, Command line arguments
      operations: serverless_operations.ServerlessOperations, Serverless client.
      service_ref: protorpc.messages.Message, A resource reference object for
        the service See googlecloudsdk.core.resources.Registry.ParseResourceId
        for details.
      service_exists: True if the service being changed already exists.

    Returns:
      allow_unauth value where
      True means to enable unauthenticated access for the service.
      False means to disable unauthenticated access for the service.
      None means to retain the current value for the service.
    """
    allow_unauth = None
    if platforms.GetPlatform() == platforms.PLATFORM_MANAGED:
      allow_unauth = flags.GetAllowUnauthenticated(
          args, operations, service_ref, not service_exists
      )
      # Avoid failure removing a policy binding for a service that
      # doesn't exist.
      if not service_exists and not allow_unauth:
        allow_unauth = None
    return allow_unauth

  def _ValidateAndGetContainers(self, args):
    if flags.FlagIsExplicitlySet(args, 'containers'):
      containers = args.containers
    else:
      containers = {'': args}

    if len(containers) > 1:
      ingress_containers = [
          c
          for c in containers.values()
          if c.IsSpecified('port') or c.IsSpecified('use_http2')
      ]
      if len(ingress_containers) != 1:
        raise c_exceptions.InvalidArgumentException(
            '--container',
            'Exactly one container must specify --port or --use-http2',
        )

    if len(containers) > 10:
      raise c_exceptions.InvalidArgumentException(
          '--container', 'Services may include at most 10 containers'
      )
    return containers

  def _ValidateNoAutomaticUpdatesForContainers(
      self, build_from_source, containers
  ):
    pass

  def _ValidateAndGetBuildFromSource(self, containers):
    build_from_source = {
        name: container
        for name, container in containers.items()
        if not container.IsSpecified('image')
    }
    if len(build_from_source) > 1:
      needs_image = [
          name
          for name, container in build_from_source.items()
          if not flags.FlagIsExplicitlySet(container, 'source')
      ]
      if needs_image:
        raise exceptions.RequiredImageArgumentException(needs_image)
      raise c_exceptions.InvalidArgumentException(
          '--container', 'At most one container can be deployed from source.'
      )
    self._ValidateNoAutomaticUpdatesForContainers(build_from_source, containers)
    for name, container in build_from_source.items():
      if not flags.FlagIsExplicitlySet(container, 'source'):
        if console_io.CanPrompt():
          container.source = flags.PromptForDefaultSource(name)
        else:
          if name:
            message = (
                'Container {} requires a container image to deploy (e.g.'
                ' `gcr.io/cloudrun/hello:latest`) if no build source is'
                ' provided.'.format(name)
            )
          else:
            message = (
                'Requires a container image to deploy (e.g.'
                ' `gcr.io/cloudrun/hello:latest`) if no build source is'
                ' provided.'
            )
          raise c_exceptions.RequiredArgumentException(
              '--image',
              message,
          )
    return build_from_source

  def _GetBaseImageForSourceContainer(self, container_args, service):
    """Returns the base image for the container.

    Args:
      container_args: command line arguments for container that is build from
        source
      service: existing Cloud run service which could be None.

    Returns:
      base_image: string. Base image of the container.
    """

    base_image = getattr(container_args, 'base_image', None)
    clear_base_image = getattr(container_args, 'clear_base_image', None)

    if base_image:
      return base_image
    # If service exists, check existing base_image annotation and populate
    # the value if --clear-base-image is not set
    if (
        base_image is None
        and service is not None
        and service_lib.RUN_FUNCTIONS_BUILD_BASE_IMAGE in service.annotations
        and not clear_base_image
    ):
      return service.annotations[service_lib.RUN_FUNCTIONS_BUILD_BASE_IMAGE]
    return service_lib.DEFAULT_BASE_IMAGE

  def _GetAutomaticUpdates(self, container_args, service):
    """Returns the automatic updates for the container."""
    automatic_updates = getattr(container_args, 'automatic_updates', None)
    clear_base_image = getattr(container_args, 'clear_base_image', None)
    base_image = self._GetBaseImageForSourceContainer(container_args, service)
    if automatic_updates is not None:
      return automatic_updates
    if clear_base_image:
      # Clear base image will disable automatic updates.
      return False
    if service is None:
      # When it's new service, when users provide
      # --base-image, it implies automatic updates = True.
      return base_image != service_lib.DEFAULT_BASE_IMAGE
    # TODO(b/365567914): Delete elif statement once the new annotations are in.
    if (
        service_lib.RUN_FUNCTIONS_BUILD_ENABLE_AUTOMATIC_UPDATES
        in service.annotations
    ):
      # When there is existing service, use the annotation value.
      # Because the annotation is sticky. Users need
      # to explicitly set --automatic-updates to change it.
      automatic_updates_annotation = service.annotations[
          service_lib.RUN_FUNCTIONS_BUILD_ENABLE_AUTOMATIC_UPDATES
      ]
      return True if automatic_updates_annotation.lower() == 'true' else False
    elif (
        service_lib.RUN_FUNCTIONS_ENABLE_AUTOMATIC_UPDATES_DEPRECATED
        in service.annotations
    ):
      automatic_updates_annotation = service.annotations[
          service_lib.RUN_FUNCTIONS_ENABLE_AUTOMATIC_UPDATES_DEPRECATED
      ]
      return True if automatic_updates_annotation.lower() == 'true' else False
    return automatic_updates

  def _BuildFromSource(
      self,
      args,
      build_from_source,
      service_ref,
      conn_context,
      platform,
      already_activated_services,
      service,
  ):
    # Get run functions annotations to make values sticky
    (
        annotated_build_service_account,
        annotated_build_worker_pool,
        annotated_build_env_vars,
    ) = (
        service.run_functions_annotations if service else (None, None, None)
    )

    # Only one container can deployed from source
    name, container_args = next(iter(build_from_source.items()))
    # If service exists and it's GCF's worker container, use the name.
    if not name and service:
      name = service.template.container.name or ''
    pack = None
    changes = []
    repo_to_create = None
    source = container_args.source
    # We cannot use flag.isExplicitlySet(args, 'function') because it will
    # return False when user provide --function after --container.
    is_function = (
        self.ReleaseTrack() != base.ReleaseTrack.GA and container_args.function
    )

    ar_repo = docker_util.DockerRepo(
        project_id=properties.VALUES.core.project.Get(required=True),
        location_id=artifact_registry.RepoRegion(
            args,
            cluster_location=(
                conn_context.cluster_location
                if platform == platforms.PLATFORM_GKE
                else None
            ),
        ),
        repo_id='cloud-run-source-deploy',
    )
    if artifact_registry.ShouldCreateRepository(
        ar_repo, skip_activation_prompt=already_activated_services
    ):
      repo_to_create = ar_repo
    # The image is built with latest tag. After build, the image digest
    # from the build result will be added to the image of the service spec.
    container_args.image = '{repo}/{service}'.format(
        repo=ar_repo.GetDockerString(), service=service_ref.servicesId
    )
    # Use GCP Buildpacks if Dockerfile doesn't exist
    docker_file = source + '/Dockerfile'
    base_image = self._GetBaseImageForSourceContainer(container_args, service)
    automatic_updates = self._GetAutomaticUpdates(container_args, service)
    if os.path.exists(docker_file):
      build_type = BuildType.DOCKERFILE
      # TODO(b/310727875): check --function is not provided
      # Check whether base_image is provided by user
      if flags.FlagIsExplicitlySet(container_args, 'base_image'):
        raise c_exceptions.InvalidArgumentException(
            '--base-image',
            'Base image is not supported for services built from Dockerfile.',
        )
      # Base image is sticky annotation.
      # Check whether there was base_image provided in previous revision
      # even if users are not providing it in this deployment.
      if base_image != service_lib.DEFAULT_BASE_IMAGE:
        raise c_exceptions.RequiredArgumentException(
            '--clear-base-image',
            'Base image is not supported for services built from Dockerfile. To'
            ' continue the deployment, please use --clear-base-image to clear'
            ' the base image.',
        )
    else:
      pack, changes = _CreateBuildPack(container_args, self.ReleaseTrack())
      build_type = BuildType.BUILDPACKS
    image = None if pack else container_args.image

    if flags.FlagIsExplicitlySet(args, 'delegate_builds') or (
        base_image is not None and base_image != service_lib.DEFAULT_BASE_IMAGE
    ):
      image = pack[0].get('image') if pack else image
    build_service_account = _GetBuildServiceAccount(
        args, annotated_build_service_account, container_args, service, changes
    )
    operation_message = (
        'Building using {build_type} and deploying container to'
    ).format(build_type=build_type.value)

    build_worker_pool = _GetBuildWorkerPool(
        args, annotated_build_worker_pool, service, changes
    )
    old_build_env_vars = (
        json.loads(annotated_build_env_vars)
        if annotated_build_env_vars
        else None
    )
    build_env_var_flags = map_util.GetMapFlagsFromArgs('build-env-vars', args)
    build_env_vars = map_util.ApplyMapFlags(
        old_build_env_vars, **build_env_var_flags
    )
    return (
        is_function,
        image,
        pack,
        source,
        operation_message,
        repo_to_create,
        base_image,
        build_service_account,
        changes,
        name,
        build_worker_pool,
        build_env_vars,
        automatic_updates,
    )

  def _GetBaseChanges(self, args):
    """Returns the service config changes with some default settings."""
    changes = flags.GetServiceConfigurationChanges(args, self.ReleaseTrack())
    changes.insert(
        0,
        config_changes.DeleteAnnotationChange(
            k8s_object.BINAUTHZ_BREAKGLASS_ANNOTATION
        ),
    )
    changes.append(
        config_changes.SetLaunchStageAnnotationChange(self.ReleaseTrack())
    )
    return changes

  def _ConnectionContext(self, args):
    # Obtaining the connection context prompts the user to select a region if
    # one hasn't been provided. We want to do this prior to preparing a source
    # deploy so that we can use that region for the Artifact Registry repo.
    return connection_context.GetConnectionContext(
        args,
        flags.Product.RUN,
        self.ReleaseTrack(),
    )

  def _GetTracker(
      self,
      args,
      service,
      changes,
      build_from_source,
      repo_to_create,
      allow_unauth,
      has_latest,
  ):
    deployment_stages = stages.ServiceStages(
        include_iam_policy_set=allow_unauth is not None,
        include_route=has_latest,
        include_build=bool(build_from_source),
        include_create_repo=repo_to_create is not None,
    )
    if build_from_source:
      header = 'Building and deploying'
    else:
      header = 'Deploying'
    if service is None:
      header += ' new service'
      # new services default cpu boost on the client
      if not flags.FlagIsExplicitlySet(args, 'cpu_boost'):
        changes.append(config_changes.StartupCpuBoostChange(cpu_boost=True))
    header += '...'
    return progress_tracker.StagedProgressTracker(
        header,
        deployment_stages,
        failure_message='Deployment failed',
        suppress_output=args.async_,
    )

  def _GetRequiredApis(self, args):
    return [api_enabler.get_run_api()]

  def _DisplaySuccessMessage(self, service, args):
    if args.async_:
      pretty_print.Success(
          'Service [{{bold}}{serv}{{reset}}] is deploying '
          'asynchronously.'.format(serv=service.name)
      )
    else:
      pretty_print.Success(
          messages_util.GetSuccessMessageForSynchronousDeploy(
              service, args.no_traffic
          )
      )

  def Run(self, args):
    """Deploy a container to Cloud Run."""
    platform = flags.GetAndValidatePlatform(
        args, self.ReleaseTrack(), flags.Product.RUN
    )

    containers = self._ValidateAndGetContainers(args)
    build_from_source = self._ValidateAndGetBuildFromSource(containers)

    service_ref = args.CONCEPTS.service.Parse()
    flags.ValidateResource(service_ref)

    required_apis = self._GetRequiredApis(args)
    if build_from_source:
      required_apis.append('artifactregistry.googleapis.com')
      required_apis.append('cloudbuild.googleapis.com')

    already_activated_services = False
    if platform == platforms.PLATFORM_MANAGED:
      already_activated_services = api_enabler.check_and_enable_apis(
          properties.VALUES.core.project.Get(), required_apis
      )

    conn_context = self._ConnectionContext(args)

    image = None
    pack = None
    source = None
    operation_message = 'Deploying container to'
    repo_to_create = None
    is_function = False
    base_image = None
    build_service_account = None
    build_env_vars = None
    build_worker_pool = None
    build_changes = []
    build_from_source_container_name = ''
    enable_automatic_updates = None
    with serverless_operations.Connect(
        conn_context, already_activated_services
    ) as operations:
      service = operations.GetService(service_ref)
      # Build an image from source if source specified
      if build_from_source:
        (
            is_function,
            image,
            pack,
            source,
            operation_message,
            repo_to_create,
            base_image,
            build_service_account,
            build_changes,
            build_from_source_container_name,
            build_worker_pool,
            build_env_vars,
            enable_automatic_updates,
        ) = self._BuildFromSource(
            args,
            build_from_source,
            service_ref,
            conn_context,
            platform,
            already_activated_services,
            service,
        )
      # Deploy a container with an image
      changes = self._GetBaseChanges(args)
      changes.extend(build_changes)
      allow_unauth = self.GetAllowUnauth(args, operations, service_ref, service)
      resource_change_validators.ValidateClearVpcConnector(service, args)
      if service:  # Service has been deployed before
        if is_function and service.template.container.command:
          clear_command = flags.PromptForClearCommand()
          if clear_command:
            changes.append(config_changes.ContainerCommandChange([]))
          else:
            raise c_exceptions.ConflictingArgumentsException(
                '--command',
                '--function',
            )

      pretty_print.Info(
          messages_util.GetStartDeployMessage(
              conn_context, service_ref, operation_message
          )
      )
      has_latest = (
          service is None or traffic.LATEST_REVISION_KEY in service.spec_traffic
      )
      with self._GetTracker(
          args,
          service,
          changes,
          build_from_source,
          repo_to_create,
          allow_unauth,
          has_latest,
      ) as tracker:
        service = operations.ReleaseService(
            service_ref,
            changes,
            self.ReleaseTrack(),
            tracker,
            asyn=args.async_,
            allow_unauthenticated=allow_unauth,
            allow_unauth_regions=self._GetAllowUnauthRegions(args),
            prefetch=service,
            build_image=image,
            build_pack=pack,
            build_source=source,
            repo_to_create=repo_to_create,
            already_activated_services=already_activated_services,
            generate_name=(
                flags.FlagIsExplicitlySet(args, 'revision_suffix')
                or flags.FlagIsExplicitlySet(args, 'tag')
            ),
            delegate_builds=flags.FlagIsExplicitlySet(args, 'delegate_builds'),
            base_image=base_image,
            build_from_source_container_name=build_from_source_container_name,
            build_service_account=build_service_account,
            build_worker_pool=build_worker_pool,
            build_env_vars=build_env_vars,
            enable_automatic_updates=enable_automatic_updates,
        )

      self._DisplaySuccessMessage(service, args)
      return service


def _CreateBuildPack(container, release_track=base.ReleaseTrack.GA):
  """A helper method to cofigure buildpack."""
  pack = [{'image': container.image}]
  changes = []
  if release_track != base.ReleaseTrack.GA:
    command_arg = getattr(container, 'command', None)
    function_arg = getattr(container, 'function', None)
    if command_arg is not None:
      command = ' '.join(command_arg)
      pack[0].update(
          {'envs': ['GOOGLE_ENTRYPOINT="{command}"'.format(command=command)]}
      )
    elif function_arg is not None:
      pack[0].update({
          'envs': [
              'GOOGLE_FUNCTION_SIGNATURE_TYPE=http',
              'GOOGLE_FUNCTION_TARGET={target}'.format(target=function_arg),
          ]
      })
  return pack, changes


def _GetBuildWorkerPool(args, annotated_build_worker_pool, service, changes):
  """Gets the build worker pool from user flags and annotations.

  Args:
    args: argparse.Namespace, Command line arguments
    annotated_build_worker_pool: Build worker pool value from service
      annotations.
    service: Existing Cloud Run service.
    changes: List of config changes.

  Returns:
    build_worker_pool value or
    None meaning clear-worker-pool flag was set
    or build-worker-pool was an empty string.
  """
  if _ShouldClearBuildWorkerPool(args):
    worker_pool_key = service_lib.RUN_FUNCTIONS_BUILD_WORKER_POOL_ANNOTATION
    if service and service.annotations.get(worker_pool_key):
      changes.append(config_changes.DeleteAnnotationChange(worker_pool_key))
    return None
  return (
      args.build_worker_pool
      if flags.FlagIsExplicitlySet(args, 'build_worker_pool')
      else annotated_build_worker_pool
  )


def _ShouldClearBuildWorkerPool(args):
  return flags.FlagIsExplicitlySet(args, 'clear_build_worker_pool') or (
      flags.FlagIsExplicitlySet(args, 'build_worker_pool')
      and not args.build_worker_pool
  )


def _GetBuildServiceAccount(
    args, annotated_build_service_account, container, service, changes
):
  """Returns cloud build service account.

  Args:
    args: argparse.Namespace, Command line arguments
    annotated_build_service_account: string. The build service account annotated
      on the service used by cloud run functions.
    container: Container. The container to deploy.
    service: Service. The service being changed.
    changes: List of config changes.

  Returns:
    build service account value where
    None means there were no annotations, user specified to clear the
    build service account, or the build service account was an empty string.
  """
  build_sa_key = service_lib.RUN_FUNCTIONS_BUILD_SERVICE_ACCOUNT_ANNOTATION
  build_service_account = getattr(container, 'build_service_account', None)
  if _ShouldClearBuildServiceAccount(args, build_service_account):
    if service and service.annotations.get(build_sa_key):
      changes.append(config_changes.DeleteAnnotationChange(build_sa_key))
    return None
  return build_service_account or annotated_build_service_account


def _ShouldClearBuildServiceAccount(args, build_service_account):
  if flags.FlagIsExplicitlySet(args, 'clear_build_service_account'):
    return True
  if (
      flags.FlagIsExplicitlySet(args, 'build_service_account')
      and not build_service_account
  ):
    return True
  return False


@base.ReleaseTracks(base.ReleaseTrack.BETA)
class BetaDeploy(Deploy):
  """Create or update a Cloud Run service."""

  def _ValidateNoAutomaticUpdatesForContainers(
      self, build_from_source, containers
  ):
    for name, container in containers.items():
      if name not in build_from_source and container.IsSpecified(
          'automatic_updates'
      ):
        raise c_exceptions.InvalidArgumentException(
            '--automatic-updates',
            '--automatic-updates can only be specified in the container that'
            ' is built from source.',
        )

  @classmethod
  def Args(cls, parser):
    cls.CommonArgs(parser)

    # Flags specific to managed CR
    managed_group = flags.GetManagedArgGroup(parser)
    flags.AddDefaultUrlFlag(managed_group)
    flags.AddDeployHealthCheckFlag(managed_group)
    flags.AddGpuTypeFlag(managed_group, hidden=False)
    flags.AddRegionsArg(managed_group)
    flags.SERVICE_MESH_FLAG.AddToParser(managed_group)
    container_args = ContainerArgGroup(cls.ReleaseTrack())
    container_parser.AddContainerFlags(parser, container_args)

  def GetAllowUnauth(self, args, operations, service_ref, service_exists):
    if self.__is_multi_region:
      allow_unauth = flags.GetAllowUnauthenticated(
          args,
          operations,
          service_ref,
          not service_exists,
          region_override=self.__is_multi_region.split(',')[0],
      )
      # Avoid failure removing a policy binding for a service that
      # doesn't exist.
      if not service_exists and not allow_unauth:
        return None
      return allow_unauth
    return super().GetAllowUnauth(args, operations, service_ref, service_exists)

  def _GetAllowUnauthRegions(self, args):
    if self.__is_multi_region:
      return self.__is_multi_region.split(',')
    return None

  def _DisplaySuccessMessage(self, service, args):
    if not self.__is_multi_region or args.async_:
      return super()._DisplaySuccessMessage(service, args)

    pretty_print.Success(
        messages_util.GetSuccessMessageForMultiRegionSynchronousDeploy(
            service, self._GetAllowUnauthRegions(args)
        )
    )

  def _ConnectionContext(self, args):
    """Returns the connection context with is_multiregion set."""
    return connection_context.GetConnectionContext(
        args,
        flags.Product.RUN,
        self.ReleaseTrack(),
        is_multiregion=self.__is_multi_region,
    )

  def _GetBaseChanges(self, args):
    """Returns the service config changes with maybe multi-region annotation."""
    changes = super()._GetBaseChanges(args)
    if self.__is_multi_region:
      changes.append(
          config_changes.SetRegionsAnnotationChange(
              regions=self.__is_multi_region
          )
      )
    return changes

  def _GetTracker(
      self,
      args,
      service,
      changes,
      build_from_source,
      repo_to_create,
      allow_unauth,
      has_latest,
  ):
    if not self.__is_multi_region:
      return super()._GetTracker(
          args,
          service,
          changes,
          build_from_source,
          repo_to_create,
          allow_unauth,
          has_latest,
      )
    deployment_stages = stages.ServiceStages(
        include_iam_policy_set=allow_unauth is not None,
        include_route=False,
        include_build=bool(build_from_source),
        include_create_repo=False,
        include_create_revision=True,
    )
    header = 'Deploying new Multi-Region service...'
    # new services default cpu boost on the client
    if not flags.FlagIsExplicitlySet(args, 'cpu_boost'):
      changes.append(config_changes.StartupCpuBoostChange(cpu_boost=True))
    return progress_tracker.StagedProgressTracker(
        header,
        deployment_stages,
        failure_message=(
            'Multi-region deployment failed. Some regions might already be'
            ' serving traffic.'
        ),
        suppress_output=args.async_,
    )

  def Run(self, args):
    """Deploy a container to Cloud Run."""
    # If this is a multi-region Service, we will use the global endpoint
    # for all operations, and append a regions annotation to the Service.
    self.__is_multi_region = flags.GetMultiRegion(args)
    return super().Run(args)


@base.ReleaseTracks(base.ReleaseTrack.ALPHA)
class AlphaDeploy(BetaDeploy):
  """Create or update a Cloud Run service."""

  @classmethod
  def Args(cls, parser):
    cls.CommonArgs(parser)

    # Flags specific to managed CR
    managed_group = flags.GetManagedArgGroup(parser)
    flags.AddDeployHealthCheckFlag(managed_group)
    flags.AddDefaultUrlFlag(managed_group)
    flags.AddIapFlag(managed_group)
    flags.AddRuntimeFlag(managed_group)
    flags.AddServiceMaxInstancesFlag(managed_group)
    flags.AddScalingModeFlag(managed_group)
    flags.AddMaxSurgeFlag(managed_group)
    flags.AddMaxUnavailableFlag(managed_group)
    flags.AddRegionsArg(managed_group)
    flags.AddDomainArg(managed_group)
    flags.AddGpuTypeFlag(managed_group)
    flags.SERVICE_MESH_FLAG.AddToParser(managed_group)
    container_args = ContainerArgGroup(cls.ReleaseTrack())
    container_parser.AddContainerFlags(parser, container_args)
    flags.AddDelegateBuildsFlag(managed_group)

  def GetAllowUnauth(self, args, operations, service_ref, service_exists):
    if self.__is_multi_region:
      allow_unauth = flags.GetAllowUnauthenticated(
          args,
          operations,
          service_ref,
          not service_exists,
          region_override=self.__is_multi_region.split(',')[0],
      )
      # Avoid failure removing a policy binding for a service that
      # doesn't exist.
      if not service_exists and not allow_unauth:
        return None
      return allow_unauth
    return super().GetAllowUnauth(args, operations, service_ref, service_exists)

  def _MaybeGetDomain(self, args):
    if self.__is_multi_region and flags.FlagIsExplicitlySet(args, 'domain'):
      return getattr(args, 'domain', None)

  def _GetRequiredApis(self, args):
    required_apis = super()._GetRequiredApis(args)
    if self._MaybeGetDomain(args):
      required_apis.append('runapps.googleapis.com')
      required_apis.append('compute.googleapis.com')
    return required_apis

  def _MaybeCreateDomainIntegration(self, service, args):
    domain_name = self._MaybeGetDomain(args)
    if not domain_name:
      return
    # We need to choose a single region, Stacks has no multi-region entry.
    first_region = flags.GetMultiRegion(args).split(',')[0]
    service_name = service.metadata.name
    params = {
        'set-mapping': '%s/*:%s' % (
            domain_name,
            service_name,
        ),
    }
    pretty_print.Info(
        'Mapping multi-region Service {svc} to domain {domain}',
        svc=service_name,
        domain=domain_name,
    )

    with run_apps_operations.ConnectWithRegion(
        first_region, None, self.ReleaseTrack()
    ) as stacks_client:
      stacks_client.VerifyLocation()
      if stacks_client.MaybeGetIntegrationGeneric('custom-domains', 'router'):
        stacks_client.UpdateIntegration('custom-domains', params)
        pretty_print.Success(
            'Successfully updated mapping {svc} to domain {domain}',
            svc=service_name,
            domain=domain_name,
        )
      else:
        stacks_client.CreateIntegration(
            'custom-domains',
            params,
            None,
        )
        pretty_print.Success(
            'Successfully created mapping {svc} to domain {domain}',
            svc=service_name,
            domain=domain_name,
        )

  def Run(self, args):
    """Deploy a container to Cloud Run."""
    # If this is a multi-region Service, we will use the global endpoint
    # for all operations, and append a regions annotation to the Service.
    self.__is_multi_region = flags.GetMultiRegion(args)
    service = super().Run(args)
    # TODO(b/364446529) - Remove from gcloud alpha.
    self._MaybeCreateDomainIntegration(service, args)
    return service


AlphaDeploy.__doc__ = Deploy.__doc__
