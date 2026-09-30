"""모든 스크립트가 공유하는 경로·헬퍼 (GNS3 판).

환경변수
  RUN_DIR   회차 산출물 디렉터리 (기본 runs/default)
  EPISODE   회차 번호 (기본 1)
  PARAMS    params.yaml 경로
"""
import base64
import configparser
import csv
import json
import os
import subprocess
import time
import urllib.error
import urllib.request

import yaml

import topo

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PARAMS_FILE = os.environ.get('PARAMS', os.path.join(BASE_DIR, 'params.yaml'))
RUN_DIR = os.environ.get('RUN_DIR', os.path.join(BASE_DIR, 'runs', 'default'))
EPISODE = int(os.environ.get('EPISODE', '1'))
PROJECT_FILE = os.path.join(BASE_DIR, 'gns3_project.json')

CAPACITY_MBPS = float(topo.CAPACITY_MBPS)
CTRL_PORT = topo.CTRL_PORT
SRV_IP = topo.SRV_IP
HOSTS = topo.CLIENTS
HOST_IP = {h: topo.HOSTS[h][0] for h in topo.CLIENTS}
STATS_INTERVAL = float(os.environ.get('STATS_INTERVAL', '10'))

# ---- 폭주 시나리오 (CHANGES.md 20번) ----
# high: 최고치 구간에서 호스트별로 보내는 속도(Mbps). low 는 모든 시나리오 공통 0.25 Mbps.
# detour_capacity: 우회 경로(S1 -> S3)에서 우리가 쓸 수 있는 대역(Mbps).
# 시나리오 이름은 LLM 프롬프트에 넣지 않는다. 운영자는 로그만 보고 판단해야 한다.
LOW_MBPS = 0.25
SCENARIOS = {
    'A': {'title': '균등형 - 스마트 계량기 정전 보고',
          'desc': '10대가 같은 양(1.8 Mbps)을 동시에 보낸다',
          'high': {h: 1.8 for h in HOSTS}, 'detour_capacity': 10.0},
    'B': {'title': '쏠림형 - 지능형 CCTV 이벤트 영상',
          'desc': 'h1~h3 은 3 Mbps 고화질 영상, 나머지 7대는 합 8 Mbps',
          'high': dict({h: 3.0 for h in HOSTS[:3]}, **{h: 8.0 / 7 for h in HOSTS[3:]}),
          'detour_capacity': 10.0},
    'C': {'title': '우회로 좁음 - 공유 망',
          'desc': 'A 와 같은 트래픽. 우회 경로를 다른 서비스가 4 Mbps 쓰고 있어 6 Mbps 만 남음',
          'high': {h: 1.8 for h in HOSTS}, 'detour_capacity': 6.0},
}


def scenario_name(name=None):
    n = (name or os.environ.get('SCENARIO') or 'A').upper()
    if n not in SCENARIOS:
        raise SystemExit('모르는 시나리오 %s (A, B, C 중 하나)' % n)
    return n


def scenario(name=None):
    return SCENARIOS[scenario_name(name)]


def detour_capacity(name=None):
    return scenario(name)['detour_capacity']

STATS_COLUMNS = ['ts', 'elapsed_s', 'default_mbps', 'default_util_pct',
                 'detour_mbps', 'detour_util_pct', 'drops', 'loss_pct', 'detour_open',
                 'default_drops', 'detour_drops']     # 뒤의 둘: drops 를 경로별로 나눈 것
EVENT_COLUMNS = ['ts', 'event', 'detail']


# ---- params.yaml ----
def load_params(path=PARAMS_FILE):
    with open(path) as f:
        p = yaml.safe_load(f) or {}
    p.setdefault('threshold', 80)
    p.setdefault('split_ratio', 50)
    p.setdefault('auto', True)
    p.setdefault('limits', {'threshold': [50, 95], 'split_ratio': [0, 100]})
    return p


def save_params(p, path=PARAMS_FILE):
    lines = ['# 컨트롤러가 읽는 튜닝 파라미터. advisor.py 가 회차마다 갱신한다.',
             'threshold: %d' % int(p['threshold']),
             'split_ratio: %d' % int(p['split_ratio']),
             'auto: %s' % ('true' if p.get('auto', True) else 'false'),
             'limits:',
             '  threshold: [%d, %d]' % tuple(p['limits']['threshold']),
             '  split_ratio: [%d, %d]' % tuple(p['limits']['split_ratio']), '']
    with open(path, 'w') as f:
        f.write('\n'.join(lines))


