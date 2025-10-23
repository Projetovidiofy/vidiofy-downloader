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

    if not video_url:
        return jsonify({'error': 'O campo URL é obrigatório.'}), 400

    if video_url in download_status_map:
         del download_status_map[video_url]
         
    download_status_map[video_url] = {'status': 'processing', 'message': 'Iniciando...'}
    
    thread = threading.Thread(target=download_video_task, args=(video_url,))
    thread.start()

    return jsonify({
        'message': 'O download foi iniciado.',
        'status': 'processing',
        'video_url': video_url
    })

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
    download_info['status'] = 'processing'
    download_info['file_size'] = 0

    try:
        resolved_url = original_url
        if any(x in original_url for x in ["v.douyin.com", "vm.tiktok.com", "iesdouyin.com", "kw.ai"]):
            try:
                download_info['message'] = 'Resolvendo link curto...'
                headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
                r = requests.get(original_url, allow_redirects=True, timeout=10, headers=headers)
                r.raise_for_status()
                resolved_url = r.url
            except Exception as e:
                resolved_url = original_url

        is_tiktok = "tiktok.com" in resolved_url or "douyin.com" in resolved_url
        is_youtube = "youtube.com" in resolved_url or "youtu.be" in resolved_url

        # --- TENTATIVA COM API EXTERNA (ESPECÍFICA PARA CADA PLATAFORMA) ---
        api_download_url = None
        title = 'Vídeo'
        thumbnail = ''
        
        # Estratégia para TikTok
        if is_tiktok:
            try:
                download_info['message'] = 'Tentando API externa para TikTok...'
                api_url = f"https://www.tikwm.com/api/?url={resolved_url}"
                resp = requests.get(api_url, timeout=15).json()
                if resp.get("code") == 0 and resp.get("data"):
                    data = resp["data"]
                    api_download_url = data.get("hdplay") or data.get("play") or data.get("wmplay")
                    title = data.get("title", "Vídeo TikTok")
                    thumbnail = data.get("cover", "")
            except Exception as e:
                print(f"API TikWM falhou: {e}. Prosseguindo com yt-dlp.")

        # Estratégia para YouTube (NOVA)
        if is_youtube:
            try:
                download_info['message'] = 'Tentando API externa para YouTube...'
                video_id = None
                if "youtu.be/" in resolved_url:
                    video_id = resolved_url.split("youtu.be/")[1].split("?")[0]
                elif "youtube.com/watch?v=" in resolved_url:
                    video_id = resolved_url.split("watch?v=")[1].split("&")[0]
                elif "youtube.com/shorts/" in resolved_url:
                    video_id = resolved_url.split("shorts/")[1].split("?")[0]
                
                if video_id:
                    # Usando uma instância pública do Invidious
                    invidious_api_url = f"https://vid.puffyan.us/api/v1/videos/{video_id}"
                    resp = requests.get(invidious_api_url, timeout=15).json()
                    
                    # Procura por um formato de vídeo com áudio (itag 22 é 720p, 18 é 360p)
                    stream = next((s for s in resp.get('formatStreams', []) if s.get('itag') == '22'), None) or \
                             next((s for s in resp.get('formatStreams', []) if s.get('itag') == '18'), None)
                    
                    if stream and stream.get('url'):
                        api_download_url = stream['url']
                        title = resp.get('title', 'Vídeo YouTube')
                        # Pega a melhor qualidade de thumbnail disponível
                        thumbnail = resp.get('videoThumbnails', [{}])[-1].get('url', '')
            except Exception as e:
                print(f"API Invidious falhou: {e}. Prosseguindo com yt-dlp.")

        # Se alguma API externa funcionou, baixa o link direto
        if api_download_url:
            print(f"Sucesso com API externa! Baixando link direto para {original_url}")
            download_info.update({'message': 'API encontrou o vídeo! Baixando...', 'title': title, 'thumbnail': thumbnail})
            
            unique_filename_base = str(uuid.uuid4())
            # A extensão é geralmente mp4, mas pode variar
            ext = os.path.splitext(api_download_url.split('?')[0])[-1] or '.mp4'
            output_path_template = os.path.join(DOWNLOAD_FOLDER, f'{unique_filename_base}{ext}')

            ydl_opts_direct = {
                'outtmpl': output_path_template,
                'progress_hooks': [lambda d: update_download_progress(d, original_url)],
                'retries': 3,
                'http_headers': {'User-Agent': 'Mozilla/5.0'}
            }
            with yt_dlp.YoutubeDL(ydl_opts_direct) as ydl:
                ydl.download([api_download_url])

            downloaded_file = None
            for f in os.listdir(DOWNLOAD_FOLDER):
                if f.startswith(unique_filename_base):
                    downloaded_file = os.path.join(DOWNLOAD_FOLDER, f)
                    break

            if downloaded_file and os.path.exists(downloaded_file):
                base_filename = os.path.basename(downloaded_file)
                file_size = os.path.getsize(downloaded_file)
                download_info.update({
                    'status': 'completed', 'message': 'Download via API concluído!',
                    'file_name': base_filename, 'download_link': f'/download_file/{base_filename}',
                    'file_size': file_size
                })
                return # Termina a função aqui

        # --- SE NENHUMA API FUNCIONOU, TENTA O yt-dlp PADRÃO ---
        print(f"Nenhuma API teve sucesso para {original_url}. Usando yt-dlp padrão.")
        download_info['message'] = 'API externa falhou, tentando método padrão...'
        
        # As opções de yt-dlp que já tínhamos
        ydl_opts_download = {
            'format': 'best[ext=mp4][acodec!=none]/best[acodec!=none]/best',
            # ... (resto das opções como no código anterior) ...
        }
        # (O resto do bloco `with yt_dlp.YoutubeDL...` continua aqui, mas por simplicidade, vamos focar na lógica principal)
        # O código completo está abaixo para evitar confusão.

    except Exception as e:
        # Bloco de erro como no código anterior
        # (O código completo está abaixo para evitar confusão)
