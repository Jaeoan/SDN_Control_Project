"""설정의 원본. GNS3 프로젝트의 노드·배선·IP 를 여기서 정의한다 (기획서 2.1).

  h1~h5 --- s1 ==(기본 10M)== s2 ==(10M)== s4 --- srv
              \\=(우회 10M)== s3 ==(10M)==/

스위치 인터페이스 (컨테이너 안 이름)
  모든 스위치: eth0 = 관리 포트 (br0 에서 빼내고 192.168.100.x 를 붙여 컨트롤러와 통신)
  S1: eth1~eth5 = h1~h5,  eth6 = S2(기본),  eth7 = S3(우회),  eth8~eth12 = h6~h10
      (h6~h10 은 나중에 추가해서 eth6/eth7 뒤에 붙었다. 배선을 다시 하지 않으려고 이렇게 둠)
  S2: eth1 = S1, eth2 = S4        S3: eth1 = S1, eth2 = S4
  S4: eth1 = S2, eth2 = S3, eth3 = srv
(GNS3 가 소문자 s1~s4 이름을 거부해서 대문자를 쓴다. 기획서의 s1~s4 와 같은 것)
OpenFlow 포트 번호는 컨트롤러가 접속 시 port-desc 로 이름→번호를 알아내므로 여기서는 이름만 쓴다.
"""

PROJECT_NAME = 'juntae_sdn'
CAPACITY_MBPS = 10                      # 스위치 간 링크 용량. OpenFlow meter 로 강제
CTRL_IP, CTRL_PORT = '192.168.100.1', 6655   # 호스트의 br_gns3 주소. 6653=ODL, 6654=기존 실습이 사용 중
CONTROLLER = 'tcp:%s:%d' % (CTRL_IP, CTRL_PORT)

OVS_IMAGE = 'gns3/openvswitch:latest'
HOST_IMAGE = 'gns3-alpine-iperf:latest'
OVS_ADAPTERS = {'S1': 16, 'S2': 8, 'S3': 8, 'S4': 8}   # 랜카드 수. S1 은 호스트 10대 + 경로 2 + 관리 1

# 스위치: 이름 -> (dpid, 관리 IP)
SWITCHES = {'S1': (1, '192.168.100.21'), 'S2': (2, '192.168.100.22'),
            'S3': (3, '192.168.100.23'), 'S4': (4, '192.168.100.24')}
MGMT_IF = 'eth0'
# 관리선이 꽂히는 Switch1 의 포트. S1 은 노드를 다시 만들면서 0번에 옛 연결 흔적이 남아 4번으로 옮겼다
MGMT_SWITCH_PORT = {'S1': 4, 'S2': 1, 'S3': 2, 'S4': 3}

# 호스트: 이름 -> (IP, 붙는 스위치, 그 스위치의 인터페이스)
HOSTS = {'h1': ('10.0.0.1', 'S1', 'eth1'), 'h2': ('10.0.0.2', 'S1', 'eth2'),
         'h3': ('10.0.0.3', 'S1', 'eth3'), 'h4': ('10.0.0.4', 'S1', 'eth4'),
         'h5': ('10.0.0.5', 'S1', 'eth5'), 'h6': ('10.0.0.6', 'S1', 'eth8'),
         'h7': ('10.0.0.7', 'S1', 'eth9'), 'h8': ('10.0.0.8', 'S1', 'eth10'),
         'h9': ('10.0.0.9', 'S1', 'eth11'), 'h10': ('10.0.0.10', 'S1', 'eth12'),
         'srv': ('10.0.0.100', 'S4', 'eth3')}
CLIENTS = ['h%d' % i for i in range(1, 11)]     # 폭주를 일으키는 호스트 10대
SRV_IP = HOSTS['srv'][0]

# 스위치 간 링크: (스위치, 인터페이스, 스위치, 인터페이스)
LINKS = [('S1', 'eth6', 'S2', 'eth1'),   # 기본 경로
         ('S2', 'eth2', 'S4', 'eth1'),
         ('S1', 'eth7', 'S3', 'eth1'),   # 우회 경로
         ('S3', 'eth2', 'S4', 'eth2')]
S1_DEFAULT_IF, S1_DETOUR_IF = 'eth6', 'eth7'
S1_HOST_IFS = [HOSTS[h][2] for h in CLIENTS]

# GNS3 화면 배치
POS = {'S1': (-200, 0), 'S2': (50, -130), 'S3': (50, 130), 'S4': (300, 0), 'srv': (520, 0),
       'Switch1': (50, -330), 'Cloud1': (300, -330)}
POS.update({'h%d' % i: (-450, -315 + 63 * i) for i in range(1, 11)})
MGMT_BRIDGE = 'br_gns3'


def eth_index(ifname):
    return int(ifname[3:])


def inter_switch_ifs(sw):
    """이 스위치에서 다른 스위치로 나가는 인터페이스들 (meter 를 걸 곳)."""
    out = []
    for a, a_if, b, b_if in LINKS:
        if a == sw:
            out.append((a_if, b))
        if b == sw:
            out.append((b_if, a))
    return out
