"""AWS cloud provider adapter — Phase 7C.

This sub-package hosts :class:`AWSCloudProvider`, the concrete
adapter that implements :class:`~app.services.cloud.base.CloudProvider`
for AWS.  It does **not** re-implement any AWS service logic; it
delegates to the existing Phase 7B modules in
:mod:`app.services.aws.*` and translates their dataclasses into the
provider-neutral shapes defined in
:mod:`app.services.cloud.models`.

Importing this package registers the AWS adapter with the
:class:`ProviderRegistry` (see :mod:`app.services.cloud.aws.adapter`).
"""
from app.services.cloud.aws.adapter import AWSCloudProvider

__all__ = ["AWSCloudProvider"]
