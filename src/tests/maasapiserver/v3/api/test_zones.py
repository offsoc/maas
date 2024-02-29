from fastapi.encoders import jsonable_encoder
from httpx import AsyncClient
import pytest
from sqlalchemy.sql.operators import eq

from maasapiserver.common.api.models.responses.errors import ErrorBodyResponse
from maasapiserver.common.db.tables import ZoneTable
from maasapiserver.common.models.constants import (
    CANNOT_DELETE_DEFAULT_ZONE_VIOLATION_TYPE,
    ETAG_PRECONDITION_VIOLATION_TYPE,
)
from maasapiserver.v3.api.models.requests.zones import ZoneRequest
from maasapiserver.v3.api.models.responses.zones import (
    ZoneResponse,
    ZonesListResponse,
)
from maasapiserver.v3.constants import EXTERNAL_V3_API_PREFIX
from maasapiserver.v3.models.zones import Zone
from tests.fixtures.factories.zone import create_test_zone
from tests.maasapiserver.fixtures.db import Fixture


@pytest.mark.usefixtures("ensuremaasdb")
@pytest.mark.asyncio
class TestZonesApi:
    def _assert_zone_in_list(
        self, zone: Zone, zones_response: ZonesListResponse
    ) -> None:
        zone_response = next(
            filter(
                lambda zone_response: zone.id == zone_response.id,
                zones_response.items,
            )
        )
        assert zone.id == zone_response.id
        assert zone.name == zone_response.name
        assert zone.description == zone_response.description

    # GET /zones
    # We don't have a test that checks for empty list because it's impossible: there is always at least one "default" zone
    # created at startup by the migration scripts.
    @pytest.mark.parametrize("zones_size", range(1, 3))
    async def test_list_default_200(
        self, zones_size: int, api_client: AsyncClient, fixture: Fixture
    ) -> None:
        # The "default" zone with id=1 is created at startup with the migrations. By consequence, we create zones_size-1 zones
        # here.
        created_zones = [
            (await create_test_zone(fixture, name=str(i), description=str(i)))
            for i in range(0, zones_size - 1)
        ]
        response = await api_client.get("/api/v3/zones")
        assert response.status_code == 200

        zones_response = ZonesListResponse(**response.json())
        assert zones_response.kind == "ZonesList"
        assert zones_response.total == zones_size
        assert len(zones_response.items) == zones_size
        for zone in created_zones:
            self._assert_zone_in_list(zone, zones_response)

    async def test_list_parameters_200(
        self, api_client: AsyncClient, fixture: Fixture
    ) -> None:
        # The "default" zone with id=1 is created at startup with the migrations. By consequence, we create zones_size-1 zones
        # here.
        for i in range(0, 9):
            await create_test_zone(fixture, name=str(i), description=str(i))

        for page in range(1, 6):
            response = await api_client.get(
                f"/api/v3/zones?page={page}&size=2"
            )
            assert response.status_code == 200
            zones_response = ZonesListResponse(**response.json())
            assert zones_response.kind == "ZonesList"
            assert zones_response.total == 10
            assert len(zones_response.items) == 2

    @pytest.mark.parametrize(
        "page,size", [(1, 0), (0, 1), (-1, -1), (1, 1001)]
    )
    async def test_list_422(
        self, page: int, size: int, api_client: AsyncClient
    ) -> None:
        response = await api_client.get(
            f"/api/v3/zones?page={page}&size={size}"
        )
        assert response.status_code == 422

        error_response = ErrorBodyResponse(**response.json())
        assert error_response.kind == "Error"
        assert error_response.code == 422

    # GET /zones/{zone_id}
    async def test_get_default(
        self, api_client: AsyncClient, fixture: Fixture
    ) -> None:
        # A "default" zone should be created at startup by the migration scripts.
        response = await api_client.get("/api/v3/zones/1")
        assert response.status_code == 200
        assert len(response.headers["ETag"]) > 0
        zone_response = ZoneResponse(**response.json())
        assert zone_response.id == 1
        assert zone_response.name == "default"

    # GET /zone/{ID}
    async def test_get_200(
        self, api_client: AsyncClient, fixture: Fixture
    ) -> None:
        created_zone = await create_test_zone(fixture)
        response = await api_client.get(f"/api/v3/zones/{created_zone.id}")
        assert response.status_code == 200
        assert len(response.headers["ETag"]) > 0
        assert response.json() == {
            "kind": "Zone",
            "id": created_zone.id,
            "name": created_zone.name,
            "description": created_zone.description,
            # TODO: FastAPI response_model_exclude_none not working. We need to fix this before making the api public
            "_embedded": None,
            "_links": {
                "self": {
                    "href": f"{EXTERNAL_V3_API_PREFIX}/zones/{created_zone.id}"
                }
            },
        }

    async def test_get_400(
        self, api_client: AsyncClient, fixture: Fixture
    ) -> None:
        response = await api_client.get("/api/v3/zones/100")
        assert response.status_code == 404
        assert "ETag" not in response.headers

        error_response = ErrorBodyResponse(**response.json())
        assert error_response.kind == "Error"
        assert error_response.code == 404

    async def test_get_422(
        self, api_client: AsyncClient, fixture: Fixture
    ) -> None:
        response = await api_client.get("/api/v3/zones/xyz")
        assert response.status_code == 422
        assert "ETag" not in response.headers

        error_response = ErrorBodyResponse(**response.json())
        assert error_response.kind == "Error"
        assert error_response.code == 422

    # POST /zones
    async def test_post_201(self, api_client: AsyncClient) -> None:
        zone_request = ZoneRequest(name="myzone", description="my description")
        response = await api_client.post(
            "/api/v3/zones", json=jsonable_encoder(zone_request)
        )
        assert response.status_code == 201
        assert len(response.headers["ETag"]) > 0
        zone_response = ZoneResponse(**response.json())
        assert zone_response.id > 1
        assert zone_response.name == zone_request.name
        assert zone_response.description == zone_request.description
        assert (
            zone_response.hal_links.self.href
            == f"{EXTERNAL_V3_API_PREFIX}/zones/{zone_response.id}"
        )

    async def test_post_default_parameters(
        self, api_client: AsyncClient
    ) -> None:
        zone_request = ZoneRequest(name="myzone", description=None)
        response = await api_client.post(
            "/api/v3/zones", json=jsonable_encoder(zone_request)
        )
        assert response.status_code == 201
        zone_response = ZoneResponse(**response.json())
        assert zone_response.description == ""

    async def test_post_409(self, api_client: AsyncClient) -> None:
        zone_request = ZoneRequest(name="myzone", description=None)
        response = await api_client.post(
            "/api/v3/zones", json=jsonable_encoder(zone_request)
        )
        assert response.status_code == 201

        response = await api_client.post(
            "/api/v3/zones", json=jsonable_encoder(zone_request)
        )
        assert response.status_code == 409

        error_response = ErrorBodyResponse(**response.json())
        assert error_response.kind == "Error"
        assert error_response.code == 409
        assert len(error_response.details) == 1
        assert error_response.details[0].type == "UniqueConstraintViolation"
        assert "already exists" in error_response.details[0].message

    @pytest.mark.parametrize(
        "zone_request",
        [
            {"name": ""},
            {"name": "-myzone"},
            {"name": "Hello$Zone"},
        ],
    )
    async def test_post_422(
        self, api_client: AsyncClient, zone_request: dict[str, str]
    ) -> None:
        response = await api_client.post("/api/v3/zones", json=zone_request)
        assert response.status_code == 422

        error_response = ErrorBodyResponse(**response.json())
        assert error_response.kind == "Error"
        assert error_response.code == 422

    # DELETE /zones/{id}
    async def test_delete_unexisting_resource(
        self, api_client: AsyncClient
    ) -> None:
        response = await api_client.delete("/api/v3/zones/100")
        assert response.status_code == 204

    async def test_delete_existing_resource(
        self, api_client: AsyncClient, fixture: Fixture
    ) -> None:
        created_zone = await create_test_zone(fixture)
        response = await api_client.delete(f"/api/v3/zones/{created_zone.id}")
        assert response.status_code == 204

        zones = await fixture.get(
            ZoneTable.name, eq(ZoneTable.c.id, created_zone.id)
        )
        assert zones == []

    async def test_delete_default_zone(
        self, api_client: AsyncClient, fixture: Fixture
    ) -> None:
        response = await api_client.delete("/api/v3/zones/1")
        error_response = ErrorBodyResponse(**response.json())
        assert response.status_code == 400
        assert error_response.code == 400
        assert error_response.message == "Bad request."
        assert (
            error_response.details[0].type
            == CANNOT_DELETE_DEFAULT_ZONE_VIOLATION_TYPE
        )

    async def test_delete_with_etag(
        self, api_client: AsyncClient, fixture: Fixture
    ) -> None:
        created_zone = await create_test_zone(fixture)
        failed_response = await api_client.delete(
            f"/api/v3/zones/{created_zone.id}", headers={"if-match": "blabla"}
        )
        assert failed_response.status_code == 412
        error_response = ErrorBodyResponse(**failed_response.json())
        assert error_response.code == 412
        assert error_response.message == "A precondition has failed."
        assert (
            error_response.details[0].type == ETAG_PRECONDITION_VIOLATION_TYPE
        )

        response = await api_client.delete(
            f"/api/v3/zones/{created_zone.id}",
            headers={"if-match": created_zone.etag()},
        )
        assert response.status_code == 204
        zones = await fixture.get(
            ZoneTable.name, eq(ZoneTable.c.id, created_zone.id)
        )
        assert zones == []
