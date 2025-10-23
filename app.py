from flask import Flask, render_template, request, jsonify, send_file
import yt_dlp
import os
import uuid
import threading
import re
import requests

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
            first_bytes = f.read(1024)  # Lê os primeiros 1KB
            
        # Detecta assinaturas de vídeo
        video_signatures = [
            b'\x00\x00\x00\x18ftyp',  # MP4
            b'\x1A\x45\xDF\xA3',      # WebM
            b'\x00\x00\x00\x1Cftyp',  # Mais MP4
            b'\x52\x49\x46\x46',      # AVI, WAV (RIFF)
        ]
        
        # Detecta assinaturas de HTML/MHTML
        html_signatures = [
            b'<!DOCTYPE html',
            b'<html',
            b'Content-Type: message/rfc822',
            b'From: <Saved by',
            b'Snapshot-Content-Location:',
            b'<!DOCTYPE HTML',
        ]
        
        first_bytes_str = first_bytes.decode('utf-8', errors='ignore')
        
        # Se contém assinatura HTML, é inválido
        for html_sig in html_signatures:
            if html_sig in first_bytes:
                return False
            if html_sig.decode('utf-8', errors='ignore').lower() in first_bytes_str.lower():
                return False
        
        # Se tem assinatura de vídeo, é válido
        for video_sig in video_signatures:
            if first_bytes.startswith(video_sig):
                return True
        
        # Fallback: se não detectou nenhum, confia na extensão
        return True
        
    except Exception as e:
        print(f"Erro na validação do arquivo: {e}")
        return False

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

        # --- PLANO A: YT-DLP DIRETO (MAIS CONFIÁVEL) ---
        print("Iniciando download com yt-dlp")
        
        ydl_opts = {
            'format': 'best[ext=mp4]/best[ext=webm]/best',
            'outtmpl': os.path.join(DOWNLOAD_FOLDER, f'{uuid.uuid4()}.%(ext)s'),
            'noplaylist': True,
            'progress_hooks': [lambda d: update_download_progress(d, original_url)],
            'retries': 3, 
            'fragment_retries': 3,
            'ignoreerrors': False,
        }

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
        download_info.update({
            'status': 'failed', 
            'message': f'Falha no download: {msg}'
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

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=True, host='0.0.0.0', port=port)
