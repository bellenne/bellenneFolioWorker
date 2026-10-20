import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from folio_worker import prompt_planner as p


class PlannerTests(unittest.TestCase):
    def run_plan(self, root, handler, prompt='Замените фон'):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        with patch.object(p.httpx, 'Client', return_value=client):
            return p.prepare({'prompt': prompt, 'width_cm': 40, 'height_cm': 60}, Path(root),
                [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,eA=='}}],
                key='test', base='https://api.aitunnel.ru/v1', mirror='https://ru-api.aitunnel.ru/v1',
                connect_timeout=60, read_timeout=60)

    def test_cache_and_guard(self):
        calls = []
        def handler(req):
            calls.append(req)
            body = json.loads(req.content)
            self.assertEqual(body['model'], p.MODEL)
            self.assertEqual(body['messages'][0]['content'], p.SYSTEM)
            self.assertEqual(body['messages'][1]['content'][0]['text'],
                'Формат картины: 40 на 60 см, вертикальный 2:3. Задание:\nЗамените фон')
            self.assertEqual(body['messages'][1]['content'][1]['type'], 'image_url')
            return httpx.Response(200, json={'choices':[{'finish_reason':'stop','message':{'content':'Красивый интерьер. ' * 12}}], 'usage':{'cost_rub':0.06}})
        with tempfile.TemporaryDirectory() as root:
            result = self.run_plan(root, handler)
            self.assertEqual(result, ('Красивый интерьер. ' * 12).strip())
            self.assertTrue(p.SYSTEM.endswith(p.GUARD))
            self.assertEqual(self.run_plan(root, handler), result)
            self.assertEqual(len(calls), 1)
            with self.assertRaises(RuntimeError):
                self.run_plan(root, handler, 'Другой запрос')

    def test_lost_response_not_retried(self):
        calls = []
        def handler(req):
            calls.append(req)
            raise httpx.ReadTimeout('lost')
        with tempfile.TemporaryDirectory() as root:
            for _ in range(2):
                with self.assertRaises(p.PlannerOutcomeUnknown):
                    self.run_plan(root, handler)
            self.assertEqual(len(calls), 1)

    def test_truncation_blocks_image_stage(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(RuntimeError):
                self.run_plan(root, lambda req:httpx.Response(200,json={'choices':[{'finish_reason':'length','message':{'content':'text '*100}}]}))
            self.assertFalse((Path(root)/'prompt-plan.json').exists())

    def test_connection_failure_uses_mirror(self):
        calls = []
        def handler(req):
            calls.append(req.url.host)
            if len(calls) == 1:
                raise httpx.ConnectError('DNS')
            return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':'Сохранить фон и позу. '*10}}]})
        with tempfile.TemporaryDirectory() as root:
            self.run_plan(root, handler)
            self.assertEqual(calls, ['api.aitunnel.ru','ru-api.aitunnel.ru'])
