#!/usr/bin/env python3
"""GNS3 프로젝트를 topo.py 대로 만든다 / 켠다 / 끈다 / 상태를 본다. sudo 불필요.

  python3 gns3_build.py create    프로젝트·노드·링크 생성 (이미 있으면 재사용). gns3_project.json 에 id 저장
  python3 gns3_build.py start     노드 기동. OVS 는 한 대씩 (동시에 켜면 랜카드가 안 붙는 함정)
  python3 gns3_build.py stop      노드 정지 (컨테이너가 버려지므로 다시 켜면 apply.py 를 다시 돌려야 한다)
  python3 gns3_build.py status    노드 상태
  python3 gns3_build.py delete    프로젝트 삭제 (확인 문구 필요)
"""
import sys
import time

import common
import topo


def find_project(g):
    for p in g.get('/projects'):
        if p['name'] == topo.PROJECT_NAME:
            return p
    return None


def create(g):
    p = find_project(g)
    if p:
        print('프로젝트 %s 가 이미 있다 (%s). 노드만 확인한다' % (p['name'], p['project_id']))
    else:
        p = g.post('/projects', {'name': topo.PROJECT_NAME})
        print('프로젝트 생성: %s %s' % (p['name'], p['project_id']))
    pid = p['project_id']
    if p['status'] != 'opened':
        g.post('/projects/%s/open' % pid)
    nodes = {n['name']: n for n in g.get('/projects/%s/nodes' % pid)}

    def add(name, body):
        x, y = topo.POS[name]
        if name in nodes:
            n = nodes[name]
            want = body.get('properties', {}).get('adapters')
            if want and n['properties'].get('adapters') != want:      # 랜카드 수 변경은 정지 상태에서만 된다
                if n['status'] == 'started':
                    g.post('/projects/%s/nodes/%s/stop' % (pid, n['node_id']))
                    time.sleep(3)
                n = g.call('PUT', '/projects/%s/nodes/%s' % (pid, n['node_id']), {'properties': {'adapters': want}})
                print('  %-8s 랜카드 %d개로 변경 (정지됨 - start 후 apply.py 필요)' % (name, want))
            if (n['x'], n['y']) != (x, y):
                n = g.call('PUT', '/projects/%s/nodes/%s' % (pid, n['node_id']), {'x': x, 'y': y})
            nodes[name] = n
            return n
        body.update({'name': name, 'compute_id': 'local', 'x': x, 'y': y})
        n = g.post('/projects/%s/nodes' % pid, body)
        if n['name'] != name:            # GNS3 가 이름을 멋대로 바꾸는 경우가 있다 (s1 -> s3)
            n = g.call('PUT', '/projects/%s/nodes/%s' % (pid, n['node_id']), {'name': name})
            if n['name'] != name:
                raise SystemExit('GNS3 가 노드 이름 %s 를 %s 로 바꿨다. topo.py 의 이름을 바꿀 것' % (name, n['name']))
        nodes[name] = n
        print('  노드 생성 %-8s %s' % (name, body['node_type']))
        return n

    for sw in topo.SWITCHES:
        add(sw, {'node_type': 'docker', 'console_type': 'telnet', 'properties': {'image': topo.OVS_IMAGE, 'adapters': topo.OVS_ADAPTERS[sw]}})
    for h in topo.HOSTS:
        add(h, {'node_type': 'docker', 'console_type': 'telnet', 'properties': {'image': topo.HOST_IMAGE, 'adapters': 1}})
    add('Switch1', {'node_type': 'ethernet_switch', 'properties': {}})
    add('Cloud1', {'node_type': 'cloud', 'properties': {'ports_mapping': [
        {'interface': topo.MGMT_BRIDGE, 'name': topo.MGMT_BRIDGE, 'port_number': 0, 'type': 'ethernet'}]}})

    # 링크: (노드, adapter) 쌍. 도커 노드는 adapter=eth 번호, port=0
    want = []
    for sw in topo.SWITCHES:                                     # 관리망
        want.append(((sw, topo.eth_index(topo.MGMT_IF)), ('Switch1', topo.MGMT_SWITCH_PORT[sw])))
    want.append((('Cloud1', 0), ('Switch1', 7)))
    for h, (ip, sw, ifn) in topo.HOSTS.items():                  # 호스트
        want.append(((h, 0), (sw, topo.eth_index(ifn))))
    for a, a_if, b, b_if in topo.LINKS:                          # 스위치 간
        want.append(((a, topo.eth_index(a_if)), (b, topo.eth_index(b_if))))

    have = set()
    for l in g.get('/projects/%s/links' % pid):
        have.add(frozenset((x['node_id'], x['adapter_number'], x['port_number']) for x in l['nodes']))

    def endpoint(name, adapter):
        n = nodes[name]
        if n['node_type'] in ('ethernet_switch', 'cloud'):
            return {'node_id': n['node_id'], 'adapter_number': 0, 'port_number': adapter}
        return {'node_id': n['node_id'], 'adapter_number': adapter, 'port_number': 0}

    made = 0
    for (a, ai), (b, bi) in want:
        ea, eb = endpoint(a, ai), endpoint(b, bi)
        key = frozenset((e['node_id'], e['adapter_number'], e['port_number']) for e in (ea, eb))
        if key in have:
            continue
        g.post('/projects/%s/links' % pid, {'nodes': [ea, eb]})
        made += 1
    print('링크 %d개 새로 만듦 (총 %d개)' % (made, len(want)))
    common.save_json(common.PROJECT_FILE, {'project_id': pid, 'name': topo.PROJECT_NAME,
                                           'nodes': {k: v['node_id'] for k, v in nodes.items()}})
    print('저장: %s' % common.PROJECT_FILE)