def detour_hosts(split_ratio):
    """split_ratio(%) 를 우회로로 보낼 호스트 수로. 호스트 10대면 50% -> 5대(h1~h5)."""
    n = int(len(HOSTS) * float(split_ratio) / 100.0 + 0.5)
    return max(0, min(len(HOSTS), n))


# ---- 회차 산출물 경로 ----
def stats_dir(run_dir=RUN_DIR):
    d = os.path.join(run_dir, 'stats')
    os.makedirs(d, exist_ok=True)
    return d


def stats_csv(ep=EPISODE, run_dir=RUN_DIR):
    return os.path.join(stats_dir(run_dir), 'ep_%d.csv' % ep)


def events_csv(ep=EPISODE, run_dir=RUN_DIR):
    return os.path.join(stats_dir(run_dir), 'ep_%d_events.csv' % ep)


def result_json(ep=EPISODE, run_dir=RUN_DIR):
    return os.path.join(stats_dir(run_dir), 'ep_%d_result.json' % ep)


def history_json(run_dir=RUN_DIR):
    return os.path.join(run_dir, 'history.json')


def state_json(run_dir=RUN_DIR):
    return os.path.join(run_dir, 'ctrl_state.json')


def cmd_file(run_dir=RUN_DIR):
    return os.path.join(run_dir, 'detour_cmd')


def append_event(event, detail='', ep=EPISODE, run_dir=RUN_DIR, ts=None):
    path = events_csv(ep, run_dir)
    new = not os.path.exists(path)
    with open(path, 'a', newline='') as f:
        w = csv.writer(f)
        if new:
            w.writerow(EVENT_COLUMNS)
        w.writerow(['%.3f' % (ts or time.time()), event, detail])


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline='') as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k, v in r.items():
            try:
                r[k] = float(v)
            except (TypeError, ValueError):
                pass
    return rows


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


# ---- GNS3 REST API ----
class Gns3:
    """~/.config/GNS3/2.2/gns3_server.conf 의 서버 주소·계정을 읽어 쓴다 (기존 lib.sh 와 같은 방식)."""

    def __init__(self):
        cp = configparser.ConfigParser()
        cp.read(os.path.expanduser('~/.config/GNS3/2.2/gns3_server.conf'))
        s = cp['Server'] if 'Server' in cp else {}
        self.base = 'http://%s:%s/v2' % (s.get('host', '127.0.0.1'), s.get('port', '3080'))
        self.auth = None
        if s.get('user'):
            self.auth = 'Basic ' + base64.b64encode(
                ('%s:%s' % (s.get('user'), s.get('password', ''))).encode()).decode()

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header('Content-Type', 'application/json')
        if self.auth:
            req.add_header('Authorization', self.auth)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                txt = r.read().decode()
        except urllib.error.HTTPError as e:
            raise RuntimeError('GNS3 API %s %s -> %d %s' % (method, path, e.code, e.read().decode()[:300]))
        return json.loads(txt) if txt else None

    def get(self, path):
        return self.call('GET', path)

    def post(self, path, body=None):
        return self.call('POST', path, body if body is not None else {})


def project_info():
    info = load_json(PROJECT_FILE)
    if not info:
        raise SystemExit('gns3_project.json 이 없다. 먼저 python3 gns3_build.py 를 실행해 프로젝트를 만들어라')
    return info


def ct(name, proj=None):
    """GNS3 도커 노드의 컨테이너 이름: GNS3.<노드>.<project_id>"""
    proj = proj or project_info()['project_id']
    return 'GNS3.%s.%s' % (name, proj)


def running_containers():
    out = subprocess.run(['docker', 'ps', '--format', '{{.Names}}'], capture_output=True, text=True).stdout
    return set(out.split())


def dexec(name, cmd, proj=None, check=False, **kw):
    """컨테이너 안에서 명령 실행. cmd 는 리스트."""
    kw.setdefault('capture_output', True)
    kw.setdefault('text', True)
    return subprocess.run(['docker', 'exec', ct(name, proj)] + cmd, check=check, **kw)


def dpopen(name, cmd, proj=None, **kw):
    return subprocess.Popen(['docker', 'exec', ct(name, proj)] + cmd, **kw)
