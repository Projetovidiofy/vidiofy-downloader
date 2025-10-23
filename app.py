import os
import uuid
import json
import threading
from urllib.parse import urlparse
from flask import Flask, request, jsonify, send_from_directory
import yt_dlp

# Configurações
PORT = int(os.environ.get("PORT", 5000))
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DOWNLOAD_ROOT = os.environ.get("DOWNLOAD_ROOT", os.path.join(BASE_DIR, "downloads"))
os.makedirs(DOWNLOAD_ROOT, exist_ok=True)

# Tenta conectar a Redis, fallback para memória se não houver Redis
try:
    import redis
    REDIS_URL = os.environ.get("REDIS_URL")
    if REDIS_URL:
        redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
        REDIS_AVAILABLE = True
    else:
        REDIS_AVAILABLE = False
        redis_client = None
except Exception:
    REDIS_AVAILABLE = False
    redis_client = None

# Token de autenticação opcional
AUTH_TOKEN = os.environ.get("API_AUTH_TOKEN", "").strip()
def require_auth(req):
    if not AUTH_TOKEN:
        return True
    return req.headers.get("X-ACCESS-TOKEN", "") == AUTH_TOKEN

def job_path(job_id):
    path = os.path.join(DOWNLOAD_ROOT, job_id)
    os.makedirs(path, exist_ok=True)
    return path

def set_status(job_id, payload):
    if REDIS_AVAILABLE and redis_client:
        try:
            redis_client.set(f"job:{job_id}", json.dumps(payload))
        except Exception as e:
            print(f"Redis write error: {e}")
            _store_in_memory(job_id, payload)
    else:
        _store_in_memory(job_id, payload)

def _store_in_memory(job_id, payload):
    global _fallback_store
    try:
        _fallback_store
    except NameError:
        _fallback_store = {}
    _fallback_store[job_id] = payload

def get_status(job_id):
    if REDIS_AVAILABLE and redis_client:
        try:
            raw = redis_client.get(f"job:{job_id}")
            if not raw:
                return None
            return json.loads(raw)
        except Exception as e:
            print(f"Redis read error: {e}")
    global _fallback_store
    if '_fallback_store' in globals():
        return globals()['_fallback_store'].get(job_id)
    return None

def is_valid_video_file_like(path):
    try:
        if not os.path.isfile(path):
            return False
        ext = os.path.splitext(path)[1].lower()
        valid_exts = ['.mp4', '.webm', '.mkv', '.mov', '.avi', '.flv', '.m4v']
        if ext not in valid_exts:
            return False
        if os.path.getsize(path) < 100000:
            return False
        with open(path, 'rb') as f:
            first = f.read(1024)
        if b'<!DOCTYPE html' in first or b'<html' in first:
            return False
        return True
    except Exception:
        return False

def download_task(original_url, job_id):
    set_status(job_id, {'status': 'processing', 'url': original_url, 'message': 'Iniciando download', 'file_size': 0})
    try:
        out_dir = job_path(job_id)
        ydl_opts = {
            'format': 'best[ext=mp4]/best',
            'outtmpl': os.path.join(out_dir, '%(title)s.%(ext)s'),
            'noplaylist': True,
            'retries': 3,
            'ignoreerrors': True,
            'progress_hooks': [lambda d: None],
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(original_url, download=True)
            if not info:
                raise Exception("Não foi possível extrair informações do vídeo")
            downloaded_file = ydl.prepare_filename(info)
            if not downloaded_file or not os.path.exists(downloaded_file):
                raise Exception("Arquivo não encontrado após download")
            if not is_valid_video_file_like(downloaded_file):
                os.remove(downloaded_file)
                raise Exception("Arquivo baixado não é um vídeo válido")
            file_size = os.path.getsize(downloaded_file)
            set_status(job_id, {
                'status': 'completed',
                'url': original_url,
                'file_name': os.path.basename(downloaded_file),
                'download_path': f'/download_file/{job_id}/{os.path.basename(downloaded_file)}',
                'title': info.get('title', 'Vídeo'),
                'thumbnail': info.get('thumbnail', ''),
                'file_size': file_size,
                'message': 'Download concluído com sucesso!'
            })
    except Exception as e:
        msg = str(e)
        set_status(job_id, {'status': 'failed', 'url': original_url, 'message': msg})

def validate_youtube_url(url):
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        if p.scheme not in ('http', 'https'):
            return False
        if not p.netloc:
            return False
        import re
        if not re.search(r'(youtube.com|youtu.be|youtube-nocookie.com)', p.netloc, re.IGNORECASE):
            return False
        return True
    except Exception:
        return False

app = Flask(__name__)

@app.route('/')
def index():
    return jsonify({'status': 'ok', 'message': 'API rodando'}), 200

@app.route('/api/download', methods=['POST'])
def api_download():
    if not require_auth(request):
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json(force=True) or {}
    video_url = data.get('url')
    if not video_url:
        return jsonify({'error': "URL 'url' obrigatório"}), 400
    if not validate_youtube_url(video_url):
        return jsonify({'error': 'URL inválida ou não suportada'}), 400
    job_id = str(uuid.uuid4())
    thread = threading.Thread(target=download_task, args=(video_url, job_id), daemon=True)
    thread.start()
    set_status(job_id, {'status': 'queued', 'url': video_url, 'message': 'Job enfileirado'})
    return jsonify({'job_id': job_id, 'status': 'queued'}), 202

@app.route('/api/status', methods=['POST'])
def api_status():
    if not require_auth(request):
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json(force=True) or {}
    job_id = data.get('job_id')
    if not job_id:
        return jsonify({'error': 'job_id obrigatório'}), 400
    status = get_status(job_id)
    if not status:
        return jsonify({'status': 'not_found'}), 404
    return jsonify(status), 200

@app.route('/download_file/<job_id>/<path:filename>')
def download_file(job_id, filename):
    dir_path = job_path(job_id)
    file_path = os.path.join(dir_path, filename)
    if not os.path.exists(file_path) or not is_valid_video_file_like(file_path):
        return jsonify({'error': 'Arquivo não encontrado ou inválido'}), 404
    base_dir = os.path.abspath(dir_path)
    if not file_path.startswith(base_dir):
        return jsonify({'error': 'Acesso negado'}), 403
    return send_from_directory(base_dir, filename, as_attachment=True)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT)
