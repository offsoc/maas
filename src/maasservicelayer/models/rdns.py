# Copyright 2025 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

from pydantic import IPvAnyAddress

from maasservicelayer.models.base import (
    generate_builder,
    MaasTimestampedBaseModel,
)


@generate_builder()
class RDNS(MaasTimestampedBaseModel):
    ip: IPvAnyAddress
    hostname: str | None
    hostnames: list[str]
    observer_id: int
