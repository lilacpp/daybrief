#!/usr/bin/env python3
"""Local Docker verification. Paid providers are never enabled."""
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.request import Request, urlopen

os.chdir(Path(__file__).resolve().parents[1])
os.environ['DAYBRIEF_MODE'] = 'mock'
base = 'http://127.0.0.1:' + os.environ.get('DAYBRIEF_PORT', '8080')


def compose(*args):
    subprocess.run(['docker', 'compose', *args], check=True)


def get(path):
    with urlopen(base + path, timeout=60) as response:
        return response.read()


compose('config', '--quiet')
compose('up', '-d', '--build', '--wait', '--wait-timeout', '180')
try:
    assert json.loads(get('/health')) == {'status': 'ok', 'mode': 'mock'}
    html = get('/').decode()
    assert 'Get the essentials.' in html
    assert json.loads(get('/api/topics'))
    chunks = re.findall(r'<script[^>]+src="([^"]+)"', html)
    assert chunks
    for chunk in chunks:
        js = get(chunk).decode()
        assert not any(url in js for url in ['http://localhost:8000', 'http://127.0.0.1:8000', 'http://backend:8000'])
    req = Request(base + '/api/briefs/generate', data=b'{"topic":"ai_technology","lookback_hours":24}', headers={'Content-Type': 'application/json'})
    with urlopen(req, timeout=120) as response:
        brief = json.load(response)
    assert brief['mode'] == 'mock' and len(brief['selected_events']) == 3
    path = '/api/briefs/' + brief['id']
    assert json.loads(get(path)) == brief
    compose('up', '-d', '--build', '--force-recreate', '--wait', '--wait-timeout', '180')
    assert json.loads(get(path)) == brief
    print('PASS: homepage, proxy API/health, browser bundles, mock generation, ID lookup, persistence:', brief['id'])
finally:
    compose('ps')
    compose('logs', '--tail', '80')
