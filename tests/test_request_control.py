import json
import unittest
from unittest.mock import patch

import httpx

from pyxcom.errors import APIError, RateLimitError
from pyxcom.transport import XTransport


class RequestControlTests(unittest.TestCase):
    def transport(self, handler):
        transport = XTransport({'auth_token': 'private-token', 'ct0': 'private-csrf'})
        transport._x.close()
        transport._x = httpx.Client(transport=httpx.MockTransport(handler))
        transport._operations = {'TweetDetail': 'route'}
        transport._bearer = 'private-bearer'
        self.addCleanup(transport.close)
        return transport

    def test_retries_gate_and_observe_each_attempt_without_sensitive_values(self):
        responses = [503, 200]
        transport = self.transport(lambda request: httpx.Response(responses.pop(0), json={'data': {}}))
        events = []
        transport._request_gate = lambda operation: events.append(('gate', operation))
        transport._request_observer = lambda metadata: events.append(('result', metadata))
        with patch('pyxcom.transport.time.sleep'):
            transport.graphql('TweetDetail', {'focalTweetId': 'sensitive-target'})
        self.assertEqual([event[0] for event in events], ['gate', 'result', 'gate', 'result'])
        for index, code in [(1, 503), (3, 200)]:
            meta = events[index][1]
            self.assertEqual(meta['operation'], 'TweetDetail')
            self.assertEqual(meta['status_code'], code)
            self.assertEqual(meta['attempt'], (index + 1) // 2)
            self.assertGreaterEqual(meta['elapsed_seconds'], 0)
            self.assertGreater(meta['response_bytes'], 0)
            self.assertEqual(set(meta), {'operation', 'status_code', 'attempt', 'elapsed_seconds', 'response_bytes', 'reset_at', 'remaining', 'error_type'})
        self.assertNotIn('private', json.dumps(events))
        self.assertNotIn('sensitive-target', json.dumps(events))

    def test_budget_gate_prevents_retry_before_http_attempt(self):
        calls = []
        transport = self.transport(lambda request: calls.append(request) or httpx.Response(503))
        events = []
        def gate(operation):
            if calls:
                raise RuntimeError('budget exhausted')
        transport._request_gate = gate
        transport._request_observer = events.append
        with patch('pyxcom.transport.time.sleep'), self.assertRaisesRegex(RuntimeError, 'budget exhausted'):
            transport.graphql('TweetDetail', {})
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(events), 1)

    def test_network_failure_metadata_has_no_exception_text(self):
        def failure(request):
            raise httpx.ConnectError('SECRET https://host/token', request=request)
        transport = self.transport(failure)
        events = []
        transport._request_observer = events.append
        with patch('pyxcom.transport.time.sleep'), self.assertRaises(APIError) as caught:
            transport.graphql('TweetDetail', {})
        self.assertEqual(caught.exception.operation, 'TweetDetail')
        self.assertIsNone(caught.exception.status_code)
        self.assertEqual(len(events), 3)
        self.assertEqual(events[-1]['error_type'], 'ConnectError')
        self.assertNotIn('SECRET', json.dumps(events))

    def test_api_failure_carries_safe_endpoint_and_codes(self):
        transport = self.transport(lambda request: httpx.Response(401, json={'errors': [{'code': 32, 'message': 'SECRET'}]}))
        with self.assertRaises(APIError) as caught:
            transport.graphql('TweetDetail', {})
        self.assertEqual(caught.exception.status_code, 401)
        self.assertEqual(caught.exception.operation, 'TweetDetail')
        self.assertEqual(caught.exception.error_codes, (32,))
        self.assertNotIn('SECRET', str(caught.exception))

    def test_quota_metadata_is_numeric_and_exception_compatible(self):
        transport = self.transport(lambda request: httpx.Response(429, headers={'x-rate-limit-reset': '12345', 'x-rate-limit-remaining': '0'}))
        events = []
        transport._request_observer = events.append
        with self.assertRaises(RateLimitError) as caught:
            transport.graphql('TweetDetail', {})
        self.assertEqual(caught.exception.reset_at, 12345)
        self.assertEqual(caught.exception.remaining, 0)
        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(caught.exception.operation, 'TweetDetail')
        self.assertEqual(events[0]['reset_at'], 12345)
        self.assertEqual(RateLimitError('old', 42).reset_at, 42)
        self.assertEqual(str(APIError('old')), 'old')

    def test_discovery_requests_are_accounted_for(self):
        transport = self.transport(lambda request: httpx.Response(500))
        transport._operations = {}
        operations = []
        transport._request_gate = operations.append
        with self.assertRaises(APIError):
            transport.discover()
        self.assertEqual(operations, ['Homepage'])

    def test_last_error_survives_swallowed_failure_and_clears_on_new_operation(self):
        replies = [httpx.Response(403, json={"errors": [{"code": 179, "message": "secret"}]}), httpx.Response(200, json={"data": {}})]
        transport = self.transport(lambda request: replies.pop(0))
        try:
            transport.graphql("TweetDetail", {})
        except APIError:
            pass
        self.assertEqual(transport.last_error["error_codes"], [179])
        self.assertEqual(transport.last_error["status_code"], 403)
        self.assertNotIn("secret", json.dumps(transport.last_error))
        transport.graphql("TweetDetail", {})
        self.assertIsNone(transport.last_error)

    def test_invalid_rate_header_is_not_copied_into_evidence(self):
        transport = self.transport(lambda request: httpx.Response(429, headers={"x-rate-limit-reset": "secret", "x-rate-limit-remaining": "private"}))
        events = []
        transport._request_observer = events.append
        with self.assertRaises(RateLimitError) as caught:
            transport.graphql("TweetDetail", {})
        self.assertIsNone(caught.exception.reset_at)
        self.assertIsNone(caught.exception.remaining)
        self.assertIsNone(events[0]["reset_at"])
        self.assertNotIn("secret", json.dumps(events))
