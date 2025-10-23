from flask import Flask, render_template, request, jsonify, send_file
import yt_dlp
import os
import uuid
import threading
import json
import re
import requests

app = Flask(__name__)

DOWNLOAD_FOLDER = 'downloads'
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)

download_status_map = {}

def clean_ansi_codes(text):
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', text)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/download', methods=['POST'])
def handle_download():
    data = request.get_json()
    video_url = data.get('url')
    if not video_url: return jsonify({'error': 'O campo URL é obrigatório.'}), 400
    if video_url in download_status_map: del download_status_map[video_url]
    download_status_map[video_url] = {'status': 'processing', 'message': 'Iniciando...'}
    thread = threading.Thread(target=download_video_task, args=(video_url,))
    thread.start()
    return jsonify({'message': 'O download foi iniciado.', 'status': 'processing', 'video_url': video_url})

@app.route('/api/check_status', methods=['POST'])
def check_download_status():
    data = request.get_json()
    video_url = data.get('url')
    status_info = download_status_map.get(video_url)
    if status_info:
        status_info.setdefault('file_size', 0)
        return jsonify(status_info)
    return jsonify({'status': 'not_found', 'message': 'Download não encontrado.'}), 404

def download_video_task(video_url):
    original_url = video_url
    download_info = download_status_map.setdefault(original_url, {})
    download_info.update({'status': 'processing', 'file_size': 0})

    try:
        resolved_url = original_url
        if any(x in original_url for x in ["vm.tiktok.com", "kw.ai"]):
            try:
                download_info['message'] = 'Resolvendo link curto...'
                headers = {'User-Agent': 'Mozilla/5.0'}
                r = requests.get(original_url, allow_redirects=True, timeout=10, headers=headers)
                r.raise_for_status()
                resolved_url = r.url
            except Exception as e:
                print(f"Falha ao resolver link curto: {e}")

        is_tiktok = "tiktok.com" in resolved_url
        is_youtube = "youtube.com" in resolved_url or "youtu.be" in resolved_url

        api_download_url, title, thumbnail = None, 'Vídeo', ''

        if is_tiktok:
            try:
                download_info['message'] = 'Tentando API externa para TikTok...'
                resp = requests.get(f"https://www.tikwm.com/api/?url={resolved_url}", timeout=15).json()
                if resp.get("code") == 0 and resp.get("data"):
                    data = resp["data"]
                    api_download_url = data.get("hdplay") or data.get("play")
                    title, thumbnail = data.get("title", title), data.get("cover", thumbnail)
            except Exception as e: print(f"API TikWM falhou: {e}")

        if is_youtube:
            try:
                download_info['message'] = 'Tentando API externa para YouTube...'
                video_id = None
                if "youtu.be/" in resolved_url: video_id = resolved_url.split("youtu.be/")[1].split("?")[0]
                elif "watch?v=" in resolved_url: video_id = resolved_url.split("watch?v=")[1].split("&")[0]
                elif "shorts/" in resolved_url: video_id = resolved_url.split("shorts/")[1].split("?")[0]
                
                if video_id:
                    # ESTA É A LINHA QUE MUDAMOS! Trocamos de 'despachante'.
                    invidious_api_url = f"https://invidious.protoklaus.com/api/v1/videos/{video_id}"
                    resp = requests.get(invidious_api_url, timeout=15).json()
                    
                    stream = next((s for s in resp.get('formatStreams', []) if s.get('itag') == '22'), None) or \
                             next((s for s in resp.get('formatStreams', []) if s.get('itag') == '18'), None)
                    if stream and stream.get('url'):
                        api_download_url = stream['url']
                        title, thumbnail = resp.get('title', title), resp.get('videoThumbnails', [{}])[-1].get('url', thumbnail)
            except Exception as e: print(f"API Invidious falhou: {e}")

        if api_download_url:
            print(f"Sucesso com API! Baixando de {api_download_url[:30]}...")
            download_info.update({'message': 'API encontrou! Baixando...', 'title': title, 'thumbnail': thumbnail})
            
            unique_filename_base = str(uuid.uuid4())
            ext = os.path.splitext(api_download_url.split('?')[0])[-1] or '.mp4'
            output_path = os.path.join(DOWNLOAD_FOLDER, f'{unique_filename_base}{ext}')

            with requests.get(api_download_url, stream=True, headers={'User-Agent': 'Mozilla/5.0'}) as r:
                r.raise_for_status()
                with open(output_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
            
            base_filename = os.path.basename(output_path)
            file_size = os.path.getsize(output_path)
            download_info.update({'status': 'completed', 'message': 'Download via API concluído!', 'file_name': base_filename, 'download_link': f'/download_file/{base_filename}', 'file_size': file_size})
            return

        print(f"Nenhuma API funcionou. Usando yt-dlp padrão para {resolved_url}")
        download_info['message'] = 'API falhou, tentando método padrão...'

        ydl_opts = {
            'format': 'best[ext=mp4][acodec!=none]/best[acodec!=none]/best',
            'outtmpl': os.path.join(DOWNLOAD_FOLDER, f'{str(uuid.uuid4())}.%(ext)s'),
            'noplaylist': True,
            'progress_hooks': [lambda d: update_download_progress(d, original_url)],
            'retries': 3,
            'fragment_retries': 3,
            'no_check_certificate': True,
            'ignoreerrors': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(resolved_url, download=True)
            if not info or not info.get('requested_downloads'):
                raise yt_dlp.utils.DownloadError("Download via yt-dlp falhou.")
            
            downloaded_file = info['requested_downloads'][0]['filepath']
            base_filename, file_size = os.path.basename(downloaded_file), os.path.getsize(downloaded_file)
            download_info.update({'status': 'completed', 'message': 'Download concluído!', 'file_name': base_filename, 'download_link': f'/download_file/{base_filename}', 'title': info.get('title', title), 'thumbnail': info.get('thumbnail', thumbnail), 'file_size': file_size})

    except Exception as e:
        error_message = f"Erro final ao baixar {original_url}: {str(e)}"
        print(error_message)
        final_error_msg = str(e)
        if isinstance(e, yt_dlp.utils.DownloadError):
            msg = clean_ansi_codes(str(e).split('\n')[0])
            if "requiring login" in msg: final_error_msg = "Este vídeo exige login."
            elif "confirm you’re not a bot" in msg: final_error_msg = "YouTube detectou atividade de bot."
            else: final_error_msg = msg
        download_info.update({'status': 'failed', 'message': final_error_msg})

def update_download_progress(d, video_url):
    info = download_status_map.setdefault(video_url, {})
    if d['status'] == 'downloading':
        percent, eta = clean_ansi_codes(d.get('_percent_str','')), clean_ansi_codes(d.get('_eta_str',''))
        info.update({'message': f"Baixando: {percent} ETA {eta}", 'file_size': d.get('total_bytes_estimate', 0)})
    elif d['status'] == 'finished':
        info.update({'message': 'Processando...', 'file_size': d.get('total_bytes', 0)})

@app.route('/download_file/<path:filename>')
def serve_downloaded_file(filename):
    return send_file(os.path.join(DOWNLOAD_FOLDER, filename), as_attachment=True)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=True, host='0.0.0.0', port=port)
