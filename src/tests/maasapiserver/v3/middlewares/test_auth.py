#  Copyright 2024 Canonical Ltd.  This software is licensed under the
#  GNU Affero General Public License version 3 (see the file LICENSE).

from datetime import timedelta
from typing import Any, AsyncIterator, Callable, Iterator
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI, Request
from httpx import AsyncClient
from macaroonbakery import bakery, checkers
from macaroonbakery.bakery import (
    DischargeRequiredError,
    PermissionDenied,
    VerificationError,
)
from pymacaroons import Macaroon
import pytest
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.operators import eq
from starlette.responses import Response

from maasapiserver.common.api.models.responses.errors import ErrorBodyResponse
from maasapiserver.common.middlewares.exceptions import ExceptionMiddleware
from maasapiserver.v3.api.public.models.responses.oauth2 import (
    AccessTokenResponse,
)
from maasapiserver.v3.constants import V3_API_PREFIX
from maasapiserver.v3.middlewares.auth import (
    AuthenticationProvidersCache,
    DjangoSessionAuthenticationProvider,
    EXTERNAL_USER_CHECK_INTERVAL,
    LocalAuthenticationProvider,
    MacaroonAuthenticationProvider,
    V3AuthenticationMiddleware,
)
from maasapiserver.v3.middlewares.context import ContextMiddleware
from maasapiserver.v3.middlewares.services import ServicesMiddleware
from maasservicelayer.auth.external_auth import (
    ExternalAuthConfig,
    ExternalAuthType,
)
from maasservicelayer.auth.jwt import InvalidToken, JWT, UserRole
from maasservicelayer.auth.macaroons.macaroon_client import (
    CandidAsyncClient,
    RbacAsyncClient,
)
from maasservicelayer.auth.macaroons.models.exceptions import (
    MacaroonApiException,
)
from maasservicelayer.auth.macaroons.models.responses import (
    GetGroupsResponse,
    PermissionResourcesMapping,
    UserDetailsResponse,
)
from maasservicelayer.constants import NODE_INIT_USERNAME, WORKER_USERNAME
from maasservicelayer.context import Context
from maasservicelayer.db import Database
from maasservicelayer.db.tables import UserTable
from maasservicelayer.enums.rbac import RbacPermission
from maasservicelayer.exceptions.catalog import (
    DischargeRequiredException,
    ForbiddenException,
    UnauthorizedException,
)
from maasservicelayer.models.auth import AuthenticatedUser
from maasservicelayer.models.users import User
from maasservicelayer.services import CacheForServices, ServiceCollectionV3
from maasservicelayer.services.auth import AuthService
from maasservicelayer.services.external_auth import ExternalAuthService
from maasservicelayer.services.users import UsersService
from maasservicelayer.utils.date import utcnow
from tests.fixtures.factories.user import (
    create_test_session,
    create_test_user,
    create_test_user_profile,
)
from tests.maasapiserver.fixtures.db import Fixture


def _make_user(is_superuser: bool = False) -> User:
    return User(
        id=0,
        username="test",
        password="password",
        is_superuser=is_superuser,
        first_name="name",
        last_name="last_name",
        is_staff=False,
        is_active=True,
        date_joined=utcnow(),
    )


@pytest.fixture
def auth_app(
    db: Database,
    db_connection: AsyncConnection,
    transaction_middleware_class: type,
) -> Iterator[FastAPI]:
    app = FastAPI()
    services_cache = CacheForServices()

    app.add_middleware(
        V3AuthenticationMiddleware,
        providers_cache=AuthenticationProvidersCache(
            jwt_authentication_providers=[LocalAuthenticationProvider()],
            session_authentication_provider=DjangoSessionAuthenticationProvider(),
            macaroon_authentication_provider=MacaroonAuthenticationProvider(),
        ),
    )
    app.add_middleware(ServicesMiddleware, cache=services_cache)
    app.add_middleware(transaction_middleware_class, db=db)
    app.add_middleware(ExceptionMiddleware)
    app.add_middleware(ContextMiddleware)
    app.add_event_handler("shutdown", services_cache.close)

    @app.get("/MAAS/a/v3/users/{username}/token")
    async def get_token(
        request: Request, username: str
    ) -> AccessTokenResponse:
        jwt_key = (
            await request.state.services.auth._get_or_create_cached_jwt_key()
        )
        return AccessTokenResponse(
            token_type="bearer",
            access_token=JWT.create(
                jwt_key, username, 0, [UserRole.USER]
            ).encoded,
        )

    @app.get("/MAAS/a/v3/users/{username}/invalid_token")
    async def get_invalid_token(
        request: Request, username: str
    ) -> AccessTokenResponse:
        return AccessTokenResponse(
            token_type="bearer",
            access_token=JWT.create(
                "definitely_not_the_key", username, 0, [UserRole.USER]
            ).encoded,
        )

    @app.get("/MAAS/a/v3/users/me")
    async def get_me(request: Request) -> Any:
        # V3 endpoints have authenticated_user == None if no bearer tokens was provided
        if request.state.authenticated_user:
            return AuthenticatedUser(
                id=request.state.authenticated_user.id,
                username=request.state.authenticated_user.username,
                roles=request.state.authenticated_user.roles,
            )
        return Response(content="authenticated_user is None", status_code=401)

    yield app


