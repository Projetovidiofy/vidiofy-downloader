from flask import Flask, render_template, request, jsonify, send_file
import yt_dlp
import os
import uuid
import threading
import re
import requests
import random

app = Flask(__name__)

DOWNLOAD_FOLDER = 'downloads'
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)

download_status_map = {}

def clean_ansi_codes(text):
    return re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])').sub('', text)

def is_valid_video_file(file_path):
    """Verifica se o arquivo é realmente um vídeo SEM usar magic"""
    try:
        # Verificação 1: pela extensão
        ext = os.path.splitext(file_path)[1].lower()
        valid_extensions = ['.mp4', '.webm', '.mkv', '.mov', '.avi', '.flv', '.m4v']
        
        if ext not in valid_extensions:
            return False
        
        # Verificação 2: pelo tamanho do arquivo (MHTML geralmente é pequeno)
        file_size = os.path.getsize(file_path)
        if file_size < 100000:  # Menos de 100KB provavelmente não é vídeo
            return False
        
        # Verificação 3: pelo conteúdo (lê os primeiros bytes)
        with open(file_path, 'rb') as f:
            first_bytes = f.read(1024)
            
        # Detecta assinaturas de HTML/MHTML
        html_signatures = [
            b'<!DOCTYPE html',
            b'<html',
            b'Content-Type: message/rfc822',
            b'From: <Saved by',
            b'Snapshot-Content-Location:',
        ]
        
        first_bytes_str = first_bytes.decode('utf-8', errors='ignore')
        
        # Se contém assinatura HTML, é inválido
        for html_sig in html_signatures:
            if html_sig in first_bytes:
                return False
            if html_sig.decode('utf-8', errors='ignore').lower() in first_bytes_str.lower():
                return False
        
        return True
        
    except Exception as e:
        print(f"Erro na validação do arquivo: {e}")
        return False

def get_user_agent():
    """Gera um User-Agent aleatório realista"""
    user_agents = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0'
    ]
    return random.choice(user_agents)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/download', methods=['POST'])
def handle_download():
    data = request.get_json()
    video_url = data.get('url')
    if not video_url: 
        return jsonify({'error': 'URL obrigatória.'}), 400
    
    if video_url in download_status_map:
        del download_status_map[video_url]
    
    download_status_map[video_url] = {'status': 'processing', 'message': 'Iniciando...'}
    thread = threading.Thread(target=download_video_task, args=(video_url,))
    thread.start()
    return jsonify({'status': 'processing', 'video_url': video_url})

@app.route('/api/check_status', methods=['POST'])
def check_download_status():
    data = request.get_json()
    video_url = data.get('url')
    status_info = download_status_map.get(video_url)
    if status_info:
        status_info.setdefault('file_size', 0)
        return jsonify(status_info)
    return jsonify({'status': 'not_found'}), 404

