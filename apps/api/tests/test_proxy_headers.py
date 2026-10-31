import json
from pathlib import Path
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI, Request
from uvicorn import Config
from uvicorn.main import main
from click.testing import CliRunner

from app.api.local_access import require_loopback

ROOT = Path(__file__).resolve().parents[3]


class ProxyHeadersTest(unittest.IsolatedAsyncioTestCase):
    async def test_supported_commands_ignore_forwarded_peer_and_scheme(self) -> None:
        commands = []
        for line in (ROOT / 'docker-compose.yml').read_text().splitlines():
            if 'command:' in line and 'uvicorn' in line:
                commands.append(json.loads(line.split('command:', 1)[1]))
        for line in (ROOT / 'apps/api/Dockerfile').read_text().splitlines():
            if line.startswith('CMD '):
                commands.append(json.loads(line[4:]))
        self.assertEqual(len(commands), 2)
        for command in commands:
            with self.subTest(command=command), patch('uvicorn.main.run') as run:
                result = CliRunner().invoke(main, command[command.index('uvicorn') + 1:])
                self.assertEqual(result.exit_code, 0, result.output)
                api = FastAPI()

                @api.get('/peer')
                async def peer(request: Request):
                    await require_loopback(request)
                    return {'peer': request.client.host, 'scheme': request.url.scheme}

                config = Config(api, proxy_headers=run.call_args.kwargs['proxy_headers'])
                config.load()
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=config.loaded_app,
                    client=('127.0.0.1', 1234)), base_url='http://localhost') as client:
                    response = await client.get('/peer', headers={'X-Forwarded-For': '127.0.0.2', 'X-Forwarded-Proto': 'https'})
                self.assertEqual(response.json(), {'peer': '127.0.0.1', 'scheme': 'http'})