@pytest.fixture
async def auth_client(auth_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(app=auth_app, base_url="http://test") as client:
        yield client


class TestV3AuthenticationMiddleware:
    async def test_authenticated_user(self, auth_client: AsyncClient) -> None:
        # v3 endpoints should have the authenticated_user in the request context if the request was not authenticated
        v3_response = await auth_client.get(f"{V3_API_PREFIX}/users/me")
        assert v3_response.text == "authenticated_user is None"
        assert v3_response.status_code == 401

        # v3 requests with malformed bearer tokens should 400
        v3_response = await auth_client.get(
            f"{V3_API_PREFIX}/users/me",
            headers={"Authorization": "bearer xyz"},
        )
        assert v3_response.status_code == 400
        error_response = ErrorBodyResponse(**v3_response.json())
        assert error_response.kind == "Error"

        # v3 requests with invalid bearer tokens should 401
        invalid_token_response = await auth_client.get(
            f"{V3_API_PREFIX}/users/test/invalid_token"
        )
        invalid_token = AccessTokenResponse(**invalid_token_response.json())
        invalid_token_v3_response = await auth_client.get(
            f"{V3_API_PREFIX}/users/me",
            headers={"Authorization": "bearer " + invalid_token.access_token},
        )
        assert invalid_token_v3_response.status_code == 401
        error_response = ErrorBodyResponse(**invalid_token_v3_response.json())
        assert error_response.kind == "Error"

        # valid token
        token_response = await auth_client.get(
            f"{V3_API_PREFIX}/users/test/token"
        )
        token_response = AccessTokenResponse(**token_response.json())
        authenticated_v3_response = await auth_client.get(
            f"{V3_API_PREFIX}/users/me",
            headers={"Authorization": "bearer " + token_response.access_token},
        )
        assert authenticated_v3_response.status_code == 200
        authenticated_user = AuthenticatedUser(
            **authenticated_v3_response.json()
        )
        assert authenticated_user.username == "test"
        assert authenticated_user.roles == {UserRole.USER}

    async def test_authentication_with_sessionid(
        self, fixture: Fixture, auth_client: AsyncClient
    ) -> None:
        # invalid session_id
        invalid_token_response = await auth_client.get(
            f"{V3_API_PREFIX}/users/me",
            headers={"Cookie": "sessionid=invalid"},
        )
        assert invalid_token_response.status_code == 401

        # valid user session_id
        user = await create_test_user(fixture)
        session_id = "mysession"
        await create_test_session(
            fixture=fixture, user_id=user.id, session_id=session_id
        )
        authenticated_v3_response = await auth_client.get(
            f"{V3_API_PREFIX}/users/me",
            headers={"Cookie": f"sessionid={session_id}"},
        )
        assert authenticated_v3_response.status_code == 200
        authenticated_user = AuthenticatedUser(
            **authenticated_v3_response.json()
        )
        assert authenticated_user.username == "myusername"
        assert authenticated_user.roles == {UserRole.USER}

        # valid admin session_id
        admin = await create_test_user(
            fixture, username="admin", is_superuser=True
        )
        admin_session_id = "adminsession"
        await create_test_session(
            fixture=fixture, user_id=admin.id, session_id=admin_session_id
        )
        authenticated_v3_response = await auth_client.get(
            f"{V3_API_PREFIX}/users/me",
            headers={"Cookie": f"sessionid={admin_session_id}"},
        )
        assert authenticated_v3_response.status_code == 200
        authenticated_admin = AuthenticatedUser(
            **authenticated_v3_response.json()
        )
        assert authenticated_admin.username == "admin"
        assert authenticated_admin.roles == {UserRole.ADMIN, UserRole.USER}

    async def test_authentication_with_macaroons(self) -> None:
        macaroon_auth_provider_mock = Mock(MacaroonAuthenticationProvider)
        macaroons_mock = [[Mock(Macaroon)]]
        macaroon_auth_provider_mock.extract_macaroons.return_value = (
            macaroons_mock
        )
        authenticated_user = AuthenticatedUser(
            id=0, username="admin", roles={UserRole.USER, UserRole.ADMIN}
        )
        macaroon_auth_provider_mock.authenticate.return_value = (
            authenticated_user
        )

        authentication_providers_cache = AuthenticationProvidersCache(
            jwt_authentication_providers=None,
            session_authentication_provider=None,
            macaroon_authentication_provider=macaroon_auth_provider_mock,
        )
        auth_middleware = V3AuthenticationMiddleware(
            app=None, providers_cache=authentication_providers_cache
        )

        request_mock = Mock(Request)
        request_mock.headers = {}
        request_mock.cookies = {}
        call_next_mock = AsyncMock(Callable)

        await auth_middleware.dispatch(request_mock, call_next_mock)
        assert request_mock.state.authenticated_user == authenticated_user
        call_next_mock.assert_called_once_with(request_mock)
        macaroon_auth_provider_mock.authenticate.assert_called_once()


class TestAuthenticationProvidersCache:
    def test_constructor(self) -> None:
        cache = AuthenticationProvidersCache()
        assert cache.size() == 0
        assert cache.get_session_provider() is None

        session_provider = DjangoSessionAuthenticationProvider()
        macaroon_provider = MacaroonAuthenticationProvider()
        cache = AuthenticationProvidersCache(
            jwt_authentication_providers=[LocalAuthenticationProvider()],
            session_authentication_provider=session_provider,
            macaroon_authentication_provider=macaroon_provider,
        )
        assert cache.size() == 1
        assert cache.get(LocalAuthenticationProvider.get_issuer()) is not None
        assert cache.get_session_provider() is session_provider
        assert cache.get_macaroon_provider() is macaroon_provider

    def test_get(self):
        provider = LocalAuthenticationProvider()
        cache = AuthenticationProvidersCache([provider])
        assert cache.size() == 1
        assert id(provider) == id(
            cache.get(LocalAuthenticationProvider.get_issuer())
        )

    def test_add(self):
        provider = LocalAuthenticationProvider()
        cache = AuthenticationProvidersCache()
        cache.add(provider)
        assert cache.size() == 1
        assert id(provider) == id(
            cache.get(LocalAuthenticationProvider.get_issuer())
        )

        replacement = LocalAuthenticationProvider()
        cache.add(replacement)
        assert cache.size() == 1
        assert id(replacement) == id(
            cache.get(LocalAuthenticationProvider.get_issuer())
        )


class TestDjangoSessionAuthenticationProvider:
    def mock_request(self, user) -> Mock:
        request = Mock(Request)
        request.state.services.users = Mock(UsersService)
        request.state.services.users.get_by_session_id.return_value = user
        return request

    async def test_dispatch(self) -> None:
        sessionid = "test"

        user = _make_user()
        request = self.mock_request(user)

        provider = DjangoSessionAuthenticationProvider()
        authenticated_user = await provider.authenticate(request, sessionid)

        assert authenticated_user.username == user.username
        assert authenticated_user.roles == {UserRole.USER}

    async def test_dispatch_admin(self) -> None:
        sessionid = "test"

        user = _make_user(is_superuser=True)
        request = self.mock_request(user)

        provider = DjangoSessionAuthenticationProvider()
        authenticated_user = await provider.authenticate(request, sessionid)

        assert authenticated_user.username == user.username
        assert authenticated_user.roles == {UserRole.ADMIN, UserRole.USER}

    async def test_dispatch_unauthenticated(self) -> None:
        request = self.mock_request(None)

        provider = DjangoSessionAuthenticationProvider()
        with pytest.raises(UnauthorizedException):
            await provider.authenticate(request, "")


class TestLocalAuthenticationProvider:
    async def test_dispatch(self) -> None:
        jwt = JWT.create("123", "test", 0, [UserRole.USER])
        request = Mock(Request)
        request.state.services.auth = Mock(AuthService)
        request.state.services.auth.decode_and_verify_token.return_value = jwt

        provider = LocalAuthenticationProvider()
        user = await provider.authenticate(request, jwt.encoded)

        assert user.username == "test"
        assert user.roles == {UserRole.USER}
        assert user.id == 0

    async def test_dispatch_unauthenticated(self) -> None:
        request = Mock(Request)
        request.state.services.auth = Mock(AuthService)
        request.state.services.auth.decode_and_verify_token.side_effect = (
            InvalidToken()
        )

        provider = LocalAuthenticationProvider()
        with pytest.raises(UnauthorizedException):
            await provider.authenticate(request, "")


class TestMacaroonAuthenticationProvider:
    MACAROON = "W3siaWRlbnRpZmllciI6ICJBd29RUWh2MUVQM1YtTHR2Zkg2RmJ5MF80UklCT1JvT0NnVnNiMmRwYmhJRmJHOW5hVzQiLCAic2lnbmF0dXJlIjogIjk0NDUzMmVjODYxZGJiNDFiNjBlNDdlOWE1Y2IzODFiMDc5MjU3MDJhYTVkNGI0MTYzYTJkZDEzZWRmMTYzZjEiLCAibG9jYXRpb24iOiAiaHR0cDovL2xvY2FsaG9zdDo1MjQwLyIsICJjYXZlYXRzIjogW3siY2lkIjogInRpbWUtYmVmb3JlIDIwMjQtMDctMjJUMDY6Mzc6NTQuMjA1MTc3WiJ9LCB7ImNpZCI6ICJleUpVYUdseVpGQmhjblI1VUhWaWJHbGpTMlY1SWpvZ0luTXhkMVZhVFdGMlUydFNUalZwYzJKYVdYUmhNRll5Y3pFd1JtMHhlSEkxZW1oc1ZFVklWVVF6UVhjOUlpd2dJa1pwY25OMFVHRnlkSGxRZFdKc2FXTkxaWGtpT2lBaWRXczJlbnBPYmt4SFUxRjJlRVZzYml0NmRsUnJlVkZtYWtOU1pIZFpXSEZaYkdwd2FuZFRXUzluUlQwaUxDQWlUbTl1WTJVaU9pQWlWME5EV1hKb1psVTVaVU5JZGsxaVZETm1jWFJaY25Odk4wWkdZMlp5ZEhRaUxDQWlTV1FpT2lBaWN6VmlValExWlV3ME4wNVBhazloVTBOQlp6aEtZa013ZHpVcldrMTNVWFpUTUcxUlJEWlhkRXgzVVV4M1ZGUlZTV1ZTTlU1VWJWRXJlblo0V1dKeVRscGFNMmhGY1cxa09IWnFRbWczTUdSbVkwNUpiMFI1YTBaQ2FERk1Nall4YTFKcWJETm1XRmhNWWtwbVdYaG9MeXRZTmtsNk9GTm1aa2sxWlRsQ1dtSXZOWFZLVERNMFBTSjkiLCAidmlkIjogIm1CdjlHOGFhRllEb1VsQVhIS0NIS014dWhsdmo0c3o0Qld2cUJCb1NtUlRvXzhuNlNwN3BkdlAtZU9iUnVraWllX1ZZR2dRZzB4OXRfWm9fMndVVW14R0FaMENyaHJJSCIsICJjbCI6ICJodHRwOi8vMTAuMC4xLjIzOjUwMDAvYXV0aCJ9XX0seyJpZGVudGlmaWVyIjogImV5SlVhR2x5WkZCaGNuUjVVSFZpYkdsalMyVjVJam9nSW5NeGQxVmFUV0YyVTJ0U1RqVnBjMkphV1hSaE1GWXljekV3Um0weGVISTFlbWhzVkVWSVZVUXpRWGM5SWl3Z0lrWnBjbk4wVUdGeWRIbFFkV0pzYVdOTFpYa2lPaUFpZFdzMmVucE9ia3hIVTFGMmVFVnNiaXQ2ZGxScmVWRm1ha05TWkhkWldIRlpiR3B3YW5kVFdTOW5SVDBpTENBaVRtOXVZMlVpT2lBaVYwTkRXWEpvWmxVNVpVTklkazFpVkRObWNYUlpjbk52TjBaR1kyWnlkSFFpTENBaVNXUWlPaUFpY3pWaVVqUTFaVXcwTjA1UGFrOWhVME5CWnpoS1lrTXdkelVyV2sxM1VYWlRNRzFSUkRaWGRFeDNVVXgzVkZSVlNXVlNOVTVVYlZFcmVuWjRXV0p5VGxwYU0yaEZjVzFrT0hacVFtZzNNR1JtWTA1SmIwUjVhMFpDYURGTU1qWXhhMUpxYkRObVdGaE1Za3BtV1hob0x5dFlOa2w2T0ZObVprazFaVGxDV21Jdk5YVktURE0wUFNKOSIsICJzaWduYXR1cmUiOiAiNDE2ZDYyN2VjNWI0ZTk3Nzc1MTY0ZTQ3ZGVkY2I2ODc0MDkyZmJiOGRlMjU0MzgzNzc1OGQxMWI3YTg3YjliNCIsICJjYXZlYXRzIjogW3siY2lkIjogImRlY2xhcmVkIHVzZXJuYW1lIGFkbWluIn1dfV0="

    def mock_request(self) -> Mock:
        request = Mock(Request)
        request.state.services.external_auth = Mock(ExternalAuthService)
        return request

    async def test_dispatch_with_headers(self) -> None:
        user = _make_user()
        request = self.mock_request()
        request.state.services.external_auth.login.return_value = user
        request.headers = {
            "x-forwarded-host": "localhost:5240",
            "x-forwarded-proto": "http",
        }

        provider = MacaroonAuthenticationProvider()
        macaroons = [[Mock(Macaroon)]]
        provider.validate_user_external_auth = AsyncMock(return_value=user)
        user = await provider.authenticate(request, macaroons)

        assert user.username == "test"
        assert user.roles == {UserRole.USER}
        request.state.services.external_auth.login.assert_called_once_with(
            macaroons=macaroons, request_absolute_uri="http://localhost:5240/"
        )

    async def test_dispatch_without_headers(self) -> None:
        user = _make_user()
        request = self.mock_request()
        request.state.services.external_auth.login.return_value = user
        request.headers = {}
        request.base_url = "http://test:5240/"

        provider = MacaroonAuthenticationProvider()
        macaroons = [[Mock(Macaroon)]]
        provider.validate_user_external_auth = AsyncMock(return_value=user)
        user = await provider.authenticate(request, macaroons)

        assert user.username == "test"
        assert user.roles == {UserRole.USER}
        request.state.services.external_auth.login.assert_called_once_with(
            macaroons=macaroons, request_absolute_uri="http://test:5240/"
        )

    async def test_dispatch_forbidden_macaroon_raises_exception(self) -> None:
        request = self.mock_request()
        request.state.services.external_auth.login.side_effect = (
            PermissionDenied()
        )
        request.headers = {}
        request.base_url = "http://test:5240/"

        provider = MacaroonAuthenticationProvider()
        macaroons = [[Mock(Macaroon)]]

        with pytest.raises(ForbiddenException):
            await provider.authenticate(request, macaroons)

    async def test_dispatch_invalid_macaroon_generates_discharge_macaroon(
        self,
    ) -> None:
        request = self.mock_request()
        request.state.services.external_auth.login.side_effect = (
            VerificationError()
        )
        bakery_mock = Mock(bakery.Bakery)
        request.state.services.external_auth.get_bakery.return_value = (
            bakery_mock
        )

        request.state.services.external_auth.get_external_auth.return_value = (
            ExternalAuthConfig(
                type=ExternalAuthType.RBAC,
                url="http://test/",
                domain="",
                admin_group="admin",
            )
        )

        macaroon_mock = Mock(bakery.Macaroon)
        request.state.services.external_auth.generate_discharge_macaroon.return_value = macaroon_mock

        request.headers = {}
        request.base_url = "http://test:5240/"

        provider = MacaroonAuthenticationProvider()
        with pytest.raises(DischargeRequiredException) as exc:
            await provider.authenticate(request, [[Mock(Macaroon)]])
        assert exc.value.macaroon == macaroon_mock
        request.state.services.external_auth.generate_discharge_macaroon.assert_called_once_with(
            macaroon_bakery=bakery_mock,
            caveats=[
                checkers.Caveat(
                    "is-authenticated-user", location="http://test/"
                )
            ],
            ops=[bakery.LOGIN_OP],
            req_headers=request.headers,
        )

    async def test_dispatch_unverified_macaroon_generates_discharge_macaroon(
        self,
    ) -> None:
        request = self.mock_request()
        cavs = [checkers.Caveat("is-authenticated", location="http://test/")]
        ops = [bakery.LOGIN_OP]
        request.state.services.external_auth.login.side_effect = (
            DischargeRequiredError(msg="", cavs=cavs, ops=ops)
        )
        bakery_mock = Mock(bakery.Bakery)
        request.state.services.external_auth.get_bakery.return_value = (
            bakery_mock
        )
        macaroon_mock = Mock(bakery.Macaroon)
        request.state.services.external_auth.generate_discharge_macaroon.return_value = macaroon_mock

        request.headers = {}
        request.base_url = "http://test:5240/"

        provider = MacaroonAuthenticationProvider()
        with pytest.raises(DischargeRequiredException) as exc:
            await provider.authenticate(request, [[Mock(Macaroon)]])
        assert exc.value.macaroon == macaroon_mock
        request.state.services.external_auth.generate_discharge_macaroon.assert_called_once_with(
            macaroon_bakery=bakery_mock,
            caveats=cavs,
            ops=ops,
            req_headers=request.headers,
        )

    def _check_macaroons(self, macaroons: list[list[Macaroon]]):
        assert len(macaroons) == 1
        assert len(macaroons[0]) == 2
        assert macaroons[0][0].location == "http://localhost:5240/"
        assert macaroons[0][1].location == ""
        assert macaroons[0][0].version == 1
        assert macaroons[0][1].version == 1

    async def test_extract_macaroons(self) -> None:
        request = self.mock_request()
        request.cookies = {"macaroon-maas": self.MACAROON}
        request.headers = {}

        provider = MacaroonAuthenticationProvider()
        macaroons_from_cookies = provider.extract_macaroons(request)
        self._check_macaroons(macaroons_from_cookies)

        request = self.mock_request()
        request.cookies = {}
        request.headers = {"Macaroons": self.MACAROON}

        provider = MacaroonAuthenticationProvider()
        macaroons_from_headers = provider.extract_macaroons(request)
        self._check_macaroons(macaroons_from_headers)

        request = self.mock_request()
        request.cookies = {}
        request.headers = {}

        provider = MacaroonAuthenticationProvider()
        no_macaroons = provider.extract_macaroons(request)
        assert no_macaroons == []


@pytest.mark.usefixtures("ensuremaasdb")
class TestValidateUserExternalAuthCandid:
    @pytest.fixture(autouse=True)
    async def prepare(self, db_connection: AsyncConnection, enable_candid):
        self.client = Mock(CandidAsyncClient)
        self.client.get_user_details.return_value = UserDetailsResponse(
            username="myusername",
            fullname="last",
            email="myusername@candid.example.com",
        )
        context = Context(connection=db_connection)
        cache = CacheForServices()
        self.request = Mock(Request)
        self.request.state.services = await ServiceCollectionV3.produce(
            context, cache
        )
        self.request.state.services.external_auth.get_candid_client = (
            AsyncMock(return_value=self.client)
        )
        self.provider = MacaroonAuthenticationProvider()
        self.default_last_check = (
            utcnow() - EXTERNAL_USER_CHECK_INTERVAL - timedelta(minutes=10)
        )

    async def test_interval_not_expired(self, fixture: Fixture):
        user = await create_test_user(fixture)
        last_check = utcnow() - timedelta(minutes=10)
        extra_details = {"auth_last_check": last_check}
        await create_test_user_profile(fixture, user.id, **extra_details)
        validated_user = await self.provider.validate_user_external_auth(
            self.request, user
        )
        assert validated_user is not None
        self.client.get_groups.assert_not_called()

    async def test_valid_user_check(self, fixture: Fixture):
        user = await create_test_user(fixture)
        extra_details = {"auth_last_check": self.default_last_check}
        await create_test_user_profile(fixture, user.id, **extra_details)
        self.client.get_groups.return_value = GetGroupsResponse(
            groups=["group1", "group2"]
        )
        validated_user = await self.provider.validate_user_external_auth(
            self.request, user
        )
        assert validated_user is not None
        assert validated_user.is_superuser is False
        assert validated_user.is_active is True
        assert validated_user.email == "myusername@candid.example.com"
        user_profile_updated = (
            await self.request.state.services.users.get_user_profile(
                validated_user.username
            )
        )
        assert user_profile_updated.auth_last_check > self.default_last_check
        self.client.get_groups.assert_called_once_with(validated_user.username)
        self.client.get_user_details.assert_called_once_with(
            validated_user.username
        )

    async def test_valid_user_check_admin(self, fixture: Fixture):
        user = await create_test_user(fixture)
        extra_details = {"auth_last_check": self.default_last_check}
        await create_test_user_profile(fixture, user.id, **extra_details)

        self.client.get_groups.return_value = GetGroupsResponse(
            groups=["group1", "group2", "admin"]
        )
        validated_user = await self.provider.validate_user_external_auth(
            self.request, user
        )
        assert validated_user is not None
        assert validated_user.is_superuser is True
        assert validated_user.is_active is True
        assert validated_user.email == "myusername@candid.example.com"
        self.client.get_groups.assert_called_once_with(user.username)
        self.client.get_user_details.assert_called_once_with(user.username)

    async def test_system_user_valid_no_check(self, fixture: Fixture):
        extra_details = {"username": WORKER_USERNAME}
        user = await create_test_user(fixture, **extra_details)
        validated_user = await self.provider.validate_user_external_auth(
            self.request, user
        )
        assert validated_user is not None
        self.client.get_groups.assert_not_called()

        extra_details = {"username": NODE_INIT_USERNAME}
        user = await create_test_user(fixture, **extra_details)
        validated_user = await self.provider.validate_user_external_auth(
            self.request, user
        )
        assert validated_user is not None
        self.client.get_groups.assert_not_called()

    async def test_valid_inactive_user_is_active(self, fixture: Fixture):
        extra_details = {"is_active": False}
        user = await create_test_user(fixture, **extra_details)
        extra_details = {"auth_last_check": self.default_last_check}
        await create_test_user_profile(fixture, user.id, **extra_details)
        self.client.get_groups.return_value = GetGroupsResponse(
            groups=["group1", "group2"]
        )
        validated_user = await self.provider.validate_user_external_auth(
            self.request, user
        )
        assert validated_user is not None
        assert validated_user.is_superuser is False
        assert validated_user.is_active is True
        assert validated_user.email == "myusername@candid.example.com"
        user_profile_updated = (
            await self.request.state.services.users.get_user_profile(
                validated_user.username
            )
        )
        assert user_profile_updated.auth_last_check > self.default_last_check
        self.client.get_groups.assert_called_once_with(user.username)
        self.client.get_user_details.assert_called_once_with(user.username)

    async def test_invalid_user_check(self, fixture: Fixture):
        user = await create_test_user(fixture)
        extra_details = {"auth_last_check": self.default_last_check}
        await create_test_user_profile(fixture, user.id, **extra_details)
        self.client.get_groups.side_effect = MacaroonApiException(
            404, "user not found"
        )
        validated_user = await self.provider.validate_user_external_auth(
            self.request, user
        )
        assert validated_user is None
        # fetch the user from the db to verify it's still active
        [user] = await fixture.get_typed(
            "auth_user", User, eq(UserTable.c.username, user.username)
        )
        assert user.is_active is True
        self.client.get_groups.assert_called_once_with(user.username)


@pytest.mark.usefixtures("ensuremaasdb")
class TestValidateUserExternalAuthRbac:
    @pytest.fixture(autouse=True)
    async def prepare(self, db_connection: AsyncConnection, enable_rbac):
        self.client = Mock(RbacAsyncClient)
        self.client.get_user_details.return_value = UserDetailsResponse(
            username="myusername",
            fullname="last",
            email="myusername@rbac.example.com",
        )
        context = Context(connection=db_connection)
        cache = CacheForServices()
        self.request = Mock(Request)
        self.request.state.services = await ServiceCollectionV3.produce(
            context,
            cache,
        )
        self.request.state.services.external_auth.get_rbac_client = AsyncMock(
            return_value=self.client
        )
        self.provider = MacaroonAuthenticationProvider()
        self.default_last_check = (
            utcnow() - EXTERNAL_USER_CHECK_INTERVAL - timedelta(minutes=10)
        )

    async def test_interval_not_expired(self, fixture: Fixture):
        user = await create_test_user(fixture)
        last_check = utcnow() - timedelta(minutes=10)
        extra_details = {"auth_last_check": last_check}
        await create_test_user_profile(fixture, user.id, **extra_details)
        validated_user = await self.provider.validate_user_external_auth(
            self.request, user
        )
        assert validated_user is not None
        self.client.allowed_for_user.assert_not_called()

    async def test_valid_user_check_has_pools_access(self, fixture: Fixture):
        user = await create_test_user(fixture)
        extra_details = {"auth_last_check": self.default_last_check}
        await create_test_user_profile(fixture, user.id, **extra_details)
        # not an admin, but has permission on pools
        self.client.is_user_admin.return_value = False
        self.client.get_resource_pool_ids.return_value = [
            PermissionResourcesMapping(
                permission=RbacPermission.VIEW, resources=["1", "2"]
            ),
            PermissionResourcesMapping(
                permission=RbacPermission.VIEW_ALL, resources=[]
            ),
            PermissionResourcesMapping(
                permission=RbacPermission.DEPLOY_MACHINES, resources=[]
            ),
            PermissionResourcesMapping(
                permission=RbacPermission.ADMIN_MACHINES, resources=[]
            ),
        ]
        validated_user = await self.provider.validate_user_external_auth(
            self.request, user
        )
        assert validated_user is not None
        assert validated_user.is_active is True
        assert validated_user.is_superuser is False
        assert validated_user.email == "myusername@rbac.example.com"

    async def test_valid_user_check_has_admin_access(self, fixture: Fixture):
        user = await create_test_user(fixture)
        extra_details = {"auth_last_check": self.default_last_check}
        await create_test_user_profile(fixture, user.id, **extra_details)
        # admin, but no permissions on pools
        self.client.is_user_admin.return_value = True

        self.client.get_resource_pool_ids.return_value = [
            PermissionResourcesMapping(
                permission=RbacPermission.VIEW, resources=[]
            ),
            PermissionResourcesMapping(
                permission=RbacPermission.VIEW_ALL, resources=[]
            ),
            PermissionResourcesMapping(
                permission=RbacPermission.DEPLOY_MACHINES, resources=[]
            ),
            PermissionResourcesMapping(
                permission=RbacPermission.ADMIN_MACHINES, resources=[]
            ),
        ]
        validated_user = await self.provider.validate_user_external_auth(
            self.request, user
        )
        assert validated_user is not None
        assert validated_user.is_active is True
        assert validated_user.is_superuser is True
        assert validated_user.email == "myusername@rbac.example.com"

    async def test_valid_user_no_permission(self, fixture: Fixture):
        user = await create_test_user(fixture)
        extra_details = {"auth_last_check": self.default_last_check}
        await create_test_user_profile(fixture, user.id, **extra_details)
        self.client.is_user_admin.return_value = False
        self.client.get_resource_pool_ids.return_value = [
            PermissionResourcesMapping(
                permission=RbacPermission.VIEW, resources=[]
            ),
            PermissionResourcesMapping(
                permission=RbacPermission.VIEW_ALL, resources=[]
            ),
            PermissionResourcesMapping(
                permission=RbacPermission.DEPLOY_MACHINES, resources=[]
            ),
            PermissionResourcesMapping(
                permission=RbacPermission.ADMIN_MACHINES, resources=[]
            ),
        ]
        validated_user = await self.provider.validate_user_external_auth(
            self.request, user
        )
        assert validated_user is not None
        assert validated_user.is_active is False
        assert validated_user.is_superuser is False
        assert validated_user.email == "myusername@rbac.example.com"

    async def test_system_user_valid_no_check(self, fixture: Fixture):
        extra_details = {"username": "MAAS"}
        user = await create_test_user(fixture, **extra_details)
        validated_user = await self.provider.validate_user_external_auth(
            self.request, user
        )
        assert validated_user is not None
        self.client.allowed_for_user.assert_not_called()

        extra_details = {"username": "maas-init-node"}
        user = await create_test_user(fixture, **extra_details)
        validated_user = await self.provider.validate_user_external_auth(
            self.request, user
        )
        assert validated_user is not None
        self.client.allowed_for_user.assert_not_called()

    async def test_valid_inactive_user_is_active(self, fixture: Fixture):
        extra_details = {"is_active": False}
        user = await create_test_user(fixture)
        extra_details = {"auth_last_check": self.default_last_check}
        await create_test_user_profile(fixture, user.id, **extra_details)
        self.client.is_user_admin.return_value = False
        self.client.get_resource_pool_ids.return_value = [
            PermissionResourcesMapping(
                permission=RbacPermission.VIEW, resources=["1", "2"]
            ),
            PermissionResourcesMapping(
                permission=RbacPermission.VIEW_ALL, resources=[]
            ),
            PermissionResourcesMapping(
                permission=RbacPermission.DEPLOY_MACHINES, resources=[]
            ),
            PermissionResourcesMapping(
                permission=RbacPermission.ADMIN_MACHINES, resources=[]
            ),
        ]
        validated_user = await self.provider.validate_user_external_auth(
            self.request, user
        )
        assert validated_user is not None
        assert validated_user.is_active is True

    async def test_failed_permission_check(self, fixture: Fixture):
        user = await create_test_user(fixture)
        extra_details = {"auth_last_check": self.default_last_check}
        await create_test_user_profile(fixture, user.id, **extra_details)
        self.client.is_user_admin.side_effect = MacaroonApiException(
            500, "fail!"
        )
        validated_user = await self.provider.validate_user_external_auth(
            self.request, user
        )
        assert validated_user is None
        self.client.is_user_admin.assert_called_once_with(user.username)

    async def test_failed_user_details_check(self, fixture: Fixture):
        user = await create_test_user(fixture)
        extra_details = {"auth_last_check": self.default_last_check}
        await create_test_user_profile(fixture, user.id, **extra_details)
        self.client.get_user_details.side_effect = MacaroonApiException(
            500, "fail!"
        )
        validated_user = await self.provider.validate_user_external_auth(
            self.request, user
        )
        assert validated_user is None
        self.client.get_user_details.assert_called_once_with(user.username)
