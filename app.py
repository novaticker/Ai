import os
import json
import time
import threading
import requests
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder='static')
CORS(app)

KIS_BASE = 'https://openapi.koreainvestment.com:9443'

# ── 캐시 저장소 ─────────────────────────────────────
CACHE = {
    'kr_candidates': [],   # 코스피/코스닥 필터 통과 종목
    'us_candidates': [],   # 나스닥 필터 통과 종목
    'last_scan': None,     # 마지막 스캔 시각
    'scan_status': 'idle', # idle | running | done | error
    'scan_log': [],        # 스캔 진행 로그
    'token': '',
    'appkey': '',
    'appsecret': '',
}

# ── 필터 기준 ────────────────────────────────────────
FILTER = {
    # 시총 (억원 단위, 코스피/코스닥)
    'kr_mktcap_min': 5_000,      # 5,000억 이상
    'kr_mktcap_max': 500_000,    # 50조 이하
    # 시총 (백만달러, 나스닥)
    'us_mktcap_min': 500,        # $500M 이상
    'us_mktcap_max': 50_000,     # $50B 이하
    # 주가
    'kr_price_min': 3_000,       # 3,000원 이상
    'us_price_min': 3.0,         # $3 이상
    # 52주 저점 대비 현재가 위치 (%)
    'from_low_min': 30,          # 저점 대비 +30% 이상 (바닥 확인)
    'from_low_max': 300,         # +300% 이하 (이미 너무 오른 것 제외)
    # 거래대금 (만원, 코스피)
    'kr_amount_min': 5_000,      # 5,000만원 = 약 $37K
    # 거래대금 (달러, 나스닥)
    'us_volume_min': 500_000,    # 거래량 50만주 이상
    # 메가트렌드 섹터 키워드
    'kr_sectors': ['반도체','AI','전지','배터리','바이오','로봇','수소','방산','우주','항공','자동차','게임','엔터','소프트웨어','클라우드','인터넷','헬스케어'],
    'us_sectors': ['NVDA','AMD','INTC','AVGO','MU','TSM','ARM','QCOM',  # 반도체
                   'PLTR','AI','SOUN','BBAI','GFAI',                    # AI
                   'RKLB','ASTS','LUNR','RDW','SPCE',                   # 우주
                   'TSLA','RIVN','LCID','NIO','LI',                     # EV
                   'CRWD','PANW','S','ZS','OKTA',                       # 사이버보안
                   'MRNA','BNTX','NVAX','PACB','RXRX',                  # 바이오
                   'IONQ','RGTI','QUBT','QMCO',                         # 양자컴퓨팅
                   'AEHR','ONTO','KLIC','ACLS','UCTT',                  # 반도체장비
                   'DOGE','COIN','MSTR','HUT','MARA'],                  # 암호화폐
}

# ── KIS API 헬퍼 ─────────────────────────────────────
def kis_get(path, tr_id, params, token=None, appkey=None, appsecret=None):
    t = token or CACHE['token']
    k = appkey or CACHE['appkey']
    s = appsecret or CACHE['appsecret']
    res = requests.get(
        f'{KIS_BASE}{path}',
        params=params,
        headers={
            'Content-Type': 'application/json; charset=utf-8',
            'Authorization': f'Bearer {t}',
            'appkey': k,
            'appsecret': s,
            'tr_id': tr_id,
            'custtype': 'P',
        },
        timeout=15
    )
    return res.json()

def log(msg):
    ts = datetime.now().strftime('%H:%M:%S')
    entry = f'[{ts}] {msg}'
    CACHE['scan_log'].append(entry)
    if len(CACHE['scan_log']) > 200:
        CACHE['scan_log'] = CACHE['scan_log'][-200:]
    print(entry)

