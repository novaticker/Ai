import os
import requests
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder='static')
CORS(app)

KIS_BASE = 'https://openapi.koreainvestment.com:9443'

@app.route('/api/token', methods=['POST'])
def get_token():
    body = request.json
    try:
        res = requests.post(f'{KIS_BASE}/oauth2/tokenP', json={
            'grant_type': 'client_credentials',
            'appkey': body['appkey'],
            'appsecret': body['appsecret'],
        }, timeout=10)
        return jsonify(res.json()), res.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/kis', methods=['GET'])
def kis_proxy():
    path      = request.args.get('path', '')
    tr_id     = request.args.get('tr_id', '')
    token     = request.headers.get('X-Token', '')
    appkey    = request.headers.get('X-Appkey', '')
    appsecret = request.headers.get('X-Appsecret', '')
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
        }, timeout=30)
        return jsonify(res.json()), res.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/health')
def health():
    return jsonify({'status': 'ok'})

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
