"""Azure cloud provider sub-package — Phase 7C.2A.

This sub-package hosts the Azure side of the cloud-provider
abstraction:

* :mod:`app.services.cloud.azure.redaction` — log/exception sanitisation
  helpers.  No JWT, no access token, no Authorization header may ever
  reach the application log stream.
* :mod:`app.services.cloud.azure.federation` — the AWS-side workload
  identity federation assertion provider.  Produces short-lived
  RS256-signed AWS JWTs that Microsoft Entra accepts as a federated
  credential.
* :mod:`app.services.cloud.azure.credential` — the AzureIdentity
  credential layer built on
  :class:`azure.identity.ClientAssertionCredential`.
* :mod:`app.services.cloud.azure.arm_client` — a minimal Azure
  Resource Manager REST wrapper used to validate subscriptions.
* :mod:`app.services.cloud.azure.registrations` — runtime configuration
  loader for Azure subscription bootstrap.
* :mod:`app.services.cloud.azure.adapter` — :class:`AzureCloudProvider`,
  the concrete :class:`~app.services.cloud.base.CloudProvider`
  implementation for Microsoft Azure.

Importing this package registers the Azure adapter with the
:class:`ProviderRegistry` when Entra runtime configuration is
present.  When ``AZURE_TENANT_ID`` / ``AZURE_CLIENT_ID`` are missing
the adapter is registered as DISABLED so the rest of the
application sees a stable ``is_registered=true`` /
``is_enabled=false`` surface.
"""
from app.services.cloud.azure.adapter import AzureCloudProvider

__all__ = ["AzureCloudProvider"]
