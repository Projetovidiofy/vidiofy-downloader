# O código completo e final do app.py
from flask import Flask, render_template, request, jsonify, send_file
import yt_dlp
import os
import uuid
import threading
import json
import re

app = Flask(__name__)

DOWNLOAD_FOLDER = 'downloads'
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)

download_status_map = {}

# (O resto do código permanece o mesmo, a única mudança é no bloco ydl_opts)
# ...
# (Para evitar um bloco gigante, a mudança principal está aqui dentro)

def download_video_task(video_url):
    original_url = video_url
    download_info = download_status_map.setdefault(original_url, {})
    download_info.update({'status': 'processing', 'file_size': 0})
    
    try:
        # Define o caminho para o arquivo de cookies
        cookies_file_path = 'cookies.txt'
        
        # Verifica se o arquivo de cookies existe. Se não, avisa no log.
        if not os.path.exists(cookies_file_path):
            print("AVISO: Arquivo 'cookies.txt' não encontrado. Downloads podem falhar.")
            cookies_file_path = None # Usa None se o arquivo não existir

        # Opções do yt-dlp usando o arquivo de cookies
        ydl_opts = {
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best[acodec!=none]',
            'outtmpl': os.path.join(DOWNLOAD_FOLDER, f'{str(uuid.uuid4())}.%(ext)s'),
            'noplaylist': True,
            'progress_hooks': [lambda d: update_download_progress(d, original_url)],
            'retries': 5, # Aumentar as tentativas
            'fragment_retries': 5,
            'no_check_certificate': True,
            'ignoreerrors': True,
            # A MÁGICA ESTÁ AQUI:
            'cookiefile': cookies_file_path,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=True)
            if not info or not info.get('requested_downloads'):
                raise yt_dlp.utils.DownloadError("Download via yt-dlp falhou.")
            
            downloaded_file = info['requested_downloads'][0]['filepath']
            base_filename = os.path.basename(downloaded_file)
            file_size = os.path.getsize(downloaded_file)
            
            download_info.update({
                'status': 'completed', 'message': 'Download concluído!',
                'file_name': base_filename, 'download_link': f'/download_file/{base_filename}',
                'title': info.get('title', 'Vídeo'), 'thumbnail': info.get('thumbnail', ''),
                'file_size': file_size
            })

    except Exception as e:
        # ... (bloco de erro continua o mesmo)