def nodes_of(g, pid):
    return {n['name']: n for n in g.get('/projects/%s/nodes' % pid)}


def start(g):
    pid = common.project_info()['project_id']
    if g.get('/projects/%s' % pid)['status'] != 'opened':
        g.post('/projects/%s/open' % pid)
    nodes = nodes_of(g, pid)
    order = ['Cloud1', 'Switch1'] + list(topo.SWITCHES) + list(topo.HOSTS)
    for name in order:
        n = nodes[name]
        if n['status'] == 'started':
            print('  %-8s 이미 켜짐' % name)
            continue
        g.post('/projects/%s/nodes/%s/start' % (pid, n['node_id']))
        print('  %-8s 기동' % name)
        if name in topo.SWITCHES:
            time.sleep(4)          # OVS 는 한 대씩
        else:
            time.sleep(1)
    time.sleep(2)
    status(g)


def stop(g):
    pid = common.project_info()['project_id']
    for name, n in nodes_of(g, pid).items():
        if n['status'] == 'started':
            g.post('/projects/%s/nodes/%s/stop' % (pid, n['node_id']))
            print('  %-8s 정지' % name)


def status(g):
    pid = common.project_info()['project_id']
    if g.get('/projects/%s' % pid)['status'] != 'opened':
        print('  프로젝트가 닫혀 있다 (노드 전부 꺼짐). python3 gns3_build.py start 로 열고 켤 것')
        return
    running = common.running_containers()
    for name, n in sorted(nodes_of(g, pid).items()):
        extra = ''
        if n['node_type'] == 'docker' and n['status'] == 'started':
            if common.ct(name, pid) in running:
                r = common.dexec(name, ['sh', '-c', 'ip -o link | grep -cE "^[0-9]+: eth"'], pid)
                extra = 'nic=%s' % r.stdout.strip()
            else:
                extra = '컨테이너 없음!'
        print('  %-8s %-16s %-8s %s' % (name, n['node_type'], n['status'], extra))


def delete(g):
    p = find_project(g)
    if not p:
        print('프로젝트 없음')
        return
    if input('프로젝트 %s 를 지운다. "delete" 를 입력: ' % p['name']) != 'delete':
        return
    g.call('DELETE', '/projects/%s' % p['project_id'])
    print('삭제됨')


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else ''
    if cmd not in ('create', 'start', 'stop', 'status', 'delete'):
        sys.exit(__doc__)
    globals()[cmd](common.Gns3())
