from flask import Flask, render_template, request, jsonify, send_file
import yt_dlp
import os
import uuid
import threading
import re

app = Flask(__name__)
DOWNLOAD_FOLDER = 'downloads'
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)
download_status_map = {}

def clean_ansi_codes(text):
    return re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])').sub('', text)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/download', methods=['POST'])
def handle_download():
    data = request.get_json()
    video_url = data.get('url')
    if not video_url: return jsonify({'error': 'URL obrigatória.'}), 400
    if video_url in download_status_map: del download_status_map[video_url]
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
        cookies_file = 'cookies.txt'
        if not os.path.exists(cookies_file):
            print("AVISO: 'cookies.txt' não encontrado. Downloads podem falhar.")
            cookies_file = None

        ydl_opts = {
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            # MUDANÇA PRINCIPAL: Salva o arquivo com o título do vídeo
            'outtmpl': os.path.join(DOWNLOAD_FOLDER, '%(title)s.%(ext)s'),
            'noplaylist': True,
            'progress_hooks': [lambda d: update_download_progress(d, original_url)],
            'cookiefile': cookies_file,
            'retries': 3,
            'fragment_retries': 3,
            'ignoreerrors': True,
            # Adiciona opção para restringir o tamanho do nome do arquivo (útil em alguns sistemas)
            'restrictfilenames': True, 
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=True)
            if not info or not info.get('requested_downloads'):
                raise yt_dlp.utils.DownloadError("Falha no download. O vídeo pode ser privado ou bloqueado.")
            
            downloaded_file = ydl.prepare_filename(info) # Pega o nome final do arquivo
            base_filename = os.path.basename(downloaded_file)

            if not base_filename.lower().endswith(('.mp4', '.webm', '.mkv', '.mov')):
                os.remove(downloaded_file)
                raise yt_dlp.utils.DownloadError("Arquivo baixado não é um vídeo válido (provavelmente HTML).")
            
            file_size = os.path.getsize(downloaded_file)
            
            download_info.update({
                'status': 'completed',
                'file_name': base_filename,
                'download_link': f'/download_file/{base_filename}',
                'title': info.get('title', 'Vídeo'),
                'thumbnail': info.get('thumbnail', ''),
                'file_size': file_size
            })
            print(f"Download concluído: {base_filename}")

    except Exception as e:
        msg = clean_ansi_codes(str(e).split('\n')[0])
        print(f"Erro final: {msg}")
        download_info.update({'status': 'failed', 'message': msg})

def update_download_progress(d, video_url):
    info = download_status_map.setdefault(video_url, {})
    if d['status'] == 'downloading':
        percent = clean_ansi_codes(d.get('_percent_str',''))
        eta = clean_ansi_codes(d.get('_eta_str',''))
        info.update({'message': f"Baixando: {percent} (ETA: {eta})", 'file_size': d.get('total_bytes_estimate', 0)})
    elif d['status'] == 'finished':
        info.update({'message': 'Finalizando...'})

@app.route('/download_file/<path:filename>')
def serve_downloaded_file(filename):
    # Por segurança, garante que o caminho não saia do diretório de downloads
    safe_path = os.path.abspath(os.path.join(DOWNLOAD_FOLDER, filename))
    if not safe_path.startswith(os.path.abspath(DOWNLOAD_FOLDER)):
        return jsonify({'error': 'Acesso negado'}), 403
    return send_file(safe_path, as_attachment=True)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=True, host='0.0.0.0', port=port)

