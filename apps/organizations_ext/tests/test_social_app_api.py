from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
from allauth.socialaccount.models import SocialApp
from django.test import AsyncClient, TestCase, override_settings
from django.urls import reverse
from model_bakery import baker
from ninja.errors import HttpError

from apps.organizations_ext.constants import OrganizationUserRole
from apps.organizations_ext.models import OrganizationSocialApp
from apps.organizations_ext.social_app_api import validate_server_url
from apps.stripe.constants import SubscriptionStatus
from apps.users.utils import aorg_social_app_exists
from glitchtip.adapters import (
    CustomSocialAccountAdapter,
    PublicOnlyConnector,
    PublicOnlyResolver,
)

VALID_PAYLOAD = {
    "name": "Corp Okta",
    "serverUrl": "https://corp.okta.example.com",
    "clientID": "client-id-1",
    "clientSecret": "sekrit",
}


async def make_sso_subscription(organization):
    return await baker.amake(
        "stripe.StripeSubscription",
        organization=organization,
        status=SubscriptionStatus.ACTIVE,
        price__product__allow_sso=True,
    )


class OrganizationSocialAppAPITestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = baker.make("users.user")
        cls.organization = baker.make(
            "organizations_ext.Organization",
            name="Organization with a long name for OIDC",
        )
        cls.organization.add_user(cls.user)  # First user becomes OWNER
        cls.member = baker.make("users.user")
        cls.organization.add_user(cls.member, role=OrganizationUserRole.MEMBER)
        cls.list_url = reverse(
            "api:list_organization_social_apps", args=[cls.organization.slug]
        )

    def setUp(self):
        self.client.force_login(self.user)
        self.async_client.force_login(self.user)

    async def test_create(self):
        await make_sso_subscription(self.organization)
        res = await self.async_client.post(
            self.list_url, VALID_PAYLOAD, "application/json"
        )
        self.assertEqual(res.status_code, 201)
        data = res.json()
        self.assertEqual(data["name"], "Corp Okta")
        self.assertEqual(data["provider"], "openid_connect")
        self.assertTrue(data["providerID"].startswith(self.organization.slug[:150]))
        self.assertIn(data["providerID"], data["callbackUrl"])
        self.assertIn(f"/login/{self.organization.slug}", data["loginUrl"])
        self.assertEqual(data["serverUrl"], "https://corp.okta.example.com")
        self.assertNotIn("sekrit", res.content.decode())

        social_app = await SocialApp.objects.aget(provider_id=data["providerID"])
        self.assertEqual(social_app.secret, "sekrit")
        self.assertTrue(
            await OrganizationSocialApp.objects.filter(
                organization=self.organization, social_app=social_app
            ).aexists()
        )

    async def test_create_google(self):
        """Google preset: no server URL needed, OIDC under the hood."""
        await make_sso_subscription(self.organization)
        payload = {
            "name": "Google",
            "provider": "google",
            "clientID": "gcp-client",
            "clientSecret": "gcp-secret",
        }
        res = await self.async_client.post(self.list_url, payload, "application/json")
        self.assertEqual(res.status_code, 201)
        data = res.json()
        # Runs on the OIDC machinery so each app has its own callback URL
        self.assertEqual(data["provider"], "openid_connect")
        self.assertEqual(data["brand"], "google")
        self.assertEqual(data["serverUrl"], "https://accounts.google.com")
        self.assertIn(data["providerID"], data["callbackUrl"])

    async def test_create_custom_requires_server_url(self):
        await make_sso_subscription(self.organization)
        res = await self.async_client.post(
            self.list_url,
            {k: v for k, v in VALID_PAYLOAD.items() if k != "serverUrl"},
            "application/json",
        )
        self.assertEqual(res.status_code, 400)

    async def test_update_google_keeps_issuer(self):
        await make_sso_subscription(self.organization)
        res = await self.async_client.post(
            self.list_url,
            {
                "name": "Google",
                "provider": "google",
                "clientID": "gcp-client",
                "clientSecret": "gcp-secret",
            },
            "application/json",
        )
        app_id = res.json()["id"]
        url = reverse(
            "api:update_organization_social_app",
            args=[self.organization.slug, app_id],
        )
        res = await self.async_client.put(
            url,
            {
                "name": "Google",
                "clientID": "new-client",
                "serverUrl": "https://evil.example.com",
            },
            "application/json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["serverUrl"], "https://accounts.google.com")
        self.assertEqual(res.json()["clientID"], "new-client")

    async def test_create_requires_sso_plan(self):
        res = await self.async_client.post(
            self.list_url, VALID_PAYLOAD, "application/json"
        )
        self.assertEqual(res.status_code, 403)

        await baker.amake(
            "stripe.StripeSubscription",
            organization=self.organization,
            status=SubscriptionStatus.ACTIVE,
            price__product__allow_sso=False,
        )
        res = await self.async_client.post(
            self.list_url, VALID_PAYLOAD, "application/json"
        )
        self.assertEqual(res.status_code, 403)

    @override_settings(BILLING_ENABLED=False, ENABLE_ORGANIZATION_CREATION=False)
    async def test_create_self_hosted(self):
        """Self-host has no subscription to check, so creation is ungated."""
        res = await self.async_client.post(
            self.list_url, VALID_PAYLOAD, "application/json"
        )
        self.assertEqual(res.status_code, 201)

    async def test_create_requires_manager(self):
        await make_sso_subscription(self.organization)
        await self.async_client.aforce_login(self.member)
        res = await self.async_client.post(
            self.list_url, VALID_PAYLOAD, "application/json"
        )
        self.assertEqual(res.status_code, 403)

    async def test_create_as_manager_role(self):
        """The permission floor is manager, not owner."""
        await make_sso_subscription(self.organization)
        manager = await baker.amake("users.user")
        await self.organization.aadd_user(manager, role=OrganizationUserRole.MANAGER)
        await self.async_client.aforce_login(manager)
        res = await self.async_client.post(
            self.list_url, VALID_PAYLOAD, "application/json"
        )
        self.assertEqual(res.status_code, 201)

    async def test_oidc_options_round_trip(self):
        await make_sso_subscription(self.organization)
        options = {
            "tokenAuthMethod": "client_secret_post",
            "oauthPkceEnabled": True,
            "fetchUserinfo": False,
        }
        res = await self.async_client.post(
            self.list_url, {**VALID_PAYLOAD, **options}, "application/json"
        )
        self.assertEqual(res.status_code, 201)
        data = res.json()
        for key, value in options.items():
            self.assertEqual(data[key], value)
        app_id = data["id"]
        app = await SocialApp.objects.aget(id=app_id)
        self.assertEqual(
            app.settings,
            {
                "server_url": VALID_PAYLOAD["serverUrl"],
                "token_auth_method": "client_secret_post",
                "oauth_pkce_enabled": True,
                "fetch_userinfo": False,
            },
        )
        url = reverse(
            "api:update_organization_social_app", args=[self.organization.slug, app_id]
        )
        # Existing clients omit the new fields; their updates must preserve them.
        res = await self.async_client.put(url, VALID_PAYLOAD, "application/json")
        self.assertEqual(res.status_code, 200)
        for key, value in options.items():
            self.assertEqual(res.json()[key], value)
        res = await self.async_client.put(
            url,
            {
                **VALID_PAYLOAD,
                "tokenAuthMethod": None,
                "oauthPkceEnabled": False,
                "fetchUserinfo": True,
            },
            "application/json",
        )
        self.assertEqual(res.status_code, 200)
        app = await SocialApp.objects.aget(id=app_id)
        self.assertNotIn("token_auth_method", app.settings)
        self.assertFalse(app.settings["oauth_pkce_enabled"])
        self.assertTrue(app.settings["fetch_userinfo"])
        listed = (await self.async_client.get(self.list_url)).json()[0]
        self.assertIsNone(listed["tokenAuthMethod"])
        self.assertFalse(listed["oauthPkceEnabled"])
        self.assertTrue(listed["fetchUserinfo"])
        self.assertNotIn("sekrit", str(listed))

    async def test_rejects_unsupported_token_auth_method(self):
        await make_sso_subscription(self.organization)
        res = await self.async_client.post(
            self.list_url,
            {
                **VALID_PAYLOAD,
                "tokenAuthMethod": "arbitrary-method",
            },
            "application/json",
        )
        self.assertEqual(res.status_code, 422)
        self.assertFalse(await SocialApp.objects.aexists())

    async def test_instance_options_require_superuser(self):
        await make_sso_subscription(self.organization)
        for field in ["isPublic", "emailAuthentication"]:
            with self.subTest(field=field):
                res = await self.async_client.post(
                    self.list_url,
                    {
                        **VALID_PAYLOAD,
                        field: True,
                    },
                    "application/json",
                )
                self.assertEqual(res.status_code, 403)
        self.assertFalse(await SocialApp.objects.aexists())
        res = await self.async_client.post(
            self.list_url, VALID_PAYLOAD, "application/json"
        )
        app_id = res.json()["id"]
        url = reverse(
            "api:update_organization_social_app", args=[self.organization.slug, app_id]
        )
        for field in ["isPublic", "emailAuthentication"]:
            with self.subTest(field=field):
                res = await self.async_client.put(
                    url,
                    {
                        **VALID_PAYLOAD,
                        field: True,
                        "name": "Must not persist",
                    },
                    "application/json",
                )
                self.assertEqual(res.status_code, 403)
        app = await SocialApp.objects.aget(id=app_id)
        self.assertEqual(app.name, VALID_PAYLOAD["name"])
        self.assertNotIn("email_authentication", app.settings)
        self.assertFalse(
            (await OrganizationSocialApp.objects.aget(social_app_id=app_id)).is_public
        )

    async def test_superuser_can_manage_instance_options_with_api_token(self):
        await make_sso_subscription(self.organization)
        self.user.is_superuser = True
        await self.user.asave(update_fields=["is_superuser"])
        token = await baker.amake("api_tokens.APIToken", user=self.user)
        setattr(token.scopes, "org:admin", True)
        await token.asave(update_fields=["scopes"])
        client = AsyncClient(AUTHORIZATION=f"Bearer {token.token}")
        res = await client.post(
            self.list_url,
            {
                **VALID_PAYLOAD,
                "isPublic": True,
                "emailAuthentication": True,
            },
            "application/json",
        )
        self.assertEqual(res.status_code, 201)
        data = res.json()
        self.assertTrue(data["isPublic"])
        self.assertTrue(data["emailAuthentication"])
        public = (await client.get(reverse("api:get_settings"))).json()["socialApps"]
        self.assertEqual([app["provider"] for app in public], [data["providerID"]])
        url = reverse(
            "api:update_organization_social_app",
            args=[self.organization.slug, data["id"]],
        )
        res = await client.put(url, VALID_PAYLOAD, "application/json")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["isPublic"])
        self.assertTrue(res.json()["emailAuthentication"])
        res = await client.put(
            url,
            {
                **VALID_PAYLOAD,
                "isPublic": False,
                "emailAuthentication": False,
            },
            "application/json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.json()["isPublic"])
        self.assertFalse(res.json()["emailAuthentication"])
        self.assertEqual(
            (await client.get(reverse("api:get_settings"))).json()["socialApps"], []
        )

    async def test_manager_cannot_modify_provider_trusted_for_email_authentication(
        self,
    ):
        await make_sso_subscription(self.organization)
        res = await self.async_client.post(
            self.list_url, VALID_PAYLOAD, "application/json"
        )
        app = await SocialApp.objects.aget(id=res.json()["id"])
        app.settings["email_authentication"] = True
        await app.asave(update_fields=["settings"])
        url = reverse(
            "api:update_organization_social_app", args=[self.organization.slug, app.id]
        )
        # Changing a trusted issuer would let an org manager authenticate as existing users.
        res = await self.async_client.put(
            url,
            {
                **VALID_PAYLOAD,
                "serverUrl": "https://other.example.com",
            },
            "application/json",
        )
        self.assertEqual(res.status_code, 403)
        await app.arefresh_from_db()
        self.assertEqual(app.settings["server_url"], VALID_PAYLOAD["serverUrl"])

    async def test_failed_link_leaves_no_orphan_social_app(self):
        """
        A SocialApp with no OrganizationSocialApp reads as instance-wide, so a
        half-finished create would publish the org's provider on the
        instance-wide login page.
        """
        await make_sso_subscription(self.organization)
        with (
            patch.object(
                OrganizationSocialApp.objects, "create", side_effect=OSError("db gone")
            ),
            self.assertRaises(OSError),
        ):
            await self.async_client.post(
                self.list_url, VALID_PAYLOAD, "application/json"
            )
        self.assertFalse(await SocialApp.objects.aexists())

    async def test_create_rejects_unsafe_server_urls(self):
        await make_sso_subscription(self.organization)
        for server_url in [
            "http://idp.example.com",
            "https://10.0.0.5/realm",
            "https://127.0.0.1",
            "https://localhost",
            "https://intranet",
            "https://keycloak.corp.internal",
            "https://user:pass@idp.example.com",
        ]:
            res = await self.async_client.post(
                self.list_url,
                {**VALID_PAYLOAD, "serverUrl": server_url},
                "application/json",
            )
            self.assertEqual(res.status_code, 400, server_url)
        self.assertFalse(await SocialApp.objects.aexists())

    async def test_list(self):
        social_app = await baker.amake(
            "socialaccount.socialapp",
            provider="openid_connect",
            provider_id="my-oidc",
            settings={"server_url": "https://idp.example.com"},
        )
        await baker.amake(
            "organizations_ext.OrganizationSocialApp",
            organization=self.organization,
            social_app=social_app,
            is_public=False,
        )
        other_org_app = await baker.amake("socialaccount.socialapp")
        await baker.amake(
            "organizations_ext.OrganizationSocialApp",
            organization=await baker.amake("organizations_ext.Organization"),
            social_app=other_org_app,
        )
        res = await self.async_client.get(self.list_url)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["providerID"], "my-oidc")

    async def test_list_requires_manager(self):
        await self.async_client.aforce_login(self.member)
        res = await self.async_client.get(self.list_url)
        self.assertEqual(res.status_code, 403)

    async def test_update(self):
        await make_sso_subscription(self.organization)
        res = await self.async_client.post(
            self.list_url, VALID_PAYLOAD, "application/json"
        )
        app_id = res.json()["id"]
        url = reverse(
            "api:update_organization_social_app",
            args=[self.organization.slug, app_id],
        )
        res = await self.async_client.put(
            url,
            {**VALID_PAYLOAD, "name": "Renamed", "clientSecret": None},
            "application/json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["name"], "Renamed")
        social_app = await SocialApp.objects.aget(id=app_id)
        self.assertEqual(social_app.secret, "sekrit", "Omitted secret is kept")

        res = await self.async_client.put(
            url, {**VALID_PAYLOAD, "clientSecret": "new-secret"}, "application/json"
        )
        self.assertEqual(res.status_code, 200)
        social_app = await SocialApp.objects.aget(id=app_id)
        self.assertEqual(social_app.secret, "new-secret")

    async def test_update_non_oidc_app_rejected(self):
        social_app = await baker.amake("socialaccount.socialapp", provider="google")
        await baker.amake(
            "organizations_ext.OrganizationSocialApp",
            organization=self.organization,
            social_app=social_app,
        )
        url = reverse(
            "api:update_organization_social_app",
            args=[self.organization.slug, social_app.id],
        )
        res = await self.async_client.put(url, VALID_PAYLOAD, "application/json")
        self.assertEqual(res.status_code, 400)

    async def test_delete(self):
        await make_sso_subscription(self.organization)
        res = await self.async_client.post(
            self.list_url, VALID_PAYLOAD, "application/json"
        )
        app_id = res.json()["id"]
        url = reverse(
            "api:delete_organization_social_app",
            args=[self.organization.slug, app_id],
        )
        res = await self.async_client.delete(url)
        self.assertEqual(res.status_code, 204)
        self.assertFalse(await SocialApp.objects.filter(id=app_id).aexists())
        self.assertFalse(await OrganizationSocialApp.objects.aexists())

    async def test_delete_other_organizations_app_not_found(self):
        social_app = await baker.amake("socialaccount.socialapp")
        await baker.amake(
            "organizations_ext.OrganizationSocialApp",
            organization=await baker.amake("organizations_ext.Organization"),
            social_app=social_app,
        )
        url = reverse(
            "api:delete_organization_social_app",
            args=[self.organization.slug, social_app.id],
        )
        res = await self.async_client.delete(url)
        self.assertEqual(res.status_code, 404)
        self.assertTrue(await SocialApp.objects.filter(id=social_app.id).aexists())


class LoginSettingsTestCase(TestCase):
    """Public login page endpoints and social app visibility."""

    @classmethod
    def setUpTestData(cls):
        cls.organization = baker.make("organizations_ext.Organization")
        cls.private_app = baker.make(
            "socialaccount.socialapp",
            name="Private Okta",
            provider="openid_connect",
            provider_id="private-oidc",
            settings={"server_url": "https://idp.example.com"},
        )
        baker.make(
            "organizations_ext.OrganizationSocialApp",
            organization=cls.organization,
            social_app=cls.private_app,
            is_public=False,
        )
        cls.public_app = baker.make("socialaccount.socialapp", provider="github")

    async def test_global_settings_hides_private_apps(self):
        res = await self.async_client.get(reverse("api:get_settings"))
        social_apps = res.json()["socialApps"]
        self.assertEqual(len(social_apps), 1)
        self.assertEqual(social_apps[0]["provider"], "github")

    async def test_global_settings_shows_public_org_apps(self):
        await OrganizationSocialApp.objects.aupdate(is_public=True)
        res = await self.async_client.get(reverse("api:get_settings"))
        self.assertEqual(len(res.json()["socialApps"]), 2)

    async def test_organization_login_settings(self):
        url = reverse(
            "api:get_organization_login_settings", args=[self.organization.slug]
        )
        res = await self.async_client.get(url)  # No auth required
        self.assertEqual(res.status_code, 200)
        social_apps = res.json()["socialApps"]
        self.assertEqual(len(social_apps), 1)
        self.assertEqual(social_apps[0]["name"], "Private Okta")
        # The headless provider redirect expects provider_id for OIDC apps
        self.assertEqual(social_apps[0]["provider"], "private-oidc")

    async def test_organization_login_settings_unknown_slug(self):
        """Unknown slugs return an empty list, not 404 (no org enumeration)."""
        url = reverse("api:get_organization_login_settings", args=["no-such-org"])
        res = await self.async_client.get(url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["socialApps"], [])

    async def test_organization_login_settings_deleted_org(self):
        self.organization.is_deleted = True
        await self.organization.asave(update_fields=["is_deleted"])
        url = reverse(
            "api:get_organization_login_settings", args=[self.organization.slug]
        )
        res = await self.async_client.get(url)
        self.assertEqual(res.json()["socialApps"], [])


class OrgSocialAppSignupTestCase(TestCase):
    """Org-linked social apps allow signup even when open signup is off."""

    @classmethod
    def setUpTestData(cls):
        # An existing user, so signup isn't open due to an empty instance
        baker.make("users.user")
        cls.social_app = baker.make(
            "socialaccount.socialapp",
            provider="openid_connect",
            provider_id="org-oidc",
        )
        baker.make(
            "organizations_ext.OrganizationSocialApp",
            organization=baker.make("organizations_ext.Organization"),
            social_app=cls.social_app,
        )

    @override_settings(ENABLE_SOCIAL_APPS_USER_REGISTRATION=False)
    async def test_org_social_app_allows_signup(self):
        adapter = CustomSocialAccountAdapter()
        sociallogin = SimpleNamespace(account=SimpleNamespace(provider="org-oidc"))
        self.assertTrue(await adapter.ais_open_for_signup(None, sociallogin))

        sociallogin = SimpleNamespace(account=SimpleNamespace(provider="github"))
        self.assertFalse(await adapter.ais_open_for_signup(None, sociallogin))

    async def test_org_social_app_exists_matches_provider_id_only(self):
        # provider_id is set, so the plain provider name must not match
        self.assertTrue(await aorg_social_app_exists("org-oidc"))
        self.assertFalse(await aorg_social_app_exists("openid_connect"))


class ValidateServerUrlTestCase(TestCase):
    def test_normalizes_trailing_slash(self):
        self.assertEqual(
            validate_server_url("https://idp.example.com/realm/"),
            "https://idp.example.com/realm",
        )

    def test_accepts_well_known_url(self):
        url = "https://idp.example.com/.well-known/openid-configuration"
        self.assertEqual(validate_server_url(url), url)

    def test_rejects_ipv6_loopback(self):
        with self.assertRaises(HttpError):
            validate_server_url("https://[::1]/realm")

    def test_rejects_link_local(self):
        with self.assertRaises(HttpError):
            validate_server_url("https://169.254.169.254/latest")


class PublicOnlyHttpTestCase(TestCase):
    """The connection-time SSRF guard for org-supplied IdP URLs."""

    async def test_connector_rejects_private_ip_literals(self):
        # TCPConnector never consults the resolver for IP-literal hosts, so
        # the connector itself must reject them (e.g. a discovery document
        # pointing its token_endpoint at cloud metadata).
        connector = PublicOnlyConnector()
        try:
            for host in ["169.254.169.254", "127.0.0.1", "10.0.0.5", "::1"]:
                with self.assertRaises(aiohttp.ClientConnectionError, msg=host):
                    await connector._resolve_host(host, 443)
        finally:
            await connector.close()

    async def test_resolver_rejects_private_resolution(self):
        # A public hostname resolving (or rebinding) to a private address
        resolver = PublicOnlyResolver()
        resolver._resolver = AsyncMock()
        resolver._resolver.resolve.return_value = [
            {"hostname": "evil.example.com", "host": "169.254.169.254", "port": 443}
        ]
        with self.assertRaises(aiohttp.ClientConnectionError):
            await resolver.resolve("evil.example.com", 443)

    async def test_resolver_allows_public_resolution(self):
        resolver = PublicOnlyResolver()
        resolver._resolver = AsyncMock()
        resolver._resolver.resolve.return_value = [
            {"hostname": "idp.example.com", "host": "93.184.216.34", "port": 443}
        ]
        infos = await resolver.resolve("idp.example.com", 443)
        self.assertEqual(infos[0]["host"], "93.184.216.34")
