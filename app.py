from flask import Flask, render_template, request, jsonify, send_file
import yt_dlp
import os
import uuid
import threading
import re
import requests
import magic  # Para detectar tipo MIME do arquivo

app = Flask(__name__)

DOWNLOAD_FOLDER = 'downloads'
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)

download_status_map = {}

def clean_ansi_codes(text):
    return re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])').sub('', text)

def is_valid_video_file(file_path):
    """Verifica se o arquivo é realmente um vídeo"""
    try:
        # Usa magic para detectar o tipo MIME
        mime = magic.Magic(mime=True)
        file_type = mime.from_file(file_path)
        
        # Verifica se é um vídeo
        if file_type.startswith('video/'):
            return True
        # Verifica se é HTML/MHTML
        elif file_type in ['text/html', 'message/rfc822', 'application/x-mimearchive']:
            return False
        else:
            # Para outros tipos, verifica pela extensão
            ext = os.path.splitext(file_path)[1].lower()
            return ext in ['.mp4', '.webm', '.mkv', '.mov', '.avi', '.flv']
    except:
        # Fallback: verifica pela extensão
        ext = os.path.splitext(file_path)[1].lower()
        return ext in ['.mp4', '.webm', '.mkv', '.mov', '.avi', '.flv']

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/download', methods=['POST'])
def handle_download():
    data = request.get_json()
    video_url = data.get('url')
    if not video_url: 
        return jsonify({'error': 'URL obrigatória.'}), 400
    
    # Limpa URL antiga se existir
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

        # --- PLANO A: TENTAR API EXTERNA (COBALT) ---
        if is_youtube:
            try:
                download_info['message'] = 'Tentando API externa (Cobalt)...'
                print(f"Iniciando tentativa com Cobalt para: {original_url}")
                
                api_url = "https://co.wuk.sh/api/json"
                headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
                data = {
                    "url": original_url, 
                    "vQuality": "720p",  # Qualidade mais compatível
                    "aFormat": "mp3",
                    "isAudioOnly": False,
                    "isNoTTWatermark": True,
                    "dubLang": False
                }
                
                resp = requests.post(api_url, headers=headers, json=data, timeout=30)
                
                if resp.status_code != 200:
                    raise ValueError(f"API retornou status {resp.status_code}")
                
                result = resp.json()
                print(f"Resposta da API: {result}")

                if result.get("status") == "stream" and result.get("url"):
                    api_download_url = result.get("url")
                    print(f"Sucesso com Cobalt! Baixando de {api_download_url}")
                    
                    # Obter informações do vídeo
                    with yt_dlp.YoutubeDL({'quiet': True, 'skip_download': True}) as ydl:
                        info = ydl.extract_info(original_url, download=False)
                        title = info.get('title', 'Vídeo do YouTube')
                        thumbnail = info.get('thumbnail', '')
                    
                    download_info.update({
                        'message': 'API encontrou! Baixando...', 
                        'title': title, 
                        'thumbnail': thumbnail
                    })

                    unique_filename = f"{uuid.uuid4()}.mp4"
                    output_path = os.path.join(DOWNLOAD_FOLDER, unique_filename)

                    # Download do arquivo
                    with requests.get(api_download_url, stream=True, timeout=30) as r:
                        r.raise_for_status()
                        total_size = int(r.headers.get('content-length', 0))
                        
                        with open(output_path, 'wb') as f:
                            for chunk in r.iter_content(chunk_size=8192):
                                if chunk:
                                    f.write(chunk)
                    
                    # VERIFICAÇÃO CRÍTICA: É mesmo um vídeo?
                    if not is_valid_video_file(output_path):
                        os.remove(output_path)
                        raise ValueError("Arquivo baixado não é um vídeo válido (provavelmente HTML).")
                    
                    file_size = os.path.getsize(output_path)
                    
                    download_info.update({
                        'status': 'completed', 
                        'message': 'Download via API concluído!', 
                        'file_name': unique_filename, 
                        'download_link': f'/download_file/{unique_filename}', 
                        'file_size': file_size
                    })
                    print("Download via Cobalt concluído com sucesso.")
                    return

                else:
                    raise ValueError(f"Cobalt não retornou stream válido: {result}")

            except Exception as e:
                print(f"API Cobalt falhou: {e}. Ativando Plano B (yt-dlp)...")
                download_info['message'] = 'API externa falhou, ativando Plano B...'
        
        # --- PLANO B: YT-DLP DIRETO (MAIS CONFIÁVEL) ---
        print("Iniciando download com yt-dlp (Plano B)")
        
        ydl_opts = {
            'format': 'best[ext=mp4]/best[ext=webm]/best',
            'outtmpl': os.path.join(DOWNLOAD_FOLDER, f'{uuid.uuid4()}.%(ext)s'),
            'noplaylist': True,
            'progress_hooks': [lambda d: update_download_progress(d, original_url)],
            'retries': 3, 
            'fragment_retries': 3,
            'ignoreerrors': False,
            'quiet': False,
            'no_warnings': False,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(original_url, download=True)
            
            if not info:
                raise yt_dlp.utils.DownloadError("Não foi possível extrair informações do vídeo")
            
            # Encontra o arquivo baixado
            downloaded_file = ydl.prepare_filename(info)
            
            # VERIFICAÇÃO: É mesmo um vídeo?
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
            print(f"Download (Plano B) concluído: {base_filename}")

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
        speed = clean_ansi_codes(d.get('_speed_str',''))
        info.update({
            'message': f"Baixando: {percent} - {speed} - ETA: {eta}", 
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

# Instalação da biblioteca magic
def install_magic():
    try:
        import magic
    except ImportError:
        print("Instalando python-magic...")
        os.system("pip install python-magic")
        import magic

# Chama a instalação no início
install_magic()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=True, host='0.0.0.0', port=port)
