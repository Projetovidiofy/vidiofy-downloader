from flask import Flask, render_template, request, jsonify, send_file
import yt_dlp
import os
import uuid
import threading
import json
import re
import requests # Adicionado para usar a API TikWM

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

    # Limpa status anterior para a mesma URL, se houver
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
        # Garante que file_size sempre exista, mesmo que seja 0
        status_info.setdefault('file_size', 0) 
        return jsonify(status_info)
    return jsonify({'status': 'not_found', 'message': 'Download não encontrado.'}), 404

def download_video_task(video_url):
    original_url = video_url # Guarda a URL original para o status_map
    download_info = download_status_map.setdefault(original_url, {}) # Garante que exista
    download_info['status'] = 'processing'
    download_info['file_size'] = 0

    try:
        # --- Resolver links curtos PRIMEIRO ---
        resolved_url = original_url
        if any(x in original_url for x in ["v.douyin.com", "vm.tiktok.com", "iesdouyin.com", "kw.ai"]):
            try:
                download_info['message'] = 'Resolvendo link curto...'
                # Usar um user-agent mais comum pode ajudar
                headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
                r = requests.get(original_url, allow_redirects=True, timeout=10, headers=headers)
                r.raise_for_status() # Levanta erro se a requisição falhar
                resolved_url = r.url
                print(f"URL resolvida de {original_url} para: {resolved_url}")
            except Exception as e:
                print(f"Não foi possível resolver o link curto {original_url}: {e}. Tentando com a URL original.")
                resolved_url = original_url # Usa a original se a resolução falhar

        is_tiktok = "tiktok.com" in resolved_url or "douyin.com" in resolved_url

        # --- TENTATIVA COM API TikWM (APENAS PARA TIKTOK) ---
        if is_tiktok:
            try:
                download_info['message'] = 'Tentando API externa para TikTok...'
                api_url = f"https://www.tikwm.com/api/?url={resolved_url}"
                headers = {'User-Agent': 'Mozilla/5.0'} # API pode precisar de user agent
                resp = requests.get(api_url, timeout=15, headers=headers).json()

                if resp.get("code") == 0 and "data" in resp and resp["data"]:
                    data = resp["data"]
                    # Prioriza HD > Normal > Com marca d'água
                    tikwm_download_url = data.get("hdplay") or data.get("play") or data.get("wmplay")
                    
                    if tikwm_download_url:
                        print("Sucesso com TikWM! Baixando link direto...")
                        download_info['message'] = 'API encontrou o vídeo! Baixando...'
                        title = data.get("title", "Vídeo TikTok")
                        thumbnail = data.get("cover", "") # Pega a capa se disponível
                        
                        unique_filename_base = str(uuid.uuid4())
                        output_path_template = os.path.join(DOWNLOAD_FOLDER, f'{unique_filename_base}.mp4') # Assume MP4 da API

                        ydl_opts_direct = {
                            'outtmpl': output_path_template,
                            'progress_hooks': [lambda d: update_download_progress(d, original_url)],
                            'retries': 3,
                            'fragment_retries': 3,
                            'noplaylist': True,
                            # Adicionar headers pode ajudar a simular um navegador
                            'http_headers': {
                                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                                'Referer': 'https://www.tiktok.com/' 
                            }
                        }

                        with yt_dlp.YoutubeDL(ydl_opts_direct) as ydl:
                            # Baixa o link direto fornecido pela API
                            ydl.download([tikwm_download_url]) 
                        
                        # Precisamos encontrar o nome exato do arquivo (pode não ser exatamente .mp4)
                        # Assume que só um arquivo foi baixado com esse UUID
                        downloaded_file = None
                        for f in os.listdir(DOWNLOAD_FOLDER):
                            if f.startswith(unique_filename_base):
                                downloaded_file = os.path.join(DOWNLOAD_FOLDER, f)
                                break

                        if downloaded_file and os.path.exists(downloaded_file):
                            base_filename = os.path.basename(downloaded_file)
                            file_size = os.path.getsize(downloaded_file)
                            
                            download_info.update({
                                'status': 'completed',
                                'message': 'Download via API concluído!',
                                'file_name': base_filename,
                                'download_link': f'/download_file/{base_filename}',
                                'title': title,
                                'thumbnail': thumbnail,
                                'file_size': file_size
                            })
                            print(f"Download via API concluído para {original_url}. Salvo como: {downloaded_file}")
                            return # Termina a função aqui, pois o download foi sucesso
                        else:
                             raise ValueError("Arquivo não encontrado após download via API.")
                             
                # Se chegou aqui, a API não funcionou como esperado
                raise ValueError(f"API TikWM não retornou link válido. Resposta: {resp.get('msg', 'Sem mensagem')}")

            except Exception as e:
                print(f"Falha na API TikWM para {resolved_url}: {e}. Prosseguindo com yt-dlp padrão...")
                download_info['message'] = 'API externa falhou, tentando método padrão...'
                # Não retorna, deixa o código continuar para o fallback yt-dlp abaixo

        # --- FALLBACK GERAL / OUTRAS PLATAFORMAS (yt-dlp) ---
        # Roda se não for TikTok ou se a API TikWM falhou
        
        # Extrai info primeiro para ter Título/Thumbnail antes do download longo
        try:
             download_info['message'] = 'Buscando informações do vídeo...'
             ydl_opts_info = {
                 'noplaylist': True, 'quiet': True, 'extract_flat': True, 'skip_download': True,
                 'no_check_certificate': True, 'ignoreerrors': True, # Tenta ser mais robusto
                 # NOVO: Adiciona headers para a fase de extração de info também
                 'http_headers': {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                    'Referer': 'https://www.youtube.com/' # Ou outra plataforma específica se for o caso
                 },
                 'sleep_interval': 1, # Pequeno atraso entre requisições
                 'max_sleep_interval': 5, # Máximo atraso
             }
             with yt_dlp.YoutubeDL(ydl_opts_info) as ydl:
                 info_dict = ydl.extract_info(resolved_url, download=False)
             
             # Se info_dict for None (yt-dlp falhou em extrair), levanta erro
             if info_dict is None:
                  raise yt_dlp.utils.DownloadError("yt-dlp não conseguiu extrair informações.")

             title = info_dict.get('title', 'Vídeo sem título')
             thumbnail = info_dict.get('thumbnail', '')
             download_info.update({'title': title, 'thumbnail': thumbnail})
        except Exception as info_e:
             print(f"Erro ao extrair info para {resolved_url}: {info_e}")
             # Tenta continuar mesmo sem info, mas atualiza mensagem
             download_info['message'] = 'Aviso: Não foi possível obter detalhes do vídeo. Tentando baixar...'
             # Define padrões caso a extração falhe
             title = 'Vídeo'
             thumbnail = ''


        # Opções de download padrão (mantém a otimização para YouTube)
        unique_filename_base = str(uuid.uuid4())
        ydl_opts_download = {
            'format': 'best[ext=mp4][acodec!=none]/best[acodec!=none]/best',
            'outtmpl': os.path.join(DOWNLOAD_FOLDER, f'{unique_filename_base}.%(ext)s'),
            'noplaylist': True,
            'concurrent_fragments': 10,
            'progress_hooks': [lambda d: update_download_progress(d, original_url)],
            'verbose': False, 
            'retries': 3,
            'fragment_retries': 3,
            'no_check_certificate': True,
            'ignoreerrors': True,
            # NOVAS OPÇÕES PARA PARECER MAIS UM NAVEGADOR
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'referer': 'https://www.youtube.com/', # Referer padrão (pode ser ajustado para outras plataformas)
            'add_header': [
                'Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language: en-US,en;q=0.5',
                'Connection: keep-alive',
                'Upgrade-Insecure-Requests: 1',
            ],
            'sleep_interval': 1, # Espera 1 segundo entre requisições (mais humano)
            'max_sleep_interval': 5, # Máximo de 5 segundos
            'buffer_size': 1048576, # Aumenta o buffer para downloads grandes
            'http_chunk_size': 1048576, # Tamanho do chunk HTTP
            # Fim das novas opções
        }

        print(f"Iniciando download padrão com yt-dlp para {resolved_url}...")
        with yt_dlp.YoutubeDL(ydl_opts_download) as ydl:
            final_info_dict = ydl.extract_info(resolved_url, download=True)

            if final_info_dict is None or not final_info_dict.get('requested_downloads'):
                 # Tenta pegar um erro mais específico, se disponível
                 raise yt_dlp.utils.DownloadError(f"Download via yt-dlp falhou. Verifique os logs do yt-dlp.")

            # Certifica-se de que requested_downloads é uma lista e pega o primeiro item
            downloaded_file = final_info_dict.get('requested_downloads')
            if downloaded_file and isinstance(downloaded_file, list) and downloaded_file[0].get('filepath'):
                downloaded_file = downloaded_file[0]['filepath']
            else:
                # Fallback caso a estrutura mude, tenta o url direto ou o outtmpl
                downloaded_file = ydl_opts_download['outtmpl'].replace('.%(ext)s', '.' + final_info_dict.get('ext', 'mp4'))
                if not os.path.exists(downloaded_file):
                    raise yt_dlp.utils.DownloadError("Não foi possível determinar o arquivo baixado.")

            base_filename = os.path.basename(downloaded_file) 
            file_size = os.path.getsize(downloaded_file) if os.path.exists(downloaded_file) else 0

            download_info.update({
                'status': 'completed',
                'message': 'Download concluído!',
                'file_name': base_filename,
                'download_link': f'/download_file/{base_filename}',
                # Usa o título/thumb extraídos antes, mas atualiza se o download final tiver
                'title': final_info_dict.get('title', title),
                'thumbnail': final_info_dict.get('thumbnail', thumbnail),
                'file_size': file_size
            })
            print(f"Download padrão concluído para {original_url}. Salvo como: {downloaded_file}")

    except Exception as e:
        error_message = f"Erro final ao baixar {original_url}: {str(e)}"
        print(error_message)
        # Tenta pegar a mensagem de erro específica do yt-dlp se for um DownloadError
        if isinstance(e, yt_dlp.utils.DownloadError):
            # Remove informações extras que podem confundir o usuário (como stack trace)
            msg = str(e).split('\n')[0] 
            # Verifica se é o erro de login do TikTok
            if "TikTok is requiring login" in msg:
                 final_error_msg = "Este vídeo do TikTok exige login. Tente outro vídeo ou use um método com cookies."
            elif "confirm you’re not a bot" in msg: # Erro específico do YouTube
                 final_error_msg = "O YouTube detectou atividade de bot. Tente novamente mais tarde ou com outro vídeo. Pode ser um bloqueio temporário do servidor."
            else:
                 final_error_msg = clean_ansi_codes(msg)
        else:
            final_error_msg = clean_ansi_codes(error_message) # Limpa a mensagem genérica
        
        download_info.update({'status': 'failed', 'message': final_error_msg})


