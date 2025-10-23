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
        is_youtube = "youtube.com" in original_url or "youtu.be" in original_url

        # --- PLANO A: TENTAR API EXTERNA (COBALT PARA YOUTUBE) ---
        if is_youtube:
            try:
                download_info['message'] = 'Tentando API externa (Cobalt)...'
                print(f"Iniciando tentativa com Cobalt para: {original_url}")
                
                api_url = "https://co.wuk.sh/api/json"
                headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
                data = {"url": original_url, "vQuality": "1080", "isNoTTWatermark": True}
                
                resp = requests.post(api_url, headers=headers, json=data, timeout=25).json()

                if resp.get("status") == "stream" and resp.get("url"):
                    api_download_url = resp.get("url")
                    print(f"Sucesso com Cobalt! Baixando de {api_download_url[:40]}...")
                    
                    # Precisamos pegar o título e thumbnail separadamente
                    with yt_dlp.YoutubeDL({'quiet': True, 'skip_download': True}) as ydl:
                        info = ydl.extract_info(original_url, download=False)
                        title = info.get('title', 'Vídeo do YouTube')
                        thumbnail = info.get('thumbnail', '')
                    
                    download_info.update({'message': 'API encontrou! Baixando...', 'title': title, 'thumbnail': thumbnail})

                    unique_filename_base = str(uuid.uuid4())
                    ext = '.mp4' # Cobalt geralmente retorna mp4
                    output_path = os.path.join(DOWNLOAD_FOLDER, f'{unique_filename_base}{ext}')

                    with requests.get(api_download_url, stream=True, headers={'User-Agent': 'Mozilla/5.0'}) as r:
                        r.raise_for_status()
                        with open(output_path, 'wb') as f:
                            for chunk in r.iter_content(chunk_size=8192):
                                f.write(chunk)
                    
                    base_filename = os.path.basename(output_path)
                    file_size = os.path.getsize(output_path)
                    
                    download_info.update({'status': 'completed', 'message': 'Download via API concluído!', 'file_name': base_filename, 'download_link': f'/download_file/{base_filename}', 'file_size': file_size})
                    print("Download via Cobalt concluído com sucesso.")
                    return # FIM, SUCESSO TOTAL!

                else:
                    raise ValueError(f"Cobalt não retornou stream. Status: {resp.get('status')}, Texto: {resp.get('text')}")

            except Exception as e:
                print(f"API Cobalt falhou: {e}. Ativando Plano B (yt-dlp com cookies)...")
                download_info['message'] = 'API externa falhou, ativando Plano B...'
        
        # --- PLANO B: YT-DLP COM COOKIES (SE NÃO FOR YOUTUBE OU SE O COBALT FALHAR) ---
        print("Iniciando download com yt-dlp (Plano B)")
        cookies_file = 'cookies.txt'
        if not os.path.exists(cookies_file):
            print("AVISO: 'cookies.txt' não encontrado.")
            cookies_file = None

        ydl_opts = {
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            'outtmpl': os.path.join(DOWNLOAD_FOLDER, f'{uuid.uuid4()}.%(ext)s'),
            'noplaylist': True,
            'progress_hooks': [lambda d: update_download_progress(d, original_url)],
            'cookiefile': cookies_file,
            'retries': 3, 'fragment_retries': 3, 'ignoreerrors': True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(original_url, download=True)
            if not info or not info.get('requested_downloads'):
                raise yt_dlp.utils.DownloadError("Falha no download (yt-dlp). O vídeo pode ser privado ou bloqueado.")
            
            downloaded_file = info['requested_downloads'][0]['filepath']
            base_filename = os.path.basename(downloaded_file)
            
            if not base_filename.lower().endswith(('.mp4', '.webm', '.mkv', '.mov')):
                os.remove(downloaded_file)
                raise yt_dlp.utils.DownloadError("Arquivo baixado não é um vídeo válido (provavelmente HTML).")

            file_size = os.path.getsize(downloaded_file)
            
            download_info.update({
                'status': 'completed', 'file_name': base_filename,
                'download_link': f'/download_file/{base_filename}',
                'title': info.get('title', 'Vídeo'), 'thumbnail': info.get('thumbnail', ''),
                'file_size': file_size
            })
            print(f"Download (Plano B) concluído: {base_filename}")

    except Exception as e:
        msg = clean_ansi_codes(str(e).split('\n')[0])
        print(f"Erro final: {msg}")
        download_info.update({'status': 'failed', 'message': msg})

def update_download_progress(d, video_url):
    info = download_status_map.setdefault(video_url, {})
    if d['status'] == 'downloading':
        percent = clean_ansi_codes(d.get('_percent_str',''))
        eta = clean_ansi_codes(d.get('_eta_str',''))
        info.update({'message': f"Baixando: {percent} ETA {eta}", 'file_size': d.get('total_bytes_estimate', 0)})
    elif d['status'] == 'finished':
        info.update({'message': 'Processando...'})

@app.route('/download_file/<path:filename>')
def serve_downloaded_file(filename):
    return send_file(os.path.join(DOWNLOAD_FOLDER, filename), as_attachment=True)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=True, host='0.0.0.0', port=port)