# ── 종목 필터 — 국내 ──────────────────────────────────
def passes_kr_filter(stock):
    """코스피/코스닥 종목이 5가지 기준을 통과하는지 확인"""
    try:
        price    = int(stock.get('stck_prpr', 0) or 0)
        mktcap   = int(stock.get('hts_avls', 0) or 0)      # 시총 (억원)
        high52   = float(stock.get('d250_hgpr', 0) or 0)   # 52주 최고
        low52    = float(stock.get('d250_lwpr', 0) or 0)   # 52주 최저
        amount   = int(stock.get('acml_tr_pbmn', 0) or 0)  # 거래대금(만원)
        name     = stock.get('hts_kor_isnm', '')

        # 1. 주가 필터
        if price < FILTER['kr_price_min']:
            return False, '주가 미달'

        # 2. 시총 필터
        if not (FILTER['kr_mktcap_min'] <= mktcap <= FILTER['kr_mktcap_max']):
            return False, f'시총 미달/초과 ({mktcap}억)'

        # 3. 52주 저점 대비 위치
        if low52 > 0:
            from_low = ((price - low52) / low52) * 100
            if not (FILTER['from_low_min'] <= from_low <= FILTER['from_low_max']):
                return False, f'저점대비 {from_low:.0f}% — 범위 밖'
        
        # 4. 거래대금 (최소 유동성)
        if amount < FILTER['kr_amount_min']:
            return False, '거래대금 미달'

        # 5. 섹터 (종목명으로 추정 — 완화: 통과시킴, 섹터 점수만 부여)
        # 섹터 미해당이어도 통과, 탐색기에서 섹터 점수로 구분

        return True, f'통과 (시총{mktcap}억, 저점+{((price-low52)/low52*100):.0f}%)'
    except Exception as e:
        return False, f'오류: {e}'

def passes_us_filter(stock):
    """나스닥 종목 필터"""
    try:
        price   = float(stock.get('last', 0) or 0)
        symb    = stock.get('symb', '')
        name    = stock.get('name', '')

        # 1. 주가
        if price < FILTER['us_price_min']:
            return False, '주가 미달'

        # 2. 거래량 (시총 정보 없으면 거래량으로 대체)
        vol = int(stock.get('tvol', 0) or 0)
        if vol < FILTER['us_volume_min']:
            return False, '거래량 미달'

        # 3. 등락률 이상 없음 (상한가/하한가 종목 제외)
        diff = abs(float(stock.get('diff_rate', 0) or 0))
        if diff > 50:
            return False, '비정상 등락률'

        return True, f'통과 ({symb} ${price:.2f})'
    except Exception as e:
        return False, f'오류: {e}'

