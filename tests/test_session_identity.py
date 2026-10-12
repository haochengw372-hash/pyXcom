import unittest
from unittest.mock import Mock, patch

import httpx

from pyxcom import XClient
from pyxcom.errors import AuthenticationError
from pyxcom.models import Profile
from pyxcom.transport import XTransport


class SessionIdentityTests(unittest.TestCase):
    def test_from_login_verifies_observer_and_passes_configuration(self):
        transport = Mock()
        transport.get_login_identity.return_value = '123'
        with patch('pyxcom.client.load_x_cookies', return_value={'ct0': 'private'}) as loader, patch('pyxcom.client.XTransport', return_value=transport) as factory:
            client = XClient.from_login(browser='edge', profile='Profile 3', cookie_db='/private/db', proxy='http://localhost:9999', delay=0.2, timeout=7)
        loader.assert_called_once_with('edge', profile='Profile 3', cookie_db='/private/db')
        factory.assert_called_once_with({'ct0': 'private'}, proxy='http://localhost:9999', timeout=7)
        self.assertEqual(client.observer_id, '123')
        self.assertEqual(client.delay, 0.2)
        with self.assertRaises(AttributeError):
            client.observer_id = 'target'
        client.close()

    def test_identity_change_is_rejected_without_relabelling(self):
        transport = Mock()
        transport.get_login_identity.side_effect = ['123', '456']
        with patch('pyxcom.client.load_x_cookies', return_value={}), patch('pyxcom.client.XTransport', return_value=transport):
            client = XClient.from_login()
        with self.assertRaises(AuthenticationError):
            client.check_login_identity()
        self.assertEqual(client.observer_id, '123')
        client.close()

    def test_unverified_client_has_no_observer_and_failed_factory_closes(self):
        transport = Mock()
        transport.get_login_identity.side_effect = AuthenticationError('unavailable')
        with patch('pyxcom.client.load_x_cookies', return_value={}), patch('pyxcom.client.XTransport', return_value=transport):
            client = XClient()
            self.assertIsNone(client.observer_id)
            with self.assertRaises(AuthenticationError):
                XClient.from_login()
        transport.close.assert_called_once()

    def test_authenticated_settings_is_resolved_without_target_cache(self):
        transport = XTransport({'auth_token': 'dummy', 'ct0': 'dummy'})
        self.addCleanup(transport.close)
        transport._operations = {'UserByScreenName': 'route'}
        transport._bearer = 'dummy'
        transport._x.close()
        requests = []
        transport._x = httpx.Client(transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200, json={'screen_name': 'Observer'})))
        with patch.object(transport, 'get_profile', return_value=Profile('987', 'Observer', 'Public name')) as profile:
            self.assertEqual(transport.get_login_identity(), '987')
        self.assertEqual(str(requests[0].url), 'https://api.x.com/1.1/account/settings.json')
        self.assertEqual(transport._operation_name(str(requests[0].url)), 'AccountSettings')
        profile.assert_called_once_with('Observer')

    def test_missing_settings_identity_cannot_use_a_target(self):
        transport = XTransport({'auth_token': 'dummy', 'ct0': 'dummy'})
        self.addCleanup(transport.close)
        transport._operations = {'UserByScreenName': 'route'}
        transport._x.close()
        transport._x = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={'user_id': 'target'})))
        with patch.object(transport, 'get_profile') as profile, self.assertRaises(AuthenticationError):
            transport.get_login_identity()
        profile.assert_not_called()

    def test_viewer_identity_uses_exact_authenticated_branch(self):
        transport = XTransport({"auth_token": "dummy", "ct0": "dummy"})
        self.addCleanup(transport.close)
        transport._operations = {"Viewer": "current-route"}
        transport._x.close()
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"data": {
                "unrelated": {"rest_id": "target"},
                "viewer": {"user_results": {"result": {"__typename": "User", "rest_id": "123"}}}
            }})
        transport._x = httpx.Client(transport=httpx.MockTransport(handler))
        self.assertEqual(transport.get_login_identity(), "123")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.path, "/i/api/graphql/current-route/Viewer")

    def test_empty_viewer_does_not_fall_back_to_other_ids(self):
        transport = XTransport({"auth_token": "dummy", "ct0": "dummy"})
        self.addCleanup(transport.close)
        transport._operations = {"Viewer": "current-route"}
        transport._x.close()
        requests = []
        transport._x = httpx.Client(transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200, json={"data": {"viewer": {}, "rest_id": "123"}})))
        with self.assertRaises(AuthenticationError):
            transport.get_login_identity()
        self.assertEqual(len(requests), 1)

    def test_viewer_auth_error_does_not_attempt_legacy_fallback(self):
        from pyxcom.errors import APIError
        transport = XTransport({"auth_token": "dummy", "ct0": "dummy"})
        self.addCleanup(transport.close)
        transport._operations = {"Viewer": "current-route"}
        transport._x.close()
        requests = []
        transport._x = httpx.Client(transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(401, json={"errors": [{"code": 32}]})))
        with self.assertRaises(APIError):
            transport.get_login_identity()
        self.assertEqual(len(requests), 1)
        self.assertEqual(transport.last_error["operation"], "Viewer")

    def test_missing_viewer_route_can_use_verified_legacy_settings(self):
        transport = XTransport({'auth_token': 'dummy', 'ct0': 'dummy'})
        self.addCleanup(transport.close)
        transport._operations = {'Viewer': 'removed-route'}
        transport._x.close()
        requests = []
        responses = [httpx.Response(404), httpx.Response(200, json={'screen_name': 'Observer'})]
        transport._x = httpx.Client(transport=httpx.MockTransport(lambda request: requests.append(request) or responses.pop(0)))
        with patch.object(transport, 'get_profile', return_value=Profile('987', 'Observer', 'Public name')):
            self.assertEqual(transport.get_login_identity(), '987')
        self.assertEqual(str(requests[1].url), 'https://api.x.com/1.1/account/settings.json')
        self.assertIsNone(transport.last_error)
