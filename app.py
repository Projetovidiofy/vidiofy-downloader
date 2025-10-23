import os
import uuid
import json
import re
import threading
from urllib.parse import urlparse

from flask import Flask, request, jsonify, send_from_directory
import yt_dlp
import redis

# Configurações
APP_HOST = "0.0.0.0"
APP_PORT = int(os.environ.get("PORT", 5000))
DOWNLOAD_ROOT = os.environ.get("DOWNLOAD_ROOT", os.path.join(os.getcwd(), "downloads"))
os.makedirs(DOWNLOAD_ROOT, exist_ok=True)

# Redis para estado compartilhado entre instâncias
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)

# Token de autenticação simples (opcional)
AUTH_TOKEN = os.environ.get("API_AUTH_TOKEN", "").strip()
def require_auth(req):
    if not AUTH_TOKEN:
        return True
    token = req.headers.get("X-ACCESS-TOKEN", "")
    return token == AUTH_TOKEN

app = Flask(__name__)

def is_valid_video_file_like(path):
    try:
        if not os.path.isfile(path):
            return False
        ext = os.path.splitext(path)[1].lower()
        valid_exts = ['.mp4', '.webm', '.mkv', '.mov', '.avi', '.flv', '.m4v']
        if ext not in valid_exts:
            return False
        if os.path.getsize(path) < 100000:  # <100KB
            return False
        with open(path, 'rb') as f:
            first = f.read(1024)
        # heurísticas simples: não retornar HTML
        if b'<!DOCTYPE html' in first or b'<html' in first:
            return False
        return True
    except Exception:
        return False

def safe_download_path(job_id, filename=None):
    base = os.path.join(DOWNLOAD_ROOT, job_id)
    os.makedirs(base, exist_ok=True)
    if filename:
        return os.path.join(base, filename)
    return base

def set_status(job_id, payload):
    redis_client.set(f"job:{job_id}", json.dumps(payload))

def get_status(job_id):
    raw = redis_client.get(f"job:{job_id}")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None

def download_task(original_url, job_id):
    # iniciar status
    set_status(job_id, {'status': 'processing', 'url': original_url, 'message': 'Iniciando download', 'file_size': 0})

    try:
        # PLANO A: usar yt_dlp Python API
        download_dir = safe_download_path(job_id)
        ydl_opts = {
            'format': 'best[ext=mp4]/best',
            'outtmpl': os.path.join(download_dir, '%(title)s.%(ext)s'),
            'noplaylist': True,
            'progress_hooks': [lambda d: update_progress(job_id, d)],
            'retries': 3,
            'logtostderr': False,
            'ignoreerrors': True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(original_url, download=True)
            if not info:
                raise Exception("Não foi possível extrair informações do vídeo")

            # localizar arquivo baixado
            base = ydl.prepare_filename(info)
            if not base:
                raise Exception("Arquivo não encontrado após download")

            downloaded_file = base
            # validação básica
            if not is_valid_video_file_like(downloaded_file):
                os.remove(downloaded_file)
                raise Exception("Arquivo baixado não é um vídeo válido (padrões HTML possivelmente retornados)")

            file_size = os.path.getsize(downloaded_file)
            set_status(job_id, {
                'status': 'completed',
                'url': original_url,
                'file_name': os.path.basename(downloaded_file),
                'download_path': f'/download_file/{job_id}/{os.path.basename(downloaded_file)}',
                'title': info.get('title', ''),
                'thumbnail': info.get('thumbnail', ''),
                'file_size': file_size,
                'message': 'Download concluído com sucesso!'
            })
    except Exception as e:
        msg = str(e)
        set_status(job_id, {'status': 'failed', 'url': original_url, 'message': msg})

def update_progress(job_id, d):
    status = get_status(job_id) or {}
    if not status:
        status = {}
    if d.get('status') == 'downloading':
        status.update({'message': f"Baixando: {d.get('_percent_str','0%')}", 'file_size': d.get('total_bytes_estimate', 0)})
    elif d.get('status') == 'finished':
        status.update({'message': 'Processamento finalizado...'})
    set_status(job_id, status)

def validate_youtube_url(url):
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            return False
        if parsed.netloc == '':
            return False
        # permissões: somente domínios relacionados ao YouTube (com limitações simples)
        if not re.search(r'(youtube.com|youtu.be|youtube-nocookie.com)', parsed.netloc, re.IGNORECASE):
            return False
        return True
    except Exception:
        return False

@app.route('/')
def index():
    return jsonify({'status': 'ok', 'message': 'API rodando'}), 200

@app.route('/api/download', methods=['POST'])
def api_download():
    if not require_auth(request):
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.get_json(force=True) or {}
    video_url = data.get('url') or data.get('video_url')
    if not video_url:
        return jsonify({'error': "URL 'url' obrigatório"}), 400

    if not validate_youtube_url(video_url):
        return jsonify({'error': 'URL inválida ou não suportada'}), 400

    job_id = str(uuid.uuid4())
    # iniciar tarefa em thread (simples) - para produção, considerar uma fila
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
    # Serve de forma segura apenas se houver o arquivo correspondente e válido
    file_path = os.path.join(safe_download_path(job_id), filename)
    if not os.path.exists(file_path) or not is_valid_video_file_like(file_path):
        return jsonify({'error': 'Arquivo não encontrado ou inválido'}), 404

    # Evita path traversal apenas servindo a partir do diretório permitido
    base_dir = os.path.abspath(safe_download_path(job_id))
    requested_path = os.path.abspath(file_path)
    if not requested_path.startswith(base_dir):
        return jsonify({'error': 'Acesso negado'}), 403

    return send_from_directory(base_dir, filename, as_attachment=True)

if __name__ == '__main__':
    app.run(host=APP_HOST, port=APP_PORT, debug=False)