def download_video_task(video_url):
    original_url = video_url
    download_info = download_status_map.setdefault(original_url, {})
    download_info.update({'status': 'processing', 'file_size': 0})
    
    try:
        is_youtube = "youtube.com" in original_url or "youtu.be" in original_url

        # --- CONFIGURAÇÃO YT-DLP COM BYPASS ---
        print("Iniciando download com yt-dlp (com bypass)")
        
        ydl_opts = {
            'format': 'best[ext=mp4]/best[ext=webm]/best',
            'outtmpl': os.path.join(DOWNLOAD_FOLDER, f'{uuid.uuid4()}.%(ext)s'),
            'noplaylist': True,
            'progress_hooks': [lambda d: update_download_progress(d, original_url)],
            'retries': 5,
            'fragment_retries': 5,
            'ignoreerrors': False,
            'extract_flat': False,
            
            # Configurações para bypass do YouTube
            'http_headers': {
                'User-Agent': get_user_agent(),
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'pt-BR,pt;q=0.9,en;q=0.8',
                'Accept-Encoding': 'gzip, deflate, br',
                'DNT': '1',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
            },
            
            # Throttle para parecer mais humano
            'throttled_rate': '1M',
            'sleep_interval': 2,
            'max_sleep_interval': 5,
            
            # Configurações específicas do YouTube
            'youtube_include_dash_manifest': False,
            'youtube_include_hls_manifest': False,
        }

        # Tenta primeiro sem cookies
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(original_url, download=True)
        except Exception as first_error:
            print(f"Primeira tentativa falhou: {first_error}. Tentando com cookies...")
            
            # Se falhou, tenta com cookies se existirem
            cookies_file = 'cookies.txt'
            if os.path.exists(cookies_file):
                ydl_opts['cookiefile'] = cookies_file
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(original_url, download=True)
            else:
                # Se não tem cookies, tenta com formato diferente
                ydl_opts['format'] = 'worst[ext=mp4]/worst'
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(original_url, download=True)
        
        if not info:
            raise yt_dlp.utils.DownloadError("Não foi possível extrair informações do vídeo")
        
        # Encontra o arquivo baixado
        downloaded_file = ydl.prepare_filename(info)
        
        # VERIFICAÇÃO CRÍTICA: É mesmo um vídeo?
        if not is_valid_video_file(downloaded_file):
            if os.path.exists(downloaded_file):
                os.remove(downloaded_file)
            raise yt_dlp.utils.DownloadError("Arquivo baixado não é um vídeo válido (provavelmente HTML).")

        base_filename = os.path.basename(downloaded_file)
        file_size = os.path.getsize(downloaded_file)
        
        download_info.update({
            'status': 'completed', 
            'file_name': base_filename,
            'download_link': f'/download_file/{base_filename}',
            'title': info.get('title', 'Vídeo'), 
            'thumbnail': info.get('thumbnail', ''),
            'file_size': file_size,
            'message': 'Download concluído com sucesso!'
        })
        print(f"Download concluído: {base_filename}")

    except Exception as e:
        msg = clean_ansi_codes(str(e))
        print(f"Erro final: {msg}")
        
        # Mensagem mais amigável para o usuário
        if "Sign in to confirm" in msg or "bot" in msg.lower():
            user_msg = "YouTube bloqueou o download. Tente novamente em alguns minutos ou use outro vídeo."
        else:
            user_msg = f'Falha no download: {msg.split(":")[-1].strip()}'
        
        download_info.update({
            'status': 'failed', 
            'message': user_msg
        })

def update_download_progress(d, video_url):
    info = download_status_map.setdefault(video_url, {})
    if d['status'] == 'downloading':
        percent = clean_ansi_codes(d.get('_percent_str','0%'))
        eta = clean_ansi_codes(d.get('_eta_str',''))
        info.update({
            'message': f"Baixando: {percent} ETA: {eta}", 
            'file_size': d.get('total_bytes_estimate', 0)
        })
    elif d['status'] == 'finished':
        info.update({'message': 'Processamento finalizado...'})

@app.route('/download_file/<path:filename>')
def serve_downloaded_file(filename):
    file_path = os.path.join(DOWNLOAD_FOLDER, filename)
    
    # Verificação final de segurança
    if not is_valid_video_file(file_path):
        return jsonify({'error': 'Arquivo inválido ou corrompido'}), 400
    
    return send_file(file_path, as_attachment=True)

# Limpeza automática de arquivos antigos
def cleanup_old_files():
    try:
        for filename in os.listdir(DOWNLOAD_FOLDER):
            file_path = os.path.join(DOWNLOAD_FOLDER, filename)
            # Remove arquivos com mais de 1 hora
            if os.path.getmtime(file_path) < (time.time() - 3600):
                os.remove(file_path)
                print(f"Arquivo antigo removido: {filename}")
    except Exception as e:
        print(f"Erro na limpeza: {e}")

if __name__ == '__main__':
    import time
    # Limpa arquivos antigos ao iniciar
    cleanup_old_files()
    
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=True, host='0.0.0.0', port=port)