# ── 전종목 스캔 메인 함수 ─────────────────────────────
def run_full_scan():
    """백그라운드에서 전 종목 스캔 실행"""
    CACHE['scan_status'] = 'running'
    CACHE['scan_log'] = []
    CACHE['kr_candidates'] = []
    CACHE['us_candidates'] = []

    log('전종목 스캔 시작')

    # ── 국내 코스피/코스닥 전종목 ────────────────────
    log('코스피/코스닥 전종목 조회 시작')
    kr_all = []

    # 시총 상위 종목 (최대 300개, KIS API 한계)
    for market in ['J', 'Q']:  # J=코스피, Q=코스닥
        try:
            # 시총 상위 종목 조회
            data = kis_get(
                '/uapi/domestic-stock/v1/ranking/market-cap',
                'FHPST01430000',
                {
                    'fid_cond_mrkt_div_code': market,
                    'fid_cond_scr_div_code': '20174',
                    'fid_div_cls_code': '0',
                    'fid_blng_cls_code': '0',
                    'fid_trgt_cls_code': '0',
                    'fid_trgt_exls_cls_code': '0',
                    'fid_input_price_1': '0',
                    'fid_input_price_2': '0',
                    'fid_vol_cnt': '0',
                    'fid_input_date_1': '0',
                }
            )
            stocks = data.get('output', [])
            log(f'{"코스피" if market=="J" else "코스닥"} {len(stocks)}개 조회')
            kr_all.extend(stocks)
            time.sleep(0.3)  # API 속도 제한 방지
        except Exception as e:
            log(f'{"코스피" if market=="J" else "코스닥"} 조회 실패: {e}')

    # 필터 적용
    log(f'국내 총 {len(kr_all)}개 필터 적용 중...')
    kr_passed = []
    for s in kr_all:
        ok, reason = passes_kr_filter(s)
        if ok:
            kr_passed.append({
                'code': s.get('mksc_shrn_iscd', '') or s.get('stck_shrn_iscd', ''),
                'name': s.get('hts_kor_isnm', ''),
                'price': int(s.get('stck_prpr', 0) or 0),
                'change': float(s.get('prdy_ctrt', 0) or 0),
                'mktcap': int(s.get('hts_avls', 0) or 0),
                'low52': float(s.get('d250_lwpr', 0) or 0),
                'high52': float(s.get('d250_hgpr', 0) or 0),
                'amount': int(s.get('acml_tr_pbmn', 0) or 0),
                'mkt': 'KR',
                'filter_reason': reason,
            })

    log(f'국내 필터 통과: {len(kr_passed)}개')
    CACHE['kr_candidates'] = kr_passed

    # ── 나스닥 전종목 (거래량 상위 방식) ─────────────
    log('나스닥 종목 조회 시작')
    us_all = []

    # 나스닥은 거래량 상위로 분할 조회 (KIS API 최대 100건씩)
    # 등락률 구간별로 나눠서 더 많은 종목 커버
    search_params_list = [
        # 상승 종목 (거래량 상위)
        {'CO_YN_VOLUME': 'Y', 'CO_ST_VOLUME': '500000', 'CO_EN_VOLUME': '',
         'CO_YN_RATE': 'Y', 'CO_ST_RATE': '0', 'CO_EN_RATE': ''},
        # 보합/하락 종목 (거래량 상위)
        {'CO_YN_VOLUME': 'Y', 'CO_ST_VOLUME': '200000', 'CO_EN_VOLUME': '',
         'CO_YN_RATE': '', 'CO_ST_RATE': '', 'CO_EN_RATE': ''},
        # 소형 거래량
        {'CO_YN_VOLUME': 'Y', 'CO_ST_VOLUME': '100000', 'CO_EN_VOLUME': '500000',
         'CO_YN_RATE': 'Y', 'CO_ST_RATE': '1', 'CO_EN_RATE': ''},
    ]

    seen_symbs = set()
    for i, sp in enumerate(search_params_list):
        try:
            params = {
                'AUTH': '', 'EXCD': 'NAS',
                'CO_YN_PRICECUR': '', 'CO_ST_PRICECUR': '', 'CO_EN_PRICECUR': '',
                'CO_YN_AMT': '', 'CO_ST_AMT': '', 'CO_EN_AMT': '',
                'CO_YN_EPS': '', 'CO_ST_EPS': '', 'CO_EN_EPS': '',
                'CO_YN_PER': '', 'CO_ST_PER': '', 'CO_EN_PER': '',
                'KEYB': '',
                **sp
            }
            data = kis_get('/uapi/overseas-price/v1/quotations/inquire-search', 'HHDFS76410000', params)
            stocks = data.get('output2') or data.get('output') or []
            new = [s for s in stocks if s.get('symb') not in seen_symbs]
            seen_symbs.update(s.get('symb') for s in new)
            us_all.extend(new)
            log(f'나스닥 조회 {i+1}/3: {len(new)}개 (누적 {len(us_all)}개)')
            time.sleep(0.5)
        except Exception as e:
            log(f'나스닥 조회 {i+1} 실패: {e}')

    # NYSE도 추가
    for i, sp in enumerate(search_params_list[:2]):
        try:
            params = {
                'AUTH': '', 'EXCD': 'NYS',
                'CO_YN_PRICECUR': '', 'CO_ST_PRICECUR': '', 'CO_EN_PRICECUR': '',
                'CO_YN_AMT': '', 'CO_ST_AMT': '', 'CO_EN_AMT': '',
                'CO_YN_EPS': '', 'CO_ST_EPS': '', 'CO_EN_EPS': '',
                'CO_YN_PER': '', 'CO_ST_PER': '', 'CO_EN_PER': '',
                'KEYB': '',
                **sp
            }
            data = kis_get('/uapi/overseas-price/v1/quotations/inquire-search', 'HHDFS76410000', params)
            stocks = data.get('output2') or data.get('output') or []
            new = [s for s in stocks if s.get('symb') not in seen_symbs]
            seen_symbs.update(s.get('symb') for s in new)
            us_all.extend(new)
            log(f'NYSE 조회 {i+1}/2: {len(new)}개 (누적 {len(us_all)}개)')
            time.sleep(0.5)
        except Exception as e:
            log(f'NYSE 조회 {i+1} 실패: {e}')

    log(f'미국 총 {len(us_all)}개 필터 적용 중...')
    us_passed = []
    for s in us_all:
        ok, reason = passes_us_filter(s)
        if ok:
            us_passed.append({
                'code': s.get('symb', ''),
                'name': s.get('name', s.get('symb', '')),
                'price': float(s.get('last', 0) or 0),
                'change': float(s.get('diff_rate', 0) or 0),
                'volume': int(s.get('tvol', 0) or 0),
                'mkt': 'US',
                'filter_reason': reason,
            })

    log(f'미국 필터 통과: {len(us_passed)}개')
    CACHE['us_candidates'] = us_passed
    CACHE['last_scan'] = datetime.now().isoformat()
    CACHE['scan_status'] = 'done'
    total = len(kr_passed) + len(us_passed)
    log(f'스캔 완료 — 국내 {len(kr_passed)}개 + 미국 {len(us_passed)}개 = 총 {total}개 후보')