def update_download_progress(d, video_url):
    # Garante que temos a entrada no mapa
    download_info = download_status_map.setdefault(video_url, {}) 
    
    if d['status'] == 'downloading':
        percent_str = clean_ansi_codes(d.get('_percent_str', ''))
        eta_str = clean_ansi_codes(d.get('_eta_str', ''))
        total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
        
        download_info.update({
            'message': f"Baixando: {percent_str} ETA {eta_str}",
            'progress': percent_str,
            'file_size': total_bytes # Atualiza o tamanho estimado
        })
    elif d['status'] == 'finished':
        download_info['message'] = 'Download concluído, processando...'
        # Atualiza o tamanho final aqui se disponível no hook 'finished'
        if 'total_bytes' in d:
            download_info['file_size'] = d['total_bytes']


@app.route('/download_file/<path:filename>')
def serve_downloaded_file(filename):
    file_path = os.path.join(DOWNLOAD_FOLDER, filename)
    if os.path.exists(file_path):
        # Adiciona header para tentar forçar o download em vez de abrir no navegador
        return send_file(file_path, as_attachment=True, download_name=filename) 
    return jsonify({'error': 'Arquivo não encontrado.'}), 404


if __name__ == '__main__':
    # Usa a porta 5000 como padrão no Pydroid, mas mantém a variável de ambiente se definida
    port = int(os.environ.get('PORT', 5000)) 
    app.run(debug=True, host='0.0.0.0', port=port)

