# Factories for the Zones
from datetime import datetime, timezone
from typing import Any

from maascommon.enums.bmc import BmcType
from maasservicelayer.models.bmc import Bmc
from tests.maasapiserver.fixtures.db import Fixture


async def create_test_bmc_entry(
    fixture: Fixture, **extra_details: Any
) -> dict[str, Any]:
    created_at = datetime.now(timezone.utc).astimezone()
    updated_at = datetime.now(timezone.utc).astimezone()
    bmc = {
        "created": created_at,
        "updated": updated_at,
        "power_type": "virsh",
        "bmc_type": BmcType.BMC,
        "cores": 1,
        "cpu_speed": 100,
        "local_storage": 1024,
        "memory": 1024,
        "name": "mybmc",
        "pool_id": 0,
        "zone_id": 1,
        "cpu_over_commit_ratio": 1,
        "memory_over_commit_ratio": 1,
        "power_parameters": {},
        "version": "1",
    }
    bmc.update(extra_details)
    [created_bmc] = await fixture.create(
        "maasserver_bmc",
        [bmc],
    )
    return created_bmc


async def create_test_bmc(fixture: Fixture, **extra_details: Any) -> Bmc:
    created_bmc = await create_test_bmc_entry(fixture, **extra_details)

    return Bmc(**created_bmc)