def start_scan_thread():
    t = threading.Thread(target=run_full_scan, daemon=True)
    t.start()

# ── 자동 스캐줄 (매일 06:00 KST) ────────────────────
def scheduler():
    while True:
        now = datetime.now()
        # 매일 새벽 6시 스캔
        if now.hour == 6 and now.minute == 0:
            if CACHE['token'] and CACHE['scan_status'] != 'running':
                log('스케줄 스캔 시작')
                start_scan_thread()
        time.sleep(60)

threading.Thread(target=scheduler, daemon=True).start()

# ── API 엔드포인트 ────────────────────────────────────

@app.route('/api/token', methods=['POST'])
def get_token():
    body = request.json
    try:
        res = requests.post(f'{KIS_BASE}/oauth2/tokenP', json={
            'grant_type': 'client_credentials',
            'appkey': body['appkey'],
            'appsecret': body['appsecret'],
        }, timeout=10)
        data = res.json()
        if data.get('access_token'):
            CACHE['token']     = data['access_token']
            CACHE['appkey']    = body['appkey']
            CACHE['appsecret'] = body['appsecret']
        return jsonify(data), res.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/kis', methods=['GET'])
def kis_proxy():
    path      = request.args.get('path', '')
    tr_id     = request.args.get('tr_id', '')
    token     = request.headers.get('X-Token', '') or CACHE['token']
    appkey    = request.headers.get('X-Appkey', '') or CACHE['appkey']
    appsecret = request.headers.get('X-Appsecret', '') or CACHE['appsecret']
    params    = {k: v for k, v in request.args.items() if k not in ('path', 'tr_id')}
    try:
        res = requests.get(f'{KIS_BASE}{path}', params=params, headers={
            'Content-Type': 'application/json; charset=utf-8',
            'Authorization': f'Bearer {token}',
            'appkey': appkey,
            'appsecret': appsecret,
            'tr_id': tr_id,
            'custtype': 'P',
        }, timeout=15)
        return jsonify(res.json()), res.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/claude', methods=['POST'])
def claude_proxy():
    body       = request.json
    claude_key = request.headers.get('X-Claude-Key', '')
    if not claude_key:
        return jsonify({'error': 'Claude API 키 없음'}), 400
    try:
        res = requests.post('https://api.anthropic.com/v1/messages', json=body, headers={
            'Content-Type': 'application/json',
            'x-api-key': claude_key,
            'anthropic-version': '2023-06-01',
        }, timeout=60)
        return jsonify(res.json()), res.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── 스캔 관련 API ─────────────────────────────────────
@app.route('/api/scan/start', methods=['POST'])
def scan_start():
    if not CACHE['token']:
        return jsonify({'error': 'KIS API 미연결'}), 400
    if CACHE['scan_status'] == 'running':
        return jsonify({'status': 'already_running', 'log': CACHE['scan_log']}), 200
    start_scan_thread()
    return jsonify({'status': 'started'})

@app.route('/api/scan/status', methods=['GET'])
def scan_status():
    return jsonify({
        'status':    CACHE['scan_status'],
        'last_scan': CACHE['last_scan'],
        'kr_count':  len(CACHE['kr_candidates']),
        'us_count':  len(CACHE['us_candidates']),
        'log':       CACHE['scan_log'][-20:],  # 최근 20줄만
    })

@app.route('/api/candidates', methods=['GET'])
def get_candidates():
    market = request.args.get('market', 'ALL')
    if market == 'KR':
        data = CACHE['kr_candidates']
    elif market == 'US':
        data = CACHE['us_candidates']
    else:
        data = CACHE['kr_candidates'] + CACHE['us_candidates']
    return jsonify({'candidates': data, 'total': len(data)})

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'scan_status': CACHE['scan_status'],
                    'candidates': len(CACHE['kr_candidates']) + len(CACHE['us_candidates'])})

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
