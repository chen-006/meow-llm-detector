"""Offline regressions for refusals and reference-only candidates; no model requests."""
import json
from pathlib import Path
import tempfile
import unittest

from gpt56_vnext.detector import DetectorSession
from gpt56_vnext.errors import AppError, RequestError
from gpt56_vnext.protocol import parse_stream
from gpt56_vnext.security import SecretGuard
from gpt56_vnext.store import SQLiteStateStore

WORK = next(p for p in Path(__file__).resolve().parents if (p / 'gpt56_vnext').is_dir())
PACKAGE = WORK / 'benchmarks/official/meow-claude-other-cap98-efficient--4.5.3-predictive.3.meow.json'
SECRET = 'synthetic-refresh-secret'


def sse(events):
    return ''.join('data: ' + json.dumps(event) + '\n\n' for event in events)


class RefusalTests(unittest.TestCase):
    def code(self, body, mode):
        with self.assertRaises(RequestError) as caught:
            parse_stream(body, mode, SecretGuard([SECRET]))
        return caught.exception.code

    def test_claude_refusal_stop_reason(self):
        body = sse([{'type': 'message_start', 'message': {'id': 'm', 'usage': {}}},
                    {'type': 'message_delta', 'delta': {'stop_reason': 'refusal'}, 'usage': {}},
                    {'type': 'message_stop'}])
        self.assertEqual(self.code(body, 'claude'), 'response_refused')

    def test_chat_refusal_finish_reason(self):
        body = sse([{'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'refusal'}]}]) + 'data: [DONE]\n\n'
        self.assertEqual(self.code(body, 'chat'), 'response_refused')

    def test_other_stop_reasons_unchanged(self):
        body = sse([{'type': 'message_start', 'message': {'id': 'm', 'usage': {}}},
                    {'type': 'message_delta', 'delta': {'stop_reason': 'max_tokens'}, 'usage': {}},
                    {'type': 'message_stop'}])
        self.assertEqual(self.code(body, 'claude'), 'response_token_limit')


class ReferenceOnlyTests(unittest.TestCase):
    def session(self, config):
        package = json.loads(PACKAGE.read_text(encoding='utf-8'))
        with tempfile.TemporaryDirectory() as folder:
            with SQLiteStateStore(Path(folder) / 'state.sqlite3') as store:
                return DetectorSession(store, 'refresh', package, {'base_url': 'https://fixture.invalid/v1', **config}, SECRET)

    def test_reference_only_candidate_is_never_requested(self):
        for config in ({'claimed_model': 'other_known_external'},
                       {'claimed_model': 'claude-opus-5', 'request_model': 'other_known_external'},
                       {'claimed_model': 'claude-opus-5', 'request_model': 'reference-only:other'}):
            with self.subTest(config=config), self.assertRaises(AppError) as caught:
                self.session(config)
            self.assertEqual(caught.exception.code, 'virtual_reference_not_requestable')

    def test_real_candidate_still_requestable(self):
        session = self.session({'claimed_model': 'claude-opus-5', 'request_model': 'claude-opus-5'})
        self.assertEqual(session.config['request_model'], 'claude-opus-5')

    def test_non_string_alias_is_configuration_error(self):
        with self.assertRaises(AppError) as caught:
            self.session({'claimed_model': 'claude-opus-5', 'request_model': ['claude-opus-5']})
        self.assertEqual(caught.exception.code, 'invalid_detection_configuration')


if __name__ == '__main__':
    unittest.main()
